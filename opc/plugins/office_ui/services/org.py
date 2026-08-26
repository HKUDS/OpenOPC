"""Organization service.

The heavy organization mutation implementation still lives in the underlying
org engine/config models; this service exposes the shared entrypoint used by UI
and CLI surfaces.
"""

from __future__ import annotations

import copy
from typing import Any

from opc.core.models import normalize_role_runtime_status
from opc.core.org_config import (
    RunnableOrgConfigError,
    apply_org_config_payload_to_config,
    allocate_org_config_id,
    build_org_config_payload_from_config,
    list_org_config_paths,
    load_org_config_payload,
    org_config_relative_path,
    org_config_path,
    validate_runnable_org_config,
    validate_saved_org_id,
    write_org_config_payload,
    write_org_index,
)
from opc.layer2_organization.org_work_item_planner import build_custom_org_work_item_blueprint
from opc.layer2_organization.external_team_compiler import apply_external_team_bindings_to_topology
from opc.layer2_organization.phase import kanban_column, should_hide_work_item_from_company_kanban
from opc.layer2_organization.work_item_identity import (
    work_item_identity_payload,
    work_item_projection_id_from_metadata,
    work_item_turn_type_from_metadata,
)
from opc.layer4_tools.output_budget import clip_text
from opc.plugins.office_ui.org_architecture_snapshot import (
    apply_org_architecture_snapshot,
    build_org_architecture_snapshot,
    dump_org_architecture_snapshot,
    parse_org_architecture_snapshot,
)

from .context import OfficeServiceContext
from .models import ServiceError, ServiceEvent, ServiceResult


