"""Canonical company-mode collaboration tool names."""

from __future__ import annotations

from typing import Any


def company_collaboration_enabled(execution_mode: str | None) -> bool:
    """Return whether company collaboration capabilities should be exposed."""
    return str(execution_mode or "").strip() == "company_mode"


def company_collaboration_enabled_for_task(task: object | None) -> bool:
    """Return whether collaboration capabilities are enabled for a task-like object."""
    if task is None:
        return False
    metadata = dict(getattr(task, "metadata", {}) or {})
    return company_collaboration_enabled(str(metadata.get("execution_mode", "") or ""))


COLLAB_PROFILE_DISABLED = "disabled"
COLLAB_PROFILE_WORKER_DEFAULT = "worker_default"
COLLAB_PROFILE_WORKER_EXECUTE_REVIEW = "worker_execute_review"
COLLAB_PROFILE_MANAGER_DEFAULT = "manager_default"
COLLAB_PROFILE_COORDINATOR_DEFAULT = "coordinator_default"
COLLAB_PROFILE_DEBUG_ADMIN = "debug_admin"

WORKER_DEFAULT_TOOL_NAMES: tuple[str, ...] = (
    "inbox",
    "send_dm",
    "ask_peer_and_wait",
    "reply_message",
)

WORKER_EXECUTE_REVIEW_TOOL_NAMES: tuple[str, ...] = WORKER_DEFAULT_TOOL_NAMES

MANAGER_DEFAULT_TOOL_NAMES: tuple[str, ...] = (
    *WORKER_DEFAULT_TOOL_NAMES,
    "delegate_work",
    "modify_work_item",
    "delete_work_item",
    "manager_board_read",
    "broadcast_issue",
    "start_meeting",
)

COORDINATOR_DEFAULT_TOOL_NAMES: tuple[str, ...] = (
    *MANAGER_DEFAULT_TOOL_NAMES,
    "propose_task_adjustment",
    "route_work",
)

DEBUG_ADMIN_TOOL_NAMES: tuple[str, ...] = (
    "read_inbox",
    "read_meeting",
    "list_colleagues",
)

MEETING_RESPONSE_TOOL_NAMES: tuple[str, ...] = ("respond_meeting",)
HUMAN_REVIEW_TOOL_NAMES: tuple[str, ...] = ("close_human_review",)
SELF_EVOLUTION_TOOL_NAMES: tuple[str, ...] = ("submit_self_evolution_patches",)


COMPANY_COLLABORATION_TOOL_NAMES: tuple[str, ...] = (
    *COORDINATOR_DEFAULT_TOOL_NAMES,
    *MEETING_RESPONSE_TOOL_NAMES,
    *HUMAN_REVIEW_TOOL_NAMES,
    *SELF_EVOLUTION_TOOL_NAMES,
)

COMPANY_DEBUG_TOOL_NAMES: tuple[str, ...] = DEBUG_ADMIN_TOOL_NAMES

COMPANY_ALL_COLLABORATION_TOOL_NAMES: tuple[str, ...] = tuple(
    dict.fromkeys(
        [
            *COMPANY_COLLABORATION_TOOL_NAMES,
            *COMPANY_DEBUG_TOOL_NAMES,
        ]
    )
)

COMPANY_APPROVAL_EXEMPT_TOOL_NAMES: tuple[str, ...] = (
    *COMPANY_ALL_COLLABORATION_TOOL_NAMES,
)

MULTI_TEAM_COORDINATION_TURN_MODES: frozenset[str] = frozenset(
    {
        "dispatch_required",
        "monitor_children",
        "synthesize_required",
        "deliver_required",
    }
)

# Kanban-push review turn: a dedicated review Task is the only thing running
# in the seat. The manager emits a structured verdict as part of the turn
# output; the runtime (``_finalize_review_work_item``) auto-applies it to
# the child work item. The restricted toolset keeps the turn focused on
# judgement — no delegation / messaging / dispatch side-channels.
REVIEW_EXECUTE_TURN_MODE: str = "review_execute"
REVIEW_EXECUTE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "manager_board_read",
    }
)

MULTI_TEAM_COORDINATOR_LEGACY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "propose_task_adjustment",
        "route_work",
    }
)


def _task_metadata(task: object | None) -> dict[str, Any]:
    return dict(getattr(task, "metadata", {}) or {}) if task is not None else {}


def _task_context_snapshot(task: object | None) -> dict[str, Any]:
    return dict(getattr(task, "context_snapshot", {}) or {}) if task is not None else {}


