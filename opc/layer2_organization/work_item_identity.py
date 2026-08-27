"""Helpers for company work-item projection identity metadata."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


WORK_ITEM_PROJECTION_ID_KEY = "work_item_projection_id"
WORK_ITEM_TURN_TYPE_KEY = "work_item_turn_type"
RESULT_DELIVERY_ID_KEY = "result_delivery_id"
SOURCE_TASK_ID_KEY = "source_task_id"
GATE_REWORK_PROJECTION_ID_KEY = "rework_projection_id"
GATE_TARGET_PROJECTION_ID_KEY = "target_projection_id"
GATE_TARGET_PROJECTION_IDS_KEY = "target_projection_ids"

CANONICAL_WORK_ITEM_TURN_TYPES: frozenset[str] = frozenset(
    {
        "intake",
        "dispatch",
        "plan",
        "setup",
        "execute",
        "review",
        "report",
        "followup",
        "monitor",
        "aggregate",
        "deliver",
        "self_evolution",
    }
)

_TURN_TYPE_ALIASES: dict[str, str] = {
    "delegate": "dispatch",
    "delegation": "dispatch",
    "delivery": "deliver",
    "follow-up": "followup",
    "follow_up": "followup",
    "synthesis": "aggregate",
    "synthesize": "aggregate",
    "self-evolution": "self_evolution",
    "self evolution": "self_evolution",
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def normalize_work_item_turn_type(value: Any, *, fallback: str = "") -> str:
    """Normalize runtime/work-item turn-kind aliases to canonical names."""
    normalized = _clean(value).lower() or _clean(fallback).lower()
    return _TURN_TYPE_ALIASES.get(normalized, normalized)


def canonical_work_item_turn_type_for_kind(value: Any, *, fallback: str = "execute") -> str:
    """Map a WorkItem/runtime business kind to the canonical runtime turn type."""
    normalized = normalize_work_item_turn_type(value, fallback="")
    if normalized in CANONICAL_WORK_ITEM_TURN_TYPES:
        return normalized
    fallback_normalized = normalize_work_item_turn_type(fallback, fallback="")
    if fallback_normalized in CANONICAL_WORK_ITEM_TURN_TYPES:
        return fallback_normalized
    return ""


def initial_current_turn_mode_for_work_item(
    turn_type: Any,
    *,
    manager_can_delegate: bool = False,
    review_execution_work_item: bool = False,
    report_execution_work_item: bool = False,
) -> str:
    """Return the authoritative initial driver mode for a new WorkItem."""
    normalized_turn = canonical_work_item_turn_type_for_kind(turn_type)
    if normalized_turn == "deliver":
        return "deliver_required"
    if normalized_turn == "aggregate":
        return "synthesize_required"
    if report_execution_work_item or normalized_turn == "report":
        return "report_required"
    if review_execution_work_item or normalized_turn == "review":
        return "review_execute"
    if manager_can_delegate:
        return "manager_decide"
    return "worker_execute"


def work_item_projection_id_from_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    fallback: str = "",
) -> str:
    """Read the canonical projected work-item task identity."""
    if not metadata:
        return _clean(fallback)
    value = _clean(metadata.get(WORK_ITEM_PROJECTION_ID_KEY))
    if value:
        return value
    return _clean(fallback)


def work_item_turn_type_from_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    fallback: str = "execute",
) -> str:
    """Read the canonical company work-item turn type."""
    if not metadata:
        return _clean(fallback).lower()
    for key in (
        WORK_ITEM_TURN_TYPE_KEY,
        "work_kind",
        "delegation_turn_kind",
    ):
        value = normalize_work_item_turn_type(metadata.get(key))
        if value:
            return value
    return normalize_work_item_turn_type(fallback)


def projection_id_for_task(task: Any) -> str:
    """Return the work-item projection identity for a projected Task."""
    metadata = dict(getattr(task, "metadata", {}) or {})
    return work_item_projection_id_from_metadata(
        metadata,
        fallback=_clean(getattr(task, "id", "")),
    )


def turn_type_for_task(task: Any, *, fallback: str = "execute") -> str:
    """Return the work-item turn type for a projected Task."""
    metadata = dict(getattr(task, "metadata", {}) or {})
    return work_item_turn_type_from_metadata(metadata, fallback=fallback)


def projection_id_for_work_item(item: Any) -> str:
    """Return the projection identity for a DelegationWorkItem-like object."""
    explicit_projection = _clean(getattr(item, "projection_id", ""))
    if explicit_projection:
        return explicit_projection
    metadata = dict(getattr(item, "metadata", {}) or {})
    return work_item_projection_id_from_metadata(
        metadata,
        fallback=(
            _clean(getattr(item, "projection_id", ""))
            or _clean(getattr(item, "work_item_id", ""))
        ),
    )


def turn_type_for_work_item(item: Any, *, fallback: str = "execute") -> str:
    """Return the turn type for a DelegationWorkItem-like object."""
    metadata = dict(getattr(item, "metadata", {}) or {})
    return work_item_turn_type_from_metadata(
        metadata,
        fallback=_clean(getattr(item, "kind", "")) or fallback,
    )


def canonical_turn_type_for_work_item(item: Any, *, fallback: str = "execute") -> str:
    """Return a canonical turn type for a WorkItem-like object or metadata."""
    if isinstance(item, Mapping):
        return work_item_turn_type_from_metadata(item, fallback=fallback)
    return turn_type_for_work_item(item, fallback=fallback)


def _turn_type_for_value(value: Any, *, fallback: str = "") -> str:
    if isinstance(value, Mapping):
        return work_item_turn_type_from_metadata(value, fallback=fallback)
    if hasattr(value, "metadata"):
        return canonical_turn_type_for_work_item(value, fallback=fallback or "execute")
    return canonical_work_item_turn_type_for_kind(value, fallback=fallback)


def is_delivery_turn(value_or_metadata: Any) -> bool:
    """Return True for final delivery turns, including legacy ``delivery`` alias."""
    return _turn_type_for_value(value_or_metadata, fallback="") == "deliver"


def is_manager_reviewable_turn(value_or_metadata: Any) -> bool:
    """Return the turn type's default manager-review policy.

    Dispatch/intake/plan are normally board-producing turns and therefore
    exempt.  The executor handles the one dynamic exception — a current turn
    that produced no board mutation — before it writes the authoritative
    WorkItem phase.  Downstream review plumbing follows that phase directly.
    """
    turn_type = _turn_type_for_value(value_or_metadata, fallback="")
    if not turn_type:
        return False
    return turn_type not in {"intake", "plan", "dispatch", "aggregate", "deliver", "self_evolution"}


def mark_work_item_projection(
    metadata: Mapping[str, Any] | None = None,
    *,
    projection_id: str = "",
    turn_type: str = "",
) -> dict[str, Any]:
    """Return metadata with canonical work-item projection keys only."""
    result = dict(metadata or {})
    projection = _clean(projection_id) or work_item_projection_id_from_metadata(result)
    turn = normalize_work_item_turn_type(turn_type) or work_item_turn_type_from_metadata(result)
    if projection:
        result[WORK_ITEM_PROJECTION_ID_KEY] = projection
    if turn:
        result[WORK_ITEM_TURN_TYPE_KEY] = turn
    return result


def mark_projected_work_item_task(
    metadata: Mapping[str, Any] | None = None,
    *,
    projection_id: str = "",
    turn_type: str = "",
) -> dict[str, Any]:
    """Return projected task/work-item metadata with canonical identity keys."""
    return mark_work_item_projection(
        metadata,
        projection_id=projection_id,
        turn_type=turn_type,
    )


def work_item_identity_payload(
    *,
    projection_id: str = "",
    turn_type: str = "",
    source: Mapping[str, Any] | None = None,
    include_empty: bool = False,
) -> dict[str, str]:
    """Build a canonical event/checkpoint/ws payload identity fragment."""
    source_meta = dict(source or {})
    projection = _clean(projection_id) or work_item_projection_id_from_metadata(source_meta, fallback="")
    turn = normalize_work_item_turn_type(turn_type) or work_item_turn_type_from_metadata(source_meta, fallback="")
    payload: dict[str, str] = {}
    if projection or include_empty:
        payload[WORK_ITEM_PROJECTION_ID_KEY] = projection
    if turn or include_empty:
        payload[WORK_ITEM_TURN_TYPE_KEY] = turn
    return payload


def work_item_identity_payload_from_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    projection_id_fallback: str = "",
    turn_type_fallback: str = "",
    include_empty: bool = False,
) -> dict[str, str]:
    """Build a canonical payload identity fragment from metadata."""
    source_meta = dict(metadata or {})
    return work_item_identity_payload(
        projection_id=work_item_projection_id_from_metadata(
            source_meta,
            fallback=projection_id_fallback,
        ),
        turn_type=work_item_turn_type_from_metadata(
            source_meta,
            fallback=turn_type_fallback,
        ),
        include_empty=include_empty,
    )


def work_item_identity_payload_for_task(
    task: Any,
    *,
    fallback_turn_type: str = "",
    include_empty: bool = False,
) -> dict[str, str]:
    """Build a canonical payload identity fragment for a Task-like object."""
    if task is None:
        return work_item_identity_payload(
            turn_type=fallback_turn_type,
            include_empty=include_empty,
        )
    return work_item_identity_payload(
        projection_id=projection_id_for_task(task),
        turn_type=turn_type_for_task(task, fallback=fallback_turn_type),
        include_empty=include_empty,
    )


def company_work_item_gate_attempt(
    task: Any,
    gate_metadata: Mapping[str, Any] | None = None,
) -> int:
    """Return the durable attempt identity used by work-item gate cards."""

    task_metadata = dict(getattr(task, "metadata", {}) or {})
    gate_values = dict(gate_metadata or {})
    values: list[int] = []
    for value in (
        task_metadata.get("gate_rework_count", 0),
        task_metadata.get("review_attempt_count", 0),
        gate_values.get("review_attempt", 0),
        gate_values.get("gate_rework_round", 0),
    ):
        try:
            values.append(max(0, int(value or 0)))
        except (TypeError, ValueError):
            values.append(0)
    return max(values, default=0)


def canonical_company_work_item_gate_envelope(
    gate: Mapping[str, Any] | Any | None,
) -> dict[str, Any]:
    """Return the semantic gate policy bound to a decision checkpoint."""

    if isinstance(gate, Mapping):
        raw = dict(gate)
        gate_type = raw.get("type", raw.get("gate_type", ""))
        rework_projection_id = raw.get("rework_projection_id", "")
        metadata = dict(raw.get("metadata", {}) or {})
    elif gate is not None:
        raw = {}
        gate_type = getattr(gate, "gate_type", "")
        rework_projection_id = getattr(gate, "rework_projection_id", "")
        metadata = dict(getattr(gate, "metadata", {}) or {})
    else:
        raw = {}
        gate_type = ""
        rework_projection_id = ""
        metadata = {}
    rework_projection_id = _clean(
        rework_projection_id
        or metadata.get(GATE_REWORK_PROJECTION_ID_KEY)
    )
    semantic_metadata_keys = (
        "source",
        "recommended_action",
        "review_level",
        "review_target_role_id",
        "review_chain_role_ids",
        "constraints",
        "blockers",
        "blocker_types",
        "review_attempt",
        "gate_rework_round",
        "target_projection_id",
        "target_projection_ids",
        "manager_gate_fallback",
        "manager_gate_original_reviewer_role",
    )
    semantic_metadata = {
        key: metadata[key]
        for key in semantic_metadata_keys
        if key in metadata
    }
    try:
        max_retries = max(
            0,
            int(
                raw.get("max_retries", 0)
                if isinstance(gate, Mapping)
                else getattr(gate, "max_retries", 0) if gate is not None else 0
            ),
        )
    except (TypeError, ValueError, OverflowError):
        max_retries = 0
    return {
        "type": _clean(gate_type),
        "instructions": _clean(
            raw.get("instructions", "")
            if isinstance(gate, Mapping)
            else getattr(gate, "instructions", "") if gate is not None else ""
        ),
        "reviewer_role": _clean(
            raw.get("reviewer_role", "")
            if isinstance(gate, Mapping)
            else getattr(gate, "reviewer_role", "") if gate is not None else ""
        ),
        "requires_human": bool(
            raw.get("requires_human", False)
            if isinstance(gate, Mapping)
            else getattr(gate, "requires_human", False) if gate is not None else False
        ),
        "on_reject": _clean(
            raw.get("on_reject", "")
            if isinstance(gate, Mapping)
            else getattr(gate, "on_reject", "") if gate is not None else ""
        ).lower(),
        GATE_REWORK_PROJECTION_ID_KEY: rework_projection_id,
        "max_retries": max_retries,
        "metadata": semantic_metadata,
    }


def company_work_item_gate_human_fallback_payload(
    gate: Mapping[str, Any] | Any | None,
) -> dict[str, Any]:
    """Return the sole manager-gate-to-owner fallback representation.

    The original reviewer is retained in the hashed semantic envelope, while
    ``requires_human`` and ``review_level`` describe the effective owner wait.
    An already-human or reviewer-less policy cannot manufacture a fallback.
    """

    if isinstance(gate, Mapping):
        raw = dict(gate)
        metadata = dict(raw.get("metadata", {}) or {})
        reviewer_role = _clean(raw.get("reviewer_role", ""))
        requires_human = bool(raw.get("requires_human", False))
        gate_type = raw.get("type", raw.get("gate_type", ""))
    elif gate is not None:
        metadata = dict(getattr(gate, "metadata", {}) or {})
        reviewer_role = _clean(getattr(gate, "reviewer_role", ""))
        requires_human = bool(getattr(gate, "requires_human", False))
        gate_type = getattr(gate, "gate_type", "")
        raw = {
            "type": gate_type,
            "instructions": getattr(gate, "instructions", ""),
            "reviewer_role": reviewer_role,
            "requires_human": requires_human,
            "on_reject": getattr(gate, "on_reject", "halt"),
            "rework_projection_id": getattr(
                gate,
                "rework_projection_id",
                None,
            ),
            "max_retries": getattr(gate, "max_retries", 1),
        }
    else:
        return {}
    if not reviewer_role or requires_human:
        return {}
    metadata.update(
        {
            "review_level": "human",
            "manager_gate_fallback": True,
            "manager_gate_original_reviewer_role": reviewer_role,
        }
    )
    raw["type"] = gate_type
    raw.pop("gate_type", None)
    raw["reviewer_role"] = None
    raw["requires_human"] = True
    raw["metadata"] = metadata
    return raw


def company_work_item_gate_basis_hash(
    task: Any,
    gate: Mapping[str, Any] | Any | None = None,
) -> str:
    """Hash the exact Task projection authorized by a work-item gate.

    This lives beside canonical work-item identity so the producer and the
    Store transaction validate one implementation rather than maintaining
    subtly different hashes across runtime layers.
    """

    task_metadata = dict(getattr(task, "metadata", {}) or {})
    context_snapshot = dict(getattr(task, "context_snapshot", {}) or {})
    output_metadata = dict(
        context_snapshot.get("work_item_owned_outputs", {}) or {}
    )
    result = getattr(task, "result", None)
    if isinstance(result, dict):
        result_content = _clean(result.get("content"))
    elif result:
        result_content = _clean(result)
    else:
        result_content = ""
    payload = {
        "task_id": _clean(getattr(task, "id", "")),
        **work_item_identity_payload_for_task(task),
        "delivery_revision": task_metadata.get("delivery_revision", ""),
        "owner_directive_revision": task_metadata.get(
            "owner_directive_revision", ""
        ),
        "work_item_attempt_seq": _clean(
            task_metadata.get("claimed_work_item_attempt_seq", 0)
        ),
        "result_content": result_content,
        "work_item_summary": _clean(
            output_metadata.get("work_item_summary", "")
            or task_metadata.get("work_item_summary", "")
        ),
        "work_item_summary_for_downstream": _clean(
            output_metadata.get("work_item_summary_for_downstream", "")
            or task_metadata.get("work_item_summary_for_downstream", "")
        ),
        "artifact_index": list(
            output_metadata.get("work_item_artifact_index", [])
            or task_metadata.get("work_item_artifact_index", [])
            or []
        ),
        "verification_status": dict(
            output_metadata.get("verification_status", {})
            or task_metadata.get("verification_status", {})
            or {}
        ),
        "verification_evidence": dict(
            output_metadata.get("verification_evidence", {})
            or task_metadata.get("verification_evidence", {})
            or {}
        ),
        "verification_verdict": _clean(
            task_metadata.get("verification_verdict", "")
        ),
        "delivery_package": (
            output_metadata.get("delivery_package")
            or task_metadata.get("delivery_package")
            or {}
        ),
        "work_item_gate": canonical_company_work_item_gate_envelope(
            task_metadata.get("work_item_gate")
        ),
        "checkpoint_gate": canonical_company_work_item_gate_envelope(gate),
        "gate_harness_pending_decision": dict(
            task_metadata.get("gate_harness_pending_decision", {}) or {}
        ),
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha1(encoded.encode("utf-8")).hexdigest()


def canonical_result_turn_id_for_task(
    task: Any,
    *,
    canonical_turn_id: str = "",
) -> str:
    """Return the canonical conversation turn owning a task result.

    Runtime events also carry iteration-scoped ``turn_id`` values.  Result
    surfaces must never use those values as their logical delivery identity,
    so this helper only falls back to the task's canonical runtime fields.
    """
    explicit = _clean(canonical_turn_id)
    if explicit:
        return explicit
    metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
    runtime_metadata = dict(metadata.get("runtime_v2", {}) or {})
    for source in (metadata, runtime_metadata):
        for key in (
            "canonical_turn_id",
            "conversation_turn_id",
            "current_turn_id",
            "runtime_v2_current_turn_id",
        ):
            value = _clean(source.get(key))
            if value:
                return value
    return ""


def result_delivery_id_for_task(
    task: Any,
    *,
    canonical_turn_id: str = "",
    execution_id: str = "",
    result_delivery_id: str = "",
) -> str:
    """Build one stable identity shared by every surface of a task result.

    The attempt suffix keeps a failed result and its retry distinct while the
    canonical turn (when available) links the runtime final, child result and
    parent mirror without inspecting their display text.
    """
    explicit = _clean(result_delivery_id)
    if explicit:
        return explicit
    task_id = _clean(getattr(task, "id", ""))
    turn_id = canonical_result_turn_id_for_task(
        task,
        canonical_turn_id=canonical_turn_id,
    )
    execution = _clean(execution_id)
    if not task_id or (not turn_id and not execution):
        return ""
    try:
        attempt = max(0, int(getattr(task, "retry_count", 0) or 0))
    except (TypeError, ValueError):
        attempt = 0
    identity_kind = "turn" if turn_id else "execution"
    identity_value = turn_id or execution
    return f"result:task:{task_id}:{identity_kind}:{identity_value}:attempt:{attempt}"


def result_delivery_identity_payload_for_task(
    task: Any,
    *,
    canonical_turn_id: str = "",
    execution_id: str = "",
    result_delivery_id: str = "",
) -> dict[str, str]:
    """Return structured lineage shared by persisted result projections."""
    delivery_id = result_delivery_id_for_task(
        task,
        canonical_turn_id=canonical_turn_id,
        execution_id=execution_id,
        result_delivery_id=result_delivery_id,
    )
    source_task_id = _clean(getattr(task, "id", ""))
    canonical_id = canonical_result_turn_id_for_task(
        task,
        canonical_turn_id=canonical_turn_id,
    )
    return {
        **({RESULT_DELIVERY_ID_KEY: delivery_id} if delivery_id else {}),
        **({SOURCE_TASK_ID_KEY: source_task_id} if source_task_id else {}),
        **({"canonical_turn_id": canonical_id} if canonical_id else {}),
    }


def migrate_work_item_projection_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    projection_id_fallback: str = "",
    turn_type_fallback: str = "",
) -> tuple[dict[str, Any], bool]:
    """Normalize canonical projection metadata from canonical inputs only."""
    before = dict(metadata or {})
    result = dict(before)
    projection = work_item_projection_id_from_metadata(
        result,
        fallback=projection_id_fallback,
    )
    turn = work_item_turn_type_from_metadata(
        result,
        fallback=turn_type_fallback or "execute",
    )
    if projection and not _clean(result.get(WORK_ITEM_PROJECTION_ID_KEY)):
        result[WORK_ITEM_PROJECTION_ID_KEY] = projection
    if turn and not _clean(result.get(WORK_ITEM_TURN_TYPE_KEY)):
        result[WORK_ITEM_TURN_TYPE_KEY] = turn
    return result, result != before


def rework_projection_id_for_gate(gate: Any, *, fallback: str = "") -> str:
    """Return the gate rework target as a work-item projection identity."""
    metadata = dict(getattr(gate, "metadata", {}) or {})
    return _clean(
        metadata.get(GATE_REWORK_PROJECTION_ID_KEY)
        or getattr(gate, "rework_projection_id", "")
        or fallback
    )


def mark_gate_rework_projection(gate: Any, projection_id: str) -> Any:
    """Attach projection-only gate rework identity."""
    projection = _clean(projection_id)
    metadata = dict(getattr(gate, "metadata", {}) or {})
    if projection:
        metadata[GATE_REWORK_PROJECTION_ID_KEY] = projection
    setattr(gate, "metadata", metadata)
    if hasattr(gate, "rework_projection_id"):
        setattr(gate, "rework_projection_id", projection or None)
    return gate


def target_projection_id_for_decision(decision: Any, *, fallback: str = "") -> str:
    """Return a gate-harness target as a work-item projection identity."""
    if isinstance(decision, Mapping):
        return _clean(decision.get(GATE_TARGET_PROJECTION_ID_KEY) or fallback)
    return _clean(
        getattr(decision, GATE_TARGET_PROJECTION_ID_KEY, "")
        or fallback
    )


def target_projection_ids_for_decision(decision: Any) -> list[str]:
    """Return all gate-harness targets as work-item projection identities."""
    if isinstance(decision, Mapping):
        raw_ids = list(decision.get(GATE_TARGET_PROJECTION_IDS_KEY, []) or [])
    else:
        raw_ids = list(getattr(decision, GATE_TARGET_PROJECTION_IDS_KEY, []) or [])
    if not raw_ids:
        single = target_projection_id_for_decision(decision)
        raw_ids = [single] if single else []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw_ids:
        value = _clean(item)
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def gate_rework_payload(
    *,
    rework_projection_id: str = "",
    target_projection_id: str = "",
    review_projection_id: str = "",
) -> dict[str, Any]:
    """Build projection-only gate/rework payload metadata."""
    rework = _clean(rework_projection_id)
    target = _clean(target_projection_id)
    review = _clean(review_projection_id)
    payload: dict[str, Any] = {}
    if rework:
        payload[GATE_REWORK_PROJECTION_ID_KEY] = rework
    if target:
        payload[GATE_TARGET_PROJECTION_ID_KEY] = target
    if review:
        payload["review_projection_id"] = review
    return payload
