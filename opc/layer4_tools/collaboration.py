"""Collaboration tools exposed to agents in company mode."""

from __future__ import annotations

import copy
from datetime import datetime
from functools import lru_cache
import inspect
import json
import re
from types import SimpleNamespace
import uuid
from typing import Any

from loguru import logger

from opc.core.company_tools import (
    COMPANY_COLLABORATION_TOOL_NAMES,
    is_self_evolution_work_item,
)
from opc.core.models import (
    AgentMessage,
    CommsSemanticType,
    DelegationEvent,
    DelegationWorkItem,
    MessageUrgency,
    Phase,
    Task,
    TaskStatus,
)
from opc.layer2_organization.company_runtime import canonical_role_session_id
from opc.layer2_organization.phase import (
    DONE_PHASES,
    IN_REVIEW_PHASES,
    InvalidPhaseTransition,
    kanban_column,
)
from opc.layer2_organization.output_contract import (
    output_contract_metadata,
    render_output_contract_context,
    replace_output_aliases,
)
from opc.layer2_organization.prompt_contract import prompt_contract_from_delegate_item
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task
from opc.layer2_organization.work_item_runtime import mark_work_item_runtime
from opc.layer2_organization.work_item_identity import mark_work_item_projection
from opc.layer2_organization.work_item_transition import (
    is_prunable_dependency_work_item,
    normalize_dependency_work_item_ids,
    refresh_dependents_for_run,
    transition_work_item,
)
from opc.layer4_tools.output_budget import clip_text
from opc.layer4_tools.registry import ToolDefinition

EXTERNAL_COLLABORATION_TOOL_NAMES = COMPANY_COLLABORATION_TOOL_NAMES

OUTPUT_METADATA_KEYS: tuple[str, ...] = (
    "work_item_summary",
    "work_item_summary_for_downstream",
    "work_item_artifact_index",
    "verification_status",
    "verification_evidence",
    "verification",
    "structured_review_verdict",
    "delivery_package",
    "downstream_assignments",
    "artifacts",
    "automated_verification_results",
    "final_feedback_evaluation",
    "feedback_followup_message",
)

_DELEGATE_WORK_ITEM_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "acceptance_criteria",
        "allow_parallel_same_role",
        "batch_id",
        "batch_index",
        "blocked_reason",
        "body",
        "brief",
        "continuation_source",
        "coordination_notes",
        "deliverable_summary",
        "deliverables",
        "delegation_rationale",
        "dependency_classes",
        "dependency_work_item_ids",
        "depends_on",
        "done_when",
        "handoff_status",
        "id",
        "kind",
        "name",
        "non_overlap_guard",
        "outputs",
        "parent_board_scope",
        "projection_id",
        "prompt",
        "prompt_brief",
        "release_on_semantic_type",
        "release_policy",
        "role_id",
        "scope_key",
        "source_message_id",
        "summary",
        "task_brief",
        "team_id",
        "title",
        "upstream_intent_summary",
        "upstream_visibility",
        "work_item_projection_id",
        "work_item_ref",
        "work_kind",
    }
)

_DELEGATE_WORK_ITEM_FIELD_SUGGESTIONS: dict[str, str] = {
    "dependencies": "depends_on",
    "dependency_scope_keys": "depends_on",
    "depends_on_role_ids": "depends_on",
    "depends_on_roles": "depends_on",
    "depends_on_scope_keys": "depends_on",
    "depends_on_work_item_ids": "dependency_work_item_ids",
}


def _normalize_message_urgency(value: str) -> MessageUrgency:
    normalized = str(value or "").strip().lower() or MessageUrgency.LOW.value
    aliases = {
        "low": MessageUrgency.LOW,
        "normal": MessageUrgency.NORMAL,
        "medium": MessageUrgency.NORMAL,
        "default": MessageUrgency.NORMAL,
        "high": MessageUrgency.HIGH,
        "urgent": MessageUrgency.HIGH,
        "critical": MessageUrgency.HIGH,
        "blocking": MessageUrgency.BLOCKING,
        "blocker": MessageUrgency.BLOCKING,
    }
    return aliases.get(normalized, MessageUrgency.NORMAL)


def _active_role(task: Task | None) -> str:
    if not task:
        return ""
    # TODO(role-identity): route through a single work-item role accessor.
    # for tasks where Task.assigned_to is empty (e.g. environment-provisioning
    # subtasks). Drop once those producers always set assigned_to.
    return str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip()


def _active_seat(task: Task | None) -> str:
    if not task:
        return ""
    return str(task.metadata.get("delegation_seat_id", "") or "").strip()


def _resolve_target_seat(communication: Any, task: Task, target_role_id: str) -> dict[str, Any]:
    runtime_topology = dict((task.metadata or {}).get("runtime_topology", {}) or {})
    seat_id = _active_seat(task)
    org_engine = getattr(communication, "org_engine", None)
    if org_engine is not None and hasattr(org_engine, "resolve_runtime_target_seat"):
        try:
            resolved = org_engine.resolve_runtime_target_seat(
                runtime_topology,
                from_seat_id=seat_id,
                target_role_id=target_role_id,
            )
        except Exception:
            resolved = None
        if isinstance(resolved, dict) and str(resolved.get("seat_id", "") or "").strip():
            return dict(resolved)
    # Fallback: direct lookup in topology seats by role_id.  Without a valid
    # seat_id the work-item's queue key won't match any member session, causing
    # the delegation to silently stall.
    seats = list(runtime_topology.get("seats", []) or [])
    for seat in seats:
        if str(seat.get("role_id", "") or "").strip() == target_role_id:
            logger.info(
                "delegate_work: org_engine seat resolution failed for role {}, "
                "falling back to topology seat {}",
                target_role_id,
                str(seat.get("seat_id", "") or ""),
            )
            return dict(seat)
    return {}


def _current_work_item_id(task: Task | None) -> str:
    return linked_work_item_id_for_task(task)


def _current_parent_work_item_id(task: Task | None) -> str:
    if not task:
        return ""
    derived_projection = dict(
        task.metadata.get("derived_work_item_projection", {})
        or {}
    )
    return str(
        task.metadata.get("manager_board_parent_work_item_id", "")
        or task.metadata.get("attention_business_parent_work_item_id", "")
        or task.metadata.get("delegation_parent_work_item_id", "")
        or derived_projection.get("parent_work_item_id", "")
        or _current_work_item_id(task)
        or ""
    ).strip()


def _parent_board_scope(task: Task | None) -> str:
    seat_id = _active_seat(task)
    parent_work_item_id = _current_parent_work_item_id(task)
    if seat_id and parent_work_item_id:
        return f"{seat_id}:{parent_work_item_id}"
    return ""


def _normalize_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        rendered = value.strip()
        return [rendered] if rendered else []
    try:
        items = list(value or [])
    except TypeError:
        rendered = str(value).strip()
        return [rendered] if rendered else []
    return [
        str(item).strip()
        for item in items
        if str(item).strip()
    ]