class OrgService:
    def __init__(self, context: OfficeServiceContext) -> None:
        self.context = context

    def _ensure_custom_org_editable(self) -> None:
        if not self.context.is_custom_org_editable():
            raise ServiceError(
                "org_read_only",
                "Corporate organization is read-only. Select or create a saved custom org before editing.",
            )

    async def info(self, *, include_events: bool = False) -> ServiceResult:
        """Build the full Office UI org_info payload.

        This mirrors the historical WS payload shape so the frontend, CLI, and
        board can share one source of truth without protocol changes.
        """
        engine = self.context.engine
        result: dict[str, Any] = {
            "roles": [],
            "employees": [],
            "company_profile": "",
            "organization_id": "",
            "organization_name": "",
            "organization_config_file": "",
            "final_decider_role_id": None,
            "top_level_role_ids": [],
            "channels": [],
            "connectors": [],
            "runtime_teams": [],
            "runtime_seats": [],
            "work_items": [],
            "frontier": {},
            "runtime_topology_preview": {},
            "work_item_runtime_preview": {},
            "project_run": {},
            "project_dossier": {},
            "seat_digests": [],
            "revision_links": [],
            "project_recovery": {},
            "org_version": 0,
            "runtime_topology_version": 0,
            "external_team_bindings": [],
            "external_execution_units": [],
            "external_agent_profiles": [],
        }
        cfg_org = getattr(getattr(engine, "config", None), "org", None)
        if cfg_org is not None:
            result["organization_id"] = str(getattr(cfg_org, "organization_id", "") or "")
            result["organization_name"] = str(getattr(cfg_org, "organization_name", "") or "")
            result["organization_config_file"] = str(getattr(cfg_org, "organization_config_file", "") or "")
            result["external_team_bindings"] = [
                binding.model_dump() if hasattr(binding, "model_dump") else dict(binding)
                for binding in list(getattr(cfg_org, "external_team_bindings", []) or [])
            ]
        try:
            result["external_agent_profiles"] = list(engine.adapter_registry.describe_all())
        except Exception:
            pass

        org = getattr(engine, "org_engine", None)
        agents: list[Any] = []
        if org:
            try:
                agents = list(org.list_agents())
                builtin_ids: set[str] = set()
                if self.context.mode_state.exec_mode not in {"org", "custom"}:
                    try:
                        from opc.layer2_organization.company_runtime_profiles import get_builtin_roles

                        for profile in ("corporate",):
                            builtin_ids.update(role.id for role in get_builtin_roles(profile))
                    except Exception:
                        pass
                result["roles"] = [
                    {
                        "role_id": agent.role_id,
                        "name": agent.name,
                        "responsibility": agent.responsibility,
                        "status": agent.status.value if hasattr(agent.status, "value") else str(agent.status),
                        "reports_to": agent.reports_to,
                        "icon": getattr(agent, "icon", None),
                        "can_spawn": list(agent.can_spawn) if agent.can_spawn else [],
                        "tools": list(agent.tools) if agent.tools else [],
                        "is_builtin": agent.role_id in builtin_ids,
                        "execution_strategy": agent.runtime_policy.get("execution_strategy", "auto")
                        if isinstance(agent.runtime_policy, dict)
                        else "auto",
                        "preferred_external_agent": agent.preferred_external_agent,
                        "prompt_refs": list(agent.prompt_refs) if agent.prompt_refs else [],
                        "skill_refs": list(getattr(agent, "skill_refs", []) or []),
                        "capabilities": [
                            item.strip()
                            for item in str(getattr(agent, "capabilities", "") or "").split(",")
                            if item.strip()
                        ],
                        "artifact_contract_ref": getattr(agent, "artifact_contract_ref", None),
                    }
                    for agent in agents
                ]
            except Exception:
                pass
            try:
                employees = list(org.list_employees())
                effective_role_ids = {agent.role_id for agent in agents}
                role_getter = getattr(org, "employee_role_ids", None)
                filtered_employees = []
                for employee in employees:
                    if callable(role_getter):
                        try:
                            role_ids = list(role_getter(employee))
                        except Exception:
                            role_ids = []
                    else:
                        role_ids = [str(getattr(employee, "role_id", "") or "").strip()]
                    if any(role_id in effective_role_ids for role_id in role_ids):
                        filtered_employees.append(employee)
                employees = filtered_employees
                emp_agent_map = {}
                if self.context.agent_store is not None:
                    getter = getattr(self.context.agent_store, "get_employee_agent_map", None)
                    if callable(getter):
                        emp_agent_map = await getter()
                emp_list = []
                for employee in employees:
                    emp_meta = dict(getattr(employee, "metadata", {}) or {})
                    role_ids: list[str] = []
                    role_getter = getattr(org, "employee_role_ids", None)
                    if callable(role_getter):
                        try:
                            role_ids = list(role_getter(employee))
                        except Exception:
                            role_ids = []
                    if not role_ids:
                        for value in [
                            getattr(employee, "role_id", ""),
                            emp_meta.get("home_role_id"),
                            *list(emp_meta.get("home_role_ids", []) or []),
                            *list(emp_meta.get("staffed_role_ids", []) or []),
                        ]:
                            role_id = str(value or "").strip()
                            if role_id and role_id not in role_ids:
                                role_ids.append(role_id)
                    emp_dict: dict[str, Any] = {
                        "employee_id": employee.employee_id,
                        "name": employee.name,
                        "role_id": employee.role_id,
                        "role_ids": role_ids,
                        "category": getattr(employee, "category", ""),
                        "domains": list(getattr(employee, "domains", [])),
                        "seniority": getattr(employee, "seniority", "junior"),
                        "status": getattr(employee, "status", "active"),
                        "tags": list(getattr(employee, "tags", [])),
                        "prompt_refs": list(getattr(employee, "prompt_refs", [])),
                        "skill_refs": list(getattr(employee, "skill_refs", [])),
                        "preferred_external_agent": getattr(employee, "preferred_external_agent", None),
                        "experience_score": 0.0,
                        "learned_skill_refs": [],
                        "is_default_employee": bool(emp_meta.get("is_default_employee", False)),
                    }
                    linked = emp_agent_map.get(employee.employee_id)
                    if linked:
                        emp_dict["linked_agent_id"] = linked
                    emp_list.append(emp_dict)
                result["employees"] = emp_list
            except Exception:
                pass
            try:
                result["company_profile"] = org.get_company_profile()
                result["final_decider_role_id"] = org.get_final_decider_role_id(strict=False)
                result["top_level_role_ids"] = org.get_top_level_role_ids()
                result["org_version"] = org.current_org_version()
                result["runtime_topology_version"] = org.current_runtime_topology_version()
            except Exception:
                pass
            try:
                result["runtime_policy"] = org.get_runtime_policy(org.get_company_profile())
            except Exception:
                pass
            try:
                preview_topology = apply_external_team_bindings_to_topology(
                    org,
                    org.build_runtime_delegation_topology(),
                )
                result["runtime_topology_preview"] = preview_topology
                units = [
                    dict(unit)
                    for unit in list(preview_topology.get("external_execution_units", []) or [])
                    if isinstance(unit, dict)
                ]
                result["external_execution_units"] = units
                unit_by_role = {
                    str(role_id): unit
                    for unit in units
                    for role_id in list(unit.get("covered_role_ids", []) or [])
                    if str(role_id).strip()
                }
                for role_payload in result["roles"]:
                    unit = unit_by_role.get(str(role_payload.get("role_id", "") or ""))
                    if unit is None:
                        continue
                    boundary = str(unit.get("boundary_role_id", "") or "")
                    role_payload.update({
                        "execution_unit_kind": "opaque_external_team",
                        "external_team_binding_id": unit.get("binding_id"),
                        "external_team_boundary_role_id": boundary,
                        "external_team_boundary": role_payload.get("role_id") == boundary,
                        "covered_by_external_team": True,
                        "selected_execution_agent": unit.get("external_agent"),
                        "staffing_locked": True,
                        "staffing_mode": "opaque_external_team",
                        "external_team_capability_manifest": (
                            dict(unit.get("capability_manifest", {}) or {})
                            if role_payload.get("role_id") == boundary
                            else None
                        ),
                    })
                if org.get_company_profile() == "custom":
                    policy = org.get_runtime_policy("custom")
                    policy_payload = policy.model_dump() if hasattr(policy, "model_dump") else dict(policy or {})
                    result["work_item_runtime_preview"] = build_custom_org_work_item_blueprint(
                        org,
                        runtime_topology=preview_topology,
                        runtime_policy=policy_payload,
                    ).to_dict()
            except Exception:
                pass

        store = getattr(engine, "store", None)
        if store is not None:
            try:
                if bool(getattr(store, "is_ready", False)):
                    project_id = getattr(engine, "project_id", None) or "default"
                    if hasattr(store, "list_open_delegation_runs"):
                        runs = await store.list_open_delegation_runs(project_id=project_id)
                    else:
                        runs = await store.list_delegation_runs(project_id=project_id, status="running")
                    if runs:
                        active_run = runs[0]
                        cells = await store.list_delegation_cells(active_run.run_id)
                        role_sessions = await store.list_delegation_role_sessions(active_run.run_id)
                        work_items = await store.list_delegation_work_items(active_run.run_id)
                        runtime_teams = await store.list_team_instances(run_id=active_run.run_id) if hasattr(store, "list_team_instances") else []
                        runtime_seats = await store.list_seat_states(run_id=active_run.run_id) if hasattr(store, "list_seat_states") else []
                        legacy_team_payload = [
                            {
                                "cell_id": cell.cell_id,
                                "manager_role_id": cell.manager_role_id,
                                "member_role_ids": list(cell.member_role_ids),
                                "status": cell.status,
                                "is_final_decider_cell": bool((cell.metadata or {}).get("is_final_decider_cell")),
                            }
                            for cell in cells
                        ]
                        legacy_seat_payload = [
                            {
                                "role_session_id": session.role_session_id,
                                "role_id": session.role_id,
                                "employee_id": session.employee_id,
                                "focused_work_item_id": session.focused_work_item_id,
                                "background_work_item_ids": list(session.background_work_item_ids),
                                "pending_work_item_ids": list(getattr(session, "pending_work_item_ids", []) or []),
                                "queue_depth": len(list(getattr(session, "pending_work_item_ids", []) or [])),
                                "manager_role_ids": list(session.manager_role_ids),
                                "status": normalize_role_runtime_status(session.status, session.focused_work_item_id),
                            }
                            for session in role_sessions
                        ]
                        column_counts = {"todo": 0, "in_progress": 0, "in_review": 0, "done": 0}
                        blocker_count = 0
                        rework_count = 0
                        for item in work_items:
                            metadata = dict(item.metadata or {})
                            if should_hide_work_item_from_company_kanban(metadata):
                                continue
                            column = kanban_column(item.phase)
                            if column in column_counts:
                                column_counts[column] += 1
                            if item.blocked_reason or item.phase.value in {
                                "waiting_for_peer",
                                "waiting_for_children",
                                "needs_attention",
                                "waiting_dependencies",
                            }:
                                blocker_count += 1
                            if str(metadata.get("rework_feedback", "") or "").strip():
                                rework_count += 1
                        result["frontier"] = {
                            "run_id": active_run.run_id,
                            "status": active_run.status,
                            "lifecycle_status": getattr(active_run, "lifecycle_status", ""),
                            "total_cells": len(cells),
                            "total_role_sessions": len(role_sessions),
                            "total_work_items": sum(column_counts.values()),
                            "todo_count": column_counts["todo"],
                            "in_progress_count": column_counts["in_progress"],
                            "in_review_count": column_counts["in_review"],
                            "done_count": column_counts["done"],
                            "blocker_count": blocker_count,
                            "rework_count": rework_count,
                            "ready_count": column_counts["todo"],
                            "running_count": column_counts["in_progress"],
                            "blocked_count": blocker_count,
                            "waiting_count": column_counts["in_review"],
                            "failed_count": 0,
                        }
                        result["project_run"] = {
                            "run_id": active_run.run_id,
                            "project_id": active_run.project_id,
                            "session_id": active_run.session_id,
                            "status": active_run.status,
                            "lifecycle_status": getattr(active_run, "lifecycle_status", ""),
                            "company_profile": active_run.company_profile,
                            "execution_model": active_run.execution_model,
                            "current_revision": getattr(active_run, "current_revision", 1),
                            "latest_deliverable_summary": getattr(active_run, "latest_deliverable_summary", ""),
                            "recovery_pointer": dict(getattr(active_run, "recovery_pointer", {}) or {}),
                        }
                        dossier = dict(getattr(active_run, "project_dossier", {}) or {})
                        memory = getattr(engine, "memory", None)
                        if not dossier and memory is not None and hasattr(memory, "build_project_dossier"):
                            try:
                                dossier = await memory.build_project_dossier(
                                    project_id=project_id,
                                    run_id=active_run.run_id,
                                    session_id=active_run.session_id,
                                )
                            except Exception:
                                dossier = {}
                        result["project_dossier"] = dossier
                        result["runtime_teams"] = [
                            {
                                "team_instance_id": team.team_instance_id,
                                "cell_id": team.team_id,
                                "team_id": team.team_id,
                                "manager_role_id": str((team.metadata or {}).get("lead_role_id", "") or ""),
                                "member_role_ids": list(team.role_ids),
                                "seat_ids": list(team.seat_ids),
                                "status": team.status,
                                "parent_team_id": str((team.metadata or {}).get("parent_team_id", "") or ""),
                            }
                            for team in runtime_teams
                        ] if runtime_teams else legacy_team_payload
                        result["runtime_seats"] = [
                            {
                                "role_session_id": seat.role_runtime_session_id,
                                "role_id": seat.role_id,
                                "employee_id": seat.employee_id,
                                "team_id": seat.team_id,
                                "team_instance_id": seat.team_instance_id,
                                "seat_id": seat.seat_id,
                                "focused_work_item_id": seat.current_work_item_id,
                                "current_work_item_id": seat.current_work_item_id,
                                "manager_role_ids": list(seat.manager_role_ids),
                                "manager_seat_id": seat.manager_seat_id,
                                "status": normalize_role_runtime_status(seat.status, seat.current_work_item_id),
                                "resident_status": normalize_role_runtime_status(seat.resident_status or seat.status, seat.current_work_item_id),
                                "latest_notification": dict(getattr(seat, "latest_notification", {}) or {}),
                                "manager_digest": dict(getattr(seat, "manager_digest", {}) or {}),
                            }
                            for seat in runtime_seats
                        ] if runtime_seats else legacy_seat_payload
                        result["work_items"] = [
                            {
                                "work_item_id": item.work_item_id,
                                "role_id": item.role_id,
                                "cell_id": item.cell_id,
                                "team_id": item.team_id,
                                "seat_id": item.seat_id,
                                "team_instance_id": item.team_instance_id,
                                "title": item.title,
                                "kind": item.kind,
                                "phase": item.phase.value,
                                "kanban_column": kanban_column(item.phase),
                                "batch_id": getattr(item, "batch_id", ""),
                                "batch_index": getattr(item, "batch_index", 0),
                                "deliverable_summary": clip_text(
                                    getattr(item, "deliverable_summary", ""),
                                    limit=1200,
                                    marker="ui deliverable preview truncated",
                                ).text,
                                "deliverable_summary_chars": len(str(getattr(item, "deliverable_summary", "") or "")),
                                "blocked_reason": getattr(item, "blocked_reason", ""),
                                "handoff_status": getattr(item, "handoff_status", ""),
                                "parent_work_item_id": item.parent_work_item_id,
                                **work_item_identity_payload(
                                    projection_id=work_item_projection_id_from_metadata(
                                        dict(item.metadata or {}),
                                        fallback=str(item.projection_id or item.work_item_id or ""),
                                    ),
                                    turn_type=work_item_turn_type_from_metadata(
                                        dict(item.metadata or {}),
                                        fallback=str(item.kind or ""),
                                    ),
                                ),
                                "metadata": dict(item.metadata or {}),
                                "adaptive": dict((item.metadata or {}).get("adaptive", {}) or {}),
                            }
                            for item in work_items
                        ]
                        result["seat_digests"] = [
                            {
                                "seat_id": seat.seat_id,
                                "team_id": seat.team_id,
                                "role_id": seat.role_id,
                                "employee_id": seat.employee_id,
                                "role_session_id": seat.role_runtime_session_id,
                                "resident_status": normalize_role_runtime_status(
                                    seat.resident_status or seat.status,
                                    seat.current_work_item_id,
                                ),
                                "current_work_item": dict(getattr(seat, "current_work_item", {}) or {}),
                                "latest_notification": dict(getattr(seat, "latest_notification", {}) or {}),
                                "manager_digest": dict(getattr(seat, "manager_digest", {}) or {}),
                            }
                            for seat in runtime_seats
                        ]
                        if hasattr(store, "get_session_links") and active_run.session_id:
                            links = await store.get_session_links(active_run.session_id, limit=50)
                            result["revision_links"] = [
                                {
                                    "link_id": link.link_id,
                                    "session_id": link.session_id,
                                    "linked_session_id": link.linked_session_id,
                                    "link_type": link.link_type,
                                    "metadata": dict(link.metadata or {}),
                                    "created_at": link.created_at.isoformat(),
                                }
                                for link in links
                                if str(link.link_type or "").strip() in {"continuation_of", "revision_of", "delivery_of"}
                            ]
                        result["project_recovery"] = dict(getattr(active_run, "recovery_pointer", {}) or {})
            except Exception:
                pass

        channel_mgr = getattr(engine, "channel_manager", None)
        if channel_mgr:
            try:
                statuses = channel_mgr.get_all_statuses()
                result["channels"] = [
                    {
                        "name": status.get("name", ""),
                        "enabled": status.get("enabled", False),
                        "running": status.get("running", False),
                        "configured": status.get("configured", False),
                        "available": status.get("available", False),
                        "ready": status.get("ready", False),
                        "last_error": status.get("last_error"),
                        "delivery_mode": status.get("delivery_mode", ""),
                    }
                    for status in statuses
                ]
            except Exception:
                pass
        try:
            result["installed_packages"] = [
                package.model_dump() if hasattr(package, "model_dump") else package
                for package in engine.config.org.installed_packages
            ]
        except Exception:
            result["installed_packages"] = []
        result["runtime_teams"] = list(result.get("runtime_teams") or [])
        result["runtime_seats"] = list(result.get("runtime_seats") or [])
        result["work_items"] = list(result.get("work_items") or [])
        result["frontier"] = dict(result.get("frontier", {}) or {})
        events = self.info_events(result) if include_events else []
        return ServiceResult(result, events)

    def info_events(self, payload: dict[str, Any]) -> list[ServiceEvent]:
        events = [ServiceEvent("org_info", payload)]
        if payload.get("project_run"):
            events.append(ServiceEvent("project_run_updated", payload["project_run"]))
        if payload.get("seat_digests"):
            events.append(ServiceEvent("seat_digest_updated", {
                "run_id": dict(payload.get("project_run", {}) or {}).get("run_id"),
                "seat_digests": payload["seat_digests"],
            }))
        if payload.get("work_items"):
            events.append(ServiceEvent("work_item_batch_updated", {
                "run_id": dict(payload.get("project_run", {}) or {}).get("run_id"),
                "work_items": payload["work_items"],
                "frontier": payload.get("frontier", {}),
            }))
        if payload.get("project_recovery"):
            events.append(ServiceEvent("project_recovery_updated", payload["project_recovery"]))
        if payload.get("revision_links"):
            events.append(ServiceEvent("project_revision_created", {
                "run_id": dict(payload.get("project_run", {}) or {}).get("run_id"),
                "revision_links": payload["revision_links"],
            }))
        return events

    async def export_config(self) -> ServiceResult:
        from opc.core.config import build_company_org_payload_from_config

        snapshot = build_org_architecture_snapshot(self.context.engine.config)
        try:
            config_payload = build_org_config_payload_from_config(self.context.engine.config)
        except ValueError:
            profile = str(getattr(self.context.engine.config.org, "company_profile", "") or "corporate").strip()
            config_payload = build_company_org_payload_from_config(
                self.context.engine.config,
                force_profile=profile or "corporate",
            )
        return ServiceResult({
            "config": config_payload,
            "yaml": dump_org_architecture_snapshot(snapshot),
        })

    async def import_config(self, payload: dict[str, Any] | str, *, dry_run: bool = False) -> ServiceResult:
        try:
            if isinstance(payload, str):
                snapshot = parse_org_architecture_snapshot(payload)
                validated = apply_org_architecture_snapshot(self.context.engine.config, snapshot)
                try:
                    validate_saved_org_id(getattr(validated.org, "organization_id", ""))
                except ValueError:
                    if "organization_id" in snapshot:
                        raise
                    config_dir = self.context.opc_home / "config"
                    organization_name = str(
                        getattr(validated.org, "organization_name", "")
                        or getattr(validated.org, "company_name", "")
                        or "org"
                    ).strip()
                    organization_id = allocate_org_config_id(config_dir, organization_name)
                    validated.org.organization_id = organization_id
                    validated.org.organization_name = organization_name
                    validated.org.organization_config_file = org_config_relative_path(organization_id)
            else:
                validated = apply_org_config_payload_to_config(self.context.engine.config, payload)
        except Exception as exc:
            raise ServiceError("org_config_invalid", str(exc), {"validation_errors": [str(exc)]}) from exc
        before_roles = {str(getattr(role, "id", getattr(role, "role_id", "")) or "") for role in self.context.engine.config.org.roles}
        after_roles = {str(getattr(role, "id", getattr(role, "role_id", "")) or "") for role in validated.org.roles}
        preview = {
            "roles_added": len(after_roles - before_roles),
            "roles_removed": len(before_roles - after_roles),
            "employees_changed": abs(len(validated.org.employees) - len(self.context.engine.config.org.employees)),
        }
        if dry_run:
            return ServiceResult({"ok": True, "dry_run": True, "preview": preview})
        self._ensure_custom_org_editable()
        async with self.context.config_lock:
            self.context.rebind_config(validated)
            await self._persist_and_reload()
        info = await self.info(include_events=True)
        return ServiceResult(
            {"ok": True, "dry_run": False, "preview": preview},
            info.events,
        )

    async def saved_list(self) -> ServiceResult:
        import yaml

        items: list[dict[str, Any]] = []
        for path in list_org_config_paths(self.context.opc_home / "config"):
            try:
                parsed = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if not isinstance(parsed, dict):
                    continue
                base_config = getattr(self.context.engine, "config", None)
                if base_config is None:
                    from opc.core.config import OPCConfig

                    base_config = OPCConfig()
                validated = apply_org_config_payload_to_config(
                    base_config,
                    parsed,
                    source_path=path,
                )
                org_id = str(parsed.get("organization_id") or path.stem.removeprefix("org_").removesuffix("_config"))
                org_name = str(parsed.get("organization_name") or (parsed.get("company") or {}).get("name") or org_id)
                items.append({
                    "name": org_id,
                    "organization_id": org_id,
                    "organization_name": org_name,
                    "filename": path.name,
                    "saved_at": path.stat().st_mtime,
                    "roles_count": len(validated.org.roles),
                    "employees_count": len(validated.org.employees),
                })
            except Exception:
                continue
        active_name = ""
        if self.context.get_active_saved_org_name is not None:
            active_name = await self.context.get_active_saved_org_name()
        return ServiceResult({"orgs": items, "active_name": active_name or None})

    async def saved_load(self, name: str) -> ServiceResult:
        try:
            organization_id = validate_saved_org_id(name)
            payload, path = load_org_config_payload(self.context.opc_home / "config", organization_id)
            validated = apply_org_config_payload_to_config(
                self.context.engine.config,
                payload,
                source_path=path,
            )
            validate_runnable_org_config(validated, organization_id=organization_id)
        except FileNotFoundError:
            raise ServiceError("saved_org_not_found", "Saved organization not found", {"name": name})
        except RunnableOrgConfigError as exc:
            raise ServiceError("saved_org_not_runnable", str(exc), {"name": name}) from exc
        except ValueError as exc:
            raise ServiceError("saved_org_reserved", str(exc), {"name": name}) from exc
        async with self.context.config_lock:
            self.context.rebind_config(validated)
            await self._persist_and_reload()
        if self.context.set_active_saved_org_name is not None:
            await self.context.set_active_saved_org_name(organization_id)
        return ServiceResult({"ok": True, "name": organization_id, "config": payload})

    async def saved_save_as(self, name: str, *, overwrite: bool = False) -> ServiceResult:
        from opc.core.config import slugify_organization_name

        self._ensure_custom_org_editable()
        organization_name = str(name or "").strip()
        if not organization_name:
            raise ServiceError("organization_name_required", "organization name required")
        config_dir = self.context.opc_home / "config"
        preferred_id = slugify_organization_name(organization_name)
        organization_id = preferred_id if overwrite else allocate_org_config_id(config_dir, organization_name, preferred_id=preferred_id)
        try:
            organization_id = validate_saved_org_id(organization_id)
        except ValueError as exc:
            raise ServiceError("saved_org_reserved", str(exc), {"name": name}) from exc
        cfg = self.context.engine.config
        async with self.context.config_lock:
            cfg.org.organization_id = organization_id
            cfg.org.organization_name = organization_name
            cfg.org.organization_config_file = org_config_relative_path(organization_id)
            cfg.org.company_name = organization_name
            cfg.org.company_profile = "custom"
            snapshot = build_org_architecture_snapshot(cfg, force_profile="custom")
            snapshot["organization_id"] = organization_id
            snapshot["organization_name"] = organization_name
            snapshot.setdefault("company", {})["name"] = organization_name
            path = write_org_config_payload(config_dir, organization_id, snapshot)
            write_org_index(config_dir, organization_id)
            await self._persist_and_reload()
        if self.context.set_active_saved_org_name is not None:
            await self.context.set_active_saved_org_name(organization_id)
        return ServiceResult({
            "ok": True,
            "name": organization_id,
            "organization_id": organization_id,
            "organization_name": organization_name,
            "filename": path.name,
            "path": str(path),
        })

    async def saved_create(self, *, organization_name: str, members: list[dict[str, Any]]) -> ServiceResult:
        from opc.core.config import EmployeeConfig, OPCConfig, RoleConfig, slugify_organization_name

        display_name = str(organization_name or "").strip()
        if not display_name:
            raise ServiceError("organization_name_required", "organization name required")

        if not isinstance(members, list):
            raise ServiceError("org_members_required", "members must be a list")

        normalized_members: list[dict[str, Any]] = []
        for index, item in enumerate(members):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            normalized_members.append({
                "source_index": index,
                "name": name,
                "responsibility": str(item.get("responsibility") or "").strip(),
                "prompt": str(item.get("prompt") or "").strip(),
                "reports_to_index": item.get("reports_to_index"),
            })
        if len(normalized_members) < 2:
            raise ServiceError("org_members_required", "organization requires at least two members")

        config_dir = self.context.opc_home / "config"
        organization_id = allocate_org_config_id(config_dir, display_name)
        role_ids: list[str] = []
        used_role_ids: set[str] = set()
        for idx, member in enumerate(normalized_members):
            base = slugify_organization_name(member["name"], fallback=f"member_{idx + 1}")
            role_id = base
            suffix = 2
            while role_id in used_role_ids:
                tail = f"_{suffix}"
                role_id = f"{base[: max(1, 64 - len(tail))].rstrip('_-') or 'member'}{tail}"
                suffix += 1
            used_role_ids.add(role_id)
            role_ids.append(role_id)

        roles: list[RoleConfig] = []
        employees: list[EmployeeConfig] = []
        for idx, member in enumerate(normalized_members):
            raw_parent = member.get("reports_to_index")
            if raw_parent in (None, "", [], {}):
                reports_to = "owner" if idx == 0 else role_ids[0]
            else:
                try:
                    parent_index = int(raw_parent)
                except (TypeError, ValueError) as exc:
                    raise ServiceError("invalid_org_member_hierarchy", "invalid reports_to_index", {"index": idx}) from exc
                if parent_index < 0 or parent_index >= idx:
                    raise ServiceError("invalid_org_member_hierarchy", "reports_to_index must point to an earlier member", {
                        "index": idx,
                        "reports_to_index": parent_index,
                    })
                reports_to = role_ids[parent_index]

            role_id = role_ids[idx]
            responsibility = member["responsibility"] or f"Owns {member['name']} responsibilities."
            roles.append(RoleConfig(
                id=role_id,
                name=member["name"],
                responsibility=responsibility,
                reports_to=reports_to,
                prompt_refs=[member["prompt"]] if member["prompt"] else [],
            ))
            employees.append(EmployeeConfig(
                employee_id=f"{role_id}-default-employee",
                template_id="system_default_employee",
                name=f"{member['name']} Default Employee",
                role_id=role_id,
                description=f"Default employee for the {member['name']} role.",
                category="general",
                metadata={
                    "is_default_employee": True,
                    "auto_created_for_role": role_id,
                    "employee_origin": "system_default",
                    "persist_to_org": True,
                },
            ))

        cfg = self.context.engine.config.model_copy(deep=True)
        cfg.org.organization_id = organization_id
        cfg.org.organization_name = display_name
        cfg.org.organization_config_file = org_config_relative_path(organization_id)
        cfg.org.company_name = display_name
        cfg.org.company_profile = "custom"
        cfg.org.company_profiles = ["corporate", "custom"]
        cfg.org.execution_model = "actor_runtime"
        cfg.org.final_decider_role_id = role_ids[0]
        cfg.org.roles = roles
        cfg.org.employees = employees
        cfg.org.escalation_rules = []
        cfg.org.runtime_policies = {}
        cfg.org.talent_templates = []
        cfg.org.teams = []
        cfg.org.team_runtime = OPCConfig().org.team_runtime
        cfg.org.installed_packages = []
        validate_runnable_org_config(cfg, organization_id=organization_id)

        async with self.context.config_lock:
            payload = build_org_config_payload_from_config(
                cfg,
                organization_id=organization_id,
                organization_name=display_name,
            )
            path = write_org_config_payload(config_dir, organization_id, payload)
            write_org_index(config_dir, organization_id)
            self.context.rebind_config(cfg)
            org = getattr(self.context.engine, "org_engine", None)
            if org and hasattr(org, "reload_from_config"):
                org.reload_from_config()
        if self.context.set_active_saved_org_name is not None:
            await self.context.set_active_saved_org_name(organization_id)
        return ServiceResult({
            "ok": True,
            "name": organization_id,
            "organization_id": organization_id,
            "organization_name": display_name,
            "filename": path.name,
            "path": str(path),
            "roles_count": len(roles),
            "employees_count": len(employees),
        })

    async def saved_delete(self, name: str) -> ServiceResult:
        try:
            organization_id = validate_saved_org_id(str(name or ""))
        except ValueError as exc:
            raise ServiceError("saved_org_reserved", str(exc), {"name": name}) from exc
        active = ""
        if self.context.get_active_saved_org_name is not None:
            active = await self.context.get_active_saved_org_name()
        if active == organization_id:
            raise ServiceError("cannot_delete_active", "cannot_delete_active", {"name": name})
        path = org_config_path(self.context.opc_home / "config", organization_id)
        if path.exists():
            path.unlink()
        return ServiceResult({"ok": True, "name": organization_id, "organization_id": organization_id, "filename": path.name})

    async def add_role(self, role_payload: dict[str, Any]) -> ServiceResult:
        from opc.core.config import RoleConfig

        self._ensure_custom_org_editable()
        role_id = str(role_payload.get("role_id") or role_payload.get("id") or "").strip()
        if not role_id:
            raise ServiceError("missing_role_id", "role_id required")
        cfg = self.context.engine.config.org
        if any(self._role_id(role) == role_id for role in cfg.roles):
            raise ServiceError("role_exists", "Role already exists", {"role_id": role_id})
        role = RoleConfig(
            id=role_id,
            name=str(role_payload.get("name") or role_id),
            responsibility=str(role_payload.get("responsibility") or role_payload.get("description") or ""),
            reports_to=str(role_payload.get("reports_to") or "owner"),
            icon=(str(role_payload.get("icon") or "").strip() or None),
            tools=list(role_payload.get("tools", []) or []),
        )
        async with self.context.config_lock:
            cfg.roles.append(role)
            await self._persist_and_reload()
        info = await self.info(include_events=True)
        events = list(info.events)
        if self.context.mode_state.exec_mode in {"org", "custom"}:
            agents = await self._ensure_custom_role_agents()
            events.append(ServiceEvent("ack", {"ok": True, "action": "agents_synced", "agents": agents}))
            if self.context.broadcast_snapshot is not None:
                await self.context.broadcast_snapshot()
        return ServiceResult({"role": self._model_payload(role)}, events)

    async def bulk_add_roles(self, roles: list[dict[str, Any]]) -> ServiceResult:
        self._ensure_custom_org_editable()
        added: list[str] = []
        for role in roles:
            role_id = str(role.get("role_id") or role.get("id") or "").strip()
            if not role_id:
                continue
            if any(self._role_id(existing) == role_id for existing in self.context.engine.config.org.roles):
                continue
            result = await self.add_role(role)
            added.append(str(result.payload.get("role", {}).get("id") or role_id))
        if not added:
            raise ServiceError("no_roles_added", "No valid roles to add")
        return ServiceResult({"role_ids": added, "count": len(added)})

    async def update_role(self, role_id: str, updates: dict[str, Any]) -> ServiceResult:
        self._ensure_custom_org_editable()
        role_id = str(role_id or "").strip()
        if not role_id:
            raise ServiceError("missing_role_id", "role_id required")
        cfg = self.context.engine.config.org
        target_index = next(
            (index for index, role in enumerate(cfg.roles) if self._role_id(role) == role_id),
            None,
        )
        target = cfg.roles[target_index] if target_index is not None else None
        if target is None:
            raise ServiceError("role_not_found", "Role not found", {"role_id": role_id})
        previous_target = copy.deepcopy(target)
        if str(updates.get("reports_to", "") or "").strip() == role_id:
            raise ServiceError("role_cycle", "Role cannot report to itself", {"role_id": role_id})
        for key in ("name", "responsibility", "reports_to", "icon", "preferred_external_agent"):
            if key in updates:
                setattr(target, key, (str(updates[key]).strip() or None) if key in {"icon", "preferred_external_agent"} else str(updates[key]).strip())
        if "reports_to" in updates:
            new_reports_to = str(updates.get("reports_to") or "").strip()
            if new_reports_to and new_reports_to != "owner":
                role_map = {self._role_id(role): str(getattr(role, "reports_to", "") or "") for role in cfg.roles}
                role_map[role_id] = new_reports_to
                visited: set[str] = set()
                cursor = new_reports_to
                while cursor and cursor != "owner":
                    if cursor in visited:
                        raise ServiceError("role_cycle", "This would create a circular hierarchy", {"role_id": role_id})
                    visited.add(cursor)
                    cursor = role_map.get(cursor, "")
        for key in ("can_spawn", "tools", "prompt_refs", "skill_refs", "capabilities"):
            if key in updates:
                value = updates.get(key) or []
                if isinstance(value, str):
                    value = [item.strip() for item in value.split(",") if item.strip()]
                else:
                    value = [str(item).strip() for item in list(value) if str(item).strip()]
                setattr(target, key, list(value))
        if "execution_strategy" in updates and hasattr(target, "runtime_policy"):
            strategy = str(updates.get("execution_strategy") or "auto").strip()
            if strategy:
                target.runtime_policy.execution_strategy = strategy
        try:
            self._validate_configured_external_team_bindings()
        except ServiceError:
            cfg.roles[target_index] = previous_target
            raise
        async with self.context.config_lock:
            await self._persist_and_reload()
        info = await self.info(include_events=True)
        return ServiceResult({"role": self._model_payload(target), "action": "role_updated", "role_id": role_id}, info.events)

    async def delete_role(self, role_id: str) -> ServiceResult:
        self._ensure_custom_org_editable()
        cfg = self.context.engine.config.org
        before = len(cfg.roles)
        cfg.roles = [role for role in cfg.roles if self._role_id(role) != role_id]
        if len(cfg.roles) == before:
            raise ServiceError("role_not_found", "Role not found", {"role_id": role_id})
        cfg.employees = [employee for employee in cfg.employees if getattr(employee, "role_id", "") != role_id]
        retained_bindings = []
        removed_binding_ids: list[str] = []
        for binding in list(cfg.external_team_bindings or []):
            if str(binding.boundary_role_id or "").strip() == role_id:
                removed_binding_ids.append(str(binding.binding_id or role_id).strip())
                continue
            if str(binding.review_owner_role_id or "").strip() == role_id:
                binding.review_owner_role_id = None
            retained_bindings.append(binding)
        cfg.external_team_bindings = retained_bindings
        for role in cfg.roles:
            role.can_spawn = [item for item in list(getattr(role, "can_spawn", []) or []) if item != role_id]
            if getattr(role, "reports_to", "") == role_id:
                role.reports_to = "owner"
        async with self.context.config_lock:
            await self._persist_and_reload()
        agents = await self.context.agent_store.get_all()
        for agent in agents:
            if agent.get("opc_role_id") == role_id:
                await self.context.agent_store.remove_agent(agent["agent_id"])
        if self.context.mode_state.exec_mode in {"org", "custom"}:
            await self.context.agent_store.sync_custom_shadow()
        info = await self.info(include_events=True)
        return ServiceResult(
            {
                "role_id": role_id,
                "action": "role_deleted",
                "removed_external_team_bindings": removed_binding_ids,
            },
            info.events,
        )

    async def update_runtime_policy(self, policy: dict[str, Any], *, profile: str = "custom") -> ServiceResult:
        from opc.core.config import RuntimePolicyConfig

        self._ensure_custom_org_editable()
        current = self.context.engine.config.org.runtime_policies.get(profile)
        base = current.model_dump() if hasattr(current, "model_dump") else {}
        merged = self._deep_merge(base, dict(policy or {}))
        self.context.engine.config.org.runtime_policies[profile] = RuntimePolicyConfig.model_validate(merged)
        async with self.context.config_lock:
            await self._persist_and_reload()
        info = await self.info(include_events=True)
        return ServiceResult(
            {"profile": profile, "policy": self.context.engine.config.org.runtime_policies[profile].model_dump(), "action": "runtime_policy_updated"},
            info.events,
        )

    async def list_external_team_bindings(self) -> ServiceResult:
        bindings = [
            binding.model_dump() if hasattr(binding, "model_dump") else dict(binding)
            for binding in list(self.context.engine.config.org.external_team_bindings or [])
        ]
        return ServiceResult({"bindings": bindings, "count": len(bindings)})

    async def bind_external_team(self, payload: dict[str, Any]) -> ServiceResult:
        from opc.core.config import ExternalTeamBindingConfig
        from opc.layer2_organization.external_team_compiler import (
            compile_external_team_bindings,
        )

        expected_org_id = str((payload or {}).get("organization_id") or "").strip()
        active_org_id = str(self.context.engine.config.org.organization_id or "").strip()
        if expected_org_id and expected_org_id != active_org_id:
            raise ServiceError(
                "organization_mismatch",
                "The requested organization is not active",
                {"requested": expected_org_id, "active": active_org_id},
            )
        try:
            candidate = ExternalTeamBindingConfig.model_validate(dict(payload or {}))
        except ValueError as exc:
            raise ServiceError("invalid_external_team_binding", str(exc)) from exc
        if candidate.external_agent != "jiuwenswarm":
            raise ServiceError(
                "invalid_external_team_agent",
                "Opaque organization team bindings currently require external_agent=jiuwenswarm",
            )
        org = getattr(self.context.engine, "org_engine", None)
        if org is None or org.get_agent(candidate.boundary_role_id) is None:
            raise ServiceError(
                "role_not_found",
                "External team boundary role not found",
                {"role_id": candidate.boundary_role_id},
            )
        cfg_org = self.context.engine.config.org
        candidate_id = str(candidate.binding_id or "").strip()
        retained = []
        for binding in list(cfg_org.external_team_bindings or []):
            same_id = candidate_id and str(binding.binding_id or "").strip() == candidate_id
            same_boundary = str(binding.boundary_role_id or "").strip() == candidate.boundary_role_id
            if not same_id and not same_boundary:
                retained.append(binding)
        proposed = [*retained, candidate]
        topology = org.build_runtime_delegation_topology()
        try:
            compiled = compile_external_team_bindings(
                org,
                runtime_topology=topology,
                bindings=proposed,
            )
        except ValueError as exc:
            raise ServiceError("invalid_external_team_binding", str(exc)) from exc
        selected = next(
            item for item in compiled if item.boundary_role_id == candidate.boundary_role_id
        )
        candidate.binding_id = selected.binding_id
        async with self.context.config_lock:
            cfg_org.external_team_bindings = proposed
            await self._persist_and_reload()
        return ServiceResult(
            {
                "binding": selected.to_dict(),
                "action": "external_team_bound",
            },
            [ServiceEvent("org_info", (await self.info()).payload)],
        )

    async def unbind_external_team(
        self,
        boundary_or_binding_id: str,
        *,
        organization_id: str | None = None,
    ) -> ServiceResult:
        expected_org_id = str(organization_id or "").strip()
        active_org_id = str(self.context.engine.config.org.organization_id or "").strip()
        if expected_org_id and expected_org_id != active_org_id:
            raise ServiceError(
                "organization_mismatch",
                "The requested organization is not active",
                {"requested": expected_org_id, "active": active_org_id},
            )
        token = str(boundary_or_binding_id or "").strip()
        if not token:
            raise ServiceError("missing_external_team_binding", "binding id or boundary role required")
        cfg_org = self.context.engine.config.org
        existing = list(cfg_org.external_team_bindings or [])
        retained = [
            binding
            for binding in existing
            if str(binding.binding_id or "").strip() != token
            and str(binding.boundary_role_id or "").strip() != token
        ]
        if len(retained) == len(existing):
            raise ServiceError(
                "external_team_binding_not_found",
                "External team binding not found",
                {"binding": token},
            )
        async with self.context.config_lock:
            cfg_org.external_team_bindings = retained
            await self._persist_and_reload()
        return ServiceResult(
            {"binding": token, "action": "external_team_unbound"},
            [ServiceEvent("org_info", (await self.info()).payload)],
        )

    async def update_org_strategy(self, *, final_decider_role_id: str | None = None) -> ServiceResult:
        self._ensure_custom_org_editable()
        value = str(final_decider_role_id or "").strip() or None
        previous = self.context.engine.config.org.final_decider_role_id
        self.context.engine.config.org.final_decider_role_id = value
        org = getattr(self.context.engine, "org_engine", None)
        if org and hasattr(org, "reload_from_config"):
            org.reload_from_config()
            validate = getattr(org, "validate_company_runtime_setup", None)
            if callable(validate):
                setup_error = validate()
                if setup_error:
                    self.context.engine.config.org.final_decider_role_id = previous
                    org.reload_from_config()
                    raise ServiceError("invalid_org_strategy", str(setup_error))
        async with self.context.config_lock:
            await self._persist_and_reload()
        info = await self.info(include_events=True)
        return ServiceResult({"final_decider_role_id": value, "action": "org_strategy_updated"}, info.events)

    async def reset_architecture(self) -> ServiceResult:
        self._ensure_custom_org_editable()
        async with self.context.config_lock:
            self.context.engine.config.org.roles = []
            self.context.engine.config.org.employees = []
            self.context.engine.config.org.installed_packages = []
            self.context.engine.config.org.external_team_bindings = []
            self.context.engine.config.org.runtime_policies.pop("custom", None)
            await self._persist_and_reload()
        try:
            for agent in await self.context.agent_store.get_all():
                await self.context.agent_store.remove_agent(agent["agent_id"])
            await self.context.agent_store.sync_custom_shadow()
        except Exception:
            pass
        info = await self.info(include_events=True)
        return ServiceResult({"ok": True, "action": "architecture_reset"}, info.events)

    async def _persist_and_reload(self) -> None:
        if self.context.persist_runtime_config is not None:
            self.context.persist_runtime_config()
        else:
            self.context.engine.config.save()
        self.context.rebind_config(self.context.engine.config)
        org = getattr(self.context.engine, "org_engine", None)
        if org and hasattr(org, "reload_from_config"):
            org.reload_from_config()

    def _validate_configured_external_team_bindings(self) -> None:
        """Reject hierarchy edits that would make persisted Team scopes overlap."""

        bindings = list(self.context.engine.config.org.external_team_bindings or [])
        if not bindings:
            return
        from opc.layer2_organization.external_team_compiler import (
            compile_external_team_bindings,
        )
        from opc.layer2_organization.org_engine import OrgEngine

        try:
            preview_org = OrgEngine(self.context.engine.config)
            compile_external_team_bindings(
                preview_org,
                runtime_topology=preview_org.build_runtime_delegation_topology(),
                bindings=bindings,
            )
        except ValueError as exc:
            raise ServiceError(
                "invalid_external_team_binding",
                "This hierarchy change would invalidate an external Team binding",
                {"validation_error": str(exc)},
            ) from exc

    async def _ensure_custom_role_agents(self) -> list[dict[str, Any]]:
        if self.context.ensure_custom_role_agents is not None:
            return await self.context.ensure_custom_role_agents()
        if self.context.agent_store is None:
            return []
        org = getattr(self.context.engine, "org_engine", None)
        if self.context.mode_state.exec_mode in {"org", "custom"} and org is not None:
            ensure = getattr(self.context.agent_store, "ensure_custom_role_agents", None)
            if callable(ensure):
                agents = await ensure(org)
                if self.context.sync_role_map is not None:
                    await self.context.sync_role_map()
                return agents
        getter = getattr(self.context.agent_store, "get_all", None)
        return await getter() if callable(getter) else []

    @staticmethod
    def _role_id(role: Any) -> str:
        return str(getattr(role, "id", getattr(role, "role_id", "")) or "")

    @staticmethod
    def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        result = dict(base)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = OrgService._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    @staticmethod
    def _model_payload(value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            return value.model_dump()
        if hasattr(value, "__dict__"):
            return dict(value.__dict__)
        return dict(value or {})