def _work_item_turn_type(metadata: dict[str, Any]) -> str:
    for key in ("work_item_turn_type", "work_item_turn_type", "work_kind", "delegation_turn_kind"):
        value = str(metadata.get(key, "") or "").strip().lower()
        if value:
            return value
    return ""


def _runtime_state_dict(runtime_state: Any | None) -> dict[str, Any]:
    return dict(runtime_state or {}) if isinstance(runtime_state, dict) else {}


def _role_type_hint(role_cfg: Any | None, runtime_state: dict[str, Any]) -> str:
    if role_cfg is not None:
        runtime_policy = getattr(role_cfg, "runtime_policy", None)
        hinted = str(
            getattr(runtime_policy, "role_type", "")
            or (runtime_policy.get("role_type", "") if isinstance(runtime_policy, dict) else "")
            or getattr(role_cfg, "role_type", "")
            or ""
        ).strip().lower()
        if hinted:
            return hinted
    return str(runtime_state.get("role_type", "") or "").strip().lower()


def _can_spawn_hint(role_cfg: Any | None, runtime_state: dict[str, Any]) -> bool:
    if role_cfg is not None:
        can_spawn = [str(item).strip() for item in list(getattr(role_cfg, "can_spawn", []) or []) if str(item).strip()]
        if can_spawn:
            return True
    return bool(
        [
            str(item).strip()
            for item in list(runtime_state.get("can_spawn", []) or [])
            if str(item).strip()
        ]
    )


def _is_execute_or_review_work_item(task: object | None) -> bool:
    metadata = _task_metadata(task)
    if not company_collaboration_enabled(str(metadata.get("execution_mode", "") or "")):
        return False
    turn_type = _work_item_turn_type(metadata)
    return turn_type in {"execute", "review"}


def is_self_evolution_work_item(task: object | None) -> bool:
    metadata = _task_metadata(task)
    if _work_item_turn_type(metadata) == "self_evolution":
        return True
    return bool(metadata.get("self_evolution_work_item", False))


def _has_active_meeting(task: object | None, runtime_state: dict[str, Any]) -> bool:
    metadata = _task_metadata(task)
    peer_wait = dict(metadata.get("peer_wait", {}) or {})
    if str(peer_wait.get("kind", "") or "").strip().lower() == "meeting":
        return True
    context_snapshot = _task_context_snapshot(task)
    for bucket in (
        context_snapshot.get("company_member_inbox", []),
        context_snapshot.get("company_member_protocol_backlog", []),
        context_snapshot.get("company_member_notification_backlog", []),
        context_snapshot.get("broker_pending_inbox", []),
    ):
        for item in list(bucket or []):
            if isinstance(item, dict) and (
                str(item.get("meeting_room_id", "") or "").strip()
                or str(dict(item.get("metadata", {}) or {}).get("meeting_room_id", "") or "").strip()
            ):
                return True
    latest_company_notification = dict(context_snapshot.get("latest_company_notification", {}) or {})
    if str(latest_company_notification.get("meeting_room_id", "") or "").strip():
        return True
    if str(runtime_state.get("meeting_room_id", "") or "").strip():
        return True
    if bool(runtime_state.get("active_meeting", False)):
        return True
    return False


def _human_review_close_allowed(task: object | None, runtime_state: dict[str, Any]) -> bool:
    metadata = _task_metadata(task)
    context_snapshot = _task_context_snapshot(task)
    return any(
        bool(source.get("human_review_close_allowed", False))
        or str(source.get("human_review_checkpoint_type", "") or "").strip() == "company_delivery_feedback"
        for source in (metadata, context_snapshot, runtime_state)
    )