def _slugify_scope_key(value: str) -> str:
    lowered = str(value or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    return slug or "work-item"


def _scope_key_aliases(value: str) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    lowered = raw.lower()
    slug = _slugify_scope_key(raw)
    return list(dict.fromkeys([raw, lowered, slug]))


def _index_scope_aliases(index: dict[str, list[Any]], value: str, target: Any) -> None:
    for alias in _scope_key_aliases(value):
        index.setdefault(alias, []).append(target)


def _lookup_scope_aliases(index: dict[str, list[Any]], value: str) -> list[Any]:
    matches: list[Any] = []
    for alias in _scope_key_aliases(value):
        matches.extend(index.get(alias, []))
    deduped: list[Any] = []
    seen: set[str] = set()
    for match in matches:
        key = str(getattr(match, "work_item_id", "") or match)
        if not key:
            key = str(id(match))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(match)
    return deduped


def _unique_scope_key(
    requested: str,
    *,
    existing_scope_keys: set[str],
    pending_scope_keys: set[str],
) -> tuple[str, bool]:
    base = _slugify_scope_key(requested)
    candidate = base
    suffix = 2
    while candidate in existing_scope_keys or candidate in pending_scope_keys:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate, candidate != str(requested or "").strip()


def _work_item_metadata(item: Any | None) -> dict[str, Any]:
    return dict(getattr(item, "metadata", {}) or {}) if item is not None else {}


def _is_attention_work_item(item: Any | None) -> bool:
    return bool(_work_item_metadata(item).get("attention_work_item", False))


def _work_item_kind(item: Any | None) -> str:
    return str(getattr(item, "kind", "") or "").strip().lower()


async def _get_work_item(store: Any, work_item_id: str) -> Any | None:
    normalized_id = str(work_item_id or "").strip()
    if not normalized_id or not hasattr(store, "get_delegation_work_item"):
        return None
    return await store.get_delegation_work_item(normalized_id)


async def _resolve_manager_board_parent_work_item_id(store: Any, task: Task | None) -> tuple[str, str]:
    """Return the business parent that manager-board tools should operate on.

    Attention cards are runtime wake-up wrappers. Their default board/delegation
    target is the business item they wake for, not the hidden attention card
    itself.
    """
    if task is None:
        return "", ""
    explicit_parent = str(
        task.metadata.get("manager_board_parent_work_item_id", "")
        or task.metadata.get("attention_business_parent_work_item_id", "")
        or ""
    ).strip()
    if explicit_parent:
        current_id = _current_work_item_id(task)
        current_item = await _get_work_item(store, current_id)
        return explicit_parent, current_id if _is_attention_work_item(current_item) else ""

    current_id = _current_work_item_id(task)
    current_item = await _get_work_item(store, current_id)
    if current_item is None or not _is_attention_work_item(current_item):
        return current_id, ""

    attention_id = str(getattr(current_item, "work_item_id", "") or current_id).strip()
    parent_id = str(getattr(current_item, "parent_work_item_id", "") or "").strip()
    if not parent_id:
        return current_id, attention_id

    parent_item = await _get_work_item(store, parent_id)
    # Delivery attention cards are often children of the hidden delivery card;
    # the actionable manager board is the delivery card's business parent.
    if (
        parent_item is not None
        and _work_item_kind(parent_item) in {"deliver", "delivery"}
        and str(getattr(parent_item, "parent_work_item_id", "") or "").strip()
    ):
        return str(getattr(parent_item, "parent_work_item_id", "") or "").strip(), attention_id
    return parent_id, attention_id


def _normalize_dependency_specs(value: Any) -> list[dict[str, str]]:
    raw_items = value if isinstance(value, list) else [value] if value not in (None, "") else []
    specs: list[dict[str, str]] = []
    for raw in raw_items:
        if isinstance(raw, dict):
            for key in ("work_item_id", "scope_key", "role_id", "ref", "id"):
                rendered = str(raw.get(key, "") or "").strip()
                if rendered:
                    kind = "work_item_id" if key == "id" else "work_item_ref" if key == "ref" else key
                    specs.append({
                        "kind": kind,
                        "value": rendered,
                        "raw": json.dumps(raw, ensure_ascii=False, sort_keys=True),
                    })
                    break
            continue
        rendered = str(raw or "").strip()
        if rendered:
            specs.append({"kind": "ref", "value": rendered, "raw": rendered})
    return specs


def _validate_delegate_work_items(items: Any) -> None:
    if not isinstance(items, list):
        raise ValueError("delegate_work requires `items` to be a list of item objects")

    invalid_items: list[str] = []
    for index, item in enumerate(items):
        label = f"items[{index}]"
        if not isinstance(item, dict):
            invalid_items.append(f"{label} must be an object, got {type(item).__name__}")
            continue

        unknown_fields = [
            str(field)
            for field in item
            if str(field) not in _DELEGATE_WORK_ITEM_ALLOWED_FIELDS
        ]
        if not unknown_fields:
            continue

        field_notes: list[str] = []
        for field in sorted(unknown_fields):
            suggested = _DELEGATE_WORK_ITEM_FIELD_SUGGESTIONS.get(field)
            if suggested:
                field_notes.append(f"`{field}` (did you mean `{suggested}`?)")
            else:
                field_notes.append(f"`{field}`")
        invalid_items.append(f"{label} unknown field(s): {', '.join(field_notes)}")

    if invalid_items:
        allowed = ", ".join(f"`{field}`" for field in sorted(_DELEGATE_WORK_ITEM_ALLOWED_FIELDS))
        raise ValueError(
            "delegate_work rejected the request before creating any work items: "
            + "; ".join(invalid_items)
            + ". Use only supported item fields. Allowed item fields: "
            + allowed
        )


def _resolve_dependency_specs(
    dependency_specs: list[dict[str, str]],
    *,
    ref_index: dict[str, str],
    work_item_ids: set[str],
    scope_index: dict[str, list[str]],
    role_index: dict[str, list[str]],
) -> list[dict[str, str]]:
    resolved: list[dict[str, str]] = []
    for spec in dependency_specs:
        kind = str(spec.get("kind", "") or "ref").strip()
        value = str(spec.get("value", "") or "").strip()
        if not value:
            continue
        candidates: list[tuple[str, str]] = []
        if kind == "work_item_id":
            if value in work_item_ids:
                candidates.append((value, "work_item_id"))
        elif kind == "scope_key":
            candidates.extend((item, "scope_key") for item in _lookup_scope_aliases(scope_index, value))
        elif kind == "role_id":
            candidates.extend((item, "role_id") for item in role_index.get(value, []))
        elif kind == "work_item_ref":
            if value in ref_index:
                candidates.append((ref_index[value], "work_item_ref"))
        else:
            if value in work_item_ids:
                candidates.append((value, "work_item_id"))
            candidates.extend((item, "scope_key") for item in _lookup_scope_aliases(scope_index, value))
            candidates.extend((item, "role_id") for item in role_index.get(value, []))
            if value in ref_index:
                candidates.append((ref_index[value], "work_item_ref"))
        deduped = list(dict.fromkeys(candidates))
        ids = list(dict.fromkeys(item for item, _source in deduped))
        if not ids:
            raise ValueError(f"delegate_work could not resolve depends_on reference `{value}`")
        if len(ids) > 1:
            raise ValueError(
                f"delegate_work depends_on reference `{value}` matched multiple work items: "
                + ", ".join(ids)
            )
        source = next((source for item, source in deduped if item == ids[0]), kind)
        resolved.append({
            "input": str(spec.get("raw", value) or value),
            "work_item_id": ids[0],
            "resolved_by": source,
        })
    return resolved


def _current_turn_mode(task: Task | None) -> str:
    if task is None:
        return ""
    return str(
        task.metadata.get("current_turn_mode", "")
        or dict(task.context_snapshot or {}).get("current_turn_mode", "")
        or ""
    ).strip().lower()


def _requires_structured_dispatch_plan(task: Task | None) -> bool:
    if task is None:
        return False
    runtime_model = str(task.metadata.get("runtime_model", "") or "").strip().lower()
    return runtime_model == "multi_team_org" and _current_turn_mode(task) == "dispatch_required"


def _int_metadata_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _is_hidden_work_item(item: Any | None) -> bool:
    metadata = _work_item_metadata(item)
    return bool(
        metadata.get("hidden_from_company_kanban")
        or str(metadata.get("upstream_visibility", "") or "").strip().lower() == "hidden"
    )


def _user_supplied_input_from_task(task: Task | None) -> str:
    if task is None:
        return ""
    context_snapshot = dict(getattr(task, "context_snapshot", {}) or {})
    metadata = dict(getattr(task, "metadata", {}) or {})
    return str(
        context_snapshot.get("user_supplied_input")
        or metadata.get("latest_user_directive")
        or metadata.get("user_supplied_input")
        or metadata.get("manager_mutation_user_input")
        or ""
    ).strip()


async def _notify_work_items_changed(communication: Any, *, label: str) -> None:
    wake_hook = getattr(communication, "on_work_items_created", None)
    if wake_hook is not None:
        try:
            wake_hook()
        except Exception:
            logger.opt(exception=True).warning(f"{label}: on_work_items_created hook failed")
    notify_hook = getattr(communication, "on_kanban_changed", None)
    if notify_hook is not None:
        try:
            notified = notify_hook()
            if inspect.isawaitable(notified):
                await notified
        except Exception:
            logger.opt(exception=True).warning(f"{label}: on_kanban_changed hook failed")


def _manager_mutation_metadata(
    item: DelegationWorkItem,
    *,
    task: Task,
    action: str,
    reason: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_metadata = dict(getattr(item, "metadata", {}) or {})
    revision = _int_metadata_value(current_metadata.get("manager_mutation_revision")) + 1
    payload: dict[str, Any] = {
        "manager_mutation_revision": revision,
        "manager_mutation_id": uuid.uuid4().hex,
        "manager_mutation_action": str(action or "").strip(),
        "manager_mutation_reason": str(reason or "").strip(),
        "manager_mutation_at": datetime.now().isoformat(),
        "manager_mutation_by_role_id": _active_role(task),
        "manager_mutation_by_seat_id": _active_seat(task),
    }
    user_input = _user_supplied_input_from_task(task)
    if user_input:
        payload["manager_mutation_user_input"] = user_input
        payload["latest_user_directive"] = user_input
    if extra:
        payload.update(dict(extra))
    return payload


def _manager_mutation_metadata_unset(*, clear_outputs: bool = False) -> list[str]:
    keys = [
        "dispatch_hold",
        "queued_behind_session",
        "claimed_by_role_session_id",
        "claimed_task_id",
        "waiting_on_work_item_ids",
        "completion_report",
        "review_verdict",
        "review_evidence",
        "manager_review_verdict",
        "rework_feedback",
    ]
    if clear_outputs:
        keys.extend(OUTPUT_METADATA_KEYS)
    return list(dict.fromkeys(keys))


async def _resolve_manager_tool_target(
    *,
    store: Any,
    task: Task | None,
    work_item_id: str,
    tool_name: str,
    allow_parent: bool = False,
) -> tuple[DelegationWorkItem, str, str, str]:
    if task is None or not _active_role(task):
        raise ValueError(f"{tool_name} requires an active assigned manager task")
    if store is None or not hasattr(store, "get_delegation_work_item"):
        raise RuntimeError(f"{tool_name} requires delegation persistence")
    run_id = str(task.metadata.get("delegation_run_id", "") or "").strip()
    manager_seat_id = _active_seat(task)
    parent_work_item_id, _attention_work_item_id = await _resolve_manager_board_parent_work_item_id(store, task)
    target_id = str(work_item_id or "").strip()
    if not run_id or not manager_seat_id or not parent_work_item_id:
        raise ValueError(f"{tool_name} requires runtime manager-board metadata")
    if not target_id:
        raise ValueError(f"{tool_name} requires work_item_id")
    item = await store.get_delegation_work_item(target_id)
    if item is None:
        raise ValueError(f"{tool_name}: no WorkItem matches work_item_id={target_id!r}")
    if str(getattr(item, "run_id", "") or "").strip() != run_id:
        raise ValueError(f"{tool_name}: target WorkItem belongs to a different run")
    if target_id == parent_work_item_id and allow_parent:
        return item, run_id, manager_seat_id, parent_work_item_id

    board_ids: set[str] = set()
    if hasattr(store, "list_manager_board"):
        board = await store.list_manager_board(
            run_id,
            manager_seat_id=manager_seat_id,
            parent_work_item_id=parent_work_item_id,
        )
        board_ids = {
            str(getattr(child, "work_item_id", "") or "").strip()
            for child in list(board or [])
            if str(getattr(child, "work_item_id", "") or "").strip()
        }
    if target_id not in board_ids:
        raise ValueError(
            f"{tool_name}: target WorkItem is not on this manager board. "
            "Call manager_board_read and pass a child work_item_id from that result."
        )
    return item, run_id, manager_seat_id, parent_work_item_id


async def _resolve_manager_tool_dependencies(
    *,
    store: Any,
    run_id: str,
    target_work_item_id: str,
    raw_dependencies: Any,
) -> tuple[list[str], list[dict[str, str]]]:
    dependency_specs = _normalize_dependency_specs(raw_dependencies)
    if not dependency_specs:
        return [], []
    work_items = (
        await store.list_delegation_work_items(run_id)
        if hasattr(store, "list_delegation_work_items")
        else []
    )
    work_item_ids: set[str] = set()
    scope_index: dict[str, list[str]] = {}
    role_index: dict[str, list[str]] = {}
    for item in list(work_items or []):
        if is_prunable_dependency_work_item(item):
            continue
        item_id = str(getattr(item, "work_item_id", "") or "").strip()
        if not item_id:
            continue
        work_item_ids.add(item_id)
        metadata = dict(getattr(item, "metadata", {}) or {})
        scope = str(metadata.get("scope_key", "") or "").strip()
        if scope:
            scope_index.setdefault(scope, []).append(item_id)
        role = str(getattr(item, "role_id", "") or "").strip()
        if role:
            role_index.setdefault(role, []).append(item_id)
    resolved = _resolve_dependency_specs(
        dependency_specs,
        ref_index={},
        work_item_ids=work_item_ids,
        scope_index=scope_index,
        role_index=role_index,
    )
    dependency_ids = [record["work_item_id"] for record in resolved]
    if str(target_work_item_id or "").strip() in dependency_ids:
        raise ValueError("modify_work_item cannot make a WorkItem depend on itself")
    return dependency_ids, resolved


async def _wake_rewritten_dependency_items(
    store: Any,
    *,
    rewritten_items: list[DelegationWorkItem],
) -> list[str]:
    if store is None or not hasattr(store, "update_delegation_work_item"):
        return []
    changed: list[str] = []
    for item in list(rewritten_items or []):
        metadata = dict(getattr(item, "metadata", {}) or {})
        dependency_ids = [
            str(dep).strip()
            for dep in list(metadata.get("dependency_work_item_ids", []) or [])
            if str(dep).strip()
        ]
        if dependency_ids:
            continue
        if getattr(item, "phase", None) not in {Phase.WAITING_DEPENDENCIES, Phase.WAITING_FOR_CHILDREN}:
            continue
        updated = await store.update_delegation_work_item(
            item.work_item_id,
            phase=Phase.READY,
            blocked_reason="",
            metadata_updates={
                "waiting_on_work_item_ids": [],
                "frontier": "resumed_after_work_item_dependency_rewrite",
            },
            claimed_by_role_runtime_session_id="",
            claimed_by_seat_id="",
        )
        if updated is not None:
            changed.append(updated.work_item_id)
    return changed


def _base_mailbox_metadata(
    task: Task | None,
    *,
    target_role_id: str = "",
    target_seat_id: str = "",
    target_team_instance_id: str = "",
    source_message_id: str = "",
    action_hint: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = {
        "work_item_id": _current_work_item_id(task),
        "parent_work_item_id": _current_parent_work_item_id(task),
        "source_message_id": str(source_message_id or "").strip(),
        "manager_role_id": str(task.metadata.get("manager_role_id", "") or "").strip() if task else "",
        "manager_seat_id": str(task.metadata.get("manager_seat_id", "") or "").strip() if task else "",
        "origin_team_instance_id": str(task.metadata.get("delegation_team_instance_id", "") or "").strip() if task else "",
        "target_team_instance_id": str(target_team_instance_id or "").strip(),
        "target_role_id": str(target_role_id or "").strip(),
        "target_seat_id": str(target_seat_id or "").strip(),
        "source_role_id": _active_role(task),
        "source_seat_id": _active_seat(task),
        "parent_board_scope": _parent_board_scope(task),
        "action_hint": str(action_hint or "").strip(),
    }
    if extra:
        metadata.update(dict(extra))
    return {
        key: value
        for key, value in metadata.items()
        if value not in (None, "", [])
    }


def _coerce_semantic_type(communication: Any, raw: str, *, fallback: CommsSemanticType) -> CommsSemanticType:
    if hasattr(communication, "_coerce_semantic_type"):
        return communication._coerce_semantic_type(raw, fallback=fallback)
    normalized = str(raw or "").strip().lower()
    for item in CommsSemanticType:
        if item.value == normalized:
            return item
    return fallback


def _serialize_board_item(item: DelegationWorkItem, *, include_full_summaries: bool = False) -> dict[str, Any]:
    metadata = dict(item.metadata or {})
    deliverable_summary = str(item.deliverable_summary or "").strip()
    deliverable_preview = clip_text(
        deliverable_summary,
        limit=1200,
        marker="deliverable summary preview truncated",
    )
    payload = {
        "work_item_id": str(item.work_item_id),
        "parent_work_item_id": str(item.parent_work_item_id or "").strip() or None,
        "role_id": str(item.role_id or "").strip(),
        "seat_id": str(item.seat_id or "").strip(),
        "title": str(item.title or "").strip(),
        "summary": str(item.summary or "").strip(),
        "kind": str(item.kind or "").strip(),
        "phase": item.phase.value,
        "kanban_column": kanban_column(item.phase),
        "deliverable_summary": deliverable_preview.text,
        "deliverable_summary_truncated": deliverable_preview.truncated,
        "deliverable_summary_chars": deliverable_preview.original_chars,
        "blocked_reason": str(item.blocked_reason or "").strip(),
        "review_owner_role_id": str(metadata.get("review_owner_role_id", "") or "").strip(),
        "review_owner_seat_id": str(metadata.get("review_owner_seat_id", "") or "").strip(),
        "review_target_work_item_id": str(metadata.get("review_target_work_item_id", "") or "").strip(),
        "scope_key": str(metadata.get("scope_key", "") or "").strip(),
        "completion_report": metadata.get("completion_report"),
        "review_evidence": dict(metadata.get("review_evidence", {}) or {}),
        "rework_feedback": str(metadata.get("rework_feedback", "") or "").strip(),
        "release_policy": str(metadata.get("release_policy", "auto") or "auto").strip(),
        "upstream_visibility": str(metadata.get("upstream_visibility", "summary_only") or "summary_only").strip(),
        "dispatch_hold": str(metadata.get("dispatch_hold", "") or "").strip(),
        "manager_mutation_action": str(metadata.get("manager_mutation_action", "") or "").strip(),
        "manager_mutation_revision": metadata.get("manager_mutation_revision"),
        "manager_mutation_reason": str(metadata.get("manager_mutation_reason", "") or "").strip(),
        "manager_mutation_user_input": str(metadata.get("manager_mutation_user_input", "") or "").strip(),
        "latest_user_directive": str(metadata.get("latest_user_directive", "") or "").strip(),
        "dependency_work_item_ids": [
            str(dep).strip()
            for dep in list(metadata.get("dependency_work_item_ids", []) or [])
            if str(dep).strip()
        ],
        "source_message_id": str(metadata.get("source_message_id", "") or "").strip(),
        "release_on_semantic_type": str(metadata.get("release_on_semantic_type", "") or "").strip(),
        "parent_board_scope": str(metadata.get("parent_board_scope", "") or "").strip(),
        "planning_context": str(metadata.get("planning_context", "") or "").strip(),
        "deliverables": _normalize_text_list(metadata.get("deliverables")),
        "acceptance_criteria": _normalize_text_list(metadata.get("acceptance_criteria")),
        "delegation_rationale": str(metadata.get("delegation_rationale", "") or "").strip(),
        "non_overlap_guard": str(metadata.get("non_overlap_guard", "") or "").strip(),
        "coordination_notes": str(metadata.get("coordination_notes", "") or "").strip(),
    }
    if include_full_summaries:
        payload["deliverable_summary_full"] = deliverable_summary
    return payload


_EXTERNAL_BRIDGE_ARGUMENT_EXAMPLES: dict[str, dict[str, Any]] = {
    "inbox": {
        "action": "status",
    },
    "delegate_work": {
        "planning_context": (
            "Dispatch planning summary: upstream goal, deliverable form, "
            "hard blockers, startable preparation, sequencing, assumptions."
        ),
        "items": [
            {
                "role_id": "target_role",
                "title": "Startable preparation slice",
                "task_brief": (
                    "Context: This child item supports the upstream goal and can start now. "
                    "Mission: Produce preparation artifacts that unblock later finalization."
                ),
                "brief": "UI/audit summary for the same child item.",
                "summary": (
                    "Backward-compatible alias for brief; UI/audit summary, not the prompt Task Brief."
                ),
                "scope_key": "target-role-prep",
                "work_kind": "execute",
                "outputs": ["Concrete preparation artifact or handoff that can start before dependencies finish"],
                "done_when": ["Preparation output is useful for the dependent finalization item"],
                "delegation_rationale": "Why this direct report owns this slice.",
                "non_overlap_guard": "This item owns preparation only; finalization is a separate sibling item.",
                "coordination_notes": "No hard dependency; record assumptions that finalization must revisit.",
                "depends_on": [],
            },
            {
                "role_id": "target_role",
                "title": "Dependency-bound finalization slice",
                "task_brief": (
                    "Context: This sibling item finalizes the same role's output after an upstream artifact lands. "
                    "Mission: Integrate the dependency into the final production deliverable."
                ),
                "brief": "UI/audit summary for the finalization item.",
                "scope_key": "target-role-finalize",
                "work_kind": "execute",
                "outputs": ["Final artifact that depends on an upstream scope"],
                "done_when": ["Final output incorporates the dependency and is ready to integrate"],
                "delegation_rationale": "Same role owns final quality for this slice.",
                "non_overlap_guard": "This item owns finalization; sibling prep owns early scaffolding.",
                "coordination_notes": "Use precise scope_key dependencies when a role has multiple sibling items.",
                "depends_on": ["target-role-prep"],
            }
        ],
    },
    "modify_work_item": {
        "work_item_id": "work-item-id-from-manager_board_read",
        "task_brief": "Revised concrete assignment for the same owner.",
        "deliverables": ["Updated artifact or handoff"],
        "acceptance_criteria": ["Updated acceptance condition"],
        "reason": "User changed the requirement / manager found the previous brief wrong.",
        "reset_to_ready": True,
    },
    "delete_work_item": {
        "work_item_id": "work-item-id-from-manager_board_read",
        "reason": "This child item is obsolete or wrong for the revised plan.",
        "replacement_dependency_work_item_ids": [],
    },
    "manager_board_read": {
        "include_children": True,
    },
    "close_human_review": {
        "summary": "The user accepted this delivery and no further internal work is required.",
        "user_message": "Acknowledged. I am closing the human review for this delivery.",
    },
    "submit_self_evolution_patches": {
        "patches": [
            {
                "summary": "One-paragraph lesson from the delivered work cycle.",
                "strengths": ["Concrete behavior worth repeating"],
                "adjustments": ["Concrete behavior to change next time"],
                "confidence": 0.8,
            }
        ],
    },
    "send_dm": {
        "to_agent": "reviewer",
        "subject": "Need review",
        "body": "Please review the draft.",
    },
}

_EXTERNAL_BRIDGE_ARGUMENT_NOTES: dict[str, tuple[str, ...]] = {
    "inbox": (
        "Use `action=status` for a lightweight unread count, `action=peek` to inspect actionable messages, and `action=ack` with `message_ids` after handling messages that do not need a reply.",
        "`reply_message` automatically acknowledges the original message; do not separately ack messages you reply to.",
        "If a manager-board review/approval already handled a matching inbox request, call `inbox` with `action=ack` for that message.",
    ),
    "delegate_work": (
        "Follow the Dispatch Planning Contract before calling `delegate_work`.",
        "Use top-level `planning_context` to summarize the upstream goal, requested deliverable form, hard blockers, startable preparation, sequencing, and assumptions.",
        "Distinguish hard dependencies from startable preparation before creating items.",
        "A single role may receive multiple items when that unlocks parallel progress or phase-specific handoffs; do not split mechanically.",
        "During a user follow-up over an existing board, do not create another active same-role child with a new scope unless the old sibling was deleted/replaced or `allow_parallel_same_role=true` is justified by `non_overlap_guard`.",
        "When the same role owns multiple sibling items, set stable `scope_key` values and use those scope keys in `depends_on` instead of the broad role name.",
        "Prefer per-item `task_brief` for the child prompt's Task Brief; keep outputs, acceptance, dependencies, and boundaries in their structured fields.",
        "`brief` and legacy `summary` are UI/audit summaries; they should not repeat structured outputs, acceptance, or boundaries.",
        "Fill per-item `outputs`/`deliverables`, `done_when`/`acceptance_criteria`, `delegation_rationale`, `non_overlap_guard`, and `coordination_notes`; use `depends_on` when sequencing matters.",
        "Dependencies must be expressed in structured `depends_on`; text in `brief`, `task_brief`, or `coordination_notes` is never treated as a dependency.",
        "Prefer stable sibling `scope_key` or `work_item_ref` values in `depends_on`; use raw work_item_id UUIDs only when copied from `manager_board_read` output.",
        "Do not invent item field names: any unsupported item field makes the whole `delegate_work` call fail before creating work items.",
        "`scope_key` is optional but idempotent on a manager board; reusing an existing scope_key returns the existing work item instead of creating a duplicate.",
        "`depends_on` may reference a sibling role_id, scope_key, work_item_ref, or work_item_id.",
        "Planning/research/checklist items are allowed as supporting work, but if the user asked for actual outputs, delegate actual production work too.",
    ),
    "modify_work_item": (
        "Use after `manager_board_read` when an existing child WorkItem is still the right owner but its instructions, deliverables, dependencies, or acceptance criteria must change.",
        "Pass the exact `work_item_id` from the board. Prefer changing the existing item over creating a duplicate replacement branch.",
        "Set `reset_to_ready=true` when previous running/review output is no longer valid and the worker should rerun from the revised brief.",
    ),
    "delete_work_item": (
        "Use after `manager_board_read` when an existing child WorkItem is obsolete, wrong, or should no longer block the parent.",
        "Pass the exact `work_item_id` from the board and a concrete reason. The tool cancels non-terminal items and hides terminal items from the active board.",
        "Use `replacement_dependency_work_item_ids` only when downstream items should depend on specific replacements; otherwise dependencies on the deleted item are removed.",
    ),
    "manager_board_read": (
        "Use only `parent_work_item_id` and optional `include_children`.",
        "For your current manager board, omit `parent_work_item_id`; attention/monitor turns automatically resolve to the underlying business parent board.",
        "Pass an explicit `parent_work_item_id` only when it is a full WorkItem id from prior tool output.",
        "Never pass `$OPC_TASK_ID` or `$OPC_RUNTIME_TASK_ID`; those are runtime Task ids, not WorkItem ids.",
        "Do NOT use legacy aliases such as `parent_id`, `include_child_outputs`, `include_outputs`, `include_artifacts`, `include_status`, `scope`, `reason`, or `note`.",
        "After handling a child approval/review through the board, acknowledge any matching inbox approval/review message unless `reply_message` already did it.",
    ),
    "close_human_review": (
        "Use only when you decide the owner-facing delivery review is complete and should be closed.",
        "Do not call this for requested changes; revise the board, delegate work, or respond instead.",
    ),
    "submit_self_evolution_patches": (
        "Available only on self-evolution work items; the recorded patches are the authoritative result of the turn.",
        "Pass `patches: []` when no experience update is needed; omit `employee_id` to target this work item's assigned employee.",
    ),
}


_EXTERNAL_CLI_KEY_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "delegate_work": ("items", "planning_context"),
    "modify_work_item": ("work_item_id", "task_brief", "brief", "deliverables", "acceptance_criteria", "depends_on", "reason"),
    "delete_work_item": ("work_item_id", "reason", "replacement_dependency_work_item_ids"),
    "inbox": ("action", "message_ids", "limit"),
    "manager_board_read": ("parent_work_item_id", "include_children"),
    "close_human_review": ("summary", "user_message"),
    "submit_self_evolution_patches": ("patches",),
    "send_dm": ("to_agent", "subject", "body", "blocking", "timeout_action", "timeout_seconds"),
    "reply_message": ("message_id", "body", "subject"),
    "broadcast_issue": ("to_agents", "subject", "body", "blocking", "timeout_action", "timeout_seconds"),
    "start_meeting": ("topic", "participants", "agenda", "decision_owner", "timeout_seconds"),
}


@lru_cache(maxsize=1)
def _external_cli_schema_map() -> dict[str, dict[str, Any]]:
    return {
        tool.name: dict(tool.parameters or {})
        for tool in create_collaboration_tools(SimpleNamespace())
    }


def build_external_cli_tool_contract_lines(
    tool_names: list[str] | set[str] | tuple[str, ...],
    *,
    include_json_examples: bool = False,
    compact: bool = True,
) -> list[str]:
    """Render per-tool argument contract lines for the ``opc-collab`` CLI.

    External prompts use the compact form by default: required/key optional
    arguments and a small number of high-signal notes. Full examples remain
    available for debug callers by passing ``include_json_examples=True`` and
    ``compact=False``.
    """
    normalized_tool_names = [
        str(tool_name).strip()
        for tool_name in sorted({str(tool_name).strip() for tool_name in tool_names if str(tool_name).strip()})
    ]
    if not normalized_tool_names:
        return []

    schema_map = _external_cli_schema_map()
    lines = [
        "Argument contract:",
        "- Use the exact argument names from the tool schema.",
        "- Do not rename keys or invent aliases from memory / prior sessions.",
    ]
    for tool_name in normalized_tool_names:
        schema = dict(schema_map.get(tool_name, {}) or {})
        properties = dict(schema.get("properties", {}) or {})
        required = {
            str(name).strip()
            for name in list(schema.get("required", []) or [])
            if str(name).strip()
        }
        if properties:
            key_names = _EXTERNAL_CLI_KEY_ARGUMENTS.get(tool_name)
            if compact and key_names:
                rendered_names = [name for name in key_names if name in properties]
            else:
                rendered_names = list(properties)
            ordered_keys = [
                f"`{name}`" + (" (required)" if name in required else "")
                for name in rendered_names
            ]
            lines.append(f"- `{tool_name}` arguments: {', '.join(ordered_keys)}")
        if tool_name == "delegate_work" and compact:
            lines.append(
                "- `delegate_work` item fields: `role_id` (required), `title` (required), "
                "recommended `task_brief`, `brief`/`summary` for UI, `scope_key`, `outputs`/`deliverables`, "
                "`done_when`/`acceptance_criteria`, `depends_on`, `coordination_notes`, "
                "`non_overlap_guard`, `allow_parallel_same_role`."
            )
        notes = _EXTERNAL_BRIDGE_ARGUMENT_NOTES.get(tool_name, ())
        if compact and not include_json_examples:
            if tool_name == "delegate_work":
                notes = (
                    "`task_brief` owns the child prompt's Task Brief; structured fields own deliverables, acceptance, dependencies, and boundaries.",
                    "Natural-language dependency wording is ignored; if an item must wait, set `depends_on` using a sibling `scope_key`/`work_item_ref`.",
                    "Unsupported item fields make the whole call fail atomically; use `depends_on`, not invented dependency key names.",
                    "`scope_key` is optional but idempotent on a manager board; reusing an existing scope_key returns the existing work item instead of creating a duplicate.",
                    "On follow-up boards, use `modify_work_item`/`delete_work_item` for existing same-role children before adding a new same-role scope.",
                )
            elif tool_name == "modify_work_item":
                notes = (
                    "Use exact child `work_item_id` from `manager_board_read`; revise existing wrong work instead of creating duplicates.",
                    "`reset_to_ready=true` invalidates stale output and lets the worker rerun the revised brief.",
                )
            elif tool_name == "delete_work_item":
                notes = (
                    "Use exact child `work_item_id` from `manager_board_read`; the item is cancelled or hidden from the active board.",
                    "Use replacements only when downstream dependencies should point to another work item.",
                )
            elif tool_name == "manager_board_read":
                notes = (
                    "For your current manager board, omit `parent_work_item_id`; attention/monitor turns automatically resolve to the underlying business parent board.",
                    "Do NOT use legacy aliases such as `parent_id`, `include_child_outputs`, `include_outputs`, `include_artifacts`, `include_status`, `scope`, `reason`, or `note`.",
                )
            elif tool_name == "close_human_review":
                notes = (
                    "Call only when the current owner-facing delivery review should be closed.",
                    "For requested changes, use board tools or a direct response instead of closing review.",
                )
            else:
                notes = notes[:2]
        for note in notes:
            lines.append(f"- `{tool_name}` note: {note}")
        if include_json_examples:
            example = _EXTERNAL_BRIDGE_ARGUMENT_EXAMPLES.get(tool_name)
            if example is not None:
                lines.append("```bash")
                lines.append("# write the JSON object to args.json, then run:")
                lines.append(f"opc-collab {tool_name} --args-json-file args.json")
                lines.append("```")
    return lines


def create_collaboration_tools(
    communication: Any,
    *,
    reorg_manager: Any | None = None,
    capability_manager: Any | None = None,
) -> list[ToolDefinition]:
    async def inbox(
        action: str = "status",
        message_ids: list[str] | None = None,
        limit: int = 10,
        task: Task | None = None,
    ) -> dict[str, Any]:
        role_id = _active_role(task)
        if not task or not role_id:
            raise ValueError("inbox requires an active assigned task")
        normalized_action = str(action or "status").strip().lower() or "status"
        if hasattr(communication, "inbox"):
            return await communication.inbox(
                agent_id=role_id,
                task=task,
                action=normalized_action,
                message_ids=list(message_ids or []),
                limit=limit,
            )
        if normalized_action in {"status", "peek"} and hasattr(communication, "read_inbox"):
            messages = await communication.read_inbox(
                agent_id=role_id,
                task=task,
                unread_only=True,
                limit=limit,
                mark_read=False,
            )
            return {
                "action": normalized_action,
                "unread_count": len(messages),
                "actionable_count": len(messages),
                "blocking_count": len([item for item in messages if bool(dict(item).get("reply_needed", False))]),
                "messages": list(messages) if normalized_action == "peek" else [],
            }
        raise RuntimeError("inbox support is not configured")

    async def send_dm(
        to_agent: str,
        subject: str,
        body: str,
        blocking: bool = False,
        timeout_action: str = "",
        timeout_seconds: int = 300,
        semantic_type: str = "",
        action_hint: str = "",
        source_message_id: str = "",
        task: Task | None = None,
    ) -> dict[str, Any]:
        role_id = _active_role(task)
        if not task or not role_id:
            raise ValueError("send_dm requires an active assigned task")
        target_seat = _resolve_target_seat(communication, task, to_agent)
        message = AgentMessage(
            msg_type="question",
            from_agent=role_id,
            to_agents=[to_agent],
            subject=subject,
            body=body,
            context_ref=task.id,
            task_id=task.id,
            urgency=MessageUrgency.NORMAL,
            reply_needed=blocking,
            semantic_type=_coerce_semantic_type(
                communication,
                semantic_type,
                fallback=CommsSemanticType.WORK_UPDATE,
            ),
            metadata={
                "async_mailbox": True,
                "reply_requested": bool(blocking),
                "timeout_action": timeout_action,
                "timeout_seconds": timeout_seconds,
                "execution_task_ids": list(task.metadata.get("execution_task_ids", [])),
                "from_seat_id": _active_seat(task),
                "target_seat_id": str(target_seat.get("seat_id", "") or "").strip(),
                "team_id": str(target_seat.get("team_id", "") or task.metadata.get("delegation_team_id", "") or "").strip(),
                **_base_mailbox_metadata(
                    task,
                    target_role_id=to_agent,
                    target_seat_id=str(target_seat.get("seat_id", "") or "").strip(),
                    target_team_instance_id=str(target_seat.get("team_instance_id", "") or "").strip(),
                    source_message_id=source_message_id,
                    action_hint=action_hint or ("await_reply" if blocking else "inform"),
                ),
            },
        )
        sent_message = await communication.send_dm(message, task=task)
        message = sent_message or message
        return {
            "message": communication._serialize_message(message),
            "delivery_mode": "async_mailbox",
            "blocking_deprecated": bool(blocking),
        }

    async def ask_peer_and_wait(
        to_agent: str,
        subject: str,
        body: str,
        timeout_action: str = "",
        timeout_seconds: int = 300,
        on_timeout: str = "continue",
        task: Task | None = None,
    ) -> dict[str, Any]:
        role_id = _active_role(task)
        if not task or not role_id:
            raise ValueError("ask_peer_and_wait requires an active assigned task")
        task.assigned_to = role_id
        task.metadata = dict(task.metadata)
        target_seat = _resolve_target_seat(communication, task, to_agent)
        task.metadata["pending_peer_target_seat_id"] = str(target_seat.get("seat_id", "") or "").strip()
        return await communication.ask_peer_and_wait(
            task=task,
            to_agent=to_agent,
            subject=subject,
            body=body,
            timeout_action=timeout_action,
            timeout_seconds=timeout_seconds,
            on_timeout=on_timeout,
        )

    async def reply_message(
        message_id: str,
        body: str,
        subject: str = "",
        task: Task | None = None,
    ) -> dict[str, Any]:
        role_id = _active_role(task)
        if not task or not role_id:
            raise ValueError("reply_message requires an active assigned task")
        reply = await communication.reply_to_message(
            original_msg_id=message_id,
            from_agent=role_id,
            body=body,
            subject=subject,
            task_id=task.id,
            metadata={
                "from_seat_id": _active_seat(task),
                "team_id": str(task.metadata.get("delegation_team_id", "") or "").strip(),
                **_base_mailbox_metadata(
                    task,
                    source_message_id=message_id,
                    action_hint="reply",
                ),
            },
        )
        return {"message": communication._serialize_message(reply)}

    async def broadcast_issue(
        to_agents: list[str],
        subject: str,
        body: str,
        blocking: bool = False,
        timeout_action: str = "",
        timeout_seconds: int = 300,
        semantic_type: str = "",
        action_hint: str = "",
        source_message_id: str = "",
        task: Task | None = None,
    ) -> dict[str, Any]:
        role_id = _active_role(task)
        if not task or not role_id:
            raise ValueError("broadcast_issue requires an active assigned task")
        message = AgentMessage(
            msg_type="flag_issue",
            from_agent=role_id,
            to_agents=list(to_agents),
            subject=subject,
            body=body,
            context_ref=task.id,
            task_id=task.id,
            urgency=MessageUrgency.HIGH,
            reply_needed=blocking,
            semantic_type=_coerce_semantic_type(
                communication,
                semantic_type,
                fallback=CommsSemanticType.WORK_UPDATE,
            ),
            metadata={
                "broadcast": True,
                "async_mailbox": True,
                "reply_requested": bool(blocking),
                "timeout_action": timeout_action,
                "timeout_seconds": timeout_seconds,
                "execution_task_ids": list(task.metadata.get("execution_task_ids", [])),
                "from_seat_id": _active_seat(task),
                "team_id": str(task.metadata.get("delegation_team_id", "") or "").strip(),
                **_base_mailbox_metadata(
                    task,
                    source_message_id=source_message_id,
                    action_hint=action_hint or "broadcast_issue",
                ),
            },
        )
        sent_message = await communication.broadcast(message)
        message = sent_message or message
        return {
            "message": communication._serialize_message(message),
            "delivery_mode": "async_mailbox",
            "blocking_deprecated": bool(blocking),
        }

    async def start_meeting(
        topic: str,
        participants: list[str],
        agenda: list[str],
        shared_context: str = "",
        decision_owner: str = "",
        decision_policy: str = "semantic_consensus_then_owner",
        timeout_seconds: int = 900,
        risk_level: str = "normal",
        task: Task | None = None,
    ) -> dict[str, Any]:
        role_id = _active_role(task)
        if not task or not role_id:
            raise ValueError("start_meeting requires an active assigned task")
        task.assigned_to = role_id
        return await communication.open_meeting_wait(
            task=task,
            topic=topic,
            participants=participants,
            agenda=agenda,
            shared_context=shared_context,
            decision_owner=decision_owner or None,
            decision_policy=decision_policy,
            timeout_seconds=timeout_seconds,
            risk_level=risk_level,
        )

    async def respond_meeting(
        room_id: str,
        content: str,
        finalize: bool = False,
        task: Task | None = None,
    ) -> dict[str, Any]:
        role_id = _active_role(task)
        if not task or not role_id:
            raise ValueError("respond_meeting requires an active assigned task")
        meeting = await communication.respond_to_meeting(
            room_id=room_id,
            from_agent=role_id,
            content=content,
            finalize=finalize,
            task=task,
        )
        return {
            "meeting": {
                "room_id": meeting.room_id,
                "status": meeting.status.value,
                "outcome": meeting.outcome,
            }
        }

    async def propose_task_adjustment(
        summary: str,
        changeset: dict[str, Any],
        task: Task | None = None,
    ) -> dict[str, Any]:
        role_id = _active_role(task)
        if not task or not role_id:
            raise ValueError("propose_task_adjustment requires an active assigned task")
        if hasattr(communication, "propose_task_adjustment"):
            return await communication.propose_task_adjustment(
                task=task,
                summary=summary,
                changeset=changeset,
            )
        if reorg_manager is None:
            raise RuntimeError("Runtime replan support is not configured")
        result = await reorg_manager.suggest_task_adjustment(
            project_id=task.project_id,
            source_role_id=role_id,
            summary=summary,
            changeset=changeset,
            session_id=task.parent_session_id or task.session_id,
            task_id=task.id,
        )
        proposal = result["proposal"]
        if result.get("auto_applied"):
            task.metadata = dict(task.metadata)
            task.metadata["reorg_proposal_id"] = proposal.proposal_id
            task.metadata.pop("pending_reorg_proposal_id", None)
            task.metadata.pop("pending_reorg_scope", None)
            return {
                "proposal_id": proposal.proposal_id,
                "scope": proposal.scope.value,
                "status": proposal.status.value,
                "auto_applied": True,
                "result": result.get("result", {}),
            }
        task.metadata = dict(task.metadata)
        task.metadata["pending_reorg_proposal_id"] = proposal.proposal_id
        task.metadata["pending_reorg_scope"] = proposal.scope.value
        return {
            "proposal_id": proposal.proposal_id,
            "scope": proposal.scope.value,
            "status": proposal.status.value,
            "auto_applied": False,
            "requires_user_input": True,
            "reason": (
                f"Proposed runtime adjustment `{proposal.proposal_id}`. "
                "Review the replan details and reply `approve` or `deny` to continue."
            ),
        }

    # ------------------------------------------------------------------
    # route_work – coordinator-oriented work routing
    # ------------------------------------------------------------------

    async def route_work(
        action: str,
        target_role: str = "",
        prompt: str = "",
        priority: str = "normal",
        task: Task | None = None,
    ) -> dict[str, Any]:
        role_id = _active_role(task)
        if not task or not role_id:
            raise ValueError("route_work requires an active assigned task")
        if action == "send_followup":
            if not target_role:
                return {"error": "target_role required for send_followup"}
            msg = AgentMessage(
                msg_type="request_review",
                from_agent=role_id,
                to_agents=[target_role],
                subject="Coordinator follow-up",
                body=prompt,
                context_ref=task.id,
                task_id=task.id,
                urgency=MessageUrgency.HIGH,
                reply_needed=False,
                metadata={"coordinator_routed": True},
            )
            msg = await communication.send_dm(msg, task=task)
            return {"action": "send_followup", "routed_to": target_role, "msg_id": msg.msg_id}
        elif action == "spawn_task":
            spawns = list(task.metadata.get("coordinator_spawn_requests", []))
            spawns.append({"target_role": target_role, "prompt": prompt, "priority": priority})
            task.metadata = dict(task.metadata)
            task.metadata["coordinator_spawn_requests"] = spawns
            return {"action": "spawn_task", "queued": True, "target_role": target_role}
        elif action == "escalate":
            manager = task.metadata.get("reports_to", "owner")
            msg = AgentMessage(
                msg_type="flag_issue",
                from_agent=role_id,
                to_agents=[manager],
                subject="Escalation from coordinator",
                body=prompt,
                context_ref=task.id,
                task_id=task.id,
                urgency=MessageUrgency.HIGH,
                reply_needed=True,
                metadata={"escalation": True},
            )
            msg = await communication.send_dm(msg, task=task)
            return {"action": "escalate", "routed_to": manager, "msg_id": msg.msg_id}
        return {"error": f"unknown action: {action}. Valid actions: send_followup, spawn_task, escalate."}

    async def close_human_review(
        summary: str,
        user_message: str = "",
        task: Task | None = None,
    ) -> dict[str, Any]:
        role_id = _active_role(task)
        if not task or not role_id:
            raise ValueError("close_human_review requires an active assigned task")
        store = getattr(communication, "store", None)
        if store is None or not hasattr(store, "save_task"):
            raise RuntimeError("close_human_review requires task persistence")
        task.metadata = dict(task.metadata or {})
        task.context_snapshot = dict(task.context_snapshot or {})
        if not (
            bool(task.metadata.get("human_review_close_allowed", False))
            or str(task.metadata.get("human_review_checkpoint_type", "") or "").strip() == "company_delivery_feedback"
            or bool(task.context_snapshot.get("human_review_close_allowed", False))
        ):
            raise ValueError("close_human_review is only available during an owner-facing delivery review follow-up")

        review_task_id = str(task.metadata.get("human_review_task_id", "") or task.id).strip()
        review_task = task
        if review_task_id and review_task_id != task.id and hasattr(store, "get_task"):
            loaded = await store.get_task(review_task_id)
            if loaded is not None:
                review_task = loaded
        review_task.metadata = dict(review_task.metadata or {})
        review_task.context_snapshot = dict(review_task.context_snapshot or {})
        now = datetime.now().isoformat()
        close_summary = str(summary or "").strip()
        close_user_message = str(user_message or "").strip()
        progress_note = (
            f"Human review closed by {role_id}: {close_summary}"
            if close_summary
            else f"Human review closed by {role_id}."
        )
        progress = list(review_task.metadata.get("progress_log", []) or [])
        progress.append(progress_note)
        review_updates = {
            "feedback_closed": True,
            "feedback_resolved": True,
            "feedback_resolution": "accepted_by_final_decider",
            "feedback_closed_at": now,
            "feedback_closed_by_role": role_id,
            "feedback_close_summary": close_summary,
            "feedback_close_user_message": close_user_message,
            "requires_user_feedback": False,
            "human_review_closed": True,
            "human_review_closed_at": now,
            "human_review_closed_by_role": role_id,
            "progress_log": progress[-50:],
        }
        review_task.metadata.update(review_updates)
        review_task.status = TaskStatus.DONE
        await store.save_task(review_task)

        if review_task.id != task.id:
            task_progress = list(task.metadata.get("progress_log", []) or [])
            task_progress.append(progress_note)
            task.metadata.update({
                "human_review_closed": True,
                "human_review_closed_at": now,
                "human_review_closed_by_role": role_id,
                "manager_no_delegation_justification": "Owner-facing human review was closed by the final decider.",
                "progress_log": task_progress[-50:],
            })
            await store.save_task(task)
        else:
            task.metadata["manager_no_delegation_justification"] = "Owner-facing human review was closed by the final decider."
            await store.save_task(task)

        review_work_item_id = str(
            task.metadata.get("human_review_work_item_id", "")
            or linked_work_item_id_for_task(review_task)
            or linked_work_item_id_for_task(task)
            or ""
        ).strip()
        if review_work_item_id and hasattr(store, "update_delegation_work_item"):
            work_item_metadata_updates = dict(review_updates)
            work_item_metadata_updates.update({
                "task_status": TaskStatus.DONE.value,
                "last_transition_reason": "human_review_closed_by_final_decider",
            })
            existing_item = None
            if hasattr(store, "get_delegation_work_item"):
                try:
                    existing_item = await store.get_delegation_work_item(review_work_item_id)
                except Exception:
                    existing_item = None
                if existing_item is not None:
                    existing_progress = list((getattr(existing_item, "metadata", {}) or {}).get("progress_log", []) or [])
                    existing_progress.append(progress_note)
                    work_item_metadata_updates["progress_log"] = existing_progress[-50:]
            try:
                await store.update_delegation_work_item(
                    review_work_item_id,
                    phase=Phase.APPROVED,
                    blocked_reason="",
                    metadata_updates=work_item_metadata_updates,
                )
            except InvalidPhaseTransition:
                current_phase = getattr(existing_item, "phase", None)
                if current_phase in {Phase.READY, Phase.READY_FOR_REWORK}:
                    await store.update_delegation_work_item(
                        review_work_item_id,
                        phase=Phase.RUNNING,
                        blocked_reason="",
                        metadata_updates=work_item_metadata_updates,
                    )
                    await store.update_delegation_work_item(
                        review_work_item_id,
                        phase=Phase.APPROVED,
                        blocked_reason="",
                        metadata_updates=work_item_metadata_updates,
                    )
                else:
                    await store.update_delegation_work_item(
                        review_work_item_id,
                        metadata_updates=work_item_metadata_updates,
                    )
            run_id = str(
                getattr(existing_item, "run_id", "")
                or review_task.metadata.get("delegation_run_id", "")
                or task.metadata.get("delegation_run_id", "")
                or ""
            ).strip()
            if run_id and hasattr(store, "get_delegation_run") and hasattr(store, "save_delegation_run"):
                try:
                    run = await store.get_delegation_run(run_id)
                except Exception:
                    run = None
                if run is not None:
                    run.lifecycle_status = "delivered"
                    run.status = "completed"
                    if close_summary:
                        run.latest_deliverable_summary = close_summary
                    run.metadata = {
                        **dict(run.metadata or {}),
                        "awaiting_owner_review": False,
                        "owner_review_closed": True,
                        "owner_review_closed_at": now,
                        "owner_review_closed_by_role": role_id,
                        "owner_review_close_summary": close_summary,
                    }
                    await store.save_delegation_run(run)

        closed_checkpoint_ids: list[str] = []
        checkpoint_id = str(task.metadata.get("human_review_checkpoint_id", "") or "").strip()
        if hasattr(store, "get_pending_checkpoints"):
            try:
                pending_checkpoints = await store.get_pending_checkpoints(
                    project_id=task.project_id,
                    checkpoint_types=["company_delivery_feedback"],
                )
            except TypeError:
                pending_checkpoints = await store.get_pending_checkpoints(task.project_id)
            for checkpoint in list(pending_checkpoints or []):
                payload = dict(getattr(checkpoint, "payload", {}) or {})
                payload_task_id = str(
                    payload.get("waiting_task_id")
                    or payload.get("task_id")
                    or getattr(checkpoint, "task_id", "")
                    or ""
                ).strip()
                payload_task_ids = {
                    str(item).strip()
                    for item in list(payload.get("task_ids", []) or [])
                    if str(item).strip()
                }
                matches = (
                    bool(checkpoint_id and checkpoint.checkpoint_id == checkpoint_id)
                    or bool(review_task_id and payload_task_id == review_task_id)
                    or bool(review_task_id and review_task_id in payload_task_ids)
                    or bool(review_work_item_id and str(payload.get("work_item_id", "") or "").strip() == review_work_item_id)
                )
                if not matches:
                    continue
                payload.update({
                    "human_review_closed": True,
                    "human_review_closed_at": now,
                    "human_review_closed_by_role": role_id,
                    "feedback_resolution": "accepted_by_final_decider",
                    "feedback_close_summary": close_summary,
                    "feedback_close_user_message": close_user_message,
                })
                checkpoint.payload = payload
                checkpoint.status = "resolved"
                checkpoint.updated_at = datetime.now()
                if hasattr(store, "save_execution_checkpoint"):
                    await store.save_execution_checkpoint(checkpoint)
                elif hasattr(store, "resolve_execution_checkpoint"):
                    await store.resolve_execution_checkpoint(checkpoint.checkpoint_id, status="resolved")
                closed_checkpoint_ids.append(checkpoint.checkpoint_id)

        return {
            "action": "close_human_review",
            "closed": True,
            "task_id": review_task.id,
            "work_item_id": review_work_item_id,
            "closed_checkpoint_ids": closed_checkpoint_ids,
            "summary": close_summary,
            "user_message": close_user_message,
        }

    async def submit_self_evolution_patches(
        patches: list[dict[str, Any]] | None = None,
        task: Task | None = None,
    ) -> dict[str, Any]:
        role_id = _active_role(task)
        if not task or not role_id:
            raise ValueError("submit_self_evolution_patches requires an active assigned task")
        if not is_self_evolution_work_item(task):
            raise ValueError(
                "submit_self_evolution_patches is only available on self-evolution work items"
            )
        normalized: list[dict[str, Any]] = []
        for index, patch in enumerate(list(patches or [])):
            if not isinstance(patch, dict):
                raise ValueError(f"patches[{index}] must be a JSON object")
            employee_id = str(patch.get("employee_id", "") or "").strip()
            if not employee_id:
                assignment = dict(task.metadata.get("employee_assignment", {}) or {})
                employee_id = str(assignment.get("employee_id", "") or "").strip()
            if not employee_id:
                raise ValueError(
                    f"patches[{index}] needs an employee_id and this work item has no assigned employee"
                )
            normalized.append({**patch, "employee_id": employee_id})
        task.metadata = dict(task.metadata or {})
        task.context_snapshot = dict(task.context_snapshot or {})
        record = {
            "patches": normalized,
            "submitted_at": datetime.now().isoformat(),
            "submitted_by_role": role_id,
        }
        task.metadata["self_evolution_submitted_patch"] = record
        task.context_snapshot["self_evolution_submitted_patch"] = dict(record)
        store = getattr(communication, "store", None)
        if store is not None and hasattr(store, "save_task"):
            await store.save_task(task)
        return {
            "status": "recorded",
            "patch_count": len(normalized),
            "note": (
                "Patches recorded for this self-evolution work item. "
                "Finish the turn with a short completion note."
            ),
        }

    async def delegate_work(
        items: list[dict[str, Any]],
        planning_context: str = "",
        task: Task | None = None,
    ) -> dict[str, Any]:
        role_id = _active_role(task)
        if not task or not role_id:
            raise ValueError("delegate_work requires an active assigned task")
        store = getattr(communication, "store", None)
        if store is None or not hasattr(store, "save_delegation_work_item"):
            raise RuntimeError("delegate_work requires delegation persistence")
        run_id = str(task.metadata.get("delegation_run_id", "") or "").strip()
        parent_work_item_id, attention_work_item_id = await _resolve_manager_board_parent_work_item_id(store, task)
        seat_id = _active_seat(task)
        if not run_id or not parent_work_item_id or not seat_id:
            raise ValueError("delegate_work requires runtime team work-item task metadata")
        _validate_delegate_work_items(items)
        structured_dispatch_required = _requires_structured_dispatch_plan(task)
        normalized_planning_context = str(planning_context or "").strip()
        latest_user_directive = _user_supplied_input_from_task(task)
        latest_user_directive_preview = (
            clip_text(latest_user_directive, limit=1600, marker="latest user directive truncated").text
            if latest_user_directive
            else ""
        )
        output_contract = output_contract_metadata(task.metadata or {})
        output_contract_context = render_output_contract_context(
            output_contract,
            heading="Runtime output contract for delegated work:",
        )
        if output_contract_context and output_contract_context not in normalized_planning_context:
            normalized_planning_context = (
                f"{output_contract_context}\n\n{normalized_planning_context}"
                if normalized_planning_context
                else output_contract_context
            )
        if structured_dispatch_required and not normalized_planning_context:
            # Auto-fill a minimal context so delegation can proceed.  The LLM
            # schema marks planning_context optional but dispatch mode needs it;
            # blocking the whole call is worse than proceeding with a sparse
            # context since the work items still carry deliverables/summaries.
            normalized_planning_context = (
                f"Auto-generated dispatch context: {role_id} initiating delegation "
                f"for parent work item {parent_work_item_id}."
            )
            logger.warning(
                "delegate_work: planning_context was empty in dispatch_required mode — "
                "auto-filled a placeholder. The calling agent should provide an explicit planning_context."
            )
        runtime_topology = dict(task.metadata.get("runtime_topology", {}) or {})
        playbook = dict(task.metadata.get("delegation_playbook", {}) or {})
        direct_report_role_ids = {
            str(item).strip()
            for item in list(task.metadata.get("direct_report_role_ids", []) or [])
            if str(item).strip()
        }
        allowed_delegate_role_ids = {
            str(item).strip()
            for item in list(task.metadata.get("allowed_delegate_role_ids", []) or [])
            if str(item).strip()
        }
        existing_parent = await store.get_delegation_work_item(parent_work_item_id) if hasattr(store, "get_delegation_work_item") else None
        # Simplified session model: every child work item files under the
        # target role's EXISTING team_instance_id (seeded by bootstrap,
        # shared across the entire run). A role has exactly one
        # ``role_runtime_session`` — new tasks join its queue; if it's idle
        # they run, otherwise they wait. No per-parent-work-item dynamic
        # team instances, no sub-sessions.
        #
        # The removed code created a fresh ``team-instance::{run_id}::{seat_id}::{parent_work_item_id}``
        # per delegate_work call and saved a TeamInstance record for it.
        # That split where work items were filed from where role sessions
        # actually listened — the dispatcher had nothing to claim even
        # though idle sessions existed. Filing under the seat's existing
        # team_instance_id is enough; the topology already records who's
        # in which team, and bootstrap already seeded every seat's
        # ``role_runtime_session``.
        existing_dependencies = [
            str(item).strip()
            for item in list((getattr(existing_parent, "metadata", {}) or {}).get("dependency_work_item_ids", []) or [])
            if str(item).strip()
        ]
        all_run_work_items = (
            await store.list_delegation_work_items(run_id)
            if hasattr(store, "list_delegation_work_items")
            else []
        )
        work_item_by_id = {
            str(getattr(item, "work_item_id", "") or "").strip(): item
            for item in list(all_run_work_items or [])
            if str(getattr(item, "work_item_id", "") or "").strip()
        }
        existing_dependencies, pruned_existing_dependencies = normalize_dependency_work_item_ids(
            existing_dependencies,
            work_item_by_id,
            owner_work_item_id=parent_work_item_id,
        )
        existing_scope_keys: set[str] = set()
        existing_scope_aliases: set[str] = set()
        existing_children: list[Any] = []
        existing_child_by_scope_alias: dict[str, list[Any]] = {}
        if hasattr(store, "list_manager_board"):
            existing_children = await store.list_manager_board(
                run_id,
                manager_seat_id=seat_id,
                parent_work_item_id=parent_work_item_id,
            )
            for child in existing_children:
                if is_prunable_dependency_work_item(child):
                    continue
                child_scope = str((child.metadata or {}).get("scope_key", "") or "").strip()
                if not child_scope:
                    continue
                existing_scope_keys.add(child_scope)
                aliases = _scope_key_aliases(child_scope)
                existing_scope_aliases.update(aliases)
                for alias in aliases:
                    existing_child_by_scope_alias.setdefault(alias, []).append(child)
        ref_to_work_item_id: dict[str, str] = {}
        pending_items: list[tuple[DelegationWorkItem, list[dict[str, str]], bool]] = []
        delegated_payloads: list[dict[str, Any]] = []
        pending_scope_keys: set[str] = set()
        pending_scope_aliases: set[str] = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            target_role = str(item.get("role_id", "") or "").strip()
            if not target_role:
                raise ValueError("delegate_work items require role_id")
            if direct_report_role_ids or allowed_delegate_role_ids:
                if target_role not in direct_report_role_ids and target_role not in allowed_delegate_role_ids:
                    raise ValueError(f"delegate_work may only target direct reports; `{target_role}` is not allowed")
            target_seat = _resolve_target_seat(communication, task, target_role)
            target_seat_id = str(target_seat.get("seat_id", "") or "").strip()
            target_team_id = str(
                target_seat.get("team_id", "")
                or item.get("team_id", "")
                or task.metadata.get("delegation_team_id", "")
                or ""
            ).strip()
            # The target role's canonical team_instance_id from bootstrap.
            # New work items file here so they land in the same dispatcher
            # queue that owns the role's single ``role_runtime_session``.
            #
            # ``target_seat`` comes from ``task.metadata.runtime_topology``;
            # that snapshot often omits ``team_instance_id`` because it is
            # stamped during bootstrap. Reconstruct the exact bootstrap
            # formula from ``run_id + team_id`` so delegated work lands in
            # the queue that the target role session already watches.
            target_team_instance_id = str(
                target_seat.get("team_instance_id", "") or ""
            ).strip()
            if not target_team_instance_id and run_id and target_team_id:
                target_team_instance_id = f"team-instance::{run_id}::{target_team_id}"
            # Fix 2: canonical fallback includes team_instance_id slot so
            # delegate_work never spawns a duplicate DB row for the target
            # role. Prior fallback used seat_id/role_id interchangeably,
            # creating the seat-scoped short form that diverged from the
            # bootstrap-generated team-instance-scoped form.
            role_runtime_session_id = (
                str(target_seat.get("role_runtime_session_id", "") or "").strip()
                or (
                    canonical_role_session_id(
                        run_id=run_id,
                        role_id=target_role,
                        team_instance_id=target_team_instance_id,
                    )
                    if target_role
                    else ""
                )
            )
            requested_work_kind = str(item.get("work_kind", "") or item.get("kind", "") or "").strip().lower()
            work_kind = requested_work_kind or "execute"
            if work_kind in {"self-evolution", "self evolution"}:
                work_kind = "self_evolution"
            prompt_task_brief = str(
                item.get("task_brief", "")
                or item.get("prompt_brief", "")
                or ""
            ).strip()
            summary = str(
                item.get("brief", "")
                or item.get("summary", "")
                or item.get("body", "")
                or item.get("prompt", "")
                or prompt_task_brief
                or ""
            ).strip()
            summary = str(replace_output_aliases(summary, output_contract) or "").strip()
            if output_contract_context and output_contract_context not in summary:
                summary = f"{output_contract_context}\n\n{summary}".strip()
            prompt_task_brief = str(replace_output_aliases(prompt_task_brief, output_contract) or "").strip()
            if not prompt_task_brief:
                prompt_task_brief = summary
            elif output_contract_context and output_contract_context not in prompt_task_brief:
                prompt_task_brief = f"{output_contract_context}\n\n{prompt_task_brief}".strip()
            title = str(item.get("title", "") or item.get("name", "") or f"{target_role} {work_kind}").strip()
            deliverables = _normalize_text_list(
                replace_output_aliases(item.get("deliverables", item.get("outputs")), output_contract)
            )
            acceptance_criteria = _normalize_text_list(
                replace_output_aliases(item.get("acceptance_criteria", item.get("done_when")), output_contract)
            )
            delegation_rationale = str(item.get("delegation_rationale", "") or "").strip()
            non_overlap_guard = str(replace_output_aliases(item.get("non_overlap_guard", ""), output_contract) or "").strip()
            coordination_notes = str(replace_output_aliases(item.get("coordination_notes", ""), output_contract) or "").strip()
            if structured_dispatch_required:
                missing_fields: list[str] = []
                if not deliverables:
                    missing_fields.append("deliverables")
                if not acceptance_criteria:
                    missing_fields.append("acceptance_criteria")
                if not delegation_rationale:
                    missing_fields.append("delegation_rationale")
                if not non_overlap_guard:
                    missing_fields.append("non_overlap_guard")
                if missing_fields:
                    # Log as warning instead of raising — blocking the entire
                    # delegation because the LLM omitted optional-in-schema
                    # fields is worse than proceeding with sparse metadata.
                    logger.warning(
                        "delegate_work: dispatch_required item `{}` is missing {} — "
                        "proceeding anyway, but quality of downstream briefs may suffer.",
                        title or target_role,
                        ", ".join(missing_fields),
                    )
            requested_scope_key = str(item.get("scope_key", "") or "").strip()
            ref_key = str(item.get("work_item_ref", "") or item.get("id", "") or "").strip() or f"item_{index + 1}"
            if requested_scope_key:
                existing_matches = _lookup_scope_aliases(existing_child_by_scope_alias, requested_scope_key)
            else:
                existing_matches = []
            if requested_scope_key and len(existing_matches) > 1:
                match_ids = [
                    str(getattr(existing_item, "work_item_id", "") or "").strip()
                    for existing_item in existing_matches
                    if str(getattr(existing_item, "work_item_id", "") or "").strip()
                ]
                raise ValueError(
                    "delegate_work scope_key "
                    f"`{requested_scope_key}` matched multiple existing work items via aliases: "
                    + ", ".join(match_ids)
                    + ". Use an exact work_item_id to disambiguate."
                )
            if requested_scope_key and len(existing_matches) == 1:
                existing_item = existing_matches[0]
                ref_to_work_item_id[ref_key] = str(getattr(existing_item, "work_item_id", "") or "").strip()
                reused_payload = _serialize_board_item(existing_item)
                reused_payload.update({
                    "reused": True,
                    "generated_scope_key": False,
                    "manager_outcome_dispatch": bool(
                        (getattr(existing_item, "metadata", {}) or {}).get("manager_outcome_dispatch", False)
                    ),
                })
                delegated_payloads.append(reused_payload)
                continue
            requested_aliases = set(_scope_key_aliases(requested_scope_key))
            if requested_scope_key and requested_aliases & pending_scope_aliases:
                raise ValueError(
                    "delegate_work received duplicate scope_key "
                    f"`{requested_scope_key}` in the same call; scope_key must be stable and unique per manager board."
                )
            allow_parallel_same_role = bool(item.get("allow_parallel_same_role", False))
            if latest_user_directive_preview and allow_parallel_same_role and not non_overlap_guard:
                raise ValueError(
                    "delegate_work item set allow_parallel_same_role=true during a user follow-up, "
                    "but did not provide non_overlap_guard explaining why same-role parallel work is intentional."
                )
            if latest_user_directive_preview and not allow_parallel_same_role:
                same_role_existing = []
                for child in existing_children:
                    child_role = str(getattr(child, "role_id", "") or "").strip()
                    if child_role != target_role:
                        continue
                    if _is_hidden_work_item(child):
                        continue
                    child_phase = getattr(child, "phase", None)
                    if child_phase in {Phase.FAILED, Phase.CANCELLED}:
                        continue
                    child_meta = dict(getattr(child, "metadata", {}) or {})
                    child_scope = str(child_meta.get("scope_key", "") or "").strip()
                    same_role_existing.append(
                        {
                            "work_item_id": str(getattr(child, "work_item_id", "") or "").strip(),
                            "title": str(getattr(child, "title", "") or "").strip(),
                            "phase": child_phase.value if isinstance(child_phase, Phase) else str(child_phase or ""),
                            "scope_key": child_scope,
                        }
                    )
                if same_role_existing:
                    refs = ", ".join(
                        f"{entry['work_item_id']} scope={entry['scope_key'] or '(none)'} phase={entry['phase']}"
                        for entry in same_role_existing[:5]
                        if entry["work_item_id"]
                    )
                    raise ValueError(
                        "delegate_work would create another active child for role "
                        f"`{target_role}` on a user follow-up board with a new scope_key `{requested_scope_key or '(generated)'}`. "
                        f"Existing same-role child item(s): {refs}. "
                        "Use `modify_work_item` to revise the existing child, `delete_work_item` to remove obsolete siblings before creating a replacement, "
                        "or set `allow_parallel_same_role=true` with a concrete `non_overlap_guard` only when the new same-role work is intentionally parallel."
                    )
            scope_source = requested_scope_key or f"{target_role}-{title}"
            scope_key, generated_scope_key = _unique_scope_key(
                scope_source,
                existing_scope_keys=existing_scope_aliases or existing_scope_keys,
                pending_scope_keys=pending_scope_keys,
            )
            pending_scope_keys.add(scope_key)
            pending_scope_aliases.update(_scope_key_aliases(scope_key))
            pending_scope_aliases.update(requested_aliases)
            release_policy = str(item.get("release_policy", "") or "auto").strip().lower() or "auto"
            upstream_visibility = str(item.get("upstream_visibility", "") or "summary_only").strip().lower() or "summary_only"
            source_message_id = str(item.get("source_message_id", "") or "").strip()
            release_on_semantic_type = str(item.get("release_on_semantic_type", "") or "").strip().lower()
            batch_id = str(
                item.get("batch_id", "")
                or task.metadata.get("work_item_batch_id", "")
                or f"batch::{parent_work_item_id or run_id}"
            ).strip()
            dependency_specs = _normalize_dependency_specs(
                item.get("depends_on", item.get("dependency_work_item_ids", []))
            )
            parent_prompt_contract = dict(task.metadata.get("prompt_contract", {}) or {})
            parent_assignment_context = dict(parent_prompt_contract.get("assignment_context", {}) or {})
            parent_prompt_assignment = dict(task.metadata.get("prompt_assignment", {}) or {})
            upstream_intent_summary = str(
                item.get("upstream_intent_summary", "")
                or parent_assignment_context.get("upstream_intent_summary", "")
                or parent_prompt_assignment.get("upstream_intent_summary", "")
                or task.metadata.get("global_intent_summary", "")
                or playbook.get("global_intent_summary", "")
                or playbook.get("intent_summary", "")
                or ""
            ).strip()
            if latest_user_directive_preview:
                directive_header = (
                    "Latest user directive (authoritative; supersedes conflicting older request details): "
                    f"{latest_user_directive_preview}"
                )
                if upstream_intent_summary and latest_user_directive_preview not in upstream_intent_summary:
                    upstream_intent_summary = (
                        f"{directive_header}\n\nBackground intent before/latest alongside this directive: "
                        f"{upstream_intent_summary}"
                    )
                elif not upstream_intent_summary:
                    upstream_intent_summary = directive_header
            initial_phase = (
                Phase.QUEUED
                if release_policy != "auto"
                else Phase.WAITING_DEPENDENCIES
                if dependency_specs
                else Phase.READY
            )
            target_delegate_roles = [
                str(role).strip()
                for role in list(target_seat.get("allowed_delegate_role_ids", []) or [])
                if str(role).strip()
            ]
            target_direct_reports = [
                str(role).strip()
                for role in list(target_seat.get("direct_report_role_ids", []) or [])
                if str(role).strip()
            ]
            target_is_manager = bool(
                target_delegate_roles
                or target_direct_reports
                or str(target_seat.get("managed_team_id", "") or "").strip()
            )
            manager_outcome_dispatch = target_is_manager and work_kind == "execute"
            inherited_self_evolution: dict[str, Any] = {}
            if work_kind == "self_evolution" or bool((task.metadata or {}).get("self_evolution_work_item", False)):
                inherited_self_evolution = {
                    key: copy.deepcopy((task.metadata or {}).get(key))
                    for key in (
                        "self_evolution_checkpoint_id",
                        "self_evolution_human_action",
                        "self_evolution_human_feedback",
                        "self_evolution_delivery_task_id",
                        "self_evolution_delivery_projection_id",
                        "self_evolution_delivery_summary",
                        "organization_id",
                        "org_id",
                        "work_item_tasks",
                        "org_graph",
                    )
                    if (task.metadata or {}).get(key) not in (None, "", [], {})
                }
                inherited_self_evolution.update({
                    "self_evolution_work_item": True,
                    "self_evolution_patch_max_retries": int((task.metadata or {}).get("self_evolution_patch_max_retries", 3) or 3),
                })
            prompt_contract = prompt_contract_from_delegate_item(
                item,
                task_brief=prompt_task_brief,
                upstream_intent_summary=upstream_intent_summary,
                manager_planning_handoff=normalized_planning_context if manager_outcome_dispatch else "",
                manager_outcome_dispatch=manager_outcome_dispatch,
                owned_outcome_kind="execute" if manager_outcome_dispatch else work_kind,
                scope_key=scope_key,
                dependency_specs=[dict(spec) for spec in dependency_specs],
            )
            work_item_projection_id = str(
                item.get("work_item_projection_id", "")
                or item.get("projection_id", "")
                or f"{target_role}::{work_kind}::{uuid.uuid4().hex[:8]}"
            ).strip()
            work_item = DelegationWorkItem(
                run_id=run_id,
                cell_id=target_team_id or str(task.metadata.get("delegation_cell_id", "") or "").strip(),
                team_instance_id=target_team_instance_id,
                team_id=target_team_id,
                role_id=target_role,
                seat_id=target_seat_id,
                seat_state_id=str(target_seat.get("seat_state_id", "") or "").strip(),
                role_runtime_session_id=role_runtime_session_id,
                parent_work_item_id=parent_work_item_id,
                source_role_id=role_id,
                source_seat_id=seat_id,
                title=title,
                summary=summary,
                kind=work_kind,
                projection_id=work_item_projection_id,
                phase=initial_phase,
                batch_id=batch_id,
                batch_index=int(item.get("batch_index", index)),
                deliverable_summary=str(item.get("deliverable_summary", "") or "").strip(),
                blocked_reason=str(item.get("blocked_reason", "") or "").strip(),
                handoff_status=str(item.get("handoff_status", "") or "pending").strip(),
                continuation_source=str(item.get("continuation_source", "") or parent_work_item_id or "").strip(),
                manager_role_id=str(target_seat.get("manager_role_id", "") or role_id).strip(),
                manager_seat_id=str(target_seat.get("manager_seat_id", "") or seat_id).strip(),
                metadata=mark_work_item_projection(mark_work_item_runtime({
                    "runtime_model": str(task.metadata.get("runtime_model", "") or "multi_team_org").strip(),
                    "team_id": target_team_id,
                    "team_instance_id": target_team_instance_id,
                    "seat_id": target_seat_id,
                    "seat_state_id": str(target_seat.get("seat_state_id", "") or "").strip(),
                    "work_kind": work_kind,
                    "batch_id": batch_id,
                    "dependency_work_item_ids": [],
                    "dependency_specs": [dict(spec) for spec in dependency_specs],
                    "scope_key": scope_key,
                    **({"requested_scope_key": requested_scope_key} if requested_scope_key else {}),
                    "scope_key_aliases": list(
                        dict.fromkeys([*_scope_key_aliases(scope_key), *_scope_key_aliases(requested_scope_key)])
                    ),
                    "generated_scope_key": generated_scope_key,
                    "created_by_seat_id": seat_id,
                    "assigned_role_runtime_id": role_runtime_session_id,
                    "delegation_playbook": playbook,
                    "contact_role_ids": list(target_seat.get("contact_role_ids", []) or []),
                    "allowed_delegate_role_ids": list(target_seat.get("allowed_delegate_role_ids", []) or []),
                    **output_contract,
                    "comms_workspace_root": str(task.metadata.get("comms_workspace_root", "") or "").strip(),
                    "comms_root": str(task.metadata.get("comms_root", "") or "").strip(),
                    "target_output_dir": str(task.metadata.get("target_output_dir", "") or "").strip(),
                    "release_policy": release_policy,
                    "upstream_visibility": upstream_visibility,
                    "source_message_id": source_message_id,
                    "release_on_semantic_type": release_on_semantic_type,
                    "parent_board_scope": str(item.get("parent_board_scope", "") or _parent_board_scope(task)).strip(),
                    "manager_board_parent_work_item_id": parent_work_item_id,
                    "dependency_classes": dict(item.get("dependency_classes", {}) or {}),
                    "planning_context": normalized_planning_context,
                    **({"latest_user_directive": latest_user_directive_preview} if latest_user_directive_preview else {}),
                    "allow_parallel_same_role": allow_parallel_same_role,
                    "brief": summary,
                    "prompt_contract": prompt_contract,
                    **inherited_self_evolution,
                    "prompt_assignment": {
                        "primary_task_brief": prompt_task_brief,
                        "upstream_intent_summary": upstream_intent_summary,
                        "manager_planning_handoff": normalized_planning_context if manager_outcome_dispatch else "",
                        "scope_key": scope_key,
                        "deliverables": deliverables,
                        "acceptance_criteria": acceptance_criteria,
                        "dependency_specs": [dict(spec) for spec in dependency_specs],
                        "coordination_notes": coordination_notes,
                        "delegation_rationale": delegation_rationale,
                        "non_overlap_guard": non_overlap_guard,
                        "manager_outcome_dispatch": manager_outcome_dispatch,
                        "owned_outcome_kind": "execute" if manager_outcome_dispatch else work_kind,
                    },
                    "deliverables": deliverables,
                    "acceptance_criteria": acceptance_criteria,
                    "outputs": deliverables,
                    "done_when": acceptance_criteria,
                    "delegation_rationale": delegation_rationale,
                    "non_overlap_guard": non_overlap_guard,
                    "coordination_notes": coordination_notes,
                    "manager_outcome_dispatch": manager_outcome_dispatch,
                    "owned_outcome_kind": "execute" if manager_outcome_dispatch else work_kind,
                }), projection_id=work_item_projection_id, turn_type=work_kind),
            )
            ref_to_work_item_id[ref_key] = work_item.work_item_id
            if requested_scope_key:
                for alias in _scope_key_aliases(requested_scope_key):
                    ref_to_work_item_id.setdefault(alias, work_item.work_item_id)
            for alias in _scope_key_aliases(scope_key):
                ref_to_work_item_id.setdefault(alias, work_item.work_item_id)
            pending_items.append((work_item, dependency_specs, generated_scope_key))
        work_item_ids: set[str] = set()
        scope_index: dict[str, list[str]] = {}
        role_index: dict[str, list[str]] = {}
        for existing in existing_children:
            if is_prunable_dependency_work_item(existing):
                continue
            existing_id = str(getattr(existing, "work_item_id", "") or "").strip()
            if not existing_id:
                continue
            work_item_ids.add(existing_id)
            existing_meta = dict(getattr(existing, "metadata", {}) or {})
            existing_scope = str(existing_meta.get("scope_key", "") or "").strip()
            if existing_scope:
                _index_scope_aliases(scope_index, existing_scope, existing_id)
            for alias_value in list(existing_meta.get("scope_key_aliases", []) or []):
                _index_scope_aliases(scope_index, str(alias_value), existing_id)
            existing_role = str(getattr(existing, "role_id", "") or "").strip()
            if existing_role:
                role_index.setdefault(existing_role, []).append(existing_id)
        for work_item, _dependency_specs, _generated_scope_key in pending_items:
            work_item_ids.add(work_item.work_item_id)
            pending_meta = dict(work_item.metadata or {})
            pending_scope = str(pending_meta.get("scope_key", "") or "").strip()
            if pending_scope:
                _index_scope_aliases(scope_index, pending_scope, work_item.work_item_id)
            requested_scope = str(pending_meta.get("requested_scope_key", "") or "").strip()
            if requested_scope:
                _index_scope_aliases(scope_index, requested_scope, work_item.work_item_id)
            for alias_value in list(pending_meta.get("scope_key_aliases", []) or []):
                _index_scope_aliases(scope_index, str(alias_value), work_item.work_item_id)
            if work_item.role_id:
                role_index.setdefault(work_item.role_id, []).append(work_item.work_item_id)
        resolved_pending_items: list[tuple[DelegationWorkItem, list[dict[str, str]], bool, list[dict[str, str]], list[str]]] = []
        for work_item, dependency_specs, generated_scope_key in pending_items:
            resolved_dependency_records = _resolve_dependency_specs(
                dependency_specs,
                ref_index=ref_to_work_item_id,
                work_item_ids=work_item_ids,
                scope_index=scope_index,
                role_index=role_index,
            )
            resolved_dependencies = [record["work_item_id"] for record in resolved_dependency_records]
            work_item.metadata = {
                **dict(work_item.metadata or {}),
                "dependency_work_item_ids": resolved_dependencies,
                "resolved_dependencies": resolved_dependency_records,
            }
            resolved_pending_items.append(
                (work_item, dependency_specs, generated_scope_key, resolved_dependency_records, resolved_dependencies)
            )
        for work_item, dependency_specs, generated_scope_key, resolved_dependency_records, resolved_dependencies in resolved_pending_items:
            # initial_phase upstream already accounted for the presence of
            # dependencies (READY vs WAITING_DEPENDENCIES vs QUEUED) so no
            # phase mutation is needed here — only the resolved dependency
            # IDs get persisted.
            await store.save_delegation_work_item(work_item)
            work_item_by_id[work_item.work_item_id] = work_item
            if hasattr(store, "save_delegation_event"):
                await store.save_delegation_event(
                    DelegationEvent(
                        run_id=run_id,
                        work_item_id=work_item.work_item_id,
                        cell_id=work_item.cell_id or None,
                        role_id=work_item.role_id or None,
                        event_type="work_item_created",
                        payload={
                            "parent_work_item_id": parent_work_item_id,
                            "seat_id": work_item.seat_id,
                            "team_id": work_item.team_id,
                            "batch_id": work_item.batch_id,
                            "dependency_work_item_ids": resolved_dependencies,
                            "dependency_specs": [dict(spec) for spec in dependency_specs],
                            "resolved_dependencies": resolved_dependency_records,
                            "work_kind": work_item.kind,
                            "created_by_seat_id": seat_id,
                            "release_policy": str(work_item.metadata.get("release_policy", "auto") or "auto"),
                            "source_message_id": str(work_item.metadata.get("source_message_id", "") or ""),
                            "scope_key": str(work_item.metadata.get("scope_key", "") or "").strip(),
                        },
                    )
                )
            delegated_payloads.append(
                {
                    "work_item_id": work_item.work_item_id,
                    "role_id": work_item.role_id,
                    "seat_id": work_item.seat_id,
                    "team_id": work_item.team_id,
                    "phase": work_item.phase.value,
                    "kanban_column": kanban_column(work_item.phase),
                    "work_kind": work_item.kind,
                    "title": work_item.title,
                    "release_policy": str(work_item.metadata.get("release_policy", "auto") or "auto"),
                    "dependency_work_item_ids": list(resolved_dependencies),
                    "dependency_specs": [dict(spec) for spec in dependency_specs],
                    "resolved_dependencies": list(resolved_dependency_records),
                    "scope_key": str(work_item.metadata.get("scope_key", "") or "").strip(),
                    "generated_scope_key": generated_scope_key,
                    "deliverables": list(work_item.metadata.get("deliverables", []) or []),
                    "acceptance_criteria": list(work_item.metadata.get("acceptance_criteria", []) or []),
                    "delegation_rationale": str(work_item.metadata.get("delegation_rationale", "") or "").strip(),
                    "non_overlap_guard": str(work_item.metadata.get("non_overlap_guard", "") or "").strip(),
                    "manager_outcome_dispatch": bool(work_item.metadata.get("manager_outcome_dispatch", False)),
                    "reused": False,
                }
            )
        # A manager that just dispatched MUST wait for its children to come
        # back, review them, synthesize, and only then submit upward. The
        # previous opt-out (``wait_for_children=False``) let an LLM bypass
        # this whole chain by claiming "done" the moment delegation
        # finished, which produced premature upper-level reviews of empty
        # handoffs and rework loops that the dispatch guard then mis-killed.
        # See `_park_for_delegated_children` (company_mode.py) for how the
        # parent gets parked in WAITING_FOR_CHILDREN once these dependency
        # ids are stamped.
        if delegated_payloads:
            parent_dependency_ids, pruned_parent_dependencies = normalize_dependency_work_item_ids(
                list(dict.fromkeys([*existing_dependencies, *[item["work_item_id"] for item in delegated_payloads]])),
                work_item_by_id,
                owner_work_item_id=parent_work_item_id,
            )
            pruned_parent_dependencies = list(
                dict.fromkeys([*pruned_existing_dependencies, *pruned_parent_dependencies])
            )
            task.metadata = dict(task.metadata)
            task.metadata["delegation_wait_for_work_item_ids"] = parent_dependency_ids
            task.metadata["manager_board_parent_work_item_id"] = parent_work_item_id
            # Reusing an existing scope-key match is not a current-turn board
            # mutation.  Only newly persisted business children make this
            # attempt a delegated-board completion.
            if resolved_pending_items:
                task.metadata["manager_board_mutation_performed"] = True
            if attention_work_item_id:
                task.metadata["attention_business_parent_work_item_id"] = parent_work_item_id
                task.metadata["attention_work_item_id"] = attention_work_item_id
            if normalized_planning_context:
                task.metadata["manager_dispatch_planning_context"] = normalized_planning_context
            if hasattr(store, "update_delegation_work_item"):
                await store.update_delegation_work_item(
                    parent_work_item_id,
                    metadata_updates={
                        "dependency_work_item_ids": parent_dependency_ids,
                        "frontier": "waiting_for_children",
                        "last_delegated_by_seat_id": seat_id,
                        "planning_context": normalized_planning_context,
                        **(
                            {
                                "pruned_dependency_work_item_ids": pruned_parent_dependencies,
                                "dependency_pruned_at": datetime.now().isoformat(),
                            }
                            if pruned_parent_dependencies
                            else {}
                        ),
                    },
                )
            if hasattr(store, "save_task"):
                await store.save_task(task)
        # Kanban-push: wake the company-mode dispatcher immediately so the
        # newly-saved TODO items can be claimed+spawned without waiting for
        # the parent's turn (and its sibling gather batch) to drain. The
        # hook is best-effort and synchronous — it just sets an
        # asyncio.Event. Callers without a wired dispatcher get a no-op.
        if delegated_payloads:
            await _notify_work_items_changed(communication, label="delegate_work")
        return {
            "delegated": delegated_payloads,
            "parent_work_item_id": parent_work_item_id,
            "current_work_item_id": _current_work_item_id(task),
            "attention_work_item_id": attention_work_item_id or "",
            "parent_board_scope": f"{seat_id}:{parent_work_item_id}",
            "frontier": "waiting_for_children" if delegated_payloads else "expanded",
            "planning_context": normalized_planning_context,
        }

    async def modify_work_item(
        work_item_id: str,
        title: str = "",
        task_brief: str = "",
        brief: str = "",
        summary: str = "",
        work_kind: str = "",
        deliverables: Any = None,
        outputs: Any = None,
        acceptance_criteria: Any = None,
        done_when: Any = None,
        depends_on: Any = None,
        dependency_work_item_ids: Any = None,
        coordination_notes: str = "",
        delegation_rationale: str = "",
        non_overlap_guard: str = "",
        reason: str = "",
        reset_to_ready: bool = True,
        clear_outputs: bool = True,
        task: Task | None = None,
    ) -> dict[str, Any]:
        store = getattr(communication, "store", None)
        item, run_id, manager_seat_id, parent_work_item_id = await _resolve_manager_tool_target(
            store=store,
            task=task,
            work_item_id=work_item_id,
            tool_name="modify_work_item",
        )
        assert task is not None
        if not hasattr(store, "amend_delegation_work_item"):
            raise RuntimeError("modify_work_item requires amend_delegation_work_item persistence support")

        output_contract = output_contract_metadata({
            **dict(task.metadata or {}),
            **dict(getattr(item, "metadata", {}) or {}),
        })
        item_metadata = dict(getattr(item, "metadata", {}) or {})
        existing_contract = dict(item_metadata.get("prompt_contract", {}) or {})
        existing_assignment = dict(existing_contract.get("assignment_context", {}) or {})
        existing_prompt_assignment = dict(item_metadata.get("prompt_assignment", {}) or {})
        dependencies_provided = depends_on is not None or dependency_work_item_ids is not None
        raw_dependencies = depends_on if depends_on is not None else dependency_work_item_ids
        resolved_dependency_records: list[dict[str, str]] = []
        if dependencies_provided:
            dependency_ids, resolved_dependency_records = await _resolve_manager_tool_dependencies(
                store=store,
                run_id=run_id,
                target_work_item_id=item.work_item_id,
                raw_dependencies=raw_dependencies,
            )
            dependency_specs_for_contract: Any = [dict(record) for record in resolved_dependency_records]
        else:
            dependency_ids = [
                str(dep).strip()
                for dep in list(item_metadata.get("dependency_work_item_ids", []) or [])
                if str(dep).strip()
            ]
            dependency_specs_for_contract = item_metadata.get(
                "dependency_specs",
                item_metadata.get("resolved_dependencies", dependency_ids),
            )

        new_task_brief = str(
            task_brief
            or existing_prompt_assignment.get("primary_task_brief")
            or existing_contract.get("task_brief")
            or item_metadata.get("task_brief")
            or item.summary
            or ""
        ).strip()
        new_task_brief = str(replace_output_aliases(new_task_brief, output_contract) or "").strip()
        new_summary = str(brief or summary or item_metadata.get("brief") or item.summary or new_task_brief).strip()
        new_summary = str(replace_output_aliases(new_summary, output_contract) or "").strip()
        new_kind = str(work_kind or item.kind or "execute").strip() or "execute"
        deliverables_source = (
            deliverables
            if deliverables is not None
            else outputs
            if outputs is not None
            else existing_assignment.get(
                "deliverables",
                item_metadata.get("deliverables", item_metadata.get("outputs", [])),
            )
        )
        acceptance_source = (
            acceptance_criteria
            if acceptance_criteria is not None
            else done_when
            if done_when is not None
            else existing_assignment.get(
                "acceptance_criteria",
                item_metadata.get("acceptance_criteria", item_metadata.get("done_when", [])),
            )
        )
        new_deliverables = _normalize_text_list(replace_output_aliases(deliverables_source, output_contract))
        new_acceptance = _normalize_text_list(replace_output_aliases(acceptance_source, output_contract))
        new_coordination_notes = str(
            coordination_notes
            or existing_assignment.get("coordination_notes")
            or item_metadata.get("coordination_notes")
            or ""
        ).strip()
        new_delegation_rationale = str(
            delegation_rationale
            or existing_assignment.get("delegation_rationale")
            or item_metadata.get("delegation_rationale")
            or ""
        ).strip()
        new_non_overlap_guard = str(
            non_overlap_guard
            or existing_assignment.get("non_overlap_guard")
            or item_metadata.get("non_overlap_guard")
            or ""
        ).strip()
        scope_key = str(existing_assignment.get("scope_key") or item_metadata.get("scope_key") or "").strip()
        upstream_intent_summary = str(
            existing_assignment.get("upstream_intent_summary")
            or existing_prompt_assignment.get("upstream_intent_summary")
            or item_metadata.get("upstream_intent_summary")
            or ""
        ).strip()
        manager_planning_handoff = str(
            existing_assignment.get("manager_planning_handoff")
            or existing_prompt_assignment.get("manager_planning_handoff")
            or item_metadata.get("planning_context")
            or ""
        ).strip()
        prompt_item = {
            "task_brief": new_task_brief,
            "brief": new_summary,
            "work_kind": new_kind,
            "scope_key": scope_key,
            "deliverables": new_deliverables,
            "acceptance_criteria": new_acceptance,
            "coordination_notes": new_coordination_notes,
            "delegation_rationale": new_delegation_rationale,
            "non_overlap_guard": new_non_overlap_guard,
            "depends_on": dependency_specs_for_contract,
        }
        manager_outcome_dispatch = bool(
            existing_assignment.get(
                "manager_outcome_dispatch",
                existing_prompt_assignment.get("manager_outcome_dispatch", False),
            )
        )
        owned_outcome_kind = str(
            existing_assignment.get("owned_outcome_kind")
            or existing_prompt_assignment.get("owned_outcome_kind")
            or new_kind
            or "execute"
        ).strip()
        prompt_contract = prompt_contract_from_delegate_item(
            prompt_item,
            task_brief=new_task_brief,
            upstream_intent_summary=upstream_intent_summary,
            manager_planning_handoff=manager_planning_handoff,
            manager_outcome_dispatch=manager_outcome_dispatch,
            owned_outcome_kind=owned_outcome_kind,
            scope_key=scope_key,
            dependency_specs=dependency_specs_for_contract,
        )
        mutation_reason = str(reason or _user_supplied_input_from_task(task) or "manager updated work item").strip()
        metadata_set = {
            **_manager_mutation_metadata(
                item,
                task=task,
                action="modify",
                reason=mutation_reason,
            ),
            "brief": new_summary,
            "prompt_contract": prompt_contract,
            "prompt_assignment": {
                "primary_task_brief": new_task_brief,
                "upstream_intent_summary": upstream_intent_summary,
                "manager_planning_handoff": manager_planning_handoff,
                "scope_key": scope_key,
                "deliverables": new_deliverables,
                "acceptance_criteria": new_acceptance,
                "dependency_specs": dependency_specs_for_contract,
                "coordination_notes": new_coordination_notes,
                "delegation_rationale": new_delegation_rationale,
                "non_overlap_guard": new_non_overlap_guard,
                "manager_outcome_dispatch": manager_outcome_dispatch,
                "owned_outcome_kind": owned_outcome_kind,
            },
            "deliverables": new_deliverables,
            "outputs": new_deliverables,
            "acceptance_criteria": new_acceptance,
            "done_when": new_acceptance,
            "coordination_notes": new_coordination_notes,
            "delegation_rationale": new_delegation_rationale,
            "non_overlap_guard": new_non_overlap_guard,
            "dependency_work_item_ids": dependency_ids,
            "dependency_specs": dependency_specs_for_contract,
            "resolved_dependencies": (
                [dict(record) for record in resolved_dependency_records]
                if dependencies_provided
                else list(item_metadata.get("resolved_dependencies", []) or [])
            ),
            "revised_by_manager_tool": True,
        }
        metadata_unset = _manager_mutation_metadata_unset(clear_outputs=bool(clear_outputs))
        target_phase: Phase | None = None
        if bool(reset_to_ready):
            target_phase = (
                Phase.READY_FOR_REWORK
                if item.phase in {Phase.AWAITING_MANAGER_REVIEW, Phase.AWAITING_HUMAN, Phase.READY_FOR_REWORK, Phase.APPROVED}
                else Phase.READY
            )

        if item.phase == Phase.APPROVED and bool(reset_to_ready):
            await store.amend_delegation_work_item(
                item.work_item_id,
                title=str(title or "").strip() or None,
                summary=new_summary,
                kind=new_kind,
                dependency_work_item_ids=dependency_ids if dependencies_provided else None,
                metadata_set=metadata_set,
                metadata_unset=metadata_unset,
                claimed_by_role_runtime_session_id="",
                claimed_by_seat_id="",
            )
            updated = await store.reopen_approved_delegation_work_item_for_rework(
                item.work_item_id,
                target_phase=target_phase or Phase.READY_FOR_REWORK,
                summary=new_summary,
                deliverable_summary="",
                blocked_reason="",
                metadata_updates={"reopened_by_manager_tool": True},
                release_claim=True,
            )
        elif item.phase in {Phase.FAILED, Phase.CANCELLED}:
            raise ValueError(
                "modify_work_item cannot reopen failed/cancelled terminal WorkItems. "
                "Create a new WorkItem or delete the terminal item from the active board."
            )
        else:
            updated = await store.amend_delegation_work_item(
                item.work_item_id,
                title=str(title or "").strip() or None,
                summary=new_summary,
                kind=new_kind,
                dependency_work_item_ids=dependency_ids if dependencies_provided else None,
                phase=target_phase,
                metadata_set=metadata_set,
                metadata_unset=metadata_unset,
                claimed_by_role_runtime_session_id="",
                claimed_by_seat_id="",
            )
            if updated is not None and hasattr(store, "update_delegation_work_item"):
                updated = await store.update_delegation_work_item(
                    item.work_item_id,
                    deliverable_summary="",
                    blocked_reason="",
                    handoff_status="pending",
                )
        if updated is None:
            raise ValueError(f"modify_work_item: no WorkItem matches work_item_id={item.work_item_id!r}")
        if hasattr(store, "save_delegation_event"):
            await store.save_delegation_event(
                DelegationEvent(
                    run_id=run_id,
                    work_item_id=updated.work_item_id,
                    cell_id=updated.cell_id or None,
                    role_id=updated.role_id or None,
                    event_type="work_item_modified_by_manager_tool",
                    payload={
                        "manager_seat_id": manager_seat_id,
                        "parent_work_item_id": parent_work_item_id,
                        "reason": mutation_reason,
                        "reset_to_ready": bool(reset_to_ready),
                        "dependency_work_item_ids": dependency_ids,
                    },
                )
            )
        task.metadata = dict(task.metadata or {})
        modified_ids = [
            str(item_id).strip()
            for item_id in list(task.metadata.get("manager_board_modified_work_item_ids", []) or [])
            if str(item_id).strip()
        ]
        modified_ids.append(updated.work_item_id)
        task.metadata["manager_board_modified_work_item_ids"] = list(dict.fromkeys(modified_ids))
        task.metadata["manager_board_mutation_performed"] = True
        if hasattr(store, "save_task"):
            await store.save_task(task)
        if bool(reset_to_ready):
            await refresh_dependents_for_run(store, run_id=run_id, source_work_item_id=updated.work_item_id)
        await _notify_work_items_changed(communication, label="modify_work_item")
        return {
            "action": "modify_work_item",
            "work_item_id": updated.work_item_id,
            "parent_work_item_id": parent_work_item_id,
            "manager_seat_id": manager_seat_id,
            "phase": updated.phase.value if isinstance(updated.phase, Phase) else str(updated.phase or ""),
            "item": _serialize_board_item(updated, include_full_summaries=True),
        }

    async def delete_work_item(
        work_item_id: str,
        reason: str = "",
        replacement_dependency_work_item_ids: Any = None,
        task: Task | None = None,
    ) -> dict[str, Any]:
        store = getattr(communication, "store", None)
        item, run_id, manager_seat_id, parent_work_item_id = await _resolve_manager_tool_target(
            store=store,
            task=task,
            work_item_id=work_item_id,
            tool_name="delete_work_item",
        )
        assert task is not None
        if item.work_item_id == parent_work_item_id:
            raise ValueError("delete_work_item can only delete child WorkItems on the current manager board")
        if not hasattr(store, "amend_delegation_work_item") or not hasattr(store, "update_delegation_work_item"):
            raise RuntimeError("delete_work_item requires work-item update persistence support")
        mutation_reason = str(reason or _user_supplied_input_from_task(task) or "manager deleted work item").strip()
        replacements = [
            str(dep).strip()
            for dep in (
                [replacement_dependency_work_item_ids]
                if isinstance(replacement_dependency_work_item_ids, str)
                else list(replacement_dependency_work_item_ids or [])
            )
            if str(dep).strip()
        ]
        metadata_set = {
            **_manager_mutation_metadata(
                item,
                task=task,
                action="delete",
                reason=mutation_reason,
                extra={
                    "deleted_by_manager_tool": True,
                    "deleted_at": datetime.now().isoformat(),
                    "delete_reason": mutation_reason,
                    "hidden_from_company_kanban": True,
                    "upstream_visibility": "hidden",
                    "replacement_dependency_work_item_ids": list(replacements),
                },
            ),
        }
        await store.amend_delegation_work_item(
            item.work_item_id,
            metadata_set=metadata_set,
            metadata_unset=_manager_mutation_metadata_unset(clear_outputs=False),
            claimed_by_role_runtime_session_id="",
            claimed_by_seat_id="",
        )
        if item.phase not in DONE_PHASES:
            updated = await store.update_delegation_work_item(
                item.work_item_id,
                phase=Phase.CANCELLED,
                blocked_reason=mutation_reason[:500],
                handoff_status="cancelled",
                claimed_by_role_runtime_session_id="",
                claimed_by_seat_id="",
            )
        else:
            updated = await store.get_delegation_work_item(item.work_item_id)

        cascade_deleted_ids: list[str] = []
        if hasattr(store, "list_delegation_work_items"):
            try:
                all_items = await store.list_delegation_work_items(run_id)
            except Exception:
                all_items = []
            children_by_parent: dict[str, list[DelegationWorkItem]] = {}
            for candidate in list(all_items or []):
                parent_id = str(getattr(candidate, "parent_work_item_id", "") or "").strip()
                if parent_id:
                    children_by_parent.setdefault(parent_id, []).append(candidate)
            stack = [item.work_item_id]
            descendants: list[DelegationWorkItem] = []
            while stack:
                parent_id = stack.pop()
                for child in children_by_parent.get(parent_id, []):
                    child_id = str(getattr(child, "work_item_id", "") or "").strip()
                    if not child_id or child_id == item.work_item_id:
                        continue
                    descendants.append(child)
                    stack.append(child_id)

            get_runtime_task = getattr(store, "get_runtime_task_for_work_item", None)
            for descendant in descendants:
                descendant_reason = (
                    f"Ancestor WorkItem `{item.work_item_id}` was deleted by manager tool: {mutation_reason}"
                )
                cascade_metadata = _manager_mutation_metadata(
                    descendant,
                    task=task,
                    action="delete",
                    reason=descendant_reason,
                    extra={
                        "deleted_by_manager_tool": True,
                        "deleted_at": datetime.now().isoformat(),
                        "delete_reason": descendant_reason,
                        "cascade_deleted_by_work_item_id": item.work_item_id,
                        "cascade_delete_root_work_item_id": item.work_item_id,
                        "hidden_from_company_kanban": True,
                        "upstream_visibility": "hidden",
                    },
                )
                try:
                    await store.amend_delegation_work_item(
                        descendant.work_item_id,
                        metadata_set=cascade_metadata,
                        metadata_unset=_manager_mutation_metadata_unset(clear_outputs=False),
                        claimed_by_role_runtime_session_id="",
                        claimed_by_seat_id="",
                    )
                    if descendant.phase not in DONE_PHASES:
                        await transition_work_item(
                            store,
                            descendant.work_item_id,
                            target_phase=Phase.CANCELLED,
                            reason=descendant_reason,
                            release_claim=True,
                            blocked_reason=descendant_reason[:500],
                            handoff_status="cancelled",
                        )
                        # Task.status is projected from the phase by the
                        # transition hooks; here we only stamp the task-owned
                        # audit trail and release the execution lock.
                        if callable(get_runtime_task):
                            runtime_task = await get_runtime_task(descendant.work_item_id)
                            if runtime_task is not None:
                                runtime_task.execution_lock = False
                                runtime_task.execution_locked_at = None
                                runtime_task.metadata = {
                                    **dict(runtime_task.metadata or {}),
                                    "last_stop_reason": "manager_deleted_ancestor_work_item",
                                    "deleted_by_manager_tool": True,
                                    "cascade_deleted_by_work_item_id": item.work_item_id,
                                }
                                if hasattr(store, "save_task"):
                                    await store.save_task(runtime_task)
                    cascade_deleted_ids.append(descendant.work_item_id)
                except Exception:
                    logger.opt(exception=True).warning(
                        "delete_work_item: failed to cascade delete descendant {}",
                        descendant.work_item_id,
                    )
        dependency_rewrites: list[str] = []
        wake_ids: list[str] = []
        if hasattr(store, "replace_work_item_dependency"):
            rewritten = await store.replace_work_item_dependency(run_id, item.work_item_id, replacements)
            dependency_rewrites = [rewritten_item.work_item_id for rewritten_item in list(rewritten or [])]
            wake_ids = await _wake_rewritten_dependency_items(store, rewritten_items=list(rewritten or []))
        await refresh_dependents_for_run(store, run_id=run_id, source_work_item_id=item.work_item_id)
        if hasattr(store, "save_delegation_event"):
            await store.save_delegation_event(
                DelegationEvent(
                    run_id=run_id,
                    work_item_id=item.work_item_id,
                    cell_id=item.cell_id or None,
                    role_id=item.role_id or None,
                    event_type="work_item_deleted_by_manager_tool",
                    payload={
                        "manager_seat_id": manager_seat_id,
                        "parent_work_item_id": parent_work_item_id,
                        "reason": mutation_reason,
                        "replacement_dependency_work_item_ids": replacements,
                        "dependency_rewrites": dependency_rewrites,
                        "woken_work_item_ids": wake_ids,
                        "cascade_deleted_work_item_ids": cascade_deleted_ids,
                    },
                )
            )
        task.metadata = dict(task.metadata or {})
        deleted_ids = [
            str(item_id).strip()
            for item_id in list(task.metadata.get("manager_board_deleted_work_item_ids", []) or [])
            if str(item_id).strip()
        ]
        deleted_ids.append(item.work_item_id)
        task.metadata["manager_board_deleted_work_item_ids"] = list(dict.fromkeys(deleted_ids))
        task.metadata["manager_board_mutation_performed"] = True
        if hasattr(store, "save_task"):
            await store.save_task(task)
        await _notify_work_items_changed(communication, label="delete_work_item")
        if updated is None:
            updated = item
        return {
            "action": "delete_work_item",
            "work_item_id": item.work_item_id,
            "parent_work_item_id": parent_work_item_id,
            "manager_seat_id": manager_seat_id,
            "phase": updated.phase.value if isinstance(updated.phase, Phase) else str(updated.phase or ""),
            "dependency_rewrites": dependency_rewrites,
            "woken_work_item_ids": wake_ids,
            "cascade_deleted_work_item_ids": cascade_deleted_ids,
            "item": _serialize_board_item(updated, include_full_summaries=True),
        }

    async def manager_board_read(
        parent_work_item_id: str = "",
        include_children: bool = True,
        include_full_summaries: bool = False,
        task: Task | None = None,
    ) -> dict[str, Any]:
        role_id = _active_role(task)
        if not task or not role_id:
            raise ValueError("manager_board_read requires an active assigned task")
        store = getattr(communication, "store", None)
        if store is None or not hasattr(store, "list_manager_board") or not hasattr(store, "summarize_parent_status"):
            raise RuntimeError("manager_board_read requires manager-board persistence helpers")
        run_id = str(task.metadata.get("delegation_run_id", "") or "").strip()
        manager_seat_id = _active_seat(task)
        include_children_flag = bool(include_children)
        if parent_work_item_id:
            target_parent = str(parent_work_item_id or "").strip()
            attention_work_item_id = ""
        else:
            target_parent, attention_work_item_id = await _resolve_manager_board_parent_work_item_id(store, task)
        if not run_id or not manager_seat_id or not target_parent:
            raise ValueError("manager_board_read requires runtime work-item metadata")
        # Verify the parent work item exists. Without this check, a
        # truncated/mistyped id silently returns an empty board (total_children=0),
        # which historically led agents to conclude "I have no delegated work"
        # while their actual children sat finished and un-synthesised.
        requested_parent_work_item_id = target_parent
        fallback_warning = ""
        parent_record = None
        if hasattr(store, "get_delegation_work_item"):
            parent_record = await store.get_delegation_work_item(target_parent)
            if parent_record is None:
                fallback_parent, fallback_attention_work_item_id = await _resolve_manager_board_parent_work_item_id(store, task)
                fallback_parent_record = (
                    await store.get_delegation_work_item(fallback_parent)
                    if fallback_parent and fallback_parent != target_parent
                    else None
                )
                if fallback_parent_record is None:
                    raise ValueError(
                        "manager_board_read: no work item matches "
                        f"parent_work_item_id={target_parent!r}. Pass the full id "
                        "(use the value shown in delegate_work / manager_board_read "
                        "responses verbatim, without truncation)."
                    )
                fallback_warning = (
                    "Requested parent_work_item_id was not found; returned the current manager board instead. "
                    "Omit parent_work_item_id unless you copied a full WorkItem id from manager_board_read/delegate_work."
                )
                target_parent = fallback_parent
                attention_work_item_id = fallback_attention_work_item_id
                parent_record = fallback_parent_record
        board_summary = await store.summarize_parent_status(
            run_id,
            manager_seat_id=manager_seat_id,
            parent_work_item_id=target_parent,
        )
        board_items = await store.list_manager_board(
            run_id,
            manager_seat_id=manager_seat_id,
            parent_work_item_id=target_parent,
        )
        visible_board_items = [item for item in board_items if not _is_hidden_work_item(item)]
        focus_item = None
        focus_work_item_id = str(task.metadata.get("review_target_work_item_id", "") or "").strip()
        if focus_work_item_id and hasattr(store, "get_delegation_work_item"):
            focus_item = await store.get_delegation_work_item(focus_work_item_id)
            if _is_hidden_work_item(focus_item):
                focus_item = None
        result = {
            "run_id": run_id,
            "manager_seat_id": manager_seat_id,
            "parent_work_item_id": target_parent,
            "parent_item": (
                _serialize_board_item(parent_record, include_full_summaries=bool(include_full_summaries))
                if parent_record is not None else None
            ),
            "current_work_item_id": _current_work_item_id(task),
            "attention_work_item_id": attention_work_item_id or "",
            "parent_board_scope": f"{manager_seat_id}:{target_parent}",
            "summary": board_summary,
            "items": [
                _serialize_board_item(item, include_full_summaries=bool(include_full_summaries))
                for item in visible_board_items
            ] if include_children_flag else [],
            "focus_item": (
                _serialize_board_item(focus_item, include_full_summaries=bool(include_full_summaries))
                if focus_item is not None else None
            ),
        }
        if fallback_warning:
            result["warning"] = fallback_warning
            result["requested_parent_work_item_id"] = requested_parent_work_item_id
        return result

    return [
        ToolDefinition(
            name="inbox",
            description="Check or acknowledge your company-mode mailbox without implicitly marking messages read.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "peek", "ack"],
                        "default": "status",
                    },
                    "message_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                    "limit": {"type": "integer", "default": 10},
                },
            },
            func=inbox,
            category="collaboration",
        ),
        ToolDefinition(
            name="send_dm",
            description="Send an async direct message to another company-mode agent.",
            parameters={
                "type": "object",
                "properties": {
                    "to_agent": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "blocking": {"type": "boolean", "default": False},
                    "timeout_action": {"type": "string", "default": ""},
                    "timeout_seconds": {"type": "integer", "default": 300},
                    "semantic_type": {"type": "string", "default": ""},
                    "action_hint": {"type": "string", "default": ""},
                    "source_message_id": {"type": "string", "default": ""},
                },
                "required": ["to_agent", "subject", "body"],
            },
            func=send_dm,
            category="collaboration",
        ),
        ToolDefinition(
            name="ask_peer_and_wait",
            description="Send a lightweight blocking peer question and pause until a reply or timeout policy fires.",
            parameters={
                "type": "object",
                "properties": {
                    "to_agent": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "timeout_action": {"type": "string", "default": ""},
                    "timeout_seconds": {"type": "integer", "default": 300},
                    "on_timeout": {
                        "type": "string",
                        "description": "Timeout policy: continue | manager | meeting",
                        "default": "continue",
                    },
                },
                "required": ["to_agent", "subject", "body"],
            },
            func=ask_peer_and_wait,
            category="collaboration",
        ),
        ToolDefinition(
            name="reply_message",
            description="Reply to a collaboration message and unblock a waiting peer.",
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "body": {"type": "string"},
                    "subject": {"type": "string", "default": ""},
                },
                "required": ["message_id", "body"],
            },
            func=reply_message,
            category="collaboration",
        ),
        ToolDefinition(
            name="broadcast_issue",
            description="Broadcast an async issue to multiple company-mode agents.",
            parameters={
                "type": "object",
                "properties": {
                    "to_agents": {"type": "array", "items": {"type": "string"}},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "blocking": {"type": "boolean", "default": False},
                    "timeout_action": {"type": "string", "default": ""},
                    "timeout_seconds": {"type": "integer", "default": 300},
                    "semantic_type": {"type": "string", "default": ""},
                    "action_hint": {"type": "string", "default": ""},
                    "source_message_id": {"type": "string", "default": ""},
                },
                "required": ["to_agents", "subject", "body"],
            },
            func=broadcast_issue,
            category="collaboration",
        ),
        ToolDefinition(
            name="start_meeting",
            description="Start a company-mode meeting and pause until outcome is available.",
            parameters={
                "type": "object",
                "properties": {
                    "topic": {"type": "string"},
                    "participants": {"type": "array", "items": {"type": "string"}},
                    "agenda": {"type": "array", "items": {"type": "string"}},
                    "shared_context": {"type": "string", "default": ""},
                    "decision_owner": {"type": "string", "default": ""},
                    "decision_policy": {
                        "type": "string",
                        "default": "semantic_consensus_then_owner",
                    },
                    "timeout_seconds": {"type": "integer", "default": 900},
                    "risk_level": {"type": "string", "default": "normal"},
                },
                "required": ["topic", "participants", "agenda"],
            },
            func=start_meeting,
            category="collaboration",
        ),
        ToolDefinition(
            name="respond_meeting",
            description="Respond to an active meeting room or finalize it as decision owner.",
            parameters={
                "type": "object",
                "properties": {
                    "room_id": {"type": "string"},
                    "content": {"type": "string"},
                    "finalize": {"type": "boolean", "default": False},
                },
                "required": ["room_id", "content"],
            },
            func=respond_meeting,
            category="collaboration",
        ),
        ToolDefinition(
            name="propose_task_adjustment",
            description="Propose a targeted runtime task adjustment and auto-apply safe low-risk changes when allowed.",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "changeset": {"type": "object"},
                },
                "required": ["summary", "changeset"],
            },
            func=propose_task_adjustment,
            category="collaboration",
        ),
        ToolDefinition(
            name="route_work",
            description=(
                "Route work as a coordinator: send_followup (high-priority nudge to a target role), "
                "spawn_task (queue a new task for a target role — runtime creates it after the turn), "
                "or escalate (flag an issue up to this task's manager). "
                "Primarily used by coordinator roles."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["send_followup", "spawn_task", "escalate"],
                    },
                    "target_role": {"type": "string", "default": ""},
                    "prompt": {"type": "string", "default": ""},
                    "priority": {"type": "string", "default": "normal"},
                },
                "required": ["action"],
            },
            func=route_work,
            category="collaboration",
        ),
        ToolDefinition(
            name="close_human_review",
            description=(
                "Close the current owner-facing company delivery review when you decide the user's latest directive "
                "means no further internal work is needed. This records the review as resolved, clears the delivery "
                "feedback wait, and closes matching delivery-feedback checkpoints. Do not call it for requested changes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "user_message": {"type": "string", "default": ""},
                },
                "required": ["summary"],
            },
            func=close_human_review,
            category="collaboration",
        ),
        ToolDefinition(
            name="submit_self_evolution_patches",
            description=(
                "Record employee experience patches for the current self-evolution work item. "
                "This tool is the authoritative channel for self-evolution results: patches recorded "
                "here are applied regardless of what the final turn text says. Pass an empty patches "
                "list when no experience update is needed for this role. Each patch targets this work "
                "item's assigned employee (employee_id is filled in automatically when omitted)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "patches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "employee_id": {"type": "string"},
                                "summary": {"type": "string"},
                                "strengths": {"type": "array", "items": {"type": "string"}},
                                "adjustments": {"type": "array", "items": {"type": "string"}},
                                "avoid_next_time": {"type": "array", "items": {"type": "string"}},
                                "routing_notes": {"type": "string"},
                                "evidence_task_ids": {"type": "array", "items": {"type": "string"}},
                                "confidence": {"type": "number"},
                            },
                        },
                    },
                },
                "required": ["patches"],
            },
            func=submit_self_evolution_patches,
            category="collaboration",
        ),
        ToolDefinition(
            name="delegate_work",
            description=(
                "Create child runtime work items for direct-report seats/roles using optional semantic scope keys and dependencies. "
                "During manager startup/dispatch turns, follow the Dispatch Planning Contract first: preserve upstream intent, "
                "distinguish hard dependencies from startable preparation, and create outcome-based child work items. "
                "Multiple items may target the same role when distinct phases unlock parallel progress. "
                "Include planning_context plus per-item task_brief (prompt owner), brief/summary (UI/audit), "
                "outputs/deliverables, done_when/acceptance_criteria, delegation_rationale, non_overlap_guard, and coordination_notes. "
                "scope_key is idempotent per manager board: an existing scope_key is reused, not duplicated. "
                "Dependencies must be expressed in structured depends_on; text in brief/task_brief/coordination_notes is never treated as a dependency. "
                "Prefer stable sibling scope_key or work_item_ref references in depends_on; use raw work_item_id UUIDs only when copied from manager_board_read. "
                "depends_on may reference sibling role_id, scope_key, work_item_ref, or work_item_id; the tool resolves these to work-item IDs. "
                "Unsupported item fields reject the whole call before any work item is created."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "role_id": {"type": "string"},
                                "title": {"type": "string"},
                                "name": {"type": "string"},
                                "task_brief": {"type": "string"},
                                "prompt_brief": {"type": "string"},
                                "brief": {"type": "string"},
                                "summary": {"type": "string"},
                                "body": {"type": "string"},
                                "prompt": {"type": "string"},
                                "scope_key": {"type": "string"},
                                "work_kind": {"type": "string", "default": "execute"},
                                "kind": {"type": "string"},
                                "outputs": {"type": "array", "items": {"type": "string"}},
                                "deliverables": {"type": "array", "items": {"type": "string"}},
                                "done_when": {"type": "array", "items": {"type": "string"}},
                                "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                                "delegation_rationale": {"type": "string", "default": ""},
                                "non_overlap_guard": {"type": "string", "default": ""},
                                "coordination_notes": {"type": "string", "default": ""},
                                "allow_parallel_same_role": {"type": "boolean", "default": False},
                                "depends_on": {
                                    "type": "array",
                                    "items": {},
                                    "description": (
                                        "Structured hard dependencies. Use sibling scope_key or work_item_ref values when possible; "
                                        "role_id is allowed only when unambiguous, and raw work_item_id UUIDs should only be copied from manager_board_read. "
                                        "Dependency wording in brief/task_brief/coordination_notes is ignored."
                                    ),
                                },
                                "dependency_work_item_ids": {"type": "array", "items": {"type": "string"}},
                                "work_item_ref": {"type": "string"},
                                "id": {"type": "string"},
                                "team_id": {"type": "string"},
                                "batch_id": {"type": "string"},
                                "batch_index": {"type": "integer"},
                                "release_policy": {"type": "string", "default": "auto"},
                                "upstream_visibility": {"type": "string", "default": "summary_only"},
                                "source_message_id": {"type": "string", "default": ""},
                                "release_on_semantic_type": {"type": "string", "default": ""},
                                "parent_board_scope": {"type": "string", "default": ""},
                                "upstream_intent_summary": {"type": "string"},
                                "dependency_classes": {"type": "object"},
                                "work_item_projection_id": {"type": "string"},
                                "projection_id": {"type": "string"},
                                "blocked_reason": {"type": "string"},
                                "handoff_status": {"type": "string"},
                                "continuation_source": {"type": "string"},
                                "deliverable_summary": {"type": "string"},
                            },
                            "required": ["role_id", "title"],
                            "additionalProperties": False,
                        },
                    },
                    "planning_context": {"type": "string", "default": ""},
                },
                "required": ["items"],
            },
            func=delegate_work,
            category="collaboration",
        ),
        ToolDefinition(
            name="modify_work_item",
            description=(
                "Revise an existing child WorkItem on the current manager board. "
                "Use when the owner is still correct but the brief, deliverables, "
                "acceptance criteria, dependencies, or boundaries need to change. "
                "The tool can reset the item to ready so the worker reruns the revised brief."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "work_item_id": {"type": "string"},
                    "title": {"type": "string", "default": ""},
                    "task_brief": {"type": "string", "default": ""},
                    "brief": {"type": "string", "default": ""},
                    "summary": {"type": "string", "default": ""},
                    "work_kind": {"type": "string", "default": ""},
                    "deliverables": {"type": "array", "items": {"type": "string"}},
                    "outputs": {"type": "array", "items": {"type": "string"}},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    "done_when": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {}},
                    "dependency_work_item_ids": {"type": "array", "items": {"type": "string"}},
                    "coordination_notes": {"type": "string", "default": ""},
                    "delegation_rationale": {"type": "string", "default": ""},
                    "non_overlap_guard": {"type": "string", "default": ""},
                    "reason": {"type": "string", "default": ""},
                    "reset_to_ready": {"type": "boolean", "default": True},
                    "clear_outputs": {"type": "boolean", "default": True},
                },
                "required": ["work_item_id"],
            },
            func=modify_work_item,
            category="collaboration",
        ),
        ToolDefinition(
            name="delete_work_item",
            description=(
                "Cancel or hide an obsolete/wrong child WorkItem on the current manager board. "
                "Use when the item should no longer block parent synthesis. "
                "Optionally rewrite downstream dependencies to replacement work item IDs."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "work_item_id": {"type": "string"},
                    "reason": {"type": "string", "default": ""},
                    "replacement_dependency_work_item_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "default": [],
                    },
                },
                "required": ["work_item_id"],
            },
            func=delete_work_item,
            category="collaboration",
        ),
        ToolDefinition(
            name="manager_board_read",
            description=(
                "READ-ONLY. List your direct reports' child work items with "
                "title, phase (the granular 14-state machine value), "
                "kanban_column (one of todo / in-progress / in-review / done), "
                "deliverable_summary, and blocker info. Use when you want to "
                "SEE the current state of your team's board. Kanban state "
                "transitions are handled by the runtime. Use modify_work_item or "
                "delete_work_item when the manager must revise the active board. "
                "In attention/monitor turns, "
                "omitting parent_work_item_id reads the underlying business parent board."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "parent_work_item_id": {"type": "string", "default": ""},
                    "include_children": {"type": "boolean", "default": True},
                },
            },
            func=manager_board_read,
            category="collaboration",
        ),
    ]
