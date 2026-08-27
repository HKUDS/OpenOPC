"""Canonical prompt contract for company-mode WorkItems.

The renderer consumes this contract directly. Legacy fields such as
``summary``/``brief`` remain useful for UI and audit, but prompt assembly
should not re-derive its own packet from them after a contract exists.
"""

from __future__ import annotations

import copy
from typing import Any


PROMPT_CONTRACT_VERSION = 2


def normalize_prompt_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            if isinstance(item, dict):
                rendered = str(
                    item.get("value", "")
                    or item.get("input", "")
                    or item.get("raw", "")
                    or item.get("work_item_id", "")
                    or ""
                ).strip()
            else:
                rendered = str(item).strip()
            if rendered:
                items.append(rendered)
        return items
    rendered = str(value).strip()
    return [rendered] if rendered else []


def normalize_dependency_specs(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [copy.deepcopy(item) for item in value if str(item).strip() or isinstance(item, dict)]
    if isinstance(value, dict):
        return [copy.deepcopy(value)]
    rendered = str(value).strip()
    return [rendered] if rendered else []


def normalize_prompt_contract(value: Any) -> dict[str, Any]:
    payload = copy.deepcopy(value) if isinstance(value, dict) else {}
    assignment = dict(payload.get("assignment_context", {}) or {})
    try:
        version = int(payload.get("version") or PROMPT_CONTRACT_VERSION)
    except (TypeError, ValueError):
        version = PROMPT_CONTRACT_VERSION
    normalized = {
        "version": version,
        "task_brief": str(payload.get("task_brief", "") or "").strip(),
        "assignment_context": {
            "upstream_intent_summary": str(assignment.get("upstream_intent_summary", "") or "").strip(),
            "manager_planning_handoff": str(assignment.get("manager_planning_handoff", "") or "").strip(),
            "manager_outcome_dispatch": bool(assignment.get("manager_outcome_dispatch", False)),
            "owned_outcome_kind": str(assignment.get("owned_outcome_kind", "") or "execute").strip() or "execute",
            "scope_key": str(assignment.get("scope_key", "") or "").strip(),
            "deliverables": normalize_prompt_text_list(assignment.get("deliverables", [])),
            "acceptance_criteria": normalize_prompt_text_list(assignment.get("acceptance_criteria", [])),
            "dependency_specs": normalize_dependency_specs(assignment.get("dependency_specs", [])),
            "coordination_notes": str(assignment.get("coordination_notes", "") or "").strip(),
            "delegation_rationale": str(assignment.get("delegation_rationale", "") or "").strip(),
            "non_overlap_guard": str(assignment.get("non_overlap_guard", "") or "").strip(),
        },
        "turn_profiles": copy.deepcopy(dict(payload.get("turn_profiles", {}) or {})),
    }
    if payload.get("target_contract"):
        normalized["target_contract"] = normalize_prompt_contract(payload.get("target_contract"))
    source = dict(payload.get("source", {}) or {})
    if source:
        normalized["source"] = copy.deepcopy(source)
    return normalized


def has_prompt_contract(value: Any) -> bool:
    contract = normalize_prompt_contract(value)
    return bool(contract.get("task_brief") or any(_assignment_has_content(contract)))


def _assignment_has_content(contract: dict[str, Any]) -> list[Any]:
    assignment = dict(contract.get("assignment_context", {}) or {})
    return [
        assignment.get("upstream_intent_summary"),
        assignment.get("manager_planning_handoff"),
        assignment.get("manager_outcome_dispatch"),
        assignment.get("scope_key"),
        assignment.get("deliverables"),
        assignment.get("acceptance_criteria"),
        assignment.get("dependency_specs"),
        assignment.get("coordination_notes"),
        assignment.get("delegation_rationale"),
        assignment.get("non_overlap_guard"),
    ]


def make_prompt_contract(
    *,
    task_brief: str,
    upstream_intent_summary: str = "",
    manager_planning_handoff: str = "",
    manager_outcome_dispatch: bool = False,
    owned_outcome_kind: str = "execute",
    scope_key: str = "",
    deliverables: Any = None,
    acceptance_criteria: Any = None,
    dependency_specs: Any = None,
    coordination_notes: str = "",
    delegation_rationale: str = "",
    non_overlap_guard: str = "",
    turn_profiles: dict[str, Any] | None = None,
    target_contract: dict[str, Any] | None = None,
    source: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": PROMPT_CONTRACT_VERSION,
        "task_brief": str(task_brief or "").strip(),
        "assignment_context": {
            "upstream_intent_summary": str(upstream_intent_summary or "").strip(),
            "manager_planning_handoff": str(manager_planning_handoff or "").strip(),
            "manager_outcome_dispatch": bool(manager_outcome_dispatch),
            "owned_outcome_kind": str(owned_outcome_kind or "execute").strip() or "execute",
            "scope_key": str(scope_key or "").strip(),
            "deliverables": normalize_prompt_text_list(deliverables),
            "acceptance_criteria": normalize_prompt_text_list(acceptance_criteria),
            "dependency_specs": normalize_dependency_specs(dependency_specs),
            "coordination_notes": str(coordination_notes or "").strip(),
            "delegation_rationale": str(delegation_rationale or "").strip(),
            "non_overlap_guard": str(non_overlap_guard or "").strip(),
        },
        "turn_profiles": copy.deepcopy(dict(turn_profiles or {})),
    }
    if target_contract:
        payload["target_contract"] = normalize_prompt_contract(target_contract)
    if source:
        payload["source"] = copy.deepcopy(dict(source))
    return normalize_prompt_contract(payload)


def make_prompt_contract_blocker(reason: str) -> dict[str, Any]:
    return make_prompt_contract(
        task_brief="SYSTEM BLOCKER: prompt_contract is missing or incomplete for this WorkItem.",
        deliverables=["Do not execute the original task until a valid prompt_contract exists."],
        acceptance_criteria=[str(reason or "Missing prompt contract.").strip()],
        source={"kind": "prompt_contract_blocker"},
    )


def prompt_contract_from_delegate_item(
    item: dict[str, Any],
    *,
    task_brief: str,
    upstream_intent_summary: str = "",
    manager_planning_handoff: str = "",
    manager_outcome_dispatch: bool = False,
    owned_outcome_kind: str = "execute",
    scope_key: str = "",
    dependency_specs: Any = None,
) -> dict[str, Any]:
    return make_prompt_contract(
        task_brief=task_brief,
        upstream_intent_summary=upstream_intent_summary,
        manager_planning_handoff=manager_planning_handoff,
        manager_outcome_dispatch=manager_outcome_dispatch,
        owned_outcome_kind=owned_outcome_kind,
        scope_key=scope_key,
        deliverables=item.get("deliverables", item.get("outputs", [])),
        acceptance_criteria=item.get("acceptance_criteria", item.get("done_when", [])),
        dependency_specs=dependency_specs,
        coordination_notes=str(item.get("coordination_notes", "") or "").strip(),
        delegation_rationale=str(item.get("delegation_rationale", "") or "").strip(),
        non_overlap_guard=str(item.get("non_overlap_guard", "") or "").strip(),
        source={"kind": "delegate_work"},
    )


def prompt_contract_from_work_item(
    work_item: Any,
    *,
    task_metadata: dict[str, Any] | None = None,
    task_description: str = "",
) -> dict[str, Any]:
    """Build a one-time compatibility contract outside the renderer."""
    metadata = dict(getattr(work_item, "metadata", {}) or {})
    task_metadata = dict(task_metadata or {})
    existing = metadata.get("prompt_contract") or task_metadata.get("prompt_contract")
    if has_prompt_contract(existing):
        return normalize_prompt_contract(existing)
    legacy_assignment = dict(metadata.get("prompt_assignment", {}) or task_metadata.get("prompt_assignment", {}) or {})
    task_brief = str(
        legacy_assignment.get("task_brief", "")
        or legacy_assignment.get("primary_task_brief", "")
        or metadata.get("brief", "")
        or getattr(work_item, "summary", "")
        or task_description
        or getattr(work_item, "title", "")
        or ""
    ).strip()
    if not task_brief:
        return make_prompt_contract_blocker(
            f"WorkItem `{str(getattr(work_item, 'work_item_id', '') or '').strip()}` has no task_brief."
        )
    playbook = dict(metadata.get("delegation_playbook", {}) or task_metadata.get("delegation_playbook", {}) or {})
    return make_prompt_contract(
        task_brief=task_brief,
        upstream_intent_summary=str(
            legacy_assignment.get("upstream_intent_summary", "")
            or metadata.get("upstream_intent_summary", "")
            or task_metadata.get("global_intent_summary", "")
            or playbook.get("global_intent_summary", "")
            or playbook.get("intent_summary", "")
            or ""
        ).strip(),
        manager_planning_handoff=str(
            legacy_assignment.get("manager_planning_handoff", "")
            or metadata.get("manager_planning_handoff", "")
            or metadata.get("planning_context", "")
            or ""
        ).strip(),
        manager_outcome_dispatch=bool(
            legacy_assignment.get("manager_outcome_dispatch", metadata.get("manager_outcome_dispatch", False))
        ),
        owned_outcome_kind=str(
            legacy_assignment.get("owned_outcome_kind", metadata.get("owned_outcome_kind", getattr(work_item, "kind", "execute")))
            or "execute"
        ).strip(),
        scope_key=str(legacy_assignment.get("scope_key", metadata.get("scope_key", "")) or "").strip(),
        deliverables=legacy_assignment.get("deliverables", legacy_assignment.get("outputs", metadata.get("deliverables", metadata.get("outputs", [])))),
        acceptance_criteria=legacy_assignment.get(
            "acceptance_criteria",
            legacy_assignment.get("done_when", metadata.get("acceptance_criteria", metadata.get("done_when", []))),
        ),
        dependency_specs=legacy_assignment.get(
            "dependency_specs",
            legacy_assignment.get("dependency_work_item_ids", metadata.get("dependency_specs", metadata.get("dependency_work_item_ids", []))),
        ),
        coordination_notes=str(legacy_assignment.get("coordination_notes", metadata.get("coordination_notes", "")) or "").strip(),
        delegation_rationale=str(legacy_assignment.get("delegation_rationale", metadata.get("delegation_rationale", "")) or "").strip(),
        non_overlap_guard=str(legacy_assignment.get("non_overlap_guard", metadata.get("non_overlap_guard", "")) or "").strip(),
        source={"kind": "normalized_legacy_work_item"},
    )


def render_assignment_context_from_contract(
    contract: dict[str, Any],
    *,
    include_dispatch_fields: bool = False,
) -> str:
    contract = normalize_prompt_contract(contract)
    assignment = dict(contract.get("assignment_context", {}) or {})
    lines: list[str] = ["## Work Item Assignment Context"]
    if include_dispatch_fields and assignment.get("manager_outcome_dispatch"):
        owned_kind = str(assignment.get("owned_outcome_kind") or "execute").strip()
        lines.append(
            "Manager outcome turn: you own the final outcome. Decide whether to "
            "complete it directly, delegate suitable parts to direct reports, or "
            f"combine both approaches. Owned outcome kind: {owned_kind}. Base the "
            "choice on the work itself and the available downstream seats."
        )
    if assignment.get("upstream_intent_summary"):
        lines.extend(["", "### Upstream Intent Summary", str(assignment["upstream_intent_summary"])])
    if include_dispatch_fields and assignment.get("manager_planning_handoff"):
        lines.extend(["", "### Manager Planning Handoff", str(assignment["manager_planning_handoff"])])
    if assignment.get("scope_key"):
        lines.extend(["", "### Scope Key", str(assignment["scope_key"])])
    for title, key in (
        ("Deliverables", "deliverables"),
        ("Acceptance Criteria", "acceptance_criteria"),
        ("Dependencies", "dependency_specs"),
    ):
        items = normalize_prompt_text_list(assignment.get(key, []))
        if items:
            lines.extend(["", f"### {title}"])
            lines.extend(f"- {item}" for item in items)
    for title, key in (
        ("Coordination Notes", "coordination_notes"),
        ("Delegation Rationale", "delegation_rationale"),
        ("Boundaries / Non-overlap Guard", "non_overlap_guard"),
    ):
        value = str(assignment.get(key, "") or "").strip()
        if value:
            lines.extend(["", f"### {title}", value])
    if len(lines) == 1:
        return ""
    return "\n".join(lines).strip()


def render_target_prompt_contract(contract: dict[str, Any], *, heading: str = "### Target Work Item Contract") -> str:
    contract = normalize_prompt_contract(contract)
    lines: list[str] = [heading]
    task_brief = str(contract.get("task_brief", "") or "").strip()
    if task_brief:
        lines.extend(["", "#### Task Brief", task_brief])
    assignment = render_assignment_context_from_contract(contract, include_dispatch_fields=True)
    if assignment:
        assignment = assignment.replace("## Work Item Assignment Context", "#### Assignment Context", 1)
        lines.extend(["", assignment])
    if len(lines) == 1:
        return ""
    return "\n".join(lines).strip()


def is_report_prompt_turn(metadata: dict[str, Any] | None) -> bool:
    payload = dict(metadata or {})
    return bool(
        payload.get("report_execution_work_item")
        or str(payload.get("current_turn_mode", "") or "").strip() == "report_required"
        or str(payload.get("work_item_turn_type", "") or "").strip() == "report"
        or str(payload.get("work_kind", "") or "").strip() == "report"
    )
