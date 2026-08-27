"""Compile opaque external-team bindings into company execution projections."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from opc.core.config import ExternalTeamBindingConfig
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemDependencySpec,
    WorkItemProjectionSpec,
    WorkItemReviewPolicy,
)


@dataclass(frozen=True)
class CompiledExternalTeamBinding:
    binding_id: str
    boundary_role_id: str
    external_agent: str
    covered_role_ids: tuple[str, ...]
    canonical_seat_id: str
    canonical_projection_id: str = ""
    scope: str = "subtree"
    collapse_subtree: bool = True
    session_scope: str = "company_run"
    max_inflight: int = 1
    failure_policy: str = "fail_closed"
    review_owner_role_id: str = ""
    artifact_isolation: str = "validated_workspace"
    provider_mode: str = "team"
    capability_manifest: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "boundary_role_id": self.boundary_role_id,
            "external_agent": self.external_agent,
            "covered_role_ids": list(self.covered_role_ids),
            "canonical_seat_id": self.canonical_seat_id,
            "canonical_projection_id": self.canonical_projection_id,
            "scope": self.scope,
            "collapse_subtree": self.collapse_subtree,
            "session_scope": self.session_scope,
            "max_inflight": self.max_inflight,
            "failure_policy": self.failure_policy,
            "review_owner_role_id": self.review_owner_role_id,
            "artifact_isolation": self.artifact_isolation,
            "provider_mode": self.provider_mode,
            "execution_unit_kind": "opaque_external_team",
            "capability_manifest": copy.deepcopy(self.capability_manifest),
        }


def is_opaque_external_team_shadow_seat(seat: Any) -> bool:
    """Return whether *seat* is organizational-only under an opaque Team.

    The compiled topology deliberately retains the complete company graph for
    architecture inspection, but only the boundary's canonical seat belongs
    to the OPC execution graph.  Consumers that create runtime identities or
    render executable agents must use this predicate instead of interpreting
    every organizational seat as a separate worker.
    """

    if not isinstance(seat, dict):
        payload = dict(getattr(seat, "__dict__", {}) or {})
    else:
        payload = dict(seat)
    metadata = dict(payload.get("metadata", {}) or {})
    binding_id = str(
        payload.get("covered_by_external_team")
        or metadata.get("covered_by_external_team")
        or ""
    ).strip()
    if not binding_id:
        return False
    dispatchable = payload.get("dispatchable")
    if dispatchable is None:
        dispatchable = metadata.get("dispatchable")
    return dispatchable is not True


def runtime_execution_seats(runtime_topology: dict[str, Any]) -> list[dict[str, Any]]:
    """Project an org-first topology to the seats OPC may execute/render."""

    return [
        dict(seat)
        for seat in list(runtime_topology.get("seats", []) or [])
        if isinstance(seat, dict) and not is_opaque_external_team_shadow_seat(seat)
    ]


def opaque_external_team_hidden_role_ids(org_engine: Any) -> set[str]:
    """Roles represented internally by a provider Team, excluding boundaries."""

    if org_engine is None:
        return set()
    topology = org_engine.build_runtime_delegation_topology()
    compiled = compile_external_team_bindings(
        org_engine,
        runtime_topology=topology,
    )
    boundaries = {binding.boundary_role_id for binding in compiled}
    return {
        role_id
        for binding in compiled
        for role_id in binding.covered_role_ids
        if role_id not in boundaries
    }


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower()).strip("-")


def _binding_id(binding: ExternalTeamBindingConfig) -> str:
    explicit = _slug(binding.binding_id)
    if explicit:
        return explicit
    return f"{_slug(binding.boundary_role_id)}-{_slug(binding.external_agent)}-team"


def _role_descendants(org_engine: Any, boundary_role_id: str) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(role_id: str) -> None:
        if role_id in seen:
            return
        seen.add(role_id)
        ordered.append(role_id)
        for child in list(org_engine.get_subordinates(role_id) or []):
            child_id = str(getattr(child, "role_id", "") or getattr(child, "id", "") or "").strip()
            if child_id:
                visit(child_id)

    visit(boundary_role_id)
    return ordered


def _canonical_seat(topology: dict[str, Any], boundary_role_id: str) -> str:
    candidates = [
        dict(seat)
        for seat in list(topology.get("seats", []) or [])
        if isinstance(seat, dict)
        and str(seat.get("role_id", "") or "").strip() == boundary_role_id
    ]
    if not candidates:
        return ""
    lead = next(
        (
            seat
            for seat in candidates
            if bool(seat.get("is_team_lead", False))
            and str(seat.get("team_id", "") or "").strip() == f"team::{boundary_role_id}"
        ),
        None,
    )
    lead = lead or next((seat for seat in candidates if bool(seat.get("is_team_lead", False))), None)
    return str((lead or candidates[0]).get("seat_id", "") or "").strip()


def _clean_strings(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = re.split(r"[,\n]", values)
    result: list[str] = []
    for value in list(values or []):
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _team_capability_manifest(
    org_engine: Any,
    *,
    binding: ExternalTeamBindingConfig,
    binding_id: str,
    covered_role_ids: list[str],
    review_owner_role_id: str,
) -> dict[str, Any]:
    """Compile a provider-independent routing contract from the live org.

    Nothing in this manifest is keyed to a hard-coded CTO/CMO catalogue. Role
    responsibilities and contracts remain the source of truth; binding
    metadata can add organization-specific capability and deliverable labels.
    """

    role_entries: list[dict[str, Any]] = []
    capabilities: list[str] = []
    skill_refs: list[str] = []
    tools: list[str] = []
    prompt_refs: list[str] = []
    artifact_contract_refs: list[str] = []

    def extend_unique(target: list[str], values: Any) -> None:
        for value in _clean_strings(values):
            if value not in target:
                target.append(value)

    for role_id in covered_role_ids:
        role = org_engine.get_agent(role_id)
        if role is None:
            continue
        role_capabilities = _clean_strings(getattr(role, "capabilities", []))
        role_skills = _clean_strings(getattr(role, "skill_refs", []))
        role_tools = _clean_strings(getattr(role, "tools", []))
        role_prompts = _clean_strings(getattr(role, "prompt_refs", []))
        artifact_contract_ref = str(
            getattr(role, "artifact_contract_ref", "") or ""
        ).strip()
        extend_unique(capabilities, role_capabilities)
        # Skill references are declared organizational abilities and therefore
        # participate in routing, while remaining separately inspectable.
        extend_unique(capabilities, role_skills)
        extend_unique(skill_refs, role_skills)
        extend_unique(tools, role_tools)
        extend_unique(prompt_refs, role_prompts)
        if artifact_contract_ref and artifact_contract_ref not in artifact_contract_refs:
            artifact_contract_refs.append(artifact_contract_ref)
        role_entries.append(
            {
                "role_id": role_id,
                "name": str(getattr(role, "name", "") or role_id).strip(),
                "responsibility": str(
                    getattr(role, "responsibility", "") or ""
                ).strip(),
                "capabilities": role_capabilities,
                "skill_refs": role_skills,
                "tools": role_tools,
                "prompt_refs": role_prompts,
                "artifact_contract_ref": artifact_contract_ref or None,
            }
        )

    binding_metadata = dict(binding.metadata or {})
    extend_unique(capabilities, binding_metadata.get("capabilities", []))
    deliverables = _clean_strings(binding_metadata.get("deliverables", []))
    out_of_scope = _clean_strings(binding_metadata.get("out_of_scope", []))
    boundary = org_engine.get_agent(binding.boundary_role_id)
    boundary_name = str(
        getattr(boundary, "name", "") or binding.boundary_role_id
    ).strip()
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "execution_unit_id": binding_id,
        "organizational_identity": binding.boundary_role_id,
        "display_name": str(
            binding_metadata.get("display_name") or f"{boundary_name} Team"
        ).strip(),
        "provider": str(binding.external_agent).strip(),
        "provider_mode": str(binding.provider_mode or "team").strip(),
        "scope": str(binding.scope or "subtree").strip(),
        "covered_role_ids": list(covered_role_ids),
        "covered_roles": role_entries,
        "capabilities": capabilities,
        "skill_refs": skill_refs,
        "tools": tools,
        "prompt_refs": prompt_refs,
        "deliverables": deliverables,
        "artifact_contract_refs": artifact_contract_refs,
        "out_of_scope": out_of_scope,
        "review_owner_role_id": review_owner_role_id,
        "org_version": int(
            org_engine.current_org_version()
            if hasattr(org_engine, "current_org_version")
            else 0
        ),
    }
    manifest_payload = json.dumps(
        {key: value for key, value in manifest.items() if key != "org_version"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    manifest["manifest_hash"] = hashlib.sha256(
        manifest_payload.encode("utf-8")
    ).hexdigest()
    return manifest


def _delegation_catalog_entry(binding: CompiledExternalTeamBinding) -> dict[str, Any]:
    manifest = dict(binding.capability_manifest or {})
    roles = [
        {
            "role_id": str(role.get("role_id", "") or "").strip(),
            "name": str(role.get("name", "") or "").strip(),
            "responsibility": str(role.get("responsibility", "") or "").strip(),
        }
        for role in list(manifest.get("covered_roles", []) or [])
        if isinstance(role, dict)
    ]
    return {
        "execution_unit_id": binding.binding_id,
        "target_role_id": binding.boundary_role_id,
        "display_name": str(
            manifest.get("display_name") or binding.boundary_role_id
        ).strip(),
        "execution_unit_kind": "opaque_external_team",
        "provider": binding.external_agent,
        "covered_role_ids": list(binding.covered_role_ids),
        "covered_roles": roles,
        "capabilities": list(manifest.get("capabilities", []) or []),
        "deliverables": list(manifest.get("deliverables", []) or []),
        "artifact_contract_refs": list(
            manifest.get("artifact_contract_refs", []) or []
        ),
        "out_of_scope": list(manifest.get("out_of_scope", []) or []),
        "capability_manifest_hash": str(manifest.get("manifest_hash", "") or ""),
    }


def compile_external_team_bindings(
    org_engine: Any,
    *,
    runtime_topology: dict[str, Any],
    bindings: Iterable[ExternalTeamBindingConfig] | None = None,
) -> list[CompiledExternalTeamBinding]:
    configured = list(
        bindings
        if bindings is not None
        else getattr(
            getattr(getattr(org_engine, "config", None), "org", None),
            "external_team_bindings",
            [],
        )
    )
    if not configured:
        return []
    compiled: list[CompiledExternalTeamBinding] = []
    claimed_roles: dict[str, str] = {}
    claimed_binding_ids: set[str] = set()
    known_roles = {
        str(getattr(role, "role_id", "") or getattr(role, "id", "") or "").strip()
        for role in list(org_engine.list_agents() or [])
    }
    for raw in configured:
        binding = (
            raw
            if isinstance(raw, ExternalTeamBindingConfig)
            else ExternalTeamBindingConfig.model_validate(raw)
        )
        if not binding.enabled:
            continue
        boundary = str(binding.boundary_role_id or "").strip()
        if not boundary or boundary not in known_roles:
            raise ValueError(f"external team binding references unknown role: {boundary!r}")
        covered = (
            _role_descendants(org_engine, boundary)
            if binding.scope == "subtree" and binding.collapse_subtree
            else [boundary]
        )
        binding_id = _binding_id(binding)
        if binding_id in claimed_binding_ids:
            raise ValueError(f"duplicate external team binding id: {binding_id!r}")
        claimed_binding_ids.add(binding_id)
        overlaps = [role_id for role_id in covered if role_id in claimed_roles]
        if overlaps:
            prior = claimed_roles[overlaps[0]]
            raise ValueError(
                f"external team bindings {prior!r} and {binding_id!r} overlap at role {overlaps[0]!r}"
            )
        for role_id in covered:
            claimed_roles[role_id] = binding_id
        manager = org_engine.get_agent(boundary)
        configured_review_owner = str(binding.review_owner_role_id or "").strip()
        if configured_review_owner and configured_review_owner not in known_roles:
            raise ValueError(
                f"external team binding {binding_id!r} references unknown review owner: "
                f"{configured_review_owner!r}"
            )
        review_owner = configured_review_owner or str(getattr(manager, "reports_to", "") or "").strip()
        if review_owner == "owner":
            review_owner = str(runtime_topology.get("final_decider_role_id", "") or "").strip()
        if review_owner in covered:
            if configured_review_owner:
                raise ValueError(
                    f"external team binding {binding_id!r} cannot review itself through "
                    f"covered role {review_owner!r}"
                )
            review_owner = ""
        canonical_seat_id = _canonical_seat(runtime_topology, boundary)
        if not canonical_seat_id:
            raise ValueError(
                f"external team boundary {boundary!r} has no runtime seat"
            )
        compiled.append(
            CompiledExternalTeamBinding(
                binding_id=binding_id,
                boundary_role_id=boundary,
                external_agent=str(binding.external_agent).strip(),
                covered_role_ids=tuple(covered),
                canonical_seat_id=canonical_seat_id,
                scope=str(binding.scope or "subtree"),
                collapse_subtree=bool(binding.collapse_subtree),
                session_scope=str(binding.session_scope or "company_run"),
                max_inflight=int(binding.max_inflight or 1),
                failure_policy=str(binding.failure_policy or "fail_closed"),
                review_owner_role_id=review_owner,
                artifact_isolation=str(binding.artifact_isolation or "validated_workspace"),
                provider_mode=str(binding.provider_mode or "team"),
                capability_manifest=_team_capability_manifest(
                    org_engine,
                    binding=binding,
                    binding_id=binding_id,
                    covered_role_ids=covered,
                    review_owner_role_id=review_owner,
                ),
            )
        )
    return compiled


def apply_external_team_bindings_to_topology(
    org_engine: Any,
    runtime_topology: dict[str, Any],
    *,
    compiled_bindings: Iterable[CompiledExternalTeamBinding] | None = None,
) -> dict[str, Any]:
    """Keep the organization graph intact and overlay its execution graph."""

    topology = copy.deepcopy(runtime_topology)
    compiled = (
        list(compiled_bindings)
        if compiled_bindings is not None
        else compile_external_team_bindings(org_engine, runtime_topology=topology)
    )
    if not compiled:
        return topology
    by_role = {
        role_id: binding
        for binding in compiled
        for role_id in binding.covered_role_ids
    }
    raw_seats_by_id = {
        str(seat.get("seat_id", "") or "").strip(): dict(seat)
        for seat in list(topology.get("seats", []) or [])
        if isinstance(seat, dict) and str(seat.get("seat_id", "") or "").strip()
    }
    seats: list[dict[str, Any]] = []
    for raw_seat in list(topology.get("seats", []) or []):
        seat = dict(raw_seat or {})
        role_id = str(seat.get("role_id", "") or "").strip()
        binding = by_role.get(role_id)
        if binding is None:
            seats.append(seat)
            continue
        canonical = str(seat.get("seat_id", "") or "").strip() == binding.canonical_seat_id
        binding_payload = binding.to_dict()
        outside_contact_role_ids = [
            str(item or "").strip()
            for item in list(seat.get("contact_role_ids", []) or [])
            if str(item or "").strip()
            and str(item or "").strip() not in set(binding.covered_role_ids)
        ]
        seat.update(
            {
                "covered_by_external_team": binding.binding_id,
                "external_team_boundary_role_id": binding.boundary_role_id,
                "dispatchable": canonical,
                "execution_unit_kind": "opaque_external_team",
                "selected_execution_agent": binding.external_agent if canonical else "covered",
                "preferred_external_agent": binding.external_agent if canonical else None,
                "execution_agent_locked": True,
                "selected_execution_agent_source": "external_team_binding",
                "force_native_execution": False if canonical else True,
                "company_external_execution_capable": canonical,
                "staffing_locked": True,
                "staffing_mode": "opaque_external_team",
                "employee_id": "",
                "employee_assignment": {},
            }
        )
        if canonical:
            # A role's home-team lead normally points at that same role's
            # liaison seat inside the manager's team.  That liaison becomes a
            # non-executable shadow under an opaque Team, so bridge the
            # canonical boundary directly to the liaison's manager.  Keeping
            # the stale intermediate seat here makes manager-board queries,
            # dependency references, review routing, and inbox ownership all
            # disagree about who owns the WorkItem.
            manager_seat_id = str(seat.get("manager_seat_id", "") or "").strip()
            manager_liaison = raw_seats_by_id.get(manager_seat_id, {})
            if (
                manager_liaison
                and str(manager_liaison.get("role_id", "") or "").strip()
                in set(binding.covered_role_ids)
                and manager_seat_id != binding.canonical_seat_id
            ):
                seat["manager_seat_id"] = str(
                    manager_liaison.get("manager_seat_id", "") or ""
                ).strip()
            # This is one opaque execution unit.  Preserve the organization
            # graph through the covered seats, but remove OPC's operational
            # delegation edges at the boundary so a dynamically-created WorkItem
            # cannot expand back into the provider team's covered roles.
            seat.update(
                {
                    "allowed_delegate_role_ids": [],
                    "direct_report_role_ids": [],
                    "direct_report_seat_ids": [],
                    "managed_team_id": "",
                    "managed_team_ids": [],
                    "contact_role_ids": outside_contact_role_ids,
                    "external_team_binding": copy.deepcopy(binding_payload),
                    "covered_role_ids": list(binding.covered_role_ids),
                    "capability_manifest": copy.deepcopy(binding.capability_manifest),
                    "external_company_execution_allowed": True,
                    "external_company_execution_fence": binding.artifact_isolation,
                    "external_session_scope": binding.session_scope,
                    "external_max_inflight": binding.max_inflight,
                    "external_failure_policy": binding.failure_policy,
                    "external_provider_mode": binding.provider_mode,
                }
            )
        seat["metadata"] = {
            **dict(seat.get("metadata", {}) or {}),
            "external_team_binding": copy.deepcopy(binding_payload),
            "covered_by_external_team": binding.binding_id,
            "dispatchable": canonical,
            "company_external_execution_capable": canonical,
            "staffing_locked": True,
            "staffing_mode": "opaque_external_team",
            "capability_manifest": (
                copy.deepcopy(binding.capability_manifest) if canonical else {}
            ),
        }
        seats.append(seat)
    catalog = [_delegation_catalog_entry(binding) for binding in compiled]
    catalog_by_target = {
        str(item.get("target_role_id", "") or "").strip(): item
        for item in catalog
    }
    for seat in seats:
        allowed = {
            str(item or "").strip()
            for item in list(seat.get("allowed_delegate_role_ids", []) or [])
            if str(item or "").strip()
        }
        entries = [
            copy.deepcopy(catalog_by_target[role_id])
            for role_id in allowed
            if role_id in catalog_by_target
        ]
        if not entries:
            continue
        seat["delegation_capability_catalog"] = entries
        seat["metadata"] = {
            **dict(seat.get("metadata", {}) or {}),
            "delegation_capability_catalog": copy.deepcopy(entries),
        }
    topology["seats"] = seats
    topology["external_execution_units"] = [binding.to_dict() for binding in compiled]
    topology["delegation_capability_catalog"] = catalog
    topology["execution_graph_compiled"] = True
    topology["execution_graph_kind"] = "org_with_opaque_external_teams"
    return topology


def _canonical_projection(
    projections: list[WorkItemProjectionSpec],
    binding: CompiledExternalTeamBinding,
) -> WorkItemProjectionSpec | None:
    candidates = [spec for spec in projections if spec.role_id == binding.boundary_role_id]
    if not candidates:
        return None
    exact = next(
        (spec for spec in candidates if spec.seat_id == binding.canonical_seat_id),
        None,
    )
    return exact or candidates[0]


def apply_external_team_bindings_to_plan(
    org_engine: Any,
    plan: CompanyWorkItemRuntimePlan,
    *,
    runtime_topology: dict[str, Any],
    compiled_bindings: Iterable[CompiledExternalTeamBinding] | None = None,
) -> CompanyWorkItemRuntimePlan:
    """Collapse every bound subtree to exactly one externally executed projection."""

    compiled = (
        list(compiled_bindings)
        if compiled_bindings is not None
        else compile_external_team_bindings(org_engine, runtime_topology=runtime_topology)
    )
    if not compiled:
        return plan
    result = copy.deepcopy(plan)
    projections = list(result.projections)
    replacement_by_projection: dict[str, str] = {}
    removed_ids: set[str] = set()
    compiled_payloads: list[dict[str, Any]] = []

    for binding in compiled:
        covered_roles = set(binding.covered_role_ids)
        covered_specs = [spec for spec in projections if spec.role_id in covered_roles]
        canonical = _canonical_projection(projections, binding)
        if canonical is None:
            raise ValueError(
                f"external team boundary {binding.boundary_role_id!r} has no work-item projection"
            )
        covered_projection_ids = {spec.projection_id for spec in covered_specs}
        for projection_id in covered_projection_ids:
            replacement_by_projection[projection_id] = canonical.projection_id
            if projection_id != canonical.projection_id:
                removed_ids.add(projection_id)

        incoming_dependencies: list[str] = []
        dependency_classes: dict[str, str] = {}
        for spec in covered_specs:
            for dependency_id in list(spec.dependency_projection_ids or []):
                if dependency_id in covered_projection_ids:
                    continue
                if dependency_id not in incoming_dependencies:
                    incoming_dependencies.append(dependency_id)
                dependency_classes[dependency_id] = str(
                    spec.dependency_classes.get(dependency_id, "hard") or "hard"
                )
        role = org_engine.get_agent(binding.boundary_role_id)
        role_name = str(getattr(role, "name", "") or binding.boundary_role_id).strip()
        team_display_name = str(
            binding.capability_manifest.get("display_name") or f"{role_name} Team"
        ).strip()
        canonical.turn_type = "execute"
        canonical.title = team_display_name
        canonical.summary = (
            f"{team_display_name} owns "
            f"the {binding.boundary_role_id} organization boundary and its covered "
            "responsibilities for this work item."
        )
        canonical.dependency_projection_ids = incoming_dependencies
        canonical.dependency_classes = dependency_classes
        canonical.preferred_external_agent = binding.external_agent
        canonical.execution_strategy = "external"
        canonical.allowed_delegate_role_ids = []
        canonical.skill_refs = list(
            binding.capability_manifest.get("skill_refs", []) or []
        )
        canonical.prompt_refs = list(
            binding.capability_manifest.get("prompt_refs", []) or []
        )
        canonical.review_policy = WorkItemReviewPolicy(
            review_owner_role_id=binding.review_owner_role_id,
            review_level="manager" if binding.review_owner_role_id else "human",
            max_reworks=canonical.review_policy.max_reworks,
            metadata={"source": "external_team_binding"},
        )
        binding_payload = {
            **binding.to_dict(),
            "canonical_projection_id": canonical.projection_id,
            "covered_projection_ids": sorted(covered_projection_ids),
        }
        canonical.metadata = {
            **dict(canonical.metadata or {}),
            "execution_unit_kind": "opaque_external_team",
            "external_team_binding": binding_payload,
            "covered_role_ids": list(binding.covered_role_ids),
            "covered_projection_ids": sorted(covered_projection_ids),
            "collapse_subtree": binding.collapse_subtree,
            "dispatchable": True,
            "selected_execution_agent": binding.external_agent,
            "preferred_external_agent": binding.external_agent,
            "execution_agent_locked": True,
            "selected_execution_agent_source": "external_team_binding",
            "external_company_execution_allowed": True,
            "external_company_execution_fence": binding.artifact_isolation,
            "external_session_scope": binding.session_scope,
            "external_max_inflight": binding.max_inflight,
            "external_failure_policy": binding.failure_policy,
            "external_provider_mode": binding.provider_mode,
            "capability_manifest": copy.deepcopy(binding.capability_manifest),
            "capability_manifest_hash": str(
                binding.capability_manifest.get("manifest_hash", "") or ""
            ),
        }
        compiled_payloads.append(binding_payload)

    result.projections = [spec for spec in projections if spec.projection_id not in removed_ids]
    result.root_projection_id = replacement_by_projection.get(
        result.root_projection_id,
        result.root_projection_id,
    )
    for spec in result.projections:
        redirected: list[str] = []
        redirected_classes: dict[str, str] = {}
        for dependency_id in list(spec.dependency_projection_ids or []):
            target = replacement_by_projection.get(dependency_id, dependency_id)
            if target == spec.projection_id or target in redirected:
                continue
            redirected.append(target)
            redirected_classes[target] = str(
                spec.dependency_classes.get(dependency_id, "hard") or "hard"
            )
        spec.dependency_projection_ids = redirected
        spec.dependency_classes = redirected_classes

    result.dependencies = [
        WorkItemDependencySpec(
            projection_id=spec.projection_id,
            dependency_projection_id=dependency_id,
            dependency_class=str(spec.dependency_classes.get(dependency_id, "hard") or "hard"),
            metadata={"source": "compiled_external_team_execution_graph"},
        )
        for spec in result.projections
        for dependency_id in spec.dependency_projection_ids
    ]
    links: list[dict[str, Any]] = []
    covered_by_role = {
        role_id: payload["binding_id"]
        for payload in compiled_payloads
        for role_id in payload["covered_role_ids"]
    }
    binding_by_role = {
        role_id: payload
        for payload in compiled_payloads
        for role_id in payload["covered_role_ids"]
    }
    seen_links: set[str] = set()
    for raw_link in result.collaboration_links:
        link = dict(raw_link or {})
        source = str(link.get("source_role_id", "") or "").strip()
        target = str(link.get("target_role_id", "") or "").strip()
        if source in covered_by_role and target in covered_by_role and covered_by_role[source] == covered_by_role[target]:
            continue
        if source in binding_by_role:
            link["source_role_id"] = binding_by_role[source]["boundary_role_id"]
        if target in binding_by_role:
            link["target_role_id"] = binding_by_role[target]["boundary_role_id"]
        if str(link.get("source_projection_id", "") or "") in replacement_by_projection:
            link["source_projection_id"] = replacement_by_projection[str(link["source_projection_id"])]
        if str(link.get("target_projection_id", "") or "") in replacement_by_projection:
            link["target_projection_id"] = replacement_by_projection[str(link["target_projection_id"])]
        if (
            str(link.get("source_role_id", "") or "").strip()
            == str(link.get("target_role_id", "") or "").strip()
            and str(link.get("source_projection_id", "") or "").strip()
            == str(link.get("target_projection_id", "") or "").strip()
        ):
            continue
        fingerprint = json.dumps(link, sort_keys=True, separators=(",", ":"), default=str)
        if fingerprint in seen_links:
            continue
        seen_links.add(fingerprint)
        links.append(link)
    result.collaboration_links = links
    catalog = [_delegation_catalog_entry(binding) for binding in compiled]
    catalog_by_target = {
        str(item.get("target_role_id", "") or "").strip(): item
        for item in catalog
    }
    for spec in result.projections:
        entries = [
            copy.deepcopy(catalog_by_target[role_id])
            for role_id in list(spec.allowed_delegate_role_ids or [])
            if role_id in catalog_by_target
        ]
        if entries:
            spec.metadata = {
                **dict(spec.metadata or {}),
                "delegation_capability_catalog": entries,
            }
    digest = hashlib.sha256(
        json.dumps(compiled_payloads, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    result.metadata = {
        **dict(result.metadata or {}),
        "external_team_bindings": compiled_payloads,
        "external_team_binding_hash": digest,
        "execution_graph_compiled": True,
        "execution_graph_kind": "org_with_opaque_external_teams",
        "delegation_capability_catalog": catalog,
    }
    return result
