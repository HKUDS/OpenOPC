"""Snapshot Builder — assembles VisualSnapshot and collab_sync data.

All data formats match what the frontend expects:
- VisualSnapshot for initial game state (types/visual.ts)
- collab_sync ack for kanban/chat data (collabSync.ts maps snake_case → camelCase)

Also provides reconcile_sessions() which backfills CLI-generated session
history into the UI rendering cache (ChatStore), so that conversations
started via CLI are visible when the user later opens the UI.
"""

from __future__ import annotations

import inspect
import json
import re
import time
import uuid
from typing import Any, TYPE_CHECKING

from loguru import logger
from opc.core.models import normalize_role_runtime_status
from opc.core.transcript_visibility import (
    FULL_DETAIL_ONLY_TRANSCRIPT_KINDS,
    TranscriptDetailLevel,
    normalize_transcript_detail_level,
    transcript_metadata_visible,
)
from opc.layer2_organization.phase import (
    DONE_PHASES,
    IN_PROGRESS_PHASES,
    IN_REVIEW_PHASES,
    Phase,
    TODO_PHASES,
    effective_owner,
    is_report_execution_work_item_metadata,
    is_review_execution_work_item_metadata,
    kanban_column,
    should_hide_work_item_from_company_kanban,
    verdict,
)
from opc.layer2_organization.company_runtime_identity import (
    ACTIVE_COMPANY_RUNTIME_CHECKPOINT_STATUSES,
    COMPANY_RUNTIME_CHECKPOINT_TYPES,
    build_company_runtime_identity_index,
    is_runtime_auxiliary_task,
)
from opc.layer2_organization.work_item_context_view import WorkItemContextView
from opc.layer2_organization.work_item_identity import (
    WORK_ITEM_PROJECTION_ID_KEY,
    WORK_ITEM_TURN_TYPE_KEY,
    projection_id_for_work_item,
    turn_type_for_work_item,
    work_item_identity_payload,
    work_item_identity_payload_from_metadata,
    work_item_projection_id_from_metadata,
    work_item_turn_type_from_metadata,
)
from opc.layer2_organization.work_item_links import task_by_linked_work_item_id
from opc.plugins.office_ui.execution_identity import (
    canonicalize_execution_identity,
    execution_identity_from_task,
    normalize_company_profile,
    normalize_exec_mode,
    normalize_preferred_agent,
)


# The frontend's KanbanColumn definitions use hyphenated ids
# ("in-progress", "in-review"); kanban_column() returns underscore form
# ("in_progress", "in_review") to match the underlying Phase values.
# Convert on the way out.
_COLUMN_ID_FOR_FRONTEND = {
    "todo": "todo",
    "in_progress": "in-progress",
    "in_review": "in-review",
    "done": "done",
}


def _frontend_column_id(phase: Phase) -> str:
    return _COLUMN_ID_FOR_FRONTEND[kanban_column(phase)]


def _runtime_status_from_member_meta(member_session_meta: dict[str, Any]) -> str:
    return normalize_role_runtime_status(
        member_session_meta.get("status") or member_session_meta.get("resident_status"),
        member_session_meta.get("focused_work_item_id"),
    )
from opc.presentation.kanban import (
    STATUS_TO_COLUMN,
    build_base_task_payload,
    build_board_columns,
    build_company_board_columns,
    datetime_to_timestamp,
    priority_to_label,
)
from opc.layer3_agent.adapters.codex_adapter import CodexAdapter

if TYPE_CHECKING:
    from opc.engine import OPCEngine
    from opc.plugins.office_ui.agent_store import AgentStore
    from opc.plugins.office_ui.chat_store import ChatStore
    from opc.plugins.office_ui.event_adapter import EventAdapter


def _coerce_timestamp(value: Any) -> float | None:
    try:
        if value in (None, "", [], {}):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _session_message_ui_identity(message: Any) -> tuple[str, float, dict[str, Any]]:
    metadata = dict(getattr(message, "metadata", {}) or {})
    canonical_id = str(metadata.get("ui_message_id") or getattr(message, "message_id", "") or "").strip()

    timestamp = _coerce_timestamp(metadata.get("ui_created_at"))
    if timestamp is None:
        created_at = getattr(message, "created_at", None)
        timestamp = created_at.timestamp() if hasattr(created_at, "timestamp") else time.time()

    ui_meta: dict[str, Any] = {}
    if canonical_id and canonical_id != str(getattr(message, "message_id", "") or "").strip():
        ui_meta["ui_message_id"] = canonical_id
    if "ui_created_at" in metadata and timestamp is not None:
        ui_meta["ui_created_at"] = timestamp
    return canonical_id or str(getattr(message, "message_id", "") or ""), timestamp, ui_meta


_FULL_DETAIL_ONLY_TRANSCRIPT_KINDS = FULL_DETAIL_ONLY_TRANSCRIPT_KINDS

_TRANSCRIPT_DUPLICATE_KIND_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({
        "runtime_v2_assistant",
        "runtime_v2_company_assistant",
        "top_level_reply",
        "company_role_result",
        "company_role_result_retry",
        "child_task_result",
        "child_task_result_retry",
        "child_result",
    }),
)

_RESULT_SURFACE_PRIORITY: dict[str, int] = {
    "child_task_result": 80,
    "child_task_result_retry": 79,
    "company_role_result": 75,
    "company_role_result_retry": 74,
    "child_result": 70,
    "runtime_v2_assistant": 60,
    "runtime_v2_company_assistant": 20,
    "top_level_reply": 40,
}

_MARKDOWN_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)

_VERIFICATION_FOOTER_RE = re.compile(r"^Verification:\s", re.IGNORECASE)


def _metadata_is_task_mode_runtime(metadata: dict[str, Any]) -> bool:
    execution_mode = str(metadata.get("execution_mode", "") or "").strip().lower()
    if execution_mode == "company_mode":
        return False
    if execution_mode in {"task_mode", "task", "project_mode", "project"}:
        return True
    projection_id = str(metadata.get("work_item_projection_id", "") or "").strip()
    if projection_id and projection_id != "task_mode_execution":
        return False
    if str(metadata.get("company_profile", "") or "").strip():
        return False
    return (
        str(metadata.get("mode", "") or "").strip().lower() == "task"
        or str(metadata.get("runtime_kind", "") or "").strip() == "task_mode_agent_turn"
        or str(metadata.get("task_mode_contract", "") or "").strip() == "single_full_capability_main_agent"
        or projection_id == "task_mode_execution"
    )


def _normalize_duplicate_content(content: Any) -> str:
    normalized = CodexAdapter.normalize_transcript_text(str(content or ""))
    normalized = "\n".join(line.rstrip() for line in normalized.splitlines()).strip()
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    normalized = _strip_narrative_title_prefix(normalized)
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", normalized) if part.strip()]
    if len(paragraphs) > 1 and _VERIFICATION_FOOTER_RE.match(paragraphs[-1]):
        normalized = "\n\n".join(paragraphs[:-1]).strip()
    return normalized


def _strip_narrative_title_prefix(content: str) -> str:
    trimmed = str(content or "").strip()
    # Comparison-only canonicalization may unwrap a deliberate leading title,
    # but must never interpret a colon inside ordinary Markdown body text as a
    # wrapper boundary.  Resolve explicit nested wrappers to a fixed point so
    # the comparison key itself is idempotent.
    while True:
        markdown_title = re.match(r"^\*\*(.{8,160}?)\*\*:\s+([\s\S]+)$", trimmed)
        if not markdown_title:
            return trimmed
        body = markdown_title.group(2).strip()
        if len(body) < 80 or body == trimmed:
            return trimmed
        trimmed = body


def _select_duplicate_display_content(
    preferred: dict[str, Any],
    secondary: dict[str, Any],
) -> str:
    """Return one original surface body, never the lossy comparison key."""
    preferred_content = str(preferred.get("content", "") or "")
    secondary_content = str(secondary.get("content", "") or "")
    preferred_key = _normalize_duplicate_content(preferred_content)
    if not preferred_key or preferred_key != _normalize_duplicate_content(secondary_content):
        return preferred_content
    if secondary_content.strip() == preferred_key and preferred_content.strip() != preferred_key:
        return secondary_content
    return preferred_content


def _normalize_transcript_detail_level(value: Any) -> TranscriptDetailLevel:
    return normalize_transcript_detail_level(value)


def _transcript_message_kind(message: Any) -> str:
    metadata = dict(getattr(message, "metadata", {}) or {})
    return str(metadata.get("kind", "") or "").strip()


def _transcript_message_visibility(kind: str) -> TranscriptDetailLevel:
    return "full" if kind in _FULL_DETAIL_ONLY_TRANSCRIPT_KINDS else "summary"


def _transcript_message_hidden_from_ui(
    message: Any,
    *,
    detail_level: TranscriptDetailLevel = "summary",
) -> bool:
    metadata = dict(getattr(message, "metadata", {}) or {})
    return not transcript_metadata_visible(metadata, detail_level=detail_level)


def _render_text_parts(parts: list[Any]) -> str:
    lines: list[str] = []
    for part in parts:
        payload = part.payload if isinstance(part.payload, dict) else {}
        if part.part_type != "text":
            continue
        text = payload.get("text", "")
        if text:
            lines.append(CodexAdapter.normalize_transcript_text(str(text)))
    return "\n\n".join(line for line in lines if line).strip()


def _render_thinking_parts(parts: list[Any]) -> str:
    lines: list[str] = []
    for part in parts:
        payload = part.payload if isinstance(part.payload, dict) else {}
        if part.part_type != "thinking":
            continue
        text = payload.get("text", "")
        if text:
            lines.append(CodexAdapter.normalize_transcript_text(str(text)))
    return "\n\n".join(line for line in lines if line).strip()


def _split_markdown_sections(content: str) -> dict[str, str]:
    normalized = CodexAdapter.normalize_transcript_text(str(content or "")).strip()
    if not normalized:
        return {}

    sections: dict[str, str] = {}
    matches = list(_MARKDOWN_SECTION_RE.finditer(normalized))
    if not matches:
        sections[""] = normalized
        return sections

    for index, match in enumerate(matches):
        title = match.group(1).strip()
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        body = normalized[body_start:body_end].strip()
        if body:
            sections[title] = body
    return sections


def _summarize_runtime_user_turn(content: str) -> str:
    sections = _split_markdown_sections(content)
    if not sections:
        return ""

    responsibility = sections.get("Your Responsibility", "").strip()
    global_intent = sections.get("Global Intent Summary", "").strip()
    task_text = sections.get("Task", "").strip()
    work_item_projection = (
        sections.get("Work Item Projection", "").strip()
        or sections.get("Work Item", "").strip()
    )
    current_role = sections.get("Current Role", "").strip()

    lines: list[str] = ["### Execution Context"]
    primary = responsibility or global_intent or task_text
    if primary:
        lines.append(primary)
    if global_intent and global_intent != primary:
        lines.append(f"Mission: {global_intent}")
    if work_item_projection:
        lines.append(f"Work item: {work_item_projection}")
    if current_role:
        lines.append(f"Role: {_humanize_identifier(current_role)}")

    if len(lines) == 1:
        fallback = next((value.strip() for value in sections.values() if value.strip()), "")
        if fallback:
            lines.append(fallback)
    return "\n\n".join(line for line in lines if line).strip()


def _strip_trailing_verification_footer(content: Any) -> tuple[str, str | None]:
    normalized = CodexAdapter.normalize_transcript_text(str(content or "")).strip()
    if not normalized:
        return "", None

    paragraphs = [part.strip() for part in re.split(r"\n{2,}", normalized) if part.strip()]
    if not paragraphs:
        return normalized, None

    footer = paragraphs[-1]
    if not _VERIFICATION_FOOTER_RE.match(footer):
        return normalized, None

    body = "\n\n".join(paragraphs[:-1]).strip()
    if not body:
        return normalized, None
    return body, footer


