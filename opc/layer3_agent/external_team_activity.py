"""Read-only telemetry projection for opaque external teams.

This module deliberately has no dependency on OpenOPC scheduling models.  It
reduces provider observations into a UI summary and cannot mutate an Agent,
Task, DelegationWorkItem, gate, or controller state.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


EXTERNAL_TEAM_RUNTIME_EVENT_TYPE = "external_team.activity"
EXTERNAL_TEAM_SCHEMA_VERSION = 1

_ACTIVE_MEMBER_STATES = {
    "active",
    "busy",
    "claimed",
    "executing",
    "in_progress",
    "running",
    "working",
}
_FAILED_MEMBER_STATES = {"error", "failed", "failure"}
_COMPLETED_MEMBER_STATES = {
    "complete",
    "completed",
    "done",
    "finished",
    "ready",
    "shutdown",
    "stopped",
}
_ACTIVE_TASK_STATES = {"claimed", "in_progress", "running", "working"}
_FAILED_TASK_STATES = {"cancelled", "error", "failed", "failure"}
_COMPLETED_TASK_STATES = {"complete", "completed", "done", "finished"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _record_by_id(items: Any, key: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        item_id = _text(item.get(key))
        if item_id:
            result[item_id] = dict(item)
    return result


def _is_leader_member(member: dict[str, Any]) -> bool:
    member_id = _text(member.get("member_id")).lower().replace("_", "-")
    role = _text(member.get("role")).lower().replace("_", "-")
    return member_id == "team-leader" or role in {"leader", "team-leader"}


def empty_external_team_summary() -> dict[str, Any]:
    return {
        "schema_version": EXTERNAL_TEAM_SCHEMA_VERSION,
        "provider": "jiuwenswarm",
        "mode": "starting",
        "leader_state": "starting",
        "team_id": "",
        "external_invocation_id": "",
        "provider_session_id": "",
        "members": [],
        "tasks": [],
        "counts": {
            "members": 0,
            "members_active": 0,
            "members_completed": 0,
            "members_failed": 0,
            "tasks": 0,
            "tasks_active": 0,
            "tasks_completed": 0,
            "tasks_failed": 0,
        },
        "last_event_at": "",
        "telemetry_incomplete": False,
    }


def reduce_external_team_event(
    current: dict[str, Any] | None,
    event: dict[str, Any],
) -> dict[str, Any]:
    """Idempotently project one normalized observation into a compact state."""

    summary = deepcopy(current) if isinstance(current, dict) else empty_external_team_summary()
    members = _record_by_id(summary.get("members"), "member_id")
    tasks = _record_by_id(summary.get("tasks"), "task_id")
    kind = _text(event.get("kind"))
    occurred_at = _text(event.get("occurred_at"))

    for key in ("team_id", "external_invocation_id", "provider_session_id"):
        value = _text(event.get(key))
        if value:
            summary[key] = value
    if occurred_at:
        summary["last_event_at"] = max(_text(summary.get("last_event_at")), occurred_at)

    member = event.get("member") if isinstance(event.get("member"), dict) else None
    if member:
        member_id = _text(member.get("member_id"))
        if member_id:
            previous = members.get(member_id, {})
            merged = {**previous, **{k: v for k, v in member.items() if v not in (None, "")}}
            merged["member_id"] = member_id
            merged["updated_at"] = occurred_at or _text(previous.get("updated_at"))
            if kind == "member_shutdown" and not _text(member.get("status")):
                merged["status"] = "shutdown"
            if _is_leader_member(merged):
                if summary.get("mode") not in {"completed", "failed"}:
                    leader_state = _text(
                        merged.get("execution_status") or merged.get("status")
                    )
                    if leader_state:
                        summary["leader_state"] = leader_state
                members.pop(member_id, None)
            else:
                members[member_id] = merged

    task = event.get("task") if isinstance(event.get("task"), dict) else None
    if task:
        task_id = _text(task.get("task_id"))
        if task_id:
            previous = tasks.get(task_id, {})
            merged = {**previous, **{k: v for k, v in task.items() if v not in (None, "")}}
            merged["task_id"] = task_id
            merged["updated_at"] = occurred_at or _text(previous.get("updated_at"))
            inferred = {
                "task_claimed": "claimed",
                "task_completed": "completed",
                "task_cancelled": "cancelled",
                "task_unblocked": "pending",
            }.get(kind)
            if inferred and not _text(task.get("status")):
                merged["status"] = inferred
            tasks[task_id] = merged

    if kind == "runtime_ready":
        summary["mode"] = "leader_only"
        summary["leader_state"] = "working"
    elif kind == "leader_output" and summary.get("mode") not in {"completed", "failed"}:
        summary["leader_state"] = "synthesizing"
    elif kind == "team_completed":
        summary["mode"] = "completed"
        summary["leader_state"] = "completed"
    elif kind == "team_error":
        summary["mode"] = "failed"
        summary["leader_state"] = "failed"

    if members and summary.get("mode") not in {"completed", "failed"}:
        summary["mode"] = "team_active"

    member_states = [
        _text(item.get("execution_status") or item.get("status")).lower()
        for item in members.values()
    ]
    task_states = [_text(item.get("status")).lower() for item in tasks.values()]
    summary["members"] = sorted(members.values(), key=lambda item: _text(item.get("member_id")))
    summary["tasks"] = sorted(tasks.values(), key=lambda item: _text(item.get("task_id")))
    summary["counts"] = {
        "members": len(members),
        "members_active": sum(state in _ACTIVE_MEMBER_STATES for state in member_states),
        "members_completed": sum(state in _COMPLETED_MEMBER_STATES for state in member_states),
        "members_failed": sum(state in _FAILED_MEMBER_STATES for state in member_states),
        "tasks": len(tasks),
        "tasks_active": sum(state in _ACTIVE_TASK_STATES for state in task_states),
        "tasks_completed": sum(state in _COMPLETED_TASK_STATES for state in task_states),
        "tasks_failed": sum(state in _FAILED_TASK_STATES for state in task_states),
    }
    return summary


def reduce_external_team_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] | None = None
    seen: set[str] = set()
    for event in sorted(
        (dict(item) for item in events if isinstance(item, dict)),
        key=lambda item: (int(item.get("sequence", 0) or 0), _text(item.get("occurred_at"))),
    ):
        event_id = _text(event.get("event_id"))
        if event_id and event_id in seen:
            continue
        if event_id:
            seen.add(event_id)
        summary = reduce_external_team_event(summary, event)
    return summary or empty_external_team_summary()