def resolve_company_turn_mode(
    task: object | None,
    runtime_state: dict[str, Any] | None = None,
) -> str:
    metadata = _task_metadata(task)
    context_snapshot = _task_context_snapshot(task)
    state = _runtime_state_dict(runtime_state)
    runtime_model = str(
        metadata.get("runtime_model", "")
        or context_snapshot.get("runtime_model", "")
        or state.get("runtime_model", "")
        or ""
    ).strip()
    if runtime_model != "multi_team_org":
        return ""
    for candidate in (
        state.get("current_turn_mode"),
        dict(state.get("manager_digest", {}) or {}).get("current_turn_mode"),
        metadata.get("current_turn_mode"),
        context_snapshot.get("current_turn_mode"),
        dict(metadata.get("member_session_state", {}) or {}).get("current_turn_mode"),
        dict(context_snapshot.get("member_session", {}) or {}).get("current_turn_mode"),
        dict(metadata.get("resident_assignment", {}) or {}).get("metadata", {}).get("current_turn_mode"),
        dict(context_snapshot.get("resident_assignment", {}) or {}).get("metadata", {}).get("current_turn_mode"),
        dict(context_snapshot.get("manager_digest", {}) or {}).get("current_turn_mode"),
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    return ""


def resolve_collaboration_profile(
    task: object | None,
    role: str = "",
    seat: str = "",
    runtime_state: dict[str, Any] | None = None,
    *,
    role_cfg: Any | None = None,
    debug_admin: bool = False,
) -> str:
    """Resolve the collaboration capability profile for a task/seat."""
    metadata = _task_metadata(task)
    state = _runtime_state_dict(runtime_state)
    if not company_collaboration_enabled_for_task(task):
        return COLLAB_PROFILE_DISABLED
    if debug_admin or bool(metadata.get("collaboration_debug_admin", False)) or bool(state.get("debug_admin", False)):
        return COLLAB_PROFILE_DEBUG_ADMIN

    managed_team_id = str(
        metadata.get("managed_team_id", "")
        or state.get("managed_team_id", "")
        or dict(metadata.get("member_session_state", {}) or {}).get("metadata", {}).get("managed_team_id", "")
        or ""
    ).strip()
    role_type = _role_type_hint(role_cfg, state)
    can_spawn = _can_spawn_hint(role_cfg, state)
    manager_board_summary = (
        dict(state.get("manager_board_summary", {}) or {})
        or dict(_task_context_snapshot(task).get("manager_board_summary", {}) or {})
    )
    work_item_turn_type = _work_item_turn_type(metadata)

    if role_type == "coordinator" or can_spawn:
        return COLLAB_PROFILE_COORDINATOR_DEFAULT
    if managed_team_id or manager_board_summary or work_item_turn_type in {"intake", "plan", "dispatch", "monitor", "aggregate", "deliver"}:
        return COLLAB_PROFILE_MANAGER_DEFAULT
    if _is_execute_or_review_work_item(task):
        return COLLAB_PROFILE_WORKER_EXECUTE_REVIEW
    return COLLAB_PROFILE_WORKER_DEFAULT


def resolve_allowed_collaboration_tools(
    profile: str,
    task: object | None = None,
    runtime_state: dict[str, Any] | None = None,
) -> set[str]:
    """Return the collaboration tools visible for the resolved profile."""
    state = _runtime_state_dict(runtime_state)
    if profile == COLLAB_PROFILE_DISABLED:
        return set()
    if profile == COLLAB_PROFILE_DEBUG_ADMIN:
        return set(DEBUG_ADMIN_TOOL_NAMES)
    if profile == COLLAB_PROFILE_WORKER_EXECUTE_REVIEW:
        allowed = set(WORKER_EXECUTE_REVIEW_TOOL_NAMES)
    elif profile == COLLAB_PROFILE_MANAGER_DEFAULT:
        allowed = set(MANAGER_DEFAULT_TOOL_NAMES)
    elif profile == COLLAB_PROFILE_COORDINATOR_DEFAULT:
        allowed = set(COORDINATOR_DEFAULT_TOOL_NAMES)
    else:
        allowed = set(WORKER_DEFAULT_TOOL_NAMES)
    turn_mode = resolve_company_turn_mode(task, runtime_state=state)
    if turn_mode == REVIEW_EXECUTE_TURN_MODE and str(_task_metadata(task).get("runtime_model", "") or "").strip() == "multi_team_org":
        return set(REVIEW_EXECUTE_TOOL_NAMES)
    if turn_mode in MULTI_TEAM_COORDINATION_TURN_MODES:
        allowed.discard("ask_peer_and_wait")
    if turn_mode and str(_task_metadata(task).get("runtime_model", "") or "").strip() == "multi_team_org":
        allowed.difference_update(MULTI_TEAM_COORDINATOR_LEGACY_TOOL_NAMES)
    if _has_active_meeting(task, state):
        allowed.update(MEETING_RESPONSE_TOOL_NAMES)
    if _human_review_close_allowed(task, state):
        allowed.update(HUMAN_REVIEW_TOOL_NAMES)
    if is_self_evolution_work_item(task):
        allowed.update(SELF_EVOLUTION_TOOL_NAMES)
    return allowed


def resolve_task_collaboration_tools(
    task: object | None,
    *,
    role: str = "",
    seat: str = "",
    runtime_state: dict[str, Any] | None = None,
    role_cfg: Any | None = None,
    debug_admin: bool = False,
) -> tuple[str, set[str]]:
    profile = resolve_collaboration_profile(
        task,
        role=role,
        seat=seat,
        runtime_state=runtime_state,
        role_cfg=role_cfg,
        debug_admin=debug_admin,
    )
    return profile, resolve_allowed_collaboration_tools(profile, task=task, runtime_state=runtime_state)