def _sanitize_ui_message_dict(message: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(message)
    sender = str(sanitized.get("sender", "") or "").strip().lower()
    metadata = dict(sanitized.get("metadata", {}) or {})
    role = str(metadata.get("role", "") or "").strip().lower()
    if sender == "user" or role == "user":
        sanitized["metadata"] = metadata
        return sanitized

    content, verification_footer = _strip_trailing_verification_footer(sanitized.get("content", ""))
    sanitized["content"] = content
    if verification_footer:
        metadata.setdefault("verification_verdict", verification_footer)
    sanitized["metadata"] = metadata
    return sanitized


def _humanize_identifier(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if "_" not in normalized and "-" not in normalized and normalized.isalpha() and len(normalized) <= 4:
        return normalized.upper()
    return normalized.replace("_", " ").replace("-", " ").title()


def _work_item_role_payload(task: Any) -> dict[str, Any]:
    meta = task.metadata if hasattr(task, "metadata") and isinstance(task.metadata, dict) else {}
    role_id = str(getattr(task, "assigned_to", "") or meta.get("work_item_role_id", "") or "").strip()
    # work_item_role_name is no longer stamped on task metadata; humanize the
    # role_id directly for UI display.
    role_name = _humanize_identifier(role_id) if role_id else ""
    return {
        "work_item_role_id": role_id or None,
        "work_item_role_name": role_name or None,
    }


_WORK_ITEM_PROGRESS_TYPES: frozenset[str] = frozenset({
    "work_item_started",
    "gate_approved",
    "gate_rejected",
    "awaiting_manager_review",
    "awaiting_human",
    "awaiting_review",
    "awaiting_peer",
    "work_item_failed",
    "deadlock",
    "gate_result",
})


def _execution_turn_alias_payload(runtime_task_id: str | None) -> dict[str, Any]:
    """Canonical identity for the runtime Task backing an execution turn."""
    task_id = str(runtime_task_id or "").strip()
    return {
        "runtime_task_id": task_id or None,
        "execution_turn_id": task_id or None,
    }


def _work_item_entry_from_progress(
    entry: dict[str, Any],
    *,
    runtime_task_id: str,
    projection_id_hint: str = "",
    projection_title_hint: str = "",
    role_name_hint: str = "",
) -> dict[str, Any] | None:
    entry_type = str(entry.get("type", "") or "").strip()
    projection_id = str(
        entry.get("work_item_projection_id")
        or entry.get("workItemProjectionId")
        or projection_id_hint
        or ""
    ).strip()
    turn_type = str(
        entry.get("work_item_turn_type")
        or entry.get("workItemTurnType")
        or entry.get("turn_type")
        or entry.get("turnType")
        or ""
    ).strip()
    if entry_type not in _WORK_ITEM_PROGRESS_TYPES and not projection_id:
        return None

    timestamp = _coerce_timestamp(entry.get("timestamp"))
    if timestamp is None:
        timestamp = time.time()

    projection_title = str(
        entry.get("work_item_projection_title")
        or entry.get("workItemProjectionTitle")
        or role_name_hint
        or projection_title_hint
        or (projection_id.replace("_", " ").replace("-", " ").title() if projection_id else "")
        or ""
    ).strip()
    role_name = str(entry.get("role_name") or entry.get("roleName") or role_name_hint or "").strip()
    detail = entry.get("detail")
    if detail is not None:
        detail = str(detail).strip() or None

    return {
        "timestamp": timestamp,
        "type": entry_type or "gate_result",
        **work_item_identity_payload(projection_id=projection_id, turn_type=turn_type),
        "work_item_projection_title": projection_title or None,
        "role_name": role_name or None,
        "detail": detail,
        **_execution_turn_alias_payload(runtime_task_id),
    }


# ── Per-role work-item rollup for the chat "Execution Progress" panel ──
#
# UI design contract: 1 row = 1 DelegationWorkItem (NOT 1 Task / runtime turn).
# Same work item walking through execute → review → rework stays a single row;
# its phase changes drive the row colour. Reviewer sees their review work
# items naturally because ``effective_owner(phase, item)`` swaps owner to the
# manager during ``in_review`` phases — no need to surface review Tasks as
# separate sessions.
#
# This rollup is consumed by ``WorkItemProgressCard`` /
# ``AgentWorkPanel`` / ``ContextPanel`` and intentionally bypasses the
# Task-keyed ``formatted_sessions`` source, which was contaminated by
# channel-message timestamps and Task-lifecycle filtering.


_PHASE_LIVE_RUNTIME_STATES = {"reflecting", "tool_active"}


def _phase_value(phase: Any) -> str:
    return phase.value if hasattr(phase, "value") else str(phase or "")


def _coerce_event_timestamp(value: Any) -> float | None:
    """Normalize a datetime / numeric / None field to ``float | None``.

    ``datetime_to_timestamp`` from ``opc.presentation.kanban`` returns
    ``time.time()`` for missing values, which is fine for kanban cards
    (they always have timestamps) but wrong for the role rollup, where a
    missing value must NOT silently become "now" — that would re-create
    the very "1s ago" bug this rewrite is supposed to fix.
    """
    if value is None:
        return None
    if hasattr(value, "timestamp"):
        try:
            return float(value.timestamp())
        except Exception:
            return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _role_aggregated_status(
    *,
    runtime_status: str,
    work_item_phases: list[str],
) -> str:
    """Reduce one role's set of work-item phases + tracker state to a single
    aggregated status the UI can colour directly.

    Rules (matches plan/bug-breezy-dragonfly.md):
      * runtime tracker is reflecting/tool_active → ``active`` (orange + pulse)
      * any phase in IN_PROGRESS_PHASES                → ``active`` (orange, no pulse)
      * any phase in IN_REVIEW_PHASES or TODO_PHASES   → ``waiting`` (yellow)
      * all phases in DONE_PHASES, none failed/cancelled → ``done`` (green)
      * all phases terminal but some failed/cancelled  → ``failed`` (red)
      * otherwise (no work items)                       → ``pending``
    """
    if not work_item_phases:
        return "pending"

    phase_set = {p for p in work_item_phases if p}
    todo_values = {p.value for p in TODO_PHASES}
    in_progress_values = {p.value for p in IN_PROGRESS_PHASES}
    in_review_values = {p.value for p in IN_REVIEW_PHASES}
    done_values = {p.value for p in DONE_PHASES}

    if runtime_status in _PHASE_LIVE_RUNTIME_STATES:
        return "active"

    all_terminal = phase_set.issubset(done_values)
    if all_terminal:
        if phase_set & {Phase.FAILED.value, Phase.CANCELLED.value}:
            return "failed"
        return "done"

    if phase_set & in_progress_values:
        return "active"
    if phase_set & (in_review_values | todo_values):
        return "waiting"
    return "pending"


def _resolve_role_runtime_status(
    *,
    event_adapter: "EventAdapter | None",
    role_id: str,
) -> str:
    """Pull `idle | reflecting | tool_active` from the per-role agent
    tracker. Falls back to ``idle`` when the tracker is missing.
    """
    if not event_adapter or not role_id:
        return "idle"
    try:
        agent_id = event_adapter._resolve_role_to_agent(role_id)
    except Exception:
        return "idle"
    if not agent_id:
        return "idle"
    try:
        tracker = event_adapter.get_tracker(agent_id)
    except Exception:
        return "idle"
    if not tracker or not hasattr(tracker, "state"):
        return "idle"
    state = tracker.state
    state_val = state.value if hasattr(state, "value") else str(state)
    if state_val in _PHASE_LIVE_RUNTIME_STATES or state_val == "idle":
        return state_val
    return "idle"


def _format_role_display_name(role_id: str, role_name: str = "") -> str:
    if role_name and role_name.strip():
        return role_name.strip()
    if not role_id:
        return ""
    return role_id.replace("_", " ").replace("-", " ").title()


def _filter_progress_log_by_projection(
    progress_log: list[Any],
    projection_id: str,
) -> list[dict[str, Any]]:
    """Return only progress entries belonging to this work-item projection.

    Entries without a ``work_item_projection_id`` are kept for backwards
    compatibility (older entries didn't stamp projection id). Entries whose
    projection id doesn't match are dropped. Output is a fresh list of
    dicts (defensive copy so downstream mutations don't bleed back).
    """
    out: list[dict[str, Any]] = []
    for entry in progress_log or []:
        if not isinstance(entry, dict):
            continue
        entry_projection = str(entry.get("work_item_projection_id") or "").strip()
        if entry_projection and projection_id and entry_projection != projection_id:
            continue
        out.append(dict(entry))
    out.sort(key=lambda e: float(e.get("timestamp") or 0.0))
    return out


_TERMINAL_PHASE_VALUES: frozenset[str] = frozenset({
    "approved", "failed", "cancelled",
})

# Tail-end gate / digest events (e.g. ``[Company:projection] gate approved``)
# can be persisted seconds after the work item's ``updated_at`` was last
# stamped, so a strict equality cap drops the closing entry. Sixty seconds
# is well under the smallest expected gap between consecutive shared work
# items on the same origin task and comfortably covers the observed lag.
_TIME_WINDOW_UPPER_GRACE_SEC = 60.0


def _work_item_time_window(item: Any) -> tuple[float | None, float | None]:
    """Return ``(lower, upper)`` epoch-second bounds for the work item's
    runtime activity. Used to scope progress entries when they have to be
    fetched from a shared origin task that also carries other work items'
    entries. ``upper`` is ``None`` for non-terminal phases — the work item
    is still emitting events, so no cap is meaningful yet.
    """
    lower = _coerce_event_timestamp(getattr(item, "created_at", None))
    phase_value = _phase_value(getattr(item, "phase", None))
    upper: float | None = None
    if phase_value in _TERMINAL_PHASE_VALUES:
        updated = _coerce_event_timestamp(getattr(item, "updated_at", None))
        if updated is not None:
            upper = updated + _TIME_WINDOW_UPPER_GRACE_SEC
    return lower, upper


def _progress_entries_for_work_item(
    item: Any,
    linked_task: Any | None,
    *,
    progress_by_task: dict[str, list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    """Return detailed runtime activity for one DelegationWorkItem.

    ``task_progress`` is the authoritative source for thinking/tool/gate
    records. Two source paths exist:

    1. The work item's own runtime task — the default and uncontested case.
    2. The runtime task's ``origin_task_id`` when ``shared_role_session=True``
       — leader-side turns (intake, review) ride on the user-facing primary
       chat and ``ws_handler`` redirects ``on_progress`` writes to the
       origin task. Without this fallback the Execution Progress panel
       renders "No runtime activity yet" for the leader's intake even
       though the LLM produced a full timeline.

    Entries pulled from path 2 are time-windowed against the work item's
    ``created_at`` / ``updated_at`` so unstamped runtime entries from a
    sibling shared-role-session work item that also rides on the same
    origin task can't bleed into this row.

    The metadata ``progress_log`` fallback exists for older snapshots and
    for tests that only construct in-memory work items.
    """
    projection_id = projection_id_for_work_item(item) or ""
    raw_entries: list[Any] = []
    seen_keys: set[tuple[Any, ...]] = set()

    def _push(entry: Any, *, window: tuple[float | None, float | None] | None = None) -> None:
        if not isinstance(entry, dict):
            return
        if window is not None:
            ts = _coerce_event_timestamp(entry.get("timestamp"))
            lower, upper = window
            if ts is not None:
                if lower is not None and ts < lower:
                    return
                if upper is not None and ts > upper:
                    return
        key = (
            entry.get("timestamp"),
            entry.get("type"),
            entry.get("summary"),
            entry.get("detail"),
        )
        if key in seen_keys:
            return
        seen_keys.add(key)
        raw_entries.append(entry)

    if progress_by_task and linked_task is not None:
        runtime_task_id = str(getattr(linked_task, "id", "") or "").strip()
        if runtime_task_id:
            for entry in progress_by_task.get(runtime_task_id, []) or []:
                _push(entry)

        metadata = dict(getattr(linked_task, "metadata", {}) or {})
        if bool(metadata.get("shared_role_session", False)):
            origin_task_id = str(metadata.get("origin_task_id", "") or "").strip()
            if origin_task_id and origin_task_id != runtime_task_id:
                window = _work_item_time_window(item)
                for entry in progress_by_task.get(origin_task_id, []) or []:
                    _push(entry, window=window)

    if not raw_entries:
        view = WorkItemContextView(item, linked_task)
        raw_entries = view.get_list("progress_log")
    return _filter_progress_log_by_projection(raw_entries, projection_id)


def _activity_section_for_work_item(
    item: Any,
    linked_task: Any | None,
    *,
    progress_by_task: dict[str, list[dict[str, Any]]] | None,
    kind: str,
    title: str,
    role_name: str = "",
) -> dict[str, Any] | None:
    entries = _progress_entries_for_work_item(
        item,
        linked_task,
        progress_by_task=progress_by_task,
    )
    runtime_task_id = (
        str(getattr(linked_task, "id", "") or "").strip()
        if linked_task is not None
        else ""
    )
    if not entries and not runtime_task_id:
        return None

    role_id = str(getattr(item, "role_id", "") or "").strip()
    view = WorkItemContextView(item, linked_task)
    display_role_name = (
        role_name
        or str(view.get("work_item_role_name", "") or "").strip()
        or _format_role_display_name(role_id)
    )
    return {
        "kind": kind,
        "title": title or str(getattr(item, "title", "") or "").strip() or kind.title(),
        "role_name": display_role_name or None,
        "runtime_task_id": runtime_task_id or None,
        "entries": entries,
        "_sort_at": _coerce_event_timestamp(getattr(item, "created_at", None))
        or _coerce_event_timestamp(getattr(item, "updated_at", None))
        or (float(entries[0].get("timestamp") or 0.0) if entries else 0.0),
    }


def _is_final_delivery_rollup_item(item: Any, meta: dict[str, Any], kind: str) -> bool:
    turn_type = turn_type_for_work_item(item, fallback=str(meta.get("work_item_turn_type", "") or ""))
    if kind != "delivery" and turn_type not in {"deliver", "delivery"}:
        return False
    return (
        str(meta.get("feedback_scope", "") or "").strip() == "final"
        or bool(meta.get("authoritative_output", False))
        or bool(meta.get("requires_user_feedback", False))
        or bool(meta.get("user_visible", False))
    )


def _public_activity_section(section: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in section.items()
        if not key.startswith("_")
    }


def _build_role_work_items_for_session(
    *,
    work_items: list[Any],
    task_by_work_item_id: dict[str, Any],
    event_adapter: "EventAdapter | None",
    progress_by_task: dict[str, list[dict[str, Any]]] | None = None,
    external_team_summary_by_task: dict[str, dict[str, Any]] | None = None,
    group_by_executor: bool = False,
) -> dict[str, dict[str, Any]]:
    """Group DelegationWorkItems and emit per-role summaries.

    The output schema is documented in ``plan/bug-breezy-dragonfly.md`` and
    consumed by ``frontend_src/lib/collabSync.ts:mapBackendRoleWorkItems``.

    Hidden report/review execution work items are not rows.  Their runtime
    activity is attached to the target visible work item so the UI keeps
    "1 row = 1 business work item" while preserving the audit trail.
    """
    result: dict[str, dict[str, Any]] = {}
    auxiliary_sections_by_target: dict[str, list[dict[str, Any]]] = {}

    for item in work_items or []:
        meta = dict(getattr(item, "metadata", {}) or {})
        if is_review_execution_work_item_metadata(meta):
            target_id = str(meta.get("review_target_work_item_id", "") or "").strip()
            section_kind = "review"
            title = str(getattr(item, "title", "") or "").strip() or "Review activity"
        elif is_report_execution_work_item_metadata(meta):
            target_id = str(meta.get("report_target_work_item_id", "") or "").strip()
            section_kind = "report"
            title = str(getattr(item, "title", "") or "").strip() or "Report activity"
        else:
            continue
        if not target_id:
            continue
        wi_id = str(getattr(item, "work_item_id", "") or "").strip()
        linked_task = task_by_work_item_id.get(wi_id)
        section = _activity_section_for_work_item(
            item,
            linked_task,
            progress_by_task=progress_by_task,
            kind=section_kind,
            title=title,
        )
        if section is None:
            continue
        auxiliary_sections_by_target.setdefault(target_id, []).append(section)

    for item in work_items or []:
        meta = dict(getattr(item, "metadata", {}) or {})
        if is_review_execution_work_item_metadata(meta):
            continue
        if is_report_execution_work_item_metadata(meta):
            continue
        if bool(meta.get("attention_work_item", False)):
            continue
        kind = str(getattr(item, "kind", "") or "").strip().lower()
        is_final_delivery = _is_final_delivery_rollup_item(item, meta, kind)
        phase = item.phase if hasattr(item, "phase") else None
        if phase is None:
            continue
        if group_by_executor and kind not in {"execute", "intake", "delivery"} and not is_final_delivery:
            continue
        if should_hide_work_item_from_company_kanban(meta):
            include_deleted_business_item = (
                group_by_executor
                and kind in {"execute", "intake"}
                and (
                    bool(meta.get("deleted_by_manager_tool", False))
                    or phase == Phase.CANCELLED
                )
            )
            if not include_deleted_business_item:
                continue
        # Top-level orchestration cards (no parent_work_item_id) are normally
        # filtered to keep parity with the company kanban's `visible_work_items`
        # filter. Exception: the leader's own `intake` card — it is the only
        # work item the leader has during planning/dispatch, and hiding it
        # leaves the leader invisible on the Execution Progress panel until
        # children produce review activity. Show it here so the leader's
        # runtime turns are visible from t=0; the kanban side stays unchanged.
        if not str(getattr(item, "parent_work_item_id", "") or "").strip():
            if kind != "intake" and not is_final_delivery:
                continue

        executor_role_id = str(getattr(item, "role_id", "") or "").strip()
        reviewer_role_id = str(getattr(item, "manager_role_id", "") or "").strip()
        try:
            if group_by_executor:
                role_id = executor_role_id
            else:
                role_id, _seat_id = effective_owner(phase, item)
        except Exception:
            role_id = executor_role_id
        role_id = (role_id or "").strip()
        if not role_id:
            continue

        wi_id = str(getattr(item, "work_item_id", "") or "").strip()
        linked_task = task_by_work_item_id.get(wi_id)
        view = WorkItemContextView(item, linked_task)
        projection_id = projection_id_for_work_item(item) or ""
        progress_log = _progress_entries_for_work_item(
            item,
            linked_task,
            progress_by_task=progress_by_task,
        )

        is_review_target = phase in IN_REVIEW_PHASES

        execution_turn_id = (
            str(getattr(linked_task, "id", "") or "").strip()
            if linked_task is not None
            else ""
        )

        wf_role_name = str(view.get("work_item_role_name", "") or "").strip()
        row_role_name = _format_role_display_name(executor_role_id, wf_role_name)
        activity_sections: list[dict[str, Any]] = []
        main_section = _activity_section_for_work_item(
            item,
            linked_task,
            progress_by_task=progress_by_task,
            kind=str(getattr(item, "kind", "") or "").strip() or "execute",
            title=str(getattr(item, "title", "") or "").strip() or "Work item activity",
            role_name=row_role_name,
        )
        if main_section is not None:
            activity_sections.append(main_section)
        activity_sections.extend(
            sorted(
                auxiliary_sections_by_target.get(wi_id, []),
                key=lambda section: (
                    float(section.get("_sort_at") or 0.0),
                    str(section.get("runtime_task_id") or ""),
                    str(section.get("kind") or ""),
                ),
            )
        )

        row = {
            "work_item_id": wi_id,
            "work_item_projection_id": projection_id or None,
            "phase": _phase_value(phase),
            "kanban_column": _COLUMN_ID_FOR_FRONTEND[kanban_column(phase)],
            "title": str(getattr(item, "title", "") or "").strip(),
            "kind": str(getattr(item, "kind", "") or "").strip() or None,
            "is_review_target": is_review_target,
            "executor_role_id": executor_role_id or None,
            "executor_role_name": row_role_name or None,
            "reviewer_role_id": reviewer_role_id or None,
            "selected_execution_agent": (
                _resolve_task_selected_execution_agent(linked_task)
                if linked_task is not None
                else meta.get("selected_execution_agent")
            ),
            "execution_unit_kind": view.get("execution_unit_kind"),
            "external_team_binding_id": view.get_dict("external_team_binding").get("binding_id"),
            "covered_role_ids": view.get_list("covered_role_ids"),
            "created_at": _coerce_event_timestamp(getattr(item, "created_at", None)),
            "updated_at": _coerce_event_timestamp(getattr(item, "updated_at", None)),
            "execution_turn_id": execution_turn_id or None,
            "progress_log": progress_log,
            "activity_sections": [
                _public_activity_section(section)
                for section in activity_sections
            ],
            "external_team_summary": (
                dict((external_team_summary_by_task or {}).get(execution_turn_id, {}))
                if execution_turn_id
                and isinstance((external_team_summary_by_task or {}).get(execution_turn_id), dict)
                else None
            ),
        }

        # Group by effective owner role. ``role_id`` is stable per logical
        # role within a run; fancier composite keys are unnecessary here
        # because each session's work items belong to a single delegation
        # run.
        summary = result.setdefault(role_id, {
            "role_key": role_id,
            "role_id": role_id,
            "role_name": _format_role_display_name(role_id),
            "team_instance_id": None,
            "runtime_status": "idle",
            "aggregated_status": "pending",
            "work_items": [],
        })
        summary["work_items"].append(row)
        # Promote a richer role_name as soon as we see one. Current-owner rows
        # in review carry the executor's role_name, so leave reviewer groups
        # named by reviewer role. Executor rollups intentionally keep that
        # executor name even while the row is waiting on review.
        if (group_by_executor or not is_review_target) and row_role_name and summary["role_name"] == _format_role_display_name(role_id):
            summary["role_name"] = row_role_name

    for summary in result.values():
        summary["work_items"].sort(
            key=lambda r: (
                float(r.get("created_at") or 0.0),
                str(r.get("work_item_id") or ""),
            )
        )

    for summary in result.values():
        summary["runtime_status"] = _resolve_role_runtime_status(
            event_adapter=event_adapter,
            role_id=summary["role_id"],
        )
        summary["aggregated_status"] = _role_aggregated_status(
            runtime_status=summary["runtime_status"],
            work_item_phases=[wi.get("phase") for wi in summary["work_items"]],
        )

    return result


def _build_executor_role_work_items_for_session(
    *,
    work_items: list[Any],
    task_by_work_item_id: dict[str, Any],
    event_adapter: "EventAdapter | None",
    progress_by_task: dict[str, list[dict[str, Any]]] | None = None,
    external_team_summary_by_task: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Display-only rollup for Execution Progress.

    Unlike ``_build_role_work_items_for_session`` this groups business rows by
    the original executor role even while they are awaiting manager review.
    Runtime ownership, review routing, and kanban board semantics continue to
    use the current-owner rollup.
    """
    return _build_role_work_items_for_session(
        work_items=work_items,
        task_by_work_item_id=task_by_work_item_id,
        event_adapter=event_adapter,
        progress_by_task=progress_by_task,
        external_team_summary_by_task=external_team_summary_by_task,
        group_by_executor=True,
    )


def _build_session_work_item_log(
    task: Any,
    *,
    task_meta: dict[str, Any],
    child_tasks: list[Any],
    task_meta_map: dict[str, dict[str, Any]],
    progress_by_task: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    work_item_entries: list[dict[str, Any]] = []
    visited_task_ids: set[str] = set()

    def collect_entries(source_task: Any, source_meta: dict[str, Any]) -> None:
        task_id = str(getattr(source_task, "id", "") or "").strip()
        if not task_id or task_id in visited_task_ids:
            return
        visited_task_ids.add(task_id)

        work_item_role = _work_item_role_payload(source_task)
        projection_id_hint = work_item_projection_id_from_metadata(source_meta)
        role_name_hint = str(work_item_role.get("work_item_role_name") or "").strip()
        projection_title_hint = role_name_hint or (
            projection_id_hint.replace("_", " ").replace("-", " ").title() if projection_id_hint else ""
        )

        for progress_entry in progress_by_task.get(task_id, []):
            if not isinstance(progress_entry, dict):
                continue
            work_item_entry = _work_item_entry_from_progress(
                progress_entry,
                runtime_task_id=task_id,
                projection_id_hint=projection_id_hint,
                projection_title_hint=projection_title_hint,
                role_name_hint=role_name_hint,
            )
            if work_item_entry is not None:
                work_item_entries.append(work_item_entry)

    if work_item_projection_id_from_metadata(task_meta):
        collect_entries(task, task_meta)

    for child in child_tasks:
        collect_entries(child, task_meta_map.get(getattr(child, "id", ""), {}))

    work_item_entries.sort(
        key=lambda item: (
            float(item.get("timestamp") or 0.0),
            str(item.get("execution_turn_id") or item.get("runtime_task_id") or ""),
            str(item.get("type") or ""),
        )
    )

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in work_item_entries:
        key = (
            item.get("timestamp"),
            item.get("type"),
            item.get("work_item_projection_id"),
            item.get("execution_turn_id") or item.get("runtime_task_id"),
            item.get("detail"),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _transcript_item_to_ui_message(
    item: dict[str, Any],
    *,
    channel_id: str,
    task_id: str,
    detail_level: TranscriptDetailLevel = "summary",
) -> dict[str, Any] | None:
    message = item.get("message")
    if (
        not message
        or getattr(message, "summary_flag", False)
        or _transcript_message_hidden_from_ui(message, detail_level=detail_level)
    ):
        return None

    kind = _transcript_message_kind(message)
    if kind in _FULL_DETAIL_ONLY_TRANSCRIPT_KINDS:
        content = _render_text_parts(item.get("parts", []))
    else:
        content = _render_parts(item.get("parts", []))
    content = content.strip()
    if kind == "runtime_v2_user_turn":
        content = _summarize_runtime_user_turn(content)
    if not content:
        return None

    role = str(getattr(message, "role", "") or "").strip().lower()
    agent_id = str(getattr(message, "agent_id", "") or "").strip()
    if kind == "runtime_v2_user_turn":
        sender = "system"
        sender_name = "Execution"
    elif agent_id and role in ("assistant", "subagent"):
        sender = agent_id
        sender_name = str(dict(getattr(message, "metadata", {}) or {}).get("visible_speaker", "") or "").strip()
        if not sender_name:
            sender_name = agent_id.replace("-", " ").replace("_", " ").title()
        if (
            sender_name.strip().lower().replace(" ", "_") == "task_generalist"
            and kind in {
                "runtime_v2_assistant",
                "runtime_v2_company_assistant",
                "runtime_v2_intermediate_assistant",
                "top_level_reply",
            }
        ):
            sender_name = "OPC"
    else:
        sender, sender_name = _ROLE_MAP.get(role, ("system", "OPC"))

    if role != "user":
        content, verification_footer = _strip_trailing_verification_footer(content)
        if not content:
            return None
    else:
        verification_footer = None

    message_id, timestamp, ui_meta = _session_message_ui_identity(message)
    message_metadata = dict(getattr(message, "metadata", {}) or {})
    runtime_thinking = (
        _render_thinking_parts(item.get("parts", []) if isinstance(item, dict) else [])
        or str(message_metadata.get("runtime_thinking", "") or "").strip()
    )
    return {
        "message_id": message_id or str(getattr(message, "message_id", "") or str(uuid.uuid4())),
        "channel_id": channel_id,
        "sender": sender,
        "sender_name": sender_name,
        "content": content,
        "created_at": timestamp,
        "reply_to_id": None,
        "mentions": [],
        "metadata": {
            "source": "engine",
            "role": role,
            "task_id": task_id,
            "transcript_kind": kind,
            "detail_visibility": (
                "summary"
                if message_metadata.get("company_final_turn")
                else _transcript_message_visibility(kind)
            ),
            **({"type": "system"} if kind == "runtime_v2_user_turn" else {}),
            **({"verification_verdict": verification_footer} if verification_footer else {}),
            **({"runtime_thinking": runtime_thinking} if runtime_thinking else {}),
            **({
                key: message_metadata.get(key)
                for key in (
                    "canonical_turn_id",
                    "turn_id",
                    "result_delivery_id",
                    "source_result_message_id",
                    "source_task_id",
                    "child_session_id",
                    "conversation_turn_id",
                    "execution_turn_id",
                    "work_item_projection_id",
                    "work_item_turn_type",
                    "runtime_session_id",
                )
                if message_metadata.get(key)
            }),
            **ui_meta,
        },
    }


def _messages_should_collapse_duplicate(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_content = _normalize_duplicate_content(left.get("content", ""))
    right_content = _normalize_duplicate_content(right.get("content", ""))
    if not left_content or not right_content or left_content != right_content:
        return False

    left_meta = dict(left.get("metadata", {}) or {})
    right_meta = dict(right.get("metadata", {}) or {})
    left_kind = str(left_meta.get("transcript_kind", "") or "").strip()
    right_kind = str(right_meta.get("transcript_kind", "") or "").strip()
    if not left_kind or not right_kind or left_kind == right_kind:
        return False
    return any({left_kind, right_kind} <= group for group in _TRANSCRIPT_DUPLICATE_KIND_GROUPS)


def _prefer_duplicate_message(left: dict[str, Any], right: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    left_kind = str(dict(left.get("metadata", {}) or {}).get("transcript_kind", "") or "").strip()
    right_kind = str(dict(right.get("metadata", {}) or {}).get("transcript_kind", "") or "").strip()
    if _RESULT_SURFACE_PRIORITY.get(left_kind, 0) >= _RESULT_SURFACE_PRIORITY.get(right_kind, 0):
        return left, right
    return right, left


def collapse_adjacent_transcript_duplicates(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate rendered result surfaces in chronological order.

    This is public within the Office UI package because transcript pagination
    formats raw database chunks incrementally.  Reusing the renderer's exact
    collapse rule at chunk boundaries keeps pagination and full snapshots
    identical.
    """
    collapsed: list[dict[str, Any]] = []
    for message in messages:
        if not collapsed:
            collapsed.append(message)
            continue
        previous = collapsed[-1]
        if not _messages_should_collapse_duplicate(previous, message):
            collapsed.append(message)
            continue

        preferred, secondary = _prefer_duplicate_message(previous, message)
        merged_metadata = {
            **dict(secondary.get("metadata", {}) or {}),
            **dict(preferred.get("metadata", {}) or {}),
        }
        merged_visibility = "summary"
        if (
            str(dict(previous.get("metadata", {}) or {}).get("detail_visibility", "") or "").strip() == "full"
            and str(dict(message.get("metadata", {}) or {}).get("detail_visibility", "") or "").strip() == "full"
        ):
            merged_visibility = "full"
        merged_metadata["detail_visibility"] = merged_visibility
        collapsed[-1] = {
            **secondary,
            **preferred,
            "content": _select_duplicate_display_content(preferred, secondary),
            "metadata": merged_metadata,
        }
    return collapsed


def build_transcript_ui_messages(
    transcript: list[dict[str, Any]],
    *,
    channel_id: str,
    task_id: str,
    detail_level: TranscriptDetailLevel = "summary",
) -> list[dict[str, Any]]:
    formatted_messages: list[dict[str, Any]] = []
    for item in transcript:
        formatted = _transcript_item_to_ui_message(
            item,
            channel_id=channel_id,
            task_id=task_id,
            detail_level="full",
        )
        if not formatted:
            continue
        formatted_messages.append({
            "message_id": formatted["message_id"],
            "sender": formatted["sender"],
            "sender_name": formatted["sender_name"],
            "content": formatted["content"],
            "timestamp": float(formatted.get("created_at") or time.time()),
            "reply_to_id": formatted.get("reply_to_id"),
            "mentions": list(formatted.get("mentions", [])),
            "metadata": dict(formatted.get("metadata", {}) or {}),
        })

    collapsed_messages = collapse_adjacent_transcript_duplicates(formatted_messages)
    normalized_detail_level = _normalize_transcript_detail_level(detail_level)
    if normalized_detail_level == "full":
        return collapsed_messages
    return [
        message
        for message in collapsed_messages
        if str(dict(message.get("metadata", {}) or {}).get("detail_visibility", "summary")).strip() != "full"
    ]


def task_to_kanban(
    task: Any,
    display_num: int,
    event_adapter: EventAdapter | None = None,
) -> dict[str, Any]:
    """Convert OPC Task to frontend KanbanTask (snake_case for collabSync).

    Frontend's collabSync.ts mapBackendTask() will convert to camelCase.
    """
    meta = task.metadata if hasattr(task, "metadata") and isinstance(task.metadata, dict) else {}
    runtime_meta = dict(meta.get("runtime_v2", {}) or {})
    member_session_meta = dict(meta.get("member_session_state", {}) or {})
    assignee_ids = (
        [event_adapter._resolve_role_to_agent(task.assigned_to)]
        if event_adapter and task.assigned_to
        else ([task.assigned_to] if task.assigned_to else [])
    )
    work_item_role = _work_item_role_payload(task)
    result = build_base_task_payload(
        task,
        display_num,
        assignee_ids=assignee_ids,
        extra={
            **work_item_identity_payload_from_metadata(meta),
            "company_profile": meta.get("company_profile"),
            **work_item_role,
            "work_item_gate": meta.get("work_item_gate"),
            "employee_assignment": meta.get("employee_assignment"),
            "selected_execution_agent": _resolve_task_selected_execution_agent(task),
            "execution_unit_kind": meta.get("execution_unit_kind"),
            "external_team_binding_id": dict(meta.get("external_team_binding", {}) or {}).get("binding_id"),
            "external_team_boundary_role_id": dict(meta.get("external_team_binding", {}) or {}).get("boundary_role_id"),
            "covered_role_ids": list(meta.get("covered_role_ids", []) or []),
            "external_company_execution_fence": meta.get("external_company_execution_fence"),
            "origin_channel": meta.get("origin_channel"),
            "progress_log": list(meta.get("progress_log", []))[-10:],
            "handoff_context": _extract_work_item_summary_for_downstream(meta),
            "runtime_session_id": runtime_meta.get("runtime_session_id"),
            "resume_cursor": runtime_meta.get("resume_cursor"),
            "worktree_path": runtime_meta.get("worktree_path"),
            "runtime_status": runtime_meta.get("status"),
            "verification": runtime_meta.get("verification"),
            "prompt_prefix_fingerprint": runtime_meta.get("prompt_prefix_fingerprint"),
            "resident_status": _runtime_status_from_member_meta(member_session_meta),
            "actionable_inbox_count": member_session_meta.get("actionable_inbox_count"),
            "protocol_backlog_count": member_session_meta.get("protocol_backlog_count"),
            "notification_backlog_count": member_session_meta.get("notification_backlog_count"),
            "latest_notification": member_session_meta.get("latest_notification"),
            # Fix 5 PR7: queue depth surfaces "CTO has 2 tasks queued"
            # behind the focused one when the serial-queue flag is on.
            "pending_queue_depth": int(
                member_session_meta.get("pending_queue_depth", 0) or 0
            ),
            "pending_work_item_ids": list(
                member_session_meta.get("pending_work_item_ids", []) or []
            ),
        },
    )

    # Agent runtime state from EventAdapter trackers (via public API)
    # task.assigned_to stores opc_role_id; get_tracker expects UI agent_id
    if event_adapter and task.assigned_to:
        agent_id = event_adapter._resolve_role_to_agent(task.assigned_to)
        tracker = event_adapter.get_tracker(agent_id)
        if tracker:
            result["agent_status"] = tracker.state.value
            result["current_tool"] = tracker.current_tool

    task_id = str(getattr(task, "id", "") or "").strip()
    result.update(_execution_turn_alias_payload(task_id))

    return result


def work_item_to_kanban(
    item: Any,
    display_num: int,
    *,
    board_id: str,
    event_adapter: EventAdapter | None = None,
    seat_by_id: dict[str, Any] | None = None,
    employee_name_by_id: dict[str, str] | None = None,
    task_by_work_item_id: dict[str, Any] | None = None,
    suspended_phase_by_work_item_id: dict[str, str] | None = None,
) -> dict[str, Any]:
    seat_by_id = seat_by_id or {}
    employee_name_by_id = employee_name_by_id or {}
    task_by_work_item_id = task_by_work_item_id or {}

    metadata = dict(getattr(item, "metadata", {}) or {})
    phase: Phase = item.phase
    work_item_id = str(getattr(item, "work_item_id", "") or "").strip()
    suspended_phase_value = str((suspended_phase_by_work_item_id or {}).get(work_item_id, "") or "").strip()
    if not suspended_phase_value and str(metadata.get("dispatch_hold", "") or "").strip() == "company_runtime_suspended":
        suspended_phase_value = str(metadata.get("suspended_phase", "") or "").strip()
    if suspended_phase_value:
        try:
            phase = Phase(suspended_phase_value)
        except ValueError:
            pass
    column = _frontend_column_id(phase)
    worker_seat_id = str(getattr(item, "seat_id", "") or metadata.get("seat_id", "") or "").strip()
    worker_role_id = str(getattr(item, "role_id", "") or "").strip()
    # Kanban-push: when a card is in `in_review`, its effective owner swaps
    # to the manager so the UI naturally places it in the reviewer's
    # swimlane/avatar row. The immutable `work_item_role_id` / `role_id`
    # values stay as the executor identity for audit purposes.
    effective_role_id, effective_seat_id = effective_owner(phase, item)
    seat_id = effective_seat_id or worker_seat_id
    seat = seat_by_id.get(seat_id)
    employee_id = str(getattr(seat, "employee_id", "") or "").strip() if seat is not None else ""
    employee_name = employee_name_by_id.get(employee_id, "")
    linked_task = task_by_work_item_id.get(str(getattr(item, "work_item_id", "") or "").strip())
    linked_meta = _task_metadata(linked_task) if linked_task is not None else {}
    external_team_binding = {
        **dict(metadata.get("external_team_binding", {}) or {}),
        **dict(linked_meta.get("external_team_binding", {}) or {}),
    }
    # Work-item context sync: WorkItemContextView prefers work_item.metadata, falls
    # back to linked_task.metadata. The four UI-critical fields
    # (work_item_role_name / employee_prompt_context / employee_delta_context
    # / progress_log) are mirrored onto work_item by Step 9, so reads below
    # transparently see them from the work_item side once mirrored.
    view = WorkItemContextView(item, linked_task)
    role_id = effective_role_id or worker_role_id
    role_name = role_id.replace("_", " ").replace("-", " ").title() if role_id else ""
    wf_role_name = str(view.get("work_item_role_name", "") or "").strip()
    if wf_role_name:
        role_name = wf_role_name
    # employee_assignment is WorkItem-owned, with a permitted runtime Task
    # execution-copy fallback during migration.
    employee_assignment = view.get_dict("employee_assignment")
    if employee_id and not str(employee_assignment.get("employee_id", "") or "").strip():
        employee_assignment["employee_id"] = employee_id
    if employee_name and not str(employee_assignment.get("name", "") or "").strip():
        employee_assignment["name"] = employee_name
    prompt_ctx = str(view.get("employee_prompt_context", "") or "").strip()
    if prompt_ctx:
        employee_assignment.setdefault("prompt_context", prompt_ctx)
    delta_ctx = str(view.get("employee_delta_context", "") or "").strip()
    if delta_ctx:
        employee_assignment.setdefault("delta_context", delta_ctx)
    assignee_ids = (
        [event_adapter._resolve_role_to_agent(role_id)]
        if event_adapter and role_id
        else ([role_id] if role_id else [])
    )
    blocked_reason = str(getattr(item, "blocked_reason", "") or "").strip()
    rework_feedback = str(metadata.get("rework_feedback", "") or "").strip()
    work_item_projection_id = projection_id_for_work_item(item)
    work_item_turn_type = turn_type_for_work_item(item)
    # Card identity is the DelegationWorkItem.work_item_id. `session_id`,
    # `chat_channel_id`, and `runtime_task_id` are audit/chat-routing references
    # to the runtime Task, not the card's identity.
    linked_task_id = str(getattr(linked_task, "id", "") or "").strip() if linked_task is not None else ""
    payload = {
        "task_id": work_item_id,
        "work_item_id": work_item_id,
        "display_id": f"OPC-{display_num}",
        "board_id": board_id,
        "column_id": column,
        "title": str(getattr(item, "title", "") or "").strip(),
        "description": str(getattr(item, "summary", "") or "").strip(),
        # `phase` is the single source of truth (14-state machine value);
        # `kanban_column` is the pure-function projection consumed by the UI
        # to place the card in one of the four columns.
        "phase": phase.value,
        "kanban_column": column,
        "priority": None,
        "assignee_ids": assignee_ids,
        "tags": [str(getattr(item, "kind", "") or "").strip()] if str(getattr(item, "kind", "") or "").strip() else [],
        "sort_order": display_num,
        "session_id": getattr(linked_task, "session_id", None) if linked_task is not None else None,
        "chat_channel_id": f"session:{getattr(linked_task, 'id', '')}" if linked_task is not None else "",
        "created_at": datetime_to_timestamp(getattr(item, "created_at", None)),
        "updated_at": datetime_to_timestamp(getattr(item, "updated_at", None)),
        "dependencies": [
            str(dep).strip()
            for dep in list(metadata.get("dependency_work_item_ids", []) or [])
            if str(dep).strip()
        ],
        **work_item_identity_payload(projection_id=work_item_projection_id, turn_type=work_item_turn_type),
        "company_profile": str(linked_meta.get("company_profile", "") or "").strip() if linked_task is not None else None,
        "selected_execution_agent": (
            _resolve_task_selected_execution_agent(linked_task)
            if linked_task is not None
            else metadata.get("selected_execution_agent")
        ),
        "execution_unit_kind": linked_meta.get("execution_unit_kind") or metadata.get("execution_unit_kind"),
        "external_team_binding_id": external_team_binding.get("binding_id"),
        "external_team_boundary_role_id": external_team_binding.get("boundary_role_id"),
        "covered_role_ids": list(linked_meta.get("covered_role_ids", []) or metadata.get("covered_role_ids", []) or []),
        "external_company_execution_fence": linked_meta.get("external_company_execution_fence") or metadata.get("external_company_execution_fence"),
        # work_item_role_id keeps the executor identity for audit/UI labels;
        # assignee_ids above already reflects the effective owner for lane
        # placement.
        "work_item_role_id": worker_role_id or None,
        "work_item_role_name": (
            (worker_role_id.replace("_", " ").replace("-", " ").title() if worker_role_id else "")
            or role_name or None
        ),
        "employee_assignment": employee_assignment or None,
        # Work-item context sync: progress_log via view (work_item side preferred;
        # falls back to linked_task.metadata). view.get_list returns a copy
        # so the subsequent [-10:] slice is safe.
        "progress_log": view.get_list("progress_log")[-10:],
        "runtime_session_id": str(metadata.get("assigned_role_runtime_id", "") or getattr(item, "role_runtime_session_id", "") or "").strip() or None,
        "blocked_reason": blocked_reason or None,
        "review_verdict": verdict(phase),
        "review_summary": (
            str((metadata.get("structured_review_verdict") or {}).get("summary", "") or "").strip() or None
            if isinstance(metadata.get("structured_review_verdict"), dict)
            else None
        ),
        "review_owner_role_id": str(metadata.get("review_owner_role_id", "") or "").strip() or None,
        "review_owner_seat_id": str(metadata.get("review_owner_seat_id", "") or "").strip() or None,
        "scope_key": str(metadata.get("scope_key", "") or "").strip() or None,
        "completion_report": metadata.get("completion_report"),
        "rework_feedback": rework_feedback or None,
        "manager_role_id": str(getattr(item, "manager_role_id", "") or "").strip() or None,
        "manager_seat_id": str(getattr(item, "manager_seat_id", "") or "").strip() or None,
        **_execution_turn_alias_payload(linked_task_id),
        "handoff_context": _extract_markdown_text(view.get("handoff_context"), max_chars=500),
        "original_message": str(linked_meta.get("original_message", "") or "").strip() or None,
        "planning_context": str(metadata.get("planning_context", "") or "").strip() or None,
        "deliverables": [
            str(value).strip()
            for value in list(metadata.get("deliverables", []) or [])
            if str(value).strip()
        ],
        "acceptance_criteria": [
            str(value).strip()
            for value in list(metadata.get("acceptance_criteria", []) or [])
            if str(value).strip()
        ],
        "delegation_rationale": str(metadata.get("delegation_rationale", "") or "").strip() or None,
        "non_overlap_guard": str(metadata.get("non_overlap_guard", "") or "").strip() or None,
        "coordination_notes": str(metadata.get("coordination_notes", "") or "").strip() or None,
        "resident_assignment": dict(linked_meta.get("resident_assignment", {}) or {}) or None,
        "member_session_state": dict(linked_meta.get("member_session_state", {}) or {}) or None,
        "ownership_contract": dict(linked_meta.get("ownership_contract", {}) or {}) or None,
    }
    return payload


def _build_board_payload(
    board_id: str,
    name: str,
    *,
    description: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    timestamp = float(now) if now is not None else time.time()
    return {
        "board_id": board_id,
        "name": name,
        "description": description or "",
        "prefix": "OPC",
        "color": "#4f46e5",
        "next_task_num": 1,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _board_label(value: str, *, fallback: str) -> str:
    label = str(value or "").strip()
    if not label:
        return fallback
    compact = " ".join(label.split())
    return compact if len(compact) <= 64 else compact[:61].rstrip() + "..."


def _primary_session_tasks_by_session_id(
    tasks: list[Any],
    *,
    task_meta_map: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    identity_index = build_company_runtime_identity_index(tasks)
    company_identities = {
        identity.runtime_session_id: identity
        for identity in identity_index.identities
    }
    primary_tasks_by_session_id: dict[str, Any] = {}
    ordered_session_ids: list[str] = []
    for task in tasks:
        if is_runtime_auxiliary_task(task):
            continue
        task_id = str(getattr(task, "id", "") or "").strip()
        task_meta = (
            task_meta_map.get(task_id, {}) if task_meta_map is not None and task_id else _task_metadata(task)
        )
        if bool(task_meta.get("review_task", False)):
            continue
        session_id = str(getattr(task, "session_id", "") or "").strip()
        company_identity = company_identities.get(session_id)
        if company_identity is not None:
            anchor = identity_index.task(company_identity.ui_anchor_task_id)
            if anchor is not None and session_id not in primary_tasks_by_session_id:
                primary_tasks_by_session_id[session_id] = anchor
                ordered_session_ids.append(session_id)
            if anchor is not None:
                # A shared final-decider/work-item Task must never replace a
                # pure UI anchor that owns the same session id.
                continue
            # Without a pure anchor this scope has no primary chat container.
            # Never synthesize one from a role/work-item Task.
            continue
        if not session_id or _task_parent_session_link(task, task_meta):
            continue
        current = primary_tasks_by_session_id.get(session_id)
        if current is None:
            primary_tasks_by_session_id[session_id] = task
            ordered_session_ids.append(session_id)
            continue
        current_meta = (
            task_meta_map.get(str(getattr(current, "id", "") or "").strip(), {})
            if task_meta_map is not None
            else _task_metadata(current)
        )
        if _session_container_rank(task, task_meta) > _session_container_rank(current, current_meta):
            primary_tasks_by_session_id[session_id] = task
    return primary_tasks_by_session_id, ordered_session_ids


def _company_config_source_tasks_by_session_id(
    tasks: list[Any],
) -> dict[str, Any]:
    identity_index = build_company_runtime_identity_index(tasks)
    return {
        identity.runtime_session_id: task
        for identity in identity_index.identities
        if identity.config_source_task_id
        and (task := identity_index.task(identity.config_source_task_id)) is not None
    }


async def build_company_kanban_projection(
    engine: "OPCEngine",
    *,
    project_id: str,
    tasks: list[Any],
    event_adapter: EventAdapter | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    """Build a per-session kanban projection for company/custom mode.

    Each primary session (parent-less task) gets its own board, regardless of
    whether a delegation run has been started yet. Sessions with an open run
    get that run's work items; sessions without a run get an empty board so
    the leader can see their workspace from the moment they create the
    session.
    """
    if not getattr(engine, "store", None):
        return [], [], [], None
    store = engine.store

    # Group primary tasks by session (order preserved by task creation time
    # when the caller passes an ordered list). Review Tasks are runtime
    # scheduling artifacts, never a session's primary task.
    primary_tasks_by_session_id, ordered_session_ids = _primary_session_tasks_by_session_id(tasks)

    if not primary_tasks_by_session_id:
        return [], [], [], None

    runs: list[Any] = []
    list_runs = getattr(store, "list_delegation_runs", None)
    if callable(list_runs):
        raw_runs = list_runs(project_id=project_id)
        raw_runs = await raw_runs if inspect.isawaitable(raw_runs) else raw_runs
        if isinstance(raw_runs, (list, tuple)):
            runs = list(raw_runs)
    if not runs:
        list_open_runs = getattr(store, "list_open_delegation_runs", None)
        if callable(list_open_runs):
            raw_runs = list_open_runs(project_id=project_id)
            raw_runs = await raw_runs if inspect.isawaitable(raw_runs) else raw_runs
            if isinstance(raw_runs, (list, tuple)):
                runs = list(raw_runs)
    runs_by_session_id: dict[str, list[Any]] = {}
    for run in runs:
        sid = str(getattr(run, "session_id", "") or "").strip()
        if sid:
            runs_by_session_id.setdefault(sid, []).append(run)

    employee_name_by_id: dict[str, str] = {}
    org_engine = getattr(engine, "org_engine", None)
    hydrate_links = getattr(store, "hydrate_task_work_item_links", None)
    if callable(hydrate_links):
        try:
            await hydrate_links(tasks)
        except Exception:
            logger.opt(exception=True).debug("build_company_kanban_projection: link hydration failed")
    task_by_work_item_id = task_by_linked_work_item_id(tasks)
    suspended_phase_by_session: dict[str, dict[str, str]] = {}
    get_checkpoints = getattr(store, "get_pending_checkpoints", None)
    if callable(get_checkpoints):
        try:
            checkpoints = await get_checkpoints(
                project_id=project_id,
                checkpoint_types=["company_runtime_suspended", "company_runtime_interrupted"],
            )
        except Exception:
            checkpoints = []
        for checkpoint in checkpoints:
            sid = str(getattr(checkpoint, "session_id", "") or "").strip()
            if not sid:
                continue
            phase_by_work_item = suspended_phase_by_session.setdefault(sid, {})
            for snapshot in list((getattr(checkpoint, "payload", {}) or {}).get("active_work_items", []) or []):
                if not isinstance(snapshot, dict):
                    continue
                wid = str(snapshot.get("work_item_id", "") or "").strip()
                phase = str(snapshot.get("phase", "") or "").strip()
                if wid and phase:
                    phase_by_work_item[wid] = phase

    formatted_tasks: list[dict[str, Any]] = []
    formatted_columns: list[dict[str, Any]] = []
    formatted_boards: list[dict[str, Any]] = []
    work_items_by_session_id: dict[str, list[Any]] = {}
    projection_meta: dict[str, Any] = {
        "run_ids": [],
        "board_ids": [],
        "session_ids": [],
        # Surfaces the run's DelegationWorkItems back to ``build_snapshot`` so
        # ``_build_role_work_items_for_session`` can render the work-item
        # driven Execution Progress panel without re-fetching from the store.
        "work_items_by_session_id": work_items_by_session_id,
        "task_by_work_item_id": task_by_work_item_id,
    }

    for session_id in ordered_session_ids:
        primary_task = primary_tasks_by_session_id[session_id]
        board_id = str(getattr(primary_task, "id", "") or "").strip() or f"session:{session_id}"
        board_name = _board_label(
            str(getattr(primary_task, "title", "") or "").strip(),
            fallback=f"Session {session_id[:8]}",
        )
        session_runs = sorted(
            runs_by_session_id.get(session_id, []),
            key=lambda item: (
                str(getattr(item, "created_at", "") or ""),
                str(getattr(item, "run_id", "") or ""),
            ),
        )
        description = (
            "Session board for delegation runs "
            + ", ".join(
                str(getattr(run, "run_id", "") or "").strip()[:8]
                for run in session_runs[:3]
            )
            if session_runs
            else f"Session board for {session_id[:8]}"
        )
        formatted_boards.append(_build_board_payload(board_id, board_name, description=description))
        formatted_columns.extend(build_company_board_columns(board_id))

        if not session_runs:
            projection_meta["run_ids"].append("")
            projection_meta["board_ids"].append(board_id)
            projection_meta["session_ids"].append(session_id)
            continue

        work_items: list[Any] = []
        seat_by_id: dict[str, Any] = {}
        for run in session_runs:
            run_id = str(getattr(run, "run_id", "") or "").strip()
            if not run_id:
                continue
            work_items.extend(await store.list_delegation_work_items(run_id))
            runtime_seats = await store.list_seat_states(run_id=run_id) if hasattr(store, "list_seat_states") else []
            for seat in runtime_seats:
                seat_id = str(getattr(seat, "seat_id", "") or "").strip()
                if seat_id:
                    seat_by_id[seat_id] = seat
        work_items = sorted(
            work_items,
            key=lambda item: (
                str(getattr(item, "created_at", "") or ""),
                str(getattr(item, "work_item_id", "") or ""),
            ),
        )
        # Stash the unfiltered work-item list so ``build_snapshot`` can hand
        # it to ``_build_role_work_items_for_session`` for the per-role
        # rollup. Do NOT use ``visible_work_items`` here — the role panel's
        # own filter already excludes hidden / auxiliary items, and using
        # the raw list keeps a single source of truth for that decision.
        work_items_by_session_id[session_id] = list(work_items)
        if org_engine is not None and hasattr(org_engine, "get_employee"):
            for seat in seat_by_id.values():
                employee_id = str(getattr(seat, "employee_id", "") or "").strip()
                if not employee_id or employee_id in employee_name_by_id:
                    continue
                employee = org_engine.get_employee(employee_id)
                employee_name_by_id[employee_id] = str(getattr(employee, "name", "") or employee_id).strip()

        visible_work_items = [
            item
            for item in work_items
            if str(getattr(item, "parent_work_item_id", "") or "").strip()
            and not bool(dict(getattr(item, "metadata", {}) or {}).get("attention_work_item", False))
            and not should_hide_work_item_from_company_kanban(dict(getattr(item, "metadata", {}) or {}))
        ]
        formatted_tasks.extend(
            work_item_to_kanban(
                item,
                index + 1,
                board_id=board_id,
                event_adapter=event_adapter,
                seat_by_id=seat_by_id,
                employee_name_by_id=employee_name_by_id,
                task_by_work_item_id=task_by_work_item_id,
                suspended_phase_by_work_item_id=suspended_phase_by_session.get(session_id, {}),
            )
            for index, item in enumerate(visible_work_items)
        )
        projection_meta["run_ids"].extend(
            str(getattr(run, "run_id", "") or "").strip()
            for run in session_runs
        )
        projection_meta["board_ids"].append(board_id)
        projection_meta["session_ids"].append(session_id)

    return formatted_tasks, formatted_columns, formatted_boards, projection_meta


def _agent_with_runtime(
    agent: dict[str, Any],
    event_adapter: EventAdapter | None,
) -> dict[str, Any]:
    result = dict(agent)
    agent_id = str(result.get("agent_id", "") or "").strip()
    tracker = event_adapter.get_tracker(agent_id) if event_adapter and agent_id else None
    runtime_status = tracker.state.value if tracker else str(result.get("status", "idle") or "idle")
    result["status"] = runtime_status
    result["runtime_status"] = runtime_status
    result["current_tool"] = tracker.current_tool if tracker else result.get("current_tool")
    result["current_task_id"] = tracker.task_id if tracker else result.get("current_task_id")
    return result


def _extract_markdown_text(value: Any, *, max_chars: int | None = None) -> str | None:
    if not value:
        return None

    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, dict):
        preferred = (
            value.get("markdown")
            or value.get("full_text")
            or value.get("text")
            or value.get("summary")
        )
        text = str(preferred).strip() if preferred else json.dumps(value, ensure_ascii=False, indent=2)
    else:
        text = str(value).strip()

    if not text:
        return None
    if max_chars is None or len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _extract_work_item_summary_for_downstream(meta: dict[str, Any]) -> str | None:
    """Extract a short handoff summary from task metadata."""
    return _extract_markdown_text(meta.get("handoff_context"), max_chars=500)


def _task_metadata(task: Any) -> dict[str, Any]:
    if hasattr(task, "metadata") and isinstance(task.metadata, dict):
        return task.metadata
    return {}


def _normalize_session_exec_mode(value: Any) -> str:
    return normalize_exec_mode(value)


def _normalize_session_company_profile(
    value: Any,
    *,
    default: str = "corporate",
) -> str:
    return normalize_company_profile(value, default=default)


def _canonical_session_identity(
    exec_mode: Any,
    company_profile: Any,
    org_id: Any = "",
) -> tuple[str, str, str]:
    identity = canonicalize_execution_identity(
        exec_mode=exec_mode,
        company_profile=company_profile,
        org_id=org_id,
        explicit_exec_mode=bool(str(exec_mode or "").strip()),
    )
    return identity.exec_mode, identity.company_profile, identity.org_id


def _normalize_session_preferred_agent(
    value: Any,
    *,
    default: str = "native",
) -> str:
    return normalize_preferred_agent(value, default=default)


def _resolve_task_session_config(
    task: Any,
    *,
    default_exec_mode: str = "task",
    default_company_profile: str = "corporate",
) -> tuple[str, str]:
    identity = execution_identity_from_task(
        task,
        default_exec_mode=default_exec_mode,
        default_company_profile=default_company_profile,
    )
    return identity.exec_mode, identity.company_profile


def _resolve_task_preferred_agent(
    task: Any,
    *,
    default_preferred_agent: str = "native",
) -> str:
    identity = execution_identity_from_task(
        task,
        default_preferred_agent=default_preferred_agent,
    )
    return _normalize_session_preferred_agent(
        identity.preferred_agent,
        default=default_preferred_agent,
    )


def _resolve_task_selected_execution_agent(
    task: Any,
    *,
    default_agent: str = "native",
) -> str:
    metadata = _task_metadata(task)
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
    explicit_selected = metadata.get("selected_execution_agent")
    if is_task_mode:
        if explicit_selected not in (None, "", [], {}):
            return _normalize_session_preferred_agent(explicit_selected, default=default_agent)
        preferred = metadata.get("preferred_agent")
        if preferred not in (None, "", [], {}):
            return _normalize_session_preferred_agent(preferred, default=default_agent)
    agent_selection = dict(metadata.get("agent_selection", {}) or {})
    selected = agent_selection.get("selected")
    if selected not in (None, "", [], {}):
        return _normalize_session_preferred_agent(selected, default=default_agent)
    if explicit_selected not in (None, "", [], {}):
        return _normalize_session_preferred_agent(explicit_selected, default=default_agent)
    assigned = getattr(task, "assigned_external_agent", None)
    if assigned not in (None, "", [], {}):
        return _normalize_session_preferred_agent(assigned, default=default_agent)
    return _normalize_session_preferred_agent("native", default=default_agent)


def _task_mode_origin_ui_task_id(task: Any, task_meta: dict[str, Any] | None = None) -> str:
    meta = task_meta if task_meta is not None else _task_metadata(task)
    task_id = str(getattr(task, "id", "") or "").strip()
    origin_task_id = str(meta.get("origin_task_id", "") or "").strip()
    if not origin_task_id or origin_task_id == task_id:
        return ""
    mode = str(meta.get("mode", "") or "").strip().lower()
    exec_mode = str(meta.get("exec_mode", "") or "").strip().lower()
    execution_mode = str(meta.get("execution_mode", "") or "").strip().lower()
    task_mode_contract = str(meta.get("task_mode_contract", "") or "").strip()
    if (
        mode == "task"
        or exec_mode in {"task", "project", "single"}
        or execution_mode in {"task", "task_mode", "project"}
        or task_mode_contract == "single_full_capability_main_agent"
    ):
        return origin_task_id
    return ""


def _task_parent_session_link(task: Any, meta: dict[str, Any]) -> str:
    """Resolve the parent session link, including older metadata-only records."""
    if bool(meta.get("shared_role_session", False)):
        return ""
    direct_parent = str(getattr(task, "parent_session_id", "") or "").strip()
    if direct_parent:
        return direct_parent

    legacy_parent = str(meta.get("parent_session_id", "") or "").strip()
    if legacy_parent and work_item_projection_id_from_metadata(meta):
        return legacy_parent
    return ""


def _shared_role_session_key(task: Any, meta: dict[str, Any]) -> str:
    if not bool(meta.get("shared_role_session", False)):
        return ""
    return str(getattr(task, "session_id", "") or "").strip()


def _session_container_rank(task: Any, meta: dict[str, Any]) -> tuple[int, tuple[int, int, int, float]]:
    role_id = str(getattr(task, "assigned_to", "") or meta.get("work_item_role_id", "") or "").strip()
    work_item_projection_id = work_item_projection_id_from_metadata(meta)
    shared_role_session = bool(meta.get("shared_role_session", False))
    # Prefer the user-facing root session row when it shares a session_id
    # with the final-decider role session. The board / sidebar container must
    # stay keyed to the origin task so frontend board selection remains stable.
    if not shared_role_session and not work_item_projection_id and not role_id:
        bucket = 3
    elif not shared_role_session:
        bucket = 2
    else:
        bucket = 1
    return bucket, _session_representative_rank(task, meta)


def _session_representative_rank(task: Any, meta: dict[str, Any]) -> tuple[int, int, int, float]:
    status = str(
        getattr(getattr(task, "status", None), "value", getattr(task, "status", "pending"))
    ).strip().lower()
    status_rank = {
        "running": 6,
        "pending": 5,
        "blocked": 4,
        "awaiting_peer": 4,
        "awaiting_manager_review": 4,
        "awaiting_human": 4,
        "awaiting_review": 4,
        "done": 3,
        "failed": 2,
        "idle": 1,
        "cancelled": 0,
    }.get(status, 1)
    role_id = str(getattr(task, "assigned_to", "") or meta.get("work_item_role_id", "") or "").strip()
    created_at = getattr(task, "created_at", None)
    created_ts = created_at.timestamp() if hasattr(created_at, "timestamp") else 0.0
    return (
        1 if role_id else 0,
        status_rank,
        1 if bool(meta.get("authoritative_output", False)) else 0,
        created_ts,
    )


def _slice_transcript_from_boundary(
    transcript: list[dict[str, Any]],
    boundary_message_id: str,
) -> list[dict[str, Any]]:
    if not boundary_message_id:
        return transcript
    for idx, item in enumerate(transcript):
        message = item.get("message")
        if getattr(message, "message_id", "") == boundary_message_id:
            return transcript[idx + 1:]
    return transcript


async def _build_session_context_preview(
    engine: "OPCEngine",
    session_id: str | None,
    *,
    max_chars: int | None = 4000,
) -> str | None:
    """Build a UI-visible context preview from persisted transcript data.

    Child sessions often have rich persisted assignment/context in their
    transcript even when task metadata lacks a dedicated handoff summary.
    """
    if not getattr(engine, "store", None) or not session_id:
        return None

    try:
        transcript = await engine.store.get_session_transcript(session_id)
    except Exception:
        return None
    if not transcript:
        return None

    try:
        compaction = await engine.store.get_latest_session_compaction(session_id)
    except Exception:
        compaction = None

    boundary_message_id = getattr(compaction, "source_boundary_message_id", "") if compaction else ""
    visible = _slice_transcript_from_boundary(transcript, boundary_message_id)

    blocks: list[str] = []
    for item in visible:
        message = item.get("message")
        if not message or getattr(message, "summary_flag", False):
            continue

        content = _render_parts(item.get("parts", [])).strip()
        if not content:
            continue

        role = str(getattr(message, "role", "") or "").strip().lower()
        if role == "user":
            label = "User"
        elif role == "system":
            label = "System"
        else:
            agent_id = str(getattr(message, "agent_id", "") or "").strip()
            label = agent_id or "Assistant"
        blocks.append(f"[{label}]\n{content}")

    if not blocks:
        return None

    rendered = "\n\n".join(blocks).strip()
    if max_chars is None or len(rendered) <= max_chars:
        return rendered
    return rendered[: max_chars - 1].rstrip() + "…"


# ── CLI → UI session reconciliation ───────────────────────────────────────

# Role mapping: OPCStore role → ChatStore (sender, sender_name)
_ROLE_MAP: dict[str, tuple[str, str]] = {
    "user": ("user", "You"),
    "assistant": ("assistant", "OPC"),
    "system": ("system", "OPC"),
}


def _render_parts(parts: list[Any]) -> str:
    """Convert session_parts into a single content string for UI display."""
    lines: list[str] = []
    for part in parts:
        pt = part.part_type
        payload = part.payload if isinstance(part.payload, dict) else {}
        if pt == "text":
            text = payload.get("text", "")
            if text:
                lines.append(CodexAdapter.normalize_transcript_text(str(text)))
        elif pt == "subtask_result":
            title = payload.get("task_title", "Sub-task")
            summary = payload.get("summary", "")
            lines.append(f"**{title}**: {summary}" if summary else f"**{title}** completed")
        elif pt == "task_result":
            title = payload.get("task_title", "Task")
            summary = payload.get("summary", "")
            lines.append(f"**{title}**: {summary}" if summary else f"**{title}** completed")
        elif pt == "tool_result":
            tool_name = str(payload.get("tool_name", "tool") or "tool")
            decision = dict(payload.get("permission_decision", {}) or {})
            summary = f"Tool result [{tool_name}]"
            rationale = str(decision.get("rationale", "") or "").strip()
            if rationale:
                summary = f"{summary}: {rationale}"
            lines.append(summary)
        # tool_output is typically noise (already shown via progress); skip
    return "\n".join(lines) if lines else ""


def _snapshot_runtime_checkpoint_payload(
    payload: dict[str, Any],
    runtime_meta: dict[str, Any],
) -> dict[str, Any]:
    return {
        "runtime_session_id": str(payload.get("runtime_session_id", "") or runtime_meta.get("runtime_session_id", "") or "").strip(),
        "resume_cursor": payload.get("resume_cursor", runtime_meta.get("resume_cursor")),
        "active_subagents": list(payload.get("active_subagents", runtime_meta.get("active_subagents", [])) or []),
        "permission_requests": list(payload.get("permission_requests", runtime_meta.get("permission_requests", [])) or []),
        "worktree_path": str(payload.get("worktree_path", "") or runtime_meta.get("worktree_path", "") or "").strip(),
    }


async def _build_snapshot_checkpoint_meta(engine: "OPCEngine", task: Any) -> dict[str, Any] | None:
    session_id = str(getattr(task, "session_id", "") or "").strip()
    if not session_id:
        return None
    getter = getattr(engine, "get_latest_pending_checkpoint_for_session", None)
    if not callable(getter):
        return None
    # Rendering a UI snapshot is read-only.  Legacy runtime-v2 enrichment may
    # be calculated for display, but must not persist through an unfenced live
    # Company Task: that write is controller-owned and its lease-loss signal is
    # intentionally a CancelledError subclass (which would tear down the WS).
    try:
        getter_parameters = inspect.signature(getter).parameters
    except (TypeError, ValueError):
        getter_parameters = {}
    if "persist_runtime_v2_migration" in getter_parameters:
        maybe_checkpoint = getter(
            session_id,
            persist_runtime_v2_migration=False,
        )
    else:
        maybe_checkpoint = getter(session_id)
    checkpoint = await maybe_checkpoint if inspect.isawaitable(maybe_checkpoint) else maybe_checkpoint
    if not checkpoint:
        return None
    payload = dict(getattr(checkpoint, "payload", {}) or {})
    checkpoint_type = str(getattr(checkpoint, "checkpoint_type", "") or "").strip()
    metadata = dict(getattr(task, "metadata", {}) or {})
    runtime_meta = dict(metadata.get("runtime_v2", {}) or {})

    if checkpoint_type == "task_user_input":
        pause_request = dict(payload.get("pause_request", {}) or {})
        is_task_mode = _metadata_is_task_mode_runtime({**metadata, **payload})
        work_item_projection_id = "" if is_task_mode else work_item_projection_id_from_metadata(metadata)
        work_item_turn_type = "" if is_task_mode else work_item_turn_type_from_metadata(metadata, fallback="")
        work_item_projection_title = str(getattr(task, "title", "") or "").strip()
        questions = [
            str(item).strip()
            for item in list(pause_request.get("questions", []) or [])
            if str(item).strip()
        ]
        input_questions = [
            dict(item)
            for item in list(pause_request.get("input_questions", []) or [])
            if isinstance(item, dict) and str(item.get("question", "") or item.get("header", "") or "").strip()
        ]
        if not input_questions:
            input_questions = [
                {
                    "id": f"question_{index + 1}",
                    "header": "",
                    "question": question,
                    "options": [],
                    "allow_freeform": True,
                    "required": True,
                }
                for index, question in enumerate(questions)
            ]
        return {
            "checkpoint_type": "task_user_input",
            "checkpoint_id": checkpoint.checkpoint_id,
            "task_id": str(payload.get("task_id", "") or getattr(task, "id", "") or "").strip(),
            **work_item_identity_payload(projection_id=work_item_projection_id, turn_type=work_item_turn_type),
            **({} if is_task_mode else {"work_item_projection_title": work_item_projection_title}),
            "execution_mode": payload.get("execution_mode") or metadata.get("execution_mode") or ("task_mode" if is_task_mode else ""),
            "summary": str(pause_request.get("reason", "") or payload.get("prompt", "") or "").strip(),
            "prompt": str(payload.get("prompt", "") or "").strip(),
            "questions": questions,
            "input_questions": input_questions,
            "required_fields": [
                str(item).strip()
                for item in list(pause_request.get("required_fields", []) or [])
                if str(item).strip()
            ],
            "context_note": str(pause_request.get("context_note", "") or "").strip(),
            "resume_hint": str(pause_request.get("resume_hint", "") or "").strip(),
            "requesting_role_id": str(
                payload.get("requesting_role_id") or pause_request.get("requesting_role_id") or ""
            ).strip(),
            "requesting_task_id": str(
                payload.get("requesting_task_id") or pause_request.get("requesting_task_id") or ""
            ).strip(),
            "requesting_work_item_id": str(
                payload.get("requesting_work_item_id") or pause_request.get("requesting_work_item_id") or ""
            ).strip(),
            "seat_id": str(payload.get("seat_id") or pause_request.get("seat_id") or "").strip(),
            **_snapshot_runtime_checkpoint_payload(payload, runtime_meta),
        }

    if checkpoint_type == "company_work_item_gate":
        gate = dict(payload.get("gate", {}) or {})
        projection_id = str(payload.get("work_item_projection_id") or "").strip()
        turn_type = str(payload.get("work_item_turn_type") or "").strip()
        projection_title = str(
            payload.get("work_item_projection_title") or projection_id or "Work item gate"
        ).strip()
        gate_type = str(gate.get("type", "") or "review").strip()
        prompt_lines = [
            f"{projection_title} requires confirmation.",
            f"Gate type: {gate_type}",
        ]
        instructions = str(gate.get("instructions", "") or "").strip()
        if instructions:
            prompt_lines.append(f"Instructions: {instructions}")
        return {
            "checkpoint_type": "company_work_item_gate",
            "checkpoint_id": checkpoint.checkpoint_id,
            **work_item_identity_payload(projection_id=projection_id, turn_type=turn_type),
            "work_item_projection_title": projection_title,
            "company_profile": payload.get("company_profile", ""),
            "summary": instructions or f"Pending {gate_type} confirmation",
            "prompt": "\n".join(prompt_lines),
            "options": [
                {"id": "approve", "label": "Approve"},
                {"id": "deny", "label": "Deny"},
            ],
            "default_action": "deny",
            **_snapshot_runtime_checkpoint_payload(payload, runtime_meta),
        }

    if checkpoint_type == "company_recruitment_confirmation":
        recruitment_plan = dict(payload.get("recruitment_plan", {}) or {})
        plan_metadata = dict(recruitment_plan.get("metadata", {}) or {})
        recruitment_agent = _normalize_session_preferred_agent(
            payload.get("recruitment_agent") or plan_metadata.get("recruitment_agent") or "native",
            default="native",
        )
        proposals_raw = list(recruitment_plan.get("proposals", []) or [])
        employee_payloads: list[dict[str, Any]] = []
        template_payloads: list[dict[str, Any]] = []
        org_engine = getattr(engine, "org_engine", None)
        reports_to_by_role = {
            str(getattr(agent, "role_id", "") or "").strip(): str(
                getattr(agent, "reports_to", "") or ""
            ).strip()
            for agent in (org_engine.list_agents() if org_engine else [])
            if str(getattr(agent, "role_id", "") or "").strip()
        }
        talent_market = getattr(engine, "talent_market", None)
        is_placeholder = getattr(engine, "_is_placeholder_staffing_employee", lambda _employee: False)
        employee_payload = getattr(engine, "_staffing_employee_payload", None)
        template_payload = getattr(engine, "_staffing_template_payload", None)
        if org_engine and callable(employee_payload):
            try:
                employee_payloads = [
                    employee_payload(employee)
                    for employee in org_engine.list_employees()
                    if not is_placeholder(employee)
                ]
            except Exception:
                employee_payloads = []
        if talent_market and callable(template_payload):
            try:
                template_payloads = [
                    template_payload(template)
                    for template in talent_market.list_available_templates()
                    if str(getattr(template, "id", "") or "").strip()
                ]
            except Exception:
                template_payloads = []
        employees_by_id = {
            str(item.get("employee_id", "") or "").strip(): item
            for item in employee_payloads
            if str(item.get("employee_id", "") or "").strip()
        }
        templates_by_id = {
            str(item.get("template_id", "") or "").strip(): item
            for item in template_payloads
            if str(item.get("template_id", "") or "").strip()
        }
        employees_by_role: dict[str, list[dict[str, Any]]] = {}
        for item in employee_payloads:
            role_ids = [
                str(role_id or "").strip()
                for role_id in list(item.get("role_ids", []) or [])
                if str(role_id or "").strip()
            ] or [str(item.get("role_id", "") or "").strip()]
            for role_id in role_ids:
                if role_id:
                    employees_by_role.setdefault(role_id, []).append(item)

        proposals: list[dict[str, Any]] = []
        staffing_roles: list[dict[str, Any]] = []
        staffing_selections: dict[str, dict[str, str]] = {}
        recruitment_rationales: list[dict[str, Any]] = []
        payload_role_agents: dict[str, str] = {}
        raw_payload_role_agents = payload.get("recruitment_role_agents")
        if isinstance(raw_payload_role_agents, dict):
            payload_role_agents = {
                str(raw_role_id or "").strip(): _normalize_session_preferred_agent(raw_agent, default="codex")
                for raw_role_id, raw_agent in raw_payload_role_agents.items()
                if str(raw_role_id or "").strip()
            }
        recruitment_role_agents: dict[str, str] = {}
        for proposal in proposals_raw:
            if not isinstance(proposal, dict):
                continue
            role_id = str(proposal.get("role_id", "") or "").strip()
            proposal_metadata = dict(proposal.get("metadata", {}) or {})
            default_agent = "codex"
            selected_agent = _normalize_session_preferred_agent(
                payload_role_agents.get(role_id) or proposal_metadata.get("selected_execution_agent"),
                default=default_agent,
            )
            if role_id:
                recruitment_role_agents[role_id] = selected_agent
            entry: dict[str, Any] = {
                "role_id": role_id,
                "status": proposal.get("status", ""),
                "rationale": proposal.get("rationale", ""),
                "role_labels": list(proposal.get("role_labels", []) or []),
                "default_agent": default_agent,
                "selected_agent": selected_agent,
            }
            candidate = proposal.get("candidate")
            if isinstance(candidate, dict):
                entry["candidate"] = {
                    "template_id": candidate.get("template_id", ""),
                    "template_name": candidate.get("template_name", ""),
                    "category": candidate.get("category", ""),
                    "domains": list(candidate.get("domains", []) or []),
                    "proposed_name": candidate.get("proposed_employee_name", ""),
                    "rationale": candidate.get("rationale", ""),
                }
                template_id = str(candidate.get("template_id", "") or "").strip()
                if template_id and template_id not in templates_by_id:
                    templates_by_id[template_id] = {
                        "kind": "template",
                        "template_id": template_id,
                        "template_name": candidate.get("template_name", "") or template_id,
                        "category": candidate.get("category", ""),
                        "domains": list(candidate.get("domains", []) or []),
                        "tags": [],
                        "description": "",
                        "preferred_external_agent": candidate.get("preferred_external_agent"),
                        "source_path": candidate.get("source_path", ""),
                    }
            existing_employee = proposal.get("existing_employee")
            if isinstance(existing_employee, dict):
                entry["existing_employee"] = {
                    "employee_id": existing_employee.get("employee_id", ""),
                    "employee_name": existing_employee.get("employee_name", ""),
                    "role_id": existing_employee.get("role_id", ""),
                    "domains": list(existing_employee.get("domains", []) or []),
                    "experience_score": existing_employee.get("experience_score", 0),
                    "rationale": existing_employee.get("rationale", ""),
                }
                employee_id = str(existing_employee.get("employee_id", "") or "").strip()
                if employee_id and employee_id not in employees_by_id:
                    item = {
                        "kind": "employee",
                        "employee_id": employee_id,
                        "employee_name": existing_employee.get("employee_name", "") or employee_id,
                        "role_id": existing_employee.get("role_id", "") or role_id,
                        "category": existing_employee.get("category", ""),
                        "domains": list(existing_employee.get("domains", []) or []),
                        "tags": [],
                        "description": "",
                        "preferred_external_agent": None,
                        "experience_score": existing_employee.get("experience_score", 0),
                    }
                    employees_by_id[employee_id] = item
                    if role_id:
                        employees_by_role.setdefault(role_id, []).append(item)
            proposals.append(entry)

            role_label = str((proposal.get("role_labels", []) or [role_id])[0] or role_id)
            default_selection: dict[str, str] = {"kind": "fallback", "id": ""}
            selection_label = "Fallback role-only"
            if isinstance(existing_employee, dict) and str(existing_employee.get("employee_id", "") or "").strip():
                employee_id = str(existing_employee.get("employee_id", "") or "").strip()
                default_selection = {"kind": "employee", "id": employee_id, "employee_id": employee_id}
                selection_label = str(existing_employee.get("employee_name", "") or employee_id)
            elif isinstance(candidate, dict) and str(candidate.get("template_id", "") or "").strip():
                template_id = str(candidate.get("template_id", "") or "").strip()
                default_selection = {"kind": "template", "id": template_id, "template_id": template_id}
                selection_label = str(candidate.get("proposed_employee_name") or candidate.get("template_name") or template_id)
            if role_id:
                staffing_selections[role_id] = dict(default_selection)
                same_role_ids = {
                    str(item.get("employee_id", "") or "").strip()
                    for item in employees_by_role.get(role_id, [])
                    if str(item.get("employee_id", "") or "").strip()
                }
                same_role_ids.update(
                    str(item or "").strip()
                    for item in list(proposal.get("existing_employee_ids", []) or [])
                    if str(item or "").strip()
                )
                staffing_roles.append(
                    {
                        "role_id": role_id,
                        "role_label": role_label,
                        "role_responsibility": "",
                        "reports_to": reports_to_by_role.get(role_id, ""),
                        "default_selection": default_selection,
                        "same_role_employee_ids": sorted(same_role_ids),
                        "fallback_available": True,
                        "default_agent": default_agent,
                        "selected_agent": selected_agent,
                        "default_source": "recruitment",
                    }
                )
                reason_parts = [
                    str((candidate or {}).get("rationale", "") or "").strip() if isinstance(candidate, dict) else "",
                    str((existing_employee or {}).get("rationale", "") or "").strip() if isinstance(existing_employee, dict) else "",
                    str(proposal.get("rationale", "") or "").strip(),
                ]
                recruitment_rationales.append(
                    {
                        "role_id": role_id,
                        "role_label": role_label,
                        "status": proposal.get("status", ""),
                        "selection_label": selection_label,
                        "rationale": next((item for item in reason_parts if item), ""),
                    }
                )
        return {
            "checkpoint_type": "company_recruitment_confirmation",
            "checkpoint_id": checkpoint.checkpoint_id,
            "company_profile": recruitment_plan.get("company_profile", "corporate"),
            "proposals": proposals,
            "summary": recruitment_plan.get("summary", ""),
            "recruitment_agent": recruitment_agent,
            "recruitment_role_agents": recruitment_role_agents,
            "recruitment_rationales": recruitment_rationales,
            "staffing_roles": staffing_roles,
            "staffing_pool": {
                "employees": list(employees_by_id.values()),
                "templates": list(templates_by_id.values()),
            },
            "staffing_selections": staffing_selections,
        }

    if checkpoint_type == "company_staffing_selection":
        org_engine = getattr(engine, "org_engine", None)
        reports_to_by_role = {
            str(getattr(agent, "role_id", "") or "").strip(): str(
                getattr(agent, "reports_to", "") or ""
            ).strip()
            for agent in (org_engine.list_agents() if org_engine else [])
            if str(getattr(agent, "role_id", "") or "").strip()
        }
        raw_role_agents = payload.get("recruitment_role_agents")
        payload_role_agents = raw_role_agents if isinstance(raw_role_agents, dict) else {}
        recruitment_agent = _normalize_session_preferred_agent(
            payload.get("recruitment_agent") or "native",
            default="native",
        )
        staffing_roles: list[dict[str, Any]] = []
        recruitment_role_agents: dict[str, str] = {}
        for raw_role in list(payload.get("staffing_roles", []) or []):
            if not isinstance(raw_role, dict):
                continue
            role = dict(raw_role)
            role_id = str(role.get("role_id", "") or "").strip()
            selected_agent = _normalize_session_preferred_agent(
                payload_role_agents.get(role_id) or role.get("selected_agent") or role.get("default_agent") or "codex",
                default="codex",
            )
            if role_id:
                role["selected_agent"] = selected_agent
                role["reports_to"] = str(
                    role.get("reports_to") or reports_to_by_role.get(role_id, "")
                ).strip()
                recruitment_role_agents[role_id] = selected_agent
            staffing_roles.append(role)
        return {
            "checkpoint_type": "company_staffing_selection",
            "checkpoint_id": checkpoint.checkpoint_id,
            "company_profile": payload.get("company_profile", "corporate"),
            "summary": payload.get("summary") or "Select staff manually, or run automatic recruitment.",
            "staffing_strategy": payload.get("staffing_strategy", ""),
            "recommended_action": payload.get("recommended_action", ""),
            "staffing_defaults": dict(payload.get("staffing_defaults", {}) or {}),
            "staffing_roles": staffing_roles,
            "recruitment_agent": recruitment_agent,
            "recruitment_role_agents": recruitment_role_agents,
            "staffing_pool": dict(payload.get("staffing_pool", {}) or {}),
        }

    if checkpoint_type == "company_delivery_feedback":
        delivery_package = payload.get("delivery_package")
        if not isinstance(delivery_package, dict):
            delivery_package = {}
        result_content = str(payload.get("result_content", "") or "").strip()
        summary = str(
            delivery_package.get("executive_summary")
            or delivery_package.get("summary")
            or payload.get("work_item_summary")
            or payload.get("work_item_summary_for_downstream")
            or result_content
            or ""
        ).strip()
        projection_id = str(payload.get("work_item_projection_id") or "").strip()
        turn_type = str(payload.get("work_item_turn_type") or "").strip()
        waiting_task_id = str(
            payload.get("waiting_task_id")
            or payload.get("task_id")
            or getattr(checkpoint, "task_id", "")
            or ""
        ).strip()
        return {
            "checkpoint_type": "company_delivery_feedback",
            "checkpoint_id": checkpoint.checkpoint_id,
            "waiting_task_id": waiting_task_id,
            "task_id": waiting_task_id or str(getattr(task, "id", "") or "").strip(),
            **work_item_identity_payload(projection_id=projection_id, turn_type=turn_type),
            "work_item_projection_title": payload.get("work_item_projection_title", ""),
            "company_profile": payload.get("company_profile", ""),
            "feedback_scope": payload.get("feedback_scope", "work_item"),
            "summary": summary,
            "prompt": payload.get("prompt", ""),
            "options": [
                {"id": "approve", "label": "Fully Agree / 完全同意"},
                {"id": "ignore", "label": "Ignore / 忽略"},
                {"id": "feedback", "label": "Feedback / 反馈"},
            ],
            "delivery_package": delivery_package,
            "result_content": result_content,
            "delivery_revision": payload.get("delivery_revision", ""),
            "owner_directive_revision": payload.get("owner_directive_revision", ""),
            "latest_user_directive": str(payload.get("latest_user_directive", "") or "").strip(),
            "waiting_work_item_id": str(payload.get("waiting_work_item_id", "") or "").strip(),
            **_snapshot_runtime_checkpoint_payload(payload, runtime_meta),
        }

    return None


def _checkpoint_meta_targets_task(checkpoint_meta: dict[str, Any], task: Any) -> bool:
    if str(checkpoint_meta.get("checkpoint_type", "") or "").strip() != "company_delivery_feedback":
        return True
    target_task_id = str(
        checkpoint_meta.get("waiting_task_id")
        or checkpoint_meta.get("task_id")
        or ""
    ).strip()
    task_id = str(getattr(task, "id", "") or "").strip()
    return not target_task_id or not task_id or target_task_id == task_id


def _message_can_host_checkpoint_meta(message: dict[str, Any], checkpoint_meta: dict[str, Any]) -> bool:
    if str(checkpoint_meta.get("checkpoint_type", "") or "").strip() != "company_delivery_feedback":
        return True
    metadata = dict(message.get("metadata", {}) or {})
    if str(message.get("sender", "") or "").strip().lower() == "system":
        return False
    if str(metadata.get("kind", "") or "").strip() == "worker_notification":
        return False
    # Parent-session child_result entries mirror child outputs. The review card
    # belongs to the actual waiting delivery task, not to every mirror.
    if str(metadata.get("transcript_kind", "") or "").strip() == "child_result":
        return False
    return True


def _synthetic_checkpoint_card_message(
    *,
    channel_id: str,
    checkpoint_meta: dict[str, Any],
) -> dict[str, Any]:
    checkpoint_id = str(checkpoint_meta.get("checkpoint_id", "") or "").strip()
    created_at = time.time()
    return {
        "message_id": f"checkpoint::{checkpoint_id}" if checkpoint_id else str(uuid.uuid4()),
        "channel_id": channel_id,
        "sender": "assistant",
        "sender_name": str(
            checkpoint_meta.get("work_item_projection_title")
            or checkpoint_meta.get("requesting_role_id")
            or "Company Member"
        ),
        "content": str(
            checkpoint_meta.get("prompt")
            or checkpoint_meta.get("summary")
            or "Human review requested."
        ),
        "timestamp": created_at,
        "created_at": created_at,
        "reply_to_id": None,
        "mentions": [],
        "metadata": dict(checkpoint_meta),
    }


def _attach_or_create_checkpoint_card(
    messages: list[dict[str, Any]],
    *,
    channel_id: str,
    checkpoint_meta: dict[str, Any],
) -> None:
    checkpoint_id = str(checkpoint_meta.get("checkpoint_id", "") or "").strip()
    synthetic_message_id = f"checkpoint::{checkpoint_id}" if checkpoint_id else ""
    for message in reversed(messages):
        if message.get("channel_id") != channel_id:
            continue
        if synthetic_message_id and str(message.get("message_id", "") or "") == synthetic_message_id:
            metadata = dict(message.get("metadata", {}) or {})
            metadata.update(checkpoint_meta)
            message["metadata"] = metadata
            return
        if str(message.get("sender", "") or "").strip().lower() == "user":
            continue
        if not _message_can_host_checkpoint_meta(message, checkpoint_meta):
            continue
        metadata = dict(message.get("metadata", {}) or {})
        metadata.update(checkpoint_meta)
        message["metadata"] = metadata
        return
    messages.append(_synthetic_checkpoint_card_message(
        channel_id=channel_id,
        checkpoint_meta=checkpoint_meta,
    ))


async def _build_company_runtime_control_by_task(
    engine: "OPCEngine",
    tasks: list[Any],
    project_id: str,
) -> dict[str, dict[str, Any]]:
    store = getattr(engine, "store", None)
    if not store:
        return {}

    checkpoints: list[Any] = []
    getter = getattr(store, "get_execution_checkpoints", None)
    if not callable(getter):
        getter = getattr(store, "get_pending_checkpoints", None)
    if callable(getter):
        try:
            kwargs = {
                "project_id": project_id,
                "checkpoint_types": sorted(COMPANY_RUNTIME_CHECKPOINT_TYPES),
            }
            if getattr(getter, "__name__", "") == "get_execution_checkpoints":
                kwargs["statuses"] = sorted(ACTIVE_COMPANY_RUNTIME_CHECKPOINT_STATUSES)
            checkpoints = await getter(**kwargs)
        except Exception:
            logger.opt(exception=True).debug("snapshot: failed to load company runtime checkpoints")
            checkpoints = []

    identity_index = build_company_runtime_identity_index(tasks, checkpoints)

    result: dict[str, dict[str, Any]] = {}
    for identity in identity_index.identities:
        group = [
            task
            for task_id in identity.runtime_task_ids
            if (task := identity_index.task(task_id)) is not None
        ]
        checkpoint = identity.checkpoint

        # Persisted RUNNING is only a projection.  The controller-local
        # execution registry is the sole proof that this process still owns a
        # coroutine capable of monitoring and persisting the run.  Driver
        # ownership can deliberately sit on a terminal work-item envelope
        # while the resumed scheduler is still active, so Task.status must not
        # filter this lookup.
        runtime_is_live = getattr(engine, "_task_runtime_is_live", None)
        has_running_task = False
        if callable(runtime_is_live):
            for task in group:
                live_result = runtime_is_live(task)
                if inspect.isawaitable(live_result):
                    live_result = await live_result
                if live_result is True:
                    has_running_task = True
                    break
        any_stop_in_progress = any(
            str((getattr(task, "metadata", {}) or {}).get("company_runtime_stop_state", "") or "").strip()
            in {"suspending", "suspended", "resuming_after_suspending"}
            and bool(str((getattr(task, "metadata", {}) or {}).get("company_runtime_stop_marked_at", "") or "").strip())
            for task in group
        )
        any_held_suspended = any(
            str((getattr(task, "metadata", {}) or {}).get("dispatch_hold", "") or "").strip()
            == "company_runtime_suspended"
            for task in group
        )
        any_resuming = any(
            str((getattr(task, "metadata", {}) or {}).get("company_runtime_stop_state", "") or "").strip() == "resuming"
            for task in group
        )
        checkpoint_status = str(getattr(checkpoint, "status", "") or "").strip().lower() if checkpoint is not None else ""
        # ``resuming`` is the durable checkpoint claim for the whole resumed
        # execution, not merely a short UI transition.  Once the controller
        # registry proves that execution ownership exists, the runtime is
        # stoppable and must project as running until that ownership ends.
        if checkpoint_status == "pending":
            state = "suspended"
        elif checkpoint_status == "resuming" and (
            any_held_suspended or any_stop_in_progress
        ):
            state = "suspending"
        elif checkpoint_status == "resuming" and has_running_task:
            state = "running"
        elif any_resuming or checkpoint_status == "resuming":
            state = "resuming"
        elif checkpoint is not None:
            state = "suspended"
        elif any_held_suspended or (
            any_stop_in_progress
            and any(
                str((getattr(task, "metadata", {}) or {}).get("company_runtime_stop_state", "") or "").strip()
                in {"suspending", "resuming_after_suspending"}
                for task in group
            )
        ):
            state = "suspending"
        elif has_running_task:
            state = "running"
        else:
            state = "idle"

        checkpoint_payload = dict(getattr(checkpoint, "payload", {}) or {}) if checkpoint is not None else {}
        pending_checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "").strip() if checkpoint is not None else ""
        for task in group:
            task_id = str(getattr(task, "id", "") or "").strip()
            if not task_id:
                continue
            result[task_id] = {
                "runtime_control_state": state,
                "can_stop": state == "running",
                "can_resume": state == "suspended",
                "resume_parent_session_id": identity.runtime_session_id,
                "pending_runtime_checkpoint_id": pending_checkpoint_id,
                "stop_intent_id": str(checkpoint_payload.get("stop_intent_id", "") or ""),
            }
    return result


async def reconcile_sessions(
    engine: "OPCEngine",
    chat_store: "ChatStore",
    tasks: list[Any],
    project_id: str = "default",
    *,
    max_hydrate: int = 10,
) -> int:
    """Backfill CLI-generated session history into ChatStore.

    For each task that has a session_id, this reads the authoritative
    transcript from engine.store (tasks.db) and inserts any messages
    that are missing from the ChatStore rendering cache (ui_state.db).

    ``max_hydrate`` caps the number of empty sessions that get their full
    transcript loaded in a single call.  This keeps collab_sync fast for
    large projects — remaining sessions are hydrated lazily when opened.

    Returns total number of messages backfilled.
    """
    if not engine.store:
        return 0

    tasks = [task for task in tasks if not is_runtime_auxiliary_task(task)]
    total_backfilled = 0
    hydrated = 0
    task_ids = {
        str(getattr(task, "id", "") or "").strip()
        for task in tasks
        if str(getattr(task, "id", "") or "").strip()
    }
    session_channel_stats = await chat_store.get_channel_stats(
        [
            f"session:{getattr(task, 'id', '')}"
            for task in tasks
            if str(getattr(task, "id", "") or "").strip()
        ],
        project_id=project_id,
    )

    for task in tasks:
        session_id = getattr(task, "session_id", None)
        if not session_id:
            continue
        task_meta = _task_metadata(task)
        canonical_task_id = _task_mode_origin_ui_task_id(task, task_meta)
        if canonical_task_id and canonical_task_id in task_ids:
            continue

        channel_id = f"session:{task.id}"

        # Ensure the UI session channel exists for every persisted task session.
        # This repairs cases where messages/progress were written before the
        # channel row was created, which would otherwise make the task look
        # orphaned and disappear from kanban.
        await chat_store.create_session_channel(
            task_id=task.id,
            title=task.title,
            project_id=project_id,
        )

        # Defer expensive transcript hydration until the session is actually opened.
        # This keeps collab_sync lightweight for large projects while still repairing
        # empty/missing channels created outside the UI.
        existing_count = int(session_channel_stats.get(channel_id, {}).get("message_count", 0) or 0)
        if existing_count > 0:
            continue

        # Cap expensive transcript loads to keep collab_sync fast for large projects.
        if hydrated >= max_hydrate:
            continue
        hydrated += 1

        # Get authoritative transcript from engine store
        try:
            transcript = await engine.store.get_session_transcript(session_id)
        except Exception:
            continue

        ui_messages = build_transcript_ui_messages(
            transcript,
            channel_id=channel_id,
            task_id=str(getattr(task, "id", "") or ""),
            detail_level="summary",
        )

        if len(ui_messages) <= existing_count:
            # ChatStore already has at least as many visible messages — skip
            continue

        inserted_messages = await chat_store.backfill_messages(channel_id, ui_messages, project_id)
        if inserted_messages:
            logger.info(f"Reconciled {len(inserted_messages)} messages for session:{task.id}")
            total_backfilled += len(inserted_messages)

    return total_backfilled


# ── Snapshot builder ───────────────────────────────────────────────────────

async def build_snapshot(
    engine: OPCEngine,
    agent_store: AgentStore,
    chat_store: ChatStore,
    event_adapter: EventAdapter,
) -> dict[str, Any]:
    """Build VisualSnapshot matching frontend types/visual.ts."""
    project_id = engine.project_id or "default"
    agents = await agent_store.get_all()
    hidden_role_ids: set[str] = set()
    try:
        from opc.layer2_organization.external_team_compiler import (
            opaque_external_team_hidden_role_ids,
        )

        hidden_role_ids = opaque_external_team_hidden_role_ids(
            getattr(engine, "org_engine", None)
        )
    except Exception:
        logger.opt(exception=True).warning(
            "Failed to project opaque external Team visibility for snapshot"
        )
    snapshot_agents = {
        str(agent.get("agent_id", "") or ""): _agent_with_runtime(agent, event_adapter)
        for agent in agents
        if str(agent.get("opc_role_id") or agent.get("agent_id") or "").strip()
        not in hidden_role_ids
    }

    # Skills
    skills_data: list[Any] = []
    if engine.skills:
        try:
            all_skills = engine.skills.list_skills()
            if all_skills:
                skills_data = all_skills
        except Exception:
            logger.debug("Failed to load skills for snapshot")

    # Timeline from event history
    timeline: list[dict[str, Any]] = []
    try:
        history = engine.event_bus.get_history(limit=50)
        for evt in history:
            visual_events = event_adapter.translate(evt)
            timeline.extend(visual_events)
    except Exception:
        logger.debug("Failed to build timeline for snapshot")

    # Agent templates
    templates = await agent_store.get_templates(engine.org_engine)
    if hidden_role_ids:
        templates = [
            template
            for template in templates
            if str(template.get("id", "") or "").strip() not in hidden_role_ids
        ]

    return {
        "project_id": project_id,
        "native_approval_default": str(
            getattr(
                getattr(getattr(engine, "config", None), "autonomy", None),
                "native_approval_level",
                "auto",
            )
            or "auto"
        ),
        "agents": snapshot_agents,
        "channels": {},  # Channels loaded via collab_sync, not snapshot
        "skills": {
            "recent": [
                {
                    "skill_name": s.name if hasattr(s, "name") else (s.get("name", "") if isinstance(s, dict) else str(s)),
                    "version": 1,
                    "timestamp": 0,
                }
                for s in (skills_data or [])[-5:]
            ],
            "total": len(skills_data or []),
        },
        "practice": {"count": 0, "last": None},
        "milestones": [],
        "timeline": timeline,
        # Extra field (accessed via snapshot?.agent_templates in App.tsx)
        "agent_templates": templates,
    }


# ── project index / collab_sync builders ───────────────────────────────────

async def build_project_index_sync(
    engine: OPCEngine,
    agent_store: AgentStore,
    chat_store: ChatStore,
    event_adapter: EventAdapter | None = None,
    *,
    exec_mode: str | None = None,
) -> dict[str, Any]:
    """Build a lightweight project index for fast project switching.

    The index deliberately avoids project-wide message/progress hydration and
    the full company work-item projection. Complete context stays available
    through ``session_detail``.
    """
    project_id = engine.project_id or "default"

    tasks: list[Any] = []
    if engine.store:
        try:
            tasks = await engine.store.get_tasks(project_id=project_id)
        except Exception:
            logger.opt(exception=True).warning("Failed to load tasks for project index")

    auxiliary_task_ids = {
        str(getattr(task, "id", "") or "").strip()
        for task in tasks
        if is_runtime_auxiliary_task(task)
        and str(getattr(task, "id", "") or "").strip()
    }
    tasks = [task for task in tasks if not is_runtime_auxiliary_task(task)]

    existing_task_ids = {
        str(getattr(task, "id", "") or "").strip()
        for task in tasks
        if str(getattr(task, "id", "") or "").strip()
    }

    get_channels = getattr(chat_store, "get_channels", None)
    if callable(get_channels):
        raw_channels = get_channels(project_id)
        resolved_channels = await raw_channels if inspect.isawaitable(raw_channels) else raw_channels
        channels = list(resolved_channels) if isinstance(resolved_channels, list) else []
    else:
        channels = []
    get_session_channels = getattr(chat_store, "get_session_channels", None)
    if callable(get_session_channels):
        raw_session_channels = get_session_channels(project_id)
        resolved_session_channels = (
            await raw_session_channels
            if inspect.isawaitable(raw_session_channels)
            else raw_session_channels
        )
        session_channels = list(resolved_session_channels) if isinstance(resolved_session_channels, list) else []
    else:
        session_channels = []
    hidden_auxiliary_channel_ids = {
        f"session:{task_id}" for task_id in auxiliary_task_ids
    }
    if hidden_auxiliary_channel_ids:
        channels = [
            channel
            for channel in channels
            if str(channel.get("channel_id", "") or "").strip()
            not in hidden_auxiliary_channel_ids
        ]
        session_channels = [
            channel
            for channel in session_channels
            if str(channel.get("channel_id", "") or "").strip()
            not in hidden_auxiliary_channel_ids
        ]
        delete_channel = getattr(chat_store, "delete_channel", None)
        if callable(delete_channel):
            for channel_id in hidden_auxiliary_channel_ids:
                try:
                    deleted = delete_channel(channel_id, project_id=project_id)
                    if inspect.isawaitable(deleted):
                        await deleted
                except Exception:
                    logger.opt(exception=True).debug(
                        "Failed to prune auxiliary runtime channel {}",
                        channel_id,
                    )
    session_channel_map = {ch["channel_id"]: ch for ch in session_channels}
    known_channel_ids = {
        str(ch.get("channel_id", "") or "").strip()
        for ch in channels
        if str(ch.get("channel_id", "") or "").strip()
    }

    for task in tasks:
        task_id = str(getattr(task, "id", "") or "").strip()
        session_id = str(getattr(task, "session_id", "") or "").strip()
        if not task_id or not session_id:
            continue
        canonical_task_id = _task_mode_origin_ui_task_id(task, _task_metadata(task))
        if canonical_task_id and canonical_task_id in existing_task_ids:
            continue
        channel_id = f"session:{task_id}"
        if channel_id in session_channel_map:
            continue
        title = str(getattr(task, "title", "") or "Session").strip() or "Session"
        try:
            create_session_channel = getattr(chat_store, "create_session_channel", None)
            if not callable(create_session_channel):
                raise AttributeError("create_session_channel unavailable")
            repaired_channel = await create_session_channel(task_id, title, project_id=project_id)
        except Exception:
            created_at = getattr(task, "created_at", None)
            repaired_channel = {
                "channel_id": channel_id,
                "type": "session",
                "name": title,
                "office_id": None,
                "participants": ["user"],
                "created_at": (
                    created_at.timestamp()
                    if hasattr(created_at, "timestamp")
                    else time.time()
                ),
            }
        session_channel_map[channel_id] = repaired_channel
        if channel_id not in known_channel_ids:
            channels.append(repaired_channel)
            known_channel_ids.add(channel_id)

    channel_ids = list(session_channel_map.keys())
    session_channel_stats: dict[str, Any] = {}
    index_stats_getter = getattr(chat_store, "get_channel_index_stats", None)
    stats_getter = getattr(chat_store, "get_channel_stats", None)
    if callable(index_stats_getter):
        raw_stats = index_stats_getter(channel_ids, project_id=project_id)
        resolved_stats = await raw_stats if inspect.isawaitable(raw_stats) else raw_stats
        session_channel_stats = resolved_stats if isinstance(resolved_stats, dict) else {}
    elif callable(stats_getter):
        raw_stats = stats_getter(channel_ids, project_id=project_id)
        resolved_stats = await raw_stats if inspect.isawaitable(raw_stats) else raw_stats
        session_channel_stats = resolved_stats if isinstance(resolved_stats, dict) else {}

    formatted_channels = [
        {
            "channel_id": ch["channel_id"],
            "channel_type": ch["type"],
            "name": ch["name"],
            "office_id": ch.get("office_id"),
            "participants": ch["participants"],
            "created_at": ch["created_at"],
        }
        for ch in channels
    ]

    now = time.time()
    all_task_meta_map = {str(getattr(t, "id", "") or ""): _task_metadata(t) for t in tasks}
    session_tasks = [
        t for t in tasks
        if not is_runtime_auxiliary_task(t)
        and not bool(all_task_meta_map.get(str(getattr(t, "id", "") or ""), {}).get("review_task", False))
        and not (
            (canonical_id := _task_mode_origin_ui_task_id(
                t,
                all_task_meta_map.get(str(getattr(t, "id", "") or ""), {}),
            ))
            and canonical_id in existing_task_ids
        )
    ]
    active_tasks = [
        t for t in session_tasks
        if (t.status.value if hasattr(t.status, "value") else str(t.status)) != "cancelled"
    ]
    task_meta_map = {str(getattr(t, "id", "") or ""): _task_metadata(t) for t in session_tasks}
    primary_tasks_by_session_id, ordered_session_ids = _primary_session_tasks_by_session_id(
        session_tasks,
        task_meta_map=task_meta_map,
    )
    company_config_tasks_by_session_id = _company_config_source_tasks_by_session_id(
        session_tasks,
    )
    child_tasks_by_parent: dict[str, list[Any]] = {}
    for task in session_tasks:
        task_meta = task_meta_map.get(str(getattr(task, "id", "") or ""), {})
        parent_link = _task_parent_session_link(task, task_meta)
        if parent_link:
            child_tasks_by_parent.setdefault(parent_link, []).append(task)

    normalized_exec_mode = _normalize_session_exec_mode(exec_mode)
    has_company_sessions = any(
        _resolve_task_session_config(task)[0] in {"company", "org"}
        for task in session_tasks
    )
    if normalized_exec_mode in {"company", "org"} or has_company_sessions:
        formatted_boards: list[dict[str, Any]] = []
        formatted_columns: list[dict[str, Any]] = []
        for session_id in ordered_session_ids:
            primary_task = primary_tasks_by_session_id[session_id]
            board_id = str(getattr(primary_task, "id", "") or "").strip() or f"session:{session_id}"
            board_name = _board_label(
                str(getattr(primary_task, "title", "") or "").strip(),
                fallback=f"Session {session_id[:8]}",
            )
            formatted_boards.append(_build_board_payload(
                board_id,
                board_name,
                description=f"Session board for {session_id[:8]}",
                now=now,
            ))
            formatted_columns.extend(build_company_board_columns(board_id, now=now))
        formatted_tasks: list[dict[str, Any]] = []
    else:
        formatted_boards = [
            _build_board_payload(
                project_id,
                project_id if project_id != "default" else "Main Board",
                now=now,
            )
        ]
        formatted_columns = build_board_columns(project_id, now=now)
        formatted_tasks = [
            task_to_kanban(t, i + 1, event_adapter)
            for i, t in enumerate(active_tasks)
        ]

    default_context_window = None
    llm_provider = getattr(engine, "llm", None)
    if llm_provider is not None:
        try:
            default_context_window = llm_provider.get_context_window()
        except Exception:
            default_context_window = None
    runtime_control_by_task = await _build_company_runtime_control_by_task(
        engine,
        session_tasks,
        project_id,
    )

    formatted_sessions: list[dict[str, Any]] = []
    for t in session_tasks:
        task_id = str(getattr(t, "id", "") or "").strip()
        channel_id = f"session:{task_id}"
        ch = session_channel_map.get(channel_id)
        if not task_id or not ch:
            continue
        t_meta = task_meta_map.get(task_id, _task_metadata(t))
        session_id = str(getattr(t, "session_id", "") or "").strip()
        representative_task = primary_tasks_by_session_id.get(session_id)
        representative_task_id = str(getattr(representative_task, "id", "") or "").strip()
        shared_session_id = _shared_role_session_key(t, t_meta)
        if shared_session_id:
            if not representative_task_id or representative_task_id != task_id:
                continue
        identity_task = t
        identity_meta = t_meta
        shared_identity_task = company_config_tasks_by_session_id.get(session_id)
        shared_identity_task_id = str(getattr(shared_identity_task, "id", "") or "").strip()
        if shared_identity_task_id and shared_identity_task_id != task_id:
            identity_task = shared_identity_task
            identity_meta = task_meta_map.get(shared_identity_task_id, _task_metadata(shared_identity_task))

        status_val = t.status.value if hasattr(t.status, "value") else str(t.status)
        channel_stats = session_channel_stats.get(channel_id, {})
        msg_count = int(channel_stats.get("message_count", 0) or 0)
        created_ts = t.created_at.timestamp() if hasattr(t.created_at, "timestamp") else time.time()
        updated_ts = created_ts
        latest_ts = channel_stats.get("latest_timestamp")
        if latest_ts:
            updated_ts = float(latest_ts)

        parent_sid = _task_parent_session_link(t, t_meta) or None
        session_mode = "child" if parent_sid else "primary"
        exec_mode_val, company_profile_val = _resolve_task_session_config(t)
        preferred_agent_val = _resolve_task_preferred_agent(t)
        selected_execution_agent_val = _resolve_task_selected_execution_agent(identity_task)

        child_keys = {task_id}
        if session_id:
            child_keys.add(session_id)
        child_tasks: list[Any] = []
        seen_child_ids: set[str] = set()
        for key in child_keys:
            for child in child_tasks_by_parent.get(key, []):
                child_id = str(getattr(child, "id", "") or "")
                if not child_id or child_id == task_id or child_id in seen_child_ids:
                    continue
                seen_child_ids.add(child_id)
                child_tasks.append(child)

        identity_task_id = str(getattr(identity_task, "id", "") or "").strip()
        company_profile = t_meta.get("company_profile") or identity_meta.get("company_profile")
        org_id = (
            t_meta.get("org_id")
            or t_meta.get("organization_id")
            or identity_meta.get("org_id")
            or identity_meta.get("organization_id")
        )
        if not company_profile and session_mode == "primary":
            for child in child_tasks:
                child_meta = task_meta_map.get(str(getattr(child, "id", "") or ""), {})
                inherited_profile = child_meta.get("company_profile")
                if inherited_profile:
                    company_profile = inherited_profile
                    break
        is_task_mode_runtime = _metadata_is_task_mode_runtime({**t_meta, **identity_meta})

        runtime_meta = {
            **dict(t_meta.get("runtime_v2", {}) or {}),
            **dict(identity_meta.get("runtime_v2", {}) or {}),
        }
        member_session_meta = {
            **dict(t_meta.get("member_session_state", {}) or {}),
            **dict(identity_meta.get("member_session_state", {}) or {}),
        }
        employee_assignment = {
            **dict(t_meta.get("employee_assignment", {}) or {}),
            **dict(identity_meta.get("employee_assignment", {}) or {}),
        }
        identity_payload = {} if is_task_mode_runtime else work_item_identity_payload_from_metadata(identity_meta)
        fallback_identity_payload = {} if is_task_mode_runtime else work_item_identity_payload_from_metadata(t_meta)
        if not is_task_mode_runtime:
            if WORK_ITEM_PROJECTION_ID_KEY not in identity_payload:
                projection_value = fallback_identity_payload.get(WORK_ITEM_PROJECTION_ID_KEY)
                if projection_value:
                    identity_payload[WORK_ITEM_PROJECTION_ID_KEY] = projection_value
            if WORK_ITEM_TURN_TYPE_KEY not in identity_payload:
                turn_value = fallback_identity_payload.get(WORK_ITEM_TURN_TYPE_KEY)
                if turn_value:
                    identity_payload[WORK_ITEM_TURN_TYPE_KEY] = turn_value

        tracker_data: dict[str, Any] = {}
        if event_adapter and getattr(identity_task, "assigned_to", ""):
            try:
                agent_id = event_adapter._resolve_role_to_agent(identity_task.assigned_to)
                get_tracker = getattr(event_adapter, "get_tracker", None)
                tracker = get_tracker(agent_id) if callable(get_tracker) else None
                if tracker:
                    tracker_data = {
                        "agent_status": tracker.state.value,
                        "current_tool": tracker.current_tool,
                    }
            except Exception:
                tracker_data = {}

        exec_mode_val, resolved_company_profile, org_id = _canonical_session_identity(
            exec_mode_val,
            company_profile or company_profile_val,
            org_id,
        )
        formatted_sessions.append({
            "project_id": project_id,
            "task_id": task_id,
            **_execution_turn_alias_payload(task_id),
            "session_id": getattr(t, "session_id", None),
            "parent_session_id": parent_sid,
            "mode": session_mode,
            "exec_mode": exec_mode_val,
            "company_profile": resolved_company_profile,
            "org_id": org_id,
            "preferred_agent": preferred_agent_val,
            "selected_execution_agent": selected_execution_agent_val,
            "native_approval_level": identity_meta.get("native_approval_level") or t_meta.get("native_approval_level") or engine.config.autonomy.native_approval_level,
            "native_permission_scope_id": identity_meta.get("native_permission_scope_id") or t_meta.get("native_permission_scope_id") or getattr(t, "session_id", None),
            "execution_unit_kind": identity_meta.get("execution_unit_kind") or t_meta.get("execution_unit_kind"),
            "external_team_binding_id": dict(identity_meta.get("external_team_binding", {}) or t_meta.get("external_team_binding", {}) or {}).get("binding_id"),
            "external_team_boundary_role_id": dict(identity_meta.get("external_team_binding", {}) or t_meta.get("external_team_binding", {}) or {}).get("boundary_role_id"),
            "covered_role_ids": list(identity_meta.get("covered_role_ids", []) or t_meta.get("covered_role_ids", []) or []),
            "external_company_execution_fence": identity_meta.get("external_company_execution_fence") or t_meta.get("external_company_execution_fence"),
            "channel_id": channel_id,
            "title": getattr(t, "title", "") or "Session",
            "status": status_val,
            "column_id": STATUS_TO_COLUMN.get(status_val, "todo"),
            "assignee_ids": [event_adapter._resolve_role_to_agent(identity_task.assigned_to)] if event_adapter and getattr(identity_task, "assigned_to", "") else ([identity_task.assigned_to] if getattr(identity_task, "assigned_to", "") else []),
            "priority": priority_to_label(t.priority) if isinstance(t.priority, int) else None,
            "tags": t.tags or [],
            "created_at": created_ts,
            "updated_at": updated_ts,
            "message_count": msg_count,
            "latest_preview": channel_stats.get("latest_preview") or "",
            "latest_sender": channel_stats.get("latest_sender") or "",
            "latest_message_id": channel_stats.get("latest_message_id") or "",
            **identity_payload,
            **({} if is_task_mode_runtime else _work_item_role_payload(identity_task)),
            "work_item_gate": None if is_task_mode_runtime else (identity_meta.get("work_item_gate") or t_meta.get("work_item_gate")),
            "employee_assignment": employee_assignment or None,
            "origin_channel": identity_meta.get("origin_channel") or t_meta.get("origin_channel"),
            "origin_task_id": t_meta.get("origin_task_id") or task_id,
            "is_company_runtime": bool(
                not is_task_mode_runtime
                and session_mode == "primary"
                and (
                    company_profile
                    or child_tasks
                    or (identity_task_id and identity_task_id != task_id)
                )
            ),
            **runtime_control_by_task.get(task_id, {}),
            "artifacts": identity_meta.get("artifacts") or t_meta.get("artifacts"),
            "runtime_session_id": runtime_meta.get("runtime_session_id"),
            "resume_cursor": runtime_meta.get("resume_cursor"),
            "active_subagents": list(runtime_meta.get("active_subagents", []) or []),
            "permission_requests": list(runtime_meta.get("permission_requests", []) or []),
            "worktree_path": runtime_meta.get("worktree_path"),
            "context_tokens": runtime_meta.get("context_tokens"),
            "context_window": runtime_meta.get("context_window") or default_context_window,
            "context_remaining_pct": runtime_meta.get("context_remaining_pct"),
            "pending_permission_count": runtime_meta.get("pending_permission_count"),
            "drain_mode": runtime_meta.get("drain_mode"),
            "resident_status": _runtime_status_from_member_meta(member_session_meta),
            "actionable_inbox_count": member_session_meta.get("actionable_inbox_count"),
            "protocol_backlog_count": member_session_meta.get("protocol_backlog_count"),
            "notification_backlog_count": member_session_meta.get("notification_backlog_count"),
            "latest_notification": member_session_meta.get("latest_notification"),
            "index_loaded": True,
            **tracker_data,
        })

    return {
        "ok": True,
        "project_id": project_id,
        "sync_scope": "index",
        "channels": formatted_channels,
        "messages": [],
        "boards": formatted_boards,
        "columns": formatted_columns,
        "tasks": formatted_tasks,
        "sessions": formatted_sessions,
    }

async def build_collab_sync(
    engine: OPCEngine,
    agent_store: AgentStore,
    chat_store: ChatStore,
    event_adapter: EventAdapter | None = None,
    *,
    exec_mode: str | None = None,
) -> dict[str, Any]:
    """Build full collab_sync response.

    Frontend collabSync.ts maps all snake_case → camelCase automatically.
    """
    project_id = engine.project_id or "default"

    # Get OPC tasks
    tasks: list[Any] = []
    if engine.store:
        try:
            tasks = await engine.store.get_tasks(project_id=project_id)
        except Exception:
            logger.warning("Failed to load tasks for collab_sync")
    tasks = [task for task in tasks if not is_runtime_auxiliary_task(task)]
    # Display-only cache for opaque external teams.  It belongs to this
    # collab-sync projection (the sole consumer), not transcript reconciliation.
    # Failure only removes auxiliary telemetry from the snapshot.
    external_team_summary_by_task: dict[str, dict[str, Any]] = {}
    has_jiuwen_team_task = any(
        _resolve_task_selected_execution_agent(task) == "jiuwenswarm"
        and str((getattr(task, "metadata", {}) or {}).get("execution_unit_kind") or "").strip()
        == "opaque_external_team"
        for task in tasks
    )
    if has_jiuwen_team_task and engine.store and hasattr(engine.store, "list_external_sessions"):
        try:
            external_sessions = await engine.store.list_external_sessions(
                project_id=project_id,
                limit=max(100, len(tasks) * 4),
            )
            for external_session in external_sessions:
                if str(getattr(external_session, "agent_type", "") or "").strip() != "jiuwenswarm":
                    continue
                task_id = str(getattr(external_session, "task_id", "") or "").strip()
                metadata = dict(getattr(external_session, "metadata", {}) or {})
                if str(metadata.get("execution_unit_kind") or "").strip() != "opaque_external_team":
                    continue
                summary = metadata.get("external_team_summary")
                if task_id and task_id not in external_team_summary_by_task and isinstance(summary, dict):
                    external_team_summary_by_task[task_id] = dict(summary)
        except Exception:
            logger.opt(exception=True).debug("Failed to load external team summaries")
    company_board_tasks: list[dict[str, Any]] = []
    company_board_columns: list[dict[str, Any]] = []
    company_boards: list[dict[str, Any]] = []
    company_projection_meta: dict[str, Any] | None = None
    if engine.store:
        try:
            company_board_tasks, company_board_columns, company_boards, company_projection_meta = await build_company_kanban_projection(
                engine,
                project_id=project_id,
                tasks=tasks,
                event_adapter=event_adapter,
            )
        except Exception:
            logger.opt(exception=True).warning("Failed to build company-mode kanban projection")
    # Per-session DelegationWorkItem rollups produced alongside the company
    # kanban projection. Used by the work-item-driven Execution Progress
    # panel — keyed by ``task.session_id`` (matches the formatted_sessions
    # loop's ``session_id`` lookup).
    work_items_by_session_id: dict[str, list[Any]] = (
        (company_projection_meta or {}).get("work_items_by_session_id", {}) or {}
    )
    company_task_by_work_item_id: dict[str, Any] = (
        (company_projection_meta or {}).get("task_by_work_item_id", {}) or {}
    )
    # In company/custom mode the projection already creates one board per
    # primary session. If the projection returned nothing (no primary sessions
    # exist yet) we intentionally leave company_boards empty so the frontend
    # shows an empty-state prompt instead of a misleading project-wide board.

    # Reconcile CLI-generated session history into ChatStore before reading
    try:
        backfilled = await reconcile_sessions(engine, chat_store, tasks, project_id)
        if backfilled:
            logger.info(f"Reconciled {backfilled} total messages from engine → ChatStore")
    except Exception:
        logger.opt(exception=True).warning("Session reconciliation failed (non-fatal)")

    existing_task_ids = {
        str(getattr(task, "id", "") or "").strip()
        for task in tasks
        if str(getattr(task, "id", "") or "").strip()
    }

    # Get chat data (project-scoped)
    channels = await chat_store.get_channels(project_id)
    messages = await chat_store.get_messages(project_id)

    # Only check for pending checkpoints on tasks that are actively waiting
    # for user input (idle/running). Skipping done/failed/cancelled tasks
    # avoids O(n) engine calls that block collab_sync on large projects.
    _checkpoint_eligible_statuses = {
        "idle",
        "running",
        "blocked",
        "awaiting_manager_review",
        "awaiting_human",
        "awaiting_review",
    }
    for task in tasks:
        t_status = task.status.value if hasattr(task.status, "value") else str(task.status)
        if t_status not in _checkpoint_eligible_statuses:
            continue
        checkpoint_meta = await _build_snapshot_checkpoint_meta(engine, task)
        if not checkpoint_meta:
            continue
        if not _checkpoint_meta_targets_task(checkpoint_meta, task):
            continue
        channel_id = f"session:{getattr(task, 'id', '')}"
        _attach_or_create_checkpoint_card(
            messages,
            channel_id=channel_id,
            checkpoint_meta=checkpoint_meta,
        )

    # Prune orphan/stale session channels and their messages.
    # A session channel is stale only when its task_id no longer exists in the DB.
    stale_session_channels: set[str] = set()
    for ch in channels:
        cid = ch["channel_id"]
        if not cid.startswith("session:"):
            continue
        tid = cid[len("session:"):]
        if tid not in existing_task_ids:
            stale_session_channels.add(cid)

    if stale_session_channels:
        channels = [ch for ch in channels if ch["channel_id"] not in stale_session_channels]
        messages = [msg for msg in messages if msg["channel_id"] not in stale_session_channels]
        # Persist: delete orphan channels and their messages from ui_state.db
        for stale_ch in stale_session_channels:
            try:
                await chat_store.delete_channel(stale_ch, project_id=project_id)
            except Exception:
                pass

    # Also prune activity messages whose metadata.task_id points to a dead task
    activity_channel = f"activity:{project_id}"
    dead_task_ids = {
        tid for ch_id in stale_session_channels
        if (tid := ch_id[len("session:"):])
    }
    if dead_task_ids:
        messages = [
            msg for msg in messages
            if not (
                msg["channel_id"] == activity_channel
                and isinstance(msg.get("metadata"), dict)
                and msg["metadata"].get("task_id") in dead_task_ids
            )
        ]
        # Persist: delete matching activity messages + progress from ui_state.db
        for tid in dead_task_ids:
            try:
                await chat_store.delete_activity_messages_for_task(project_id, tid)
            except Exception:
                pass
            try:
                await chat_store.delete_progress(tid, project_id=project_id)
            except Exception:
                pass

    # Format channels for collabSync.ts mapBackendChannel()
    formatted_channels = [
        {
            "channel_id": ch["channel_id"],
            "channel_type": ch["type"],
            "name": ch["name"],
            "office_id": ch.get("office_id"),
            "participants": ch["participants"],
            "created_at": ch["created_at"],
        }
        for ch in channels
    ]

    # Format messages for collabSync.ts mapBackendMessage()
    formatted_messages = []
    for msg in messages:
        sanitized = _sanitize_ui_message_dict(msg)
        formatted_messages.append({
            "message_id": sanitized["message_id"],
            "channel_id": sanitized["channel_id"],
            "sender": sanitized["sender"],
            "sender_name": sanitized["sender_name"],
            "content": sanitized["content"],
            "created_at": sanitized["created_at"],
            "reply_to_id": sanitized.get("reply_to_id"),
            "mentions": sanitized.get("mentions", []),
            "metadata": sanitized.get("metadata", {}),
        })

    # Board (one per project in task mode; one per session in company/org
    # mode). In company/org mode with no primary sessions
    # yet, leave the boards/columns empty so the frontend shows an
    # empty-state prompt instead of a stale project-wide board that would
    # fight the session-driven selection logic and cause a render loop.
    now = time.time()
    normalized_exec_mode = _normalize_session_exec_mode(exec_mode)
    has_company_sessions = any(
        _resolve_task_session_config(task)[0] in {"company", "org"}
        for task in tasks
    )
    using_company_board = (
        normalized_exec_mode in {"company", "org"}
        or has_company_sessions
    )
    if using_company_board:
        formatted_boards = list(company_boards)
        formatted_columns = list(company_board_columns)
        if company_boards:
            task_counts_by_board: dict[str, int] = {}
            for task in company_board_tasks:
                board_id = str(task.get("board_id", "") or "").strip()
                if not board_id:
                    continue
                task_counts_by_board[board_id] = task_counts_by_board.get(board_id, 0) + 1
            for board in formatted_boards:
                board_id = str(board.get("board_id", "") or "").strip()
                board["next_task_num"] = int(task_counts_by_board.get(board_id, 0) or 0) + 1
    else:
        formatted_boards = [
            _build_board_payload(
                project_id,
                project_id if project_id != "default" else "Main Board",
                now=now,
            )
        ]
        formatted_columns = build_board_columns(project_id, now=now)

    # Sessions — merged task+channel data for the sidebar
    session_channels = await chat_store.get_session_channels(project_id)
    session_channel_map = {ch["channel_id"]: ch for ch in session_channels}
    known_channel_ids = {
        str(ch.get("channel_id", "") or "").strip()
        for ch in channels
        if str(ch.get("channel_id", "") or "").strip()
    }
    persisted_task_ids = {
        str(getattr(task, "id", "") or "").strip()
        for task in tasks
        if str(getattr(task, "id", "") or "").strip()
    }
    repaired_session_channel_ids: list[str] = []
    for task in tasks:
        task_id = str(getattr(task, "id", "") or "").strip()
        session_id = str(getattr(task, "session_id", "") or "").strip()
        if not task_id or not session_id:
            continue
        canonical_task_id = _task_mode_origin_ui_task_id(task, _task_metadata(task))
        if canonical_task_id and canonical_task_id in persisted_task_ids:
            continue
        channel_id = f"session:{task_id}"
        if channel_id in session_channel_map:
            continue
        title = str(getattr(task, "title", "") or "Session").strip() or "Session"
        try:
            repaired_channel = await chat_store.create_session_channel(
                task_id,
                title,
                project_id=project_id,
            )
        except Exception:
            created_at = getattr(task, "created_at", None)
            repaired_channel = {
                "channel_id": channel_id,
                "type": "session",
                "name": title,
                "office_id": None,
                "participants": ["user"],
                "created_at": (
                    created_at.timestamp()
                    if hasattr(created_at, "timestamp")
                    else time.time()
                ),
            }
        session_channel_map[channel_id] = repaired_channel
        repaired_session_channel_ids.append(channel_id)
        if channel_id not in known_channel_ids:
            channels.append(repaired_channel)
            known_channel_ids.add(channel_id)

    session_channel_stats = await chat_store.get_channel_stats(
        list(session_channel_map.keys()),
        project_id=project_id,
    )
    if repaired_session_channel_ids:
        logger.info(
            "Recovered %d missing session channel(s) for project %s",
            len(repaired_session_channel_ids),
            project_id,
        )
    progress_by_task = await chat_store.get_progress_many(
        [
            str(getattr(task, "id", "") or "").strip()
            for task in tasks
            if str(getattr(task, "id", "") or "").strip()
        ],
        project_id=project_id,
    )

    # Tasks — keep cancelled rows in session state, but exclude them from
    # task-mode kanban cards. Missing session channels are repaired above so
    # valid work-item projections remain visible in historical and live UI.
    # Kanban-push review Tasks are runtime-internal scheduling units (they
    # exist only to drive manager review turns) and must never surface on
    # the kanban board or the sessions sidebar — the user already sees the
    # child work item's card move into the "In Review" column.
    all_task_meta_map = {
        str(getattr(t, "id", "") or ""): _task_metadata(t)
        for t in tasks
    }
    session_tasks = [
        t for t in tasks
        if not is_runtime_auxiliary_task(t)
        and not bool(all_task_meta_map.get(str(getattr(t, "id", "") or ""), {}).get("review_task", False))
        and not (
            (canonical_id := _task_mode_origin_ui_task_id(
                t,
                all_task_meta_map.get(str(getattr(t, "id", "") or ""), {}),
            ))
            and canonical_id in persisted_task_ids
        )
    ]
    active_tasks = [
        t for t in session_tasks
        if (t.status.value if hasattr(t.status, "value") else str(t.status)) != "cancelled"
    ]
    task_meta_map = {t.id: _task_metadata(t) for t in session_tasks}
    primary_tasks_by_session_id, _ = _primary_session_tasks_by_session_id(
        session_tasks,
        task_meta_map=task_meta_map,
    )
    company_config_tasks_by_session_id = _company_config_source_tasks_by_session_id(
        session_tasks,
    )
    child_tasks_by_parent: dict[str, list[Any]] = {}
    for task in session_tasks:
        task_meta = task_meta_map.get(task.id, {})
        parent_link = _task_parent_session_link(task, task_meta)
        if parent_link:
            child_tasks_by_parent.setdefault(parent_link, []).append(task)

    formatted_tasks = (
        company_board_tasks
        if using_company_board
        else [task_to_kanban(t, i + 1, event_adapter) for i, t in enumerate(active_tasks)]
    )
    formatted_sessions = []
    default_context_window = None
    llm_provider = getattr(engine, "llm", None)
    if llm_provider is not None:
        try:
            default_context_window = llm_provider.get_context_window()
        except Exception:
            default_context_window = None
    runtime_control_by_task = await _build_company_runtime_control_by_task(
        engine,
        session_tasks,
        project_id,
    )
    # Walk `session_tasks` (review_task already filtered out above) instead
    # of the raw `tasks` list, so stopped/cancelled child sessions remain
    # visible after refresh while review tasks stay hidden. Review tasks are runtime
    # scheduling artefacts for manager review turns — a single executed
    # work item can spawn N of them, each with its own Task row and session
    # channel. Showing them as separate "turns" in the execution panel
    # makes every role look like it did 3-5x the work it actually did.
    for t in session_tasks:
        channel_id = f"session:{t.id}"
        ch = session_channel_map.get(channel_id)
        # Only include tasks that have an actual session channel (created via chat)
        if not ch:
            continue
        t_meta = task_meta_map.get(t.id, _task_metadata(t))
        session_id = str(getattr(t, "session_id", "") or "").strip()
        representative_task = primary_tasks_by_session_id.get(session_id)
        representative_task_id = str(getattr(representative_task, "id", "") or "").strip()
        shared_session_id = _shared_role_session_key(t, t_meta)
        if shared_session_id:
            if (
                not representative_task_id
                or representative_task_id != str(getattr(t, "id", "") or "").strip()
            ):
                continue
        identity_task = t
        identity_meta = t_meta
        shared_identity_task = company_config_tasks_by_session_id.get(session_id)
        shared_identity_task_id = str(getattr(shared_identity_task, "id", "") or "").strip()
        if shared_identity_task_id and shared_identity_task_id != str(getattr(t, "id", "") or "").strip():
            identity_task = shared_identity_task
            identity_meta = task_meta_map.get(shared_identity_task_id, _task_metadata(shared_identity_task))
        status_val = t.status.value if hasattr(t.status, "value") else str(t.status)
        channel_stats = session_channel_stats.get(channel_id, {})
        msg_count = int(channel_stats.get("message_count", 0) or 0)
        tracker_data: dict[str, Any] = {}
        if event_adapter and getattr(identity_task, "assigned_to", ""):
            agent_id = event_adapter._resolve_role_to_agent(identity_task.assigned_to)
            tracker = event_adapter.get_tracker(agent_id)
            if tracker:
                tracker_data = {
                    "agent_status": tracker.state.value,
                    "current_tool": tracker.current_tool,
                }
        # Determine parent linkage and mode
        parent_sid = _task_parent_session_link(t, t_meta) or None
        session_mode = "child" if parent_sid else "primary"
        exec_mode_val, company_profile_val = _resolve_task_session_config(t)
        preferred_agent_val = _resolve_task_preferred_agent(t)
        selected_execution_agent_val = _resolve_task_selected_execution_agent(identity_task)
        _created_ts = t.created_at.timestamp() if hasattr(t.created_at, "timestamp") else time.time()
        _updated_ts = _created_ts
        if ch and msg_count > 0:
            latest_ts = channel_stats.get("latest_timestamp")
            if latest_ts:
                _updated_ts = float(latest_ts)

        session_id = str(getattr(t, "session_id", "") or "").strip()
        child_keys = {t.id}
        if session_id:
            child_keys.add(session_id)
        child_tasks: list[Any] = []
        seen_child_ids: set[str] = set()
        for key in child_keys:
            for child in child_tasks_by_parent.get(key, []):
                child_id = str(getattr(child, "id", "") or "")
                if not child_id or child_id == t.id or child_id in seen_child_ids:
                    continue
                seen_child_ids.add(child_id)
                child_tasks.append(child)

        identity_task_id = str(getattr(identity_task, "id", "") or "").strip()
        company_profile = t_meta.get("company_profile") or identity_meta.get("company_profile")
        if not company_profile and session_mode == "primary":
            for child in child_tasks:
                child_meta = task_meta_map.get(child.id, {})
                inherited_profile = child_meta.get("company_profile")
                if inherited_profile:
                    company_profile = inherited_profile
                    break
        is_task_mode_runtime = _metadata_is_task_mode_runtime({**t_meta, **identity_meta})

        progress_log = list(progress_by_task.get(t.id, []))
        if identity_task_id and identity_task_id != str(getattr(t, "id", "") or "").strip():
            for entry in progress_by_task.get(identity_task_id, []):
                if entry not in progress_log:
                    progress_log.append(entry)
            progress_log.sort(key=lambda item: float(item.get("timestamp") or 0.0))
        if is_task_mode_runtime:
            filtered_progress: list[dict[str, Any]] = []
            visible_task_mode_progress = {"thinking", "tool_call", "autonomy", "needs_input", "work_item_failed"}
            for raw_entry in progress_log:
                entry = dict(raw_entry or {})
                entry_type = str(entry.get("type", "") or "").strip()
                if entry_type not in visible_task_mode_progress:
                    continue
                if str(entry.get("work_item_projection_id", "") or "").strip() == "task_mode_execution":
                    entry.pop("work_item_projection_id", None)
                    entry.pop("work_item_turn_type", None)
                    entry.pop("work_item_projection_title", None)
                    entry.pop("is_company_runtime", None)
                if entry_type == "thinking":
                    entry["summary"] = "Thinking"
                filtered_progress.append(entry)
            progress_log = filtered_progress
        work_item_child_tasks = list(child_tasks)
        if identity_task_id and identity_task_id != str(getattr(t, "id", "") or "").strip():
            if all(str(getattr(child, "id", "") or "").strip() != identity_task_id for child in work_item_child_tasks):
                work_item_child_tasks.insert(0, identity_task)
        work_item_log = _build_session_work_item_log(
            t,
            task_meta=t_meta,
            child_tasks=work_item_child_tasks,
            task_meta_map=task_meta_map,
            progress_by_task=progress_by_task,
        )
        if is_task_mode_runtime:
            work_item_log = []
        # Work-item-driven rollup for the chat "Execution Progress" panel.
        # Only meaningful for primary company-mode sessions (children share
        # the parent's work-item set through the parent's payload).
        role_work_items_payload: dict[str, Any] | None = None
        executor_role_work_items_payload: dict[str, Any] | None = None
        if session_mode == "primary" and not is_task_mode_runtime:
            session_work_items = work_items_by_session_id.get(session_id, [])
            if session_work_items:
                role_work_items_payload = _build_role_work_items_for_session(
                    work_items=session_work_items,
                    task_by_work_item_id=company_task_by_work_item_id,
                    event_adapter=event_adapter,
                    progress_by_task=progress_by_task,
                    external_team_summary_by_task=external_team_summary_by_task,
                )
                if not role_work_items_payload:
                    role_work_items_payload = None
                executor_role_work_items_payload = _build_executor_role_work_items_for_session(
                    work_items=session_work_items,
                    task_by_work_item_id=company_task_by_work_item_id,
                    event_adapter=event_adapter,
                    progress_by_task=progress_by_task,
                    external_team_summary_by_task=external_team_summary_by_task,
                )
                if not executor_role_work_items_payload:
                    executor_role_work_items_payload = None
        handoff_context = (
            _extract_work_item_summary_for_downstream(t_meta)
            or _extract_work_item_summary_for_downstream(identity_meta)
        )
        if is_task_mode_runtime:
            handoff_context = ""
        runtime_meta = {
            **dict(t_meta.get("runtime_v2", {}) or {}),
            **dict(identity_meta.get("runtime_v2", {}) or {}),
        }
        member_session_meta = {
            **dict(t_meta.get("member_session_state", {}) or {}),
            **dict(identity_meta.get("member_session_state", {}) or {}),
        }
        runtime_control_meta = runtime_control_by_task.get(str(getattr(t, "id", "") or "").strip(), {})
        employee_assignment = {
            **dict(t_meta.get("employee_assignment", {}) or {}),
            **dict(identity_meta.get("employee_assignment", {}) or {}),
        }
        identity_payload = {} if is_task_mode_runtime else work_item_identity_payload_from_metadata(identity_meta)
        fallback_identity_payload = {} if is_task_mode_runtime else work_item_identity_payload_from_metadata(t_meta)
        if not is_task_mode_runtime:
            if WORK_ITEM_PROJECTION_ID_KEY not in identity_payload:
                projection_value = fallback_identity_payload.get(WORK_ITEM_PROJECTION_ID_KEY)
                if projection_value:
                    identity_payload[WORK_ITEM_PROJECTION_ID_KEY] = projection_value
            if WORK_ITEM_TURN_TYPE_KEY not in identity_payload:
                turn_value = fallback_identity_payload.get(WORK_ITEM_TURN_TYPE_KEY)
                if turn_value:
                    identity_payload[WORK_ITEM_TURN_TYPE_KEY] = turn_value
        # Defer expensive transcript-based context preview to session_detail.
        # Loading full transcripts for every child task blocks collab_sync
        # and makes large projects unresponsive on click.
        runtime_task_id = str(getattr(t, "id", "") or "").strip()
        org_id = (
            t_meta.get("org_id")
            or t_meta.get("organization_id")
            or identity_meta.get("org_id")
            or identity_meta.get("organization_id")
        )
        exec_mode_val, resolved_company_profile, org_id = _canonical_session_identity(
            exec_mode_val,
            company_profile or company_profile_val,
            org_id,
        )
        formatted_sessions.append({
            "project_id": project_id,
            "task_id": t.id,
            **_execution_turn_alias_payload(runtime_task_id),
            "session_id": t.session_id,
            "parent_session_id": parent_sid,
            "mode": session_mode,
            "exec_mode": exec_mode_val,
            "company_profile": resolved_company_profile,
            "org_id": org_id,
            "preferred_agent": preferred_agent_val,
            "selected_execution_agent": selected_execution_agent_val,
            "native_approval_level": identity_meta.get("native_approval_level") or t_meta.get("native_approval_level") or engine.config.autonomy.native_approval_level,
            "native_permission_scope_id": identity_meta.get("native_permission_scope_id") or t_meta.get("native_permission_scope_id") or getattr(t, "session_id", None),
            "execution_unit_kind": identity_meta.get("execution_unit_kind") or t_meta.get("execution_unit_kind"),
            "external_team_binding_id": dict(identity_meta.get("external_team_binding", {}) or t_meta.get("external_team_binding", {}) or {}).get("binding_id"),
            "external_team_boundary_role_id": dict(identity_meta.get("external_team_binding", {}) or t_meta.get("external_team_binding", {}) or {}).get("boundary_role_id"),
            "covered_role_ids": list(identity_meta.get("covered_role_ids", []) or t_meta.get("covered_role_ids", []) or []),
            "external_team_summary": (
                external_team_summary_by_task.get(identity_task_id)
                or external_team_summary_by_task.get(runtime_task_id)
            ),
            "external_company_execution_fence": identity_meta.get("external_company_execution_fence") or t_meta.get("external_company_execution_fence"),
            "channel_id": channel_id,
            "title": t.title,
            "status": status_val,
            "column_id": STATUS_TO_COLUMN.get(status_val, "todo"),
            # Convert opc_role_id → UI agent_id for frontend matching
            "assignee_ids": [event_adapter._resolve_role_to_agent(identity_task.assigned_to)] if event_adapter and getattr(identity_task, "assigned_to", "") else ([identity_task.assigned_to] if getattr(identity_task, "assigned_to", "") else []),
            "priority": priority_to_label(t.priority) if isinstance(t.priority, int) else None,
            "tags": t.tags or [],
            "created_at": _created_ts,
            "updated_at": _updated_ts,
            "message_count": msg_count,
            # Company Mode metadata
            **identity_payload,
            "company_profile": resolved_company_profile,
            **({} if is_task_mode_runtime else _work_item_role_payload(identity_task)),
            "work_item_gate": None if is_task_mode_runtime else (identity_meta.get("work_item_gate") or t_meta.get("work_item_gate")),
            "employee_assignment": employee_assignment or None,
            "origin_channel": identity_meta.get("origin_channel") or t_meta.get("origin_channel"),
            "origin_task_id": t_meta.get("origin_task_id") or t.id,
            # Progress + handoff (restored on page refresh)
            "progress_log": progress_log,
            "work_item_log": work_item_log,
            # Per-role DelegationWorkItem rollup. The frontend uses this to
            # drive the Execution Progress panel (1 row = 1 work item),
            # superseding the older Task-keyed grouping that mistook
            # runtime turns for work-item rows.
            "role_work_items": role_work_items_payload,
            # Display-only executor rollup. Execution Progress uses this so
            # original worker chips stay visible while work waits on review.
            "executor_role_work_items": executor_role_work_items_payload,
            "handoff_context": handoff_context,
            "is_company_runtime": bool(
                not is_task_mode_runtime
                and
                session_mode == "primary"
                and (
                    company_profile
                    or child_tasks
                    or (identity_task_id and identity_task_id != str(getattr(t, "id", "") or "").strip())
                    or work_item_log
                )
            ),
            **runtime_control_meta,
            "artifacts": identity_meta.get("artifacts") or t_meta.get("artifacts"),
            "runtime_session_id": runtime_meta.get("runtime_session_id"),
            "resume_cursor": runtime_meta.get("resume_cursor"),
            "active_subagents": list(runtime_meta.get("active_subagents", []) or []),
            "permission_requests": list(runtime_meta.get("permission_requests", []) or []),
            "worktree_path": runtime_meta.get("worktree_path"),
            "context_tokens": runtime_meta.get("context_tokens"),
            "context_window": runtime_meta.get("context_window") or default_context_window,
            "context_remaining_pct": runtime_meta.get("context_remaining_pct"),
            "input_tokens": runtime_meta.get("input_tokens"),
            "output_tokens": runtime_meta.get("output_tokens"),
            "total_tokens": runtime_meta.get("total_tokens"),
            "turn_cost_usd": runtime_meta.get("turn_cost_usd"),
            "session_cost_usd": runtime_meta.get("session_cost_usd"),
            "pending_permission_count": runtime_meta.get("pending_permission_count"),
            "drain_mode": runtime_meta.get("drain_mode"),
            "resident_status": _runtime_status_from_member_meta(member_session_meta),
            "actionable_inbox_count": member_session_meta.get("actionable_inbox_count"),
            "protocol_backlog_count": member_session_meta.get("protocol_backlog_count"),
            "notification_backlog_count": member_session_meta.get("notification_backlog_count"),
            "latest_notification": member_session_meta.get("latest_notification"),
            # Fix 5 PR7: queue depth surfaces "CTO has 2 tasks queued"
            # behind the focused one when the serial-queue flag is on.
            "pending_queue_depth": int(
                member_session_meta.get("pending_queue_depth", 0) or 0
            ),
            "pending_work_item_ids": list(
                member_session_meta.get("pending_work_item_ids", []) or []
            ),
            **tracker_data,
        })

    return {
        "ok": True,
        "project_id": project_id,
        "sync_scope": "full",
        "channels": formatted_channels,
        "messages": formatted_messages,
        "boards": formatted_boards,
        "columns": formatted_columns,
        "tasks": formatted_tasks,
        "sessions": formatted_sessions,
    }
