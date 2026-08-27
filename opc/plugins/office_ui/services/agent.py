"""Agent registry service."""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from opc.layer2_organization.company_runtime_identity import is_runtime_auxiliary_task

from .context import OfficeServiceContext
from .models import ServiceError, ServiceEvent, ServiceResult


class AgentService:
    def __init__(self, context: OfficeServiceContext) -> None:
        self.context = context

    async def list(self) -> ServiceResult:
        agents = await self.context.agent_store.get_all()
        enriched: list[dict[str, Any]] = []
        for agent in agents:
            item = dict(agent)
            agent_id = str(item.get("agent_id", "") or "")
            tracker = self.context.event_adapter.get_tracker(agent_id) if agent_id else None
            runtime_status = tracker.state.value if tracker else str(item.get("status", "idle") or "idle")
            item["status"] = runtime_status
            item["runtime_status"] = runtime_status
            item["current_tool"] = tracker.current_tool if tracker else item.get("current_tool")
            item["current_task_id"] = tracker.task_id if tracker else item.get("current_task_id")
            enriched.append(item)
        return ServiceResult({"agents": enriched})

    async def create(self, *, name: str, role_id: str, office_id: str = "office-0", description: str = "", specialties: list[str] | None = None) -> ServiceResult:
        if not name or not role_id:
            raise ServiceError("missing_agent_fields", "name and role_id required")
        agent = await self.context.agent_store.create_agent(
            name=name,
            opc_role_id=role_id,
            office_id=office_id,
            org_engine=getattr(self.context.engine, "org_engine", None),
            description=description,
            specialties=specialties or [],
        )
        if self.context.mode_state.exec_mode in {"org", "custom"}:
            await self.context.agent_store.sync_custom_shadow()
        return ServiceResult({"agent": agent}, [ServiceEvent("event", {"type": "agent_created", "agent_id": agent.get("agent_id"), "data": agent})])

    async def create_from_template(self, *, template_id: str, role_id: str = "", office_id: str = "office-0") -> ServiceResult:
        from opc.layer2_organization.talent_market import TalentMarket

        template_id = str(template_id or "").strip()
        if not template_id:
            raise ServiceError("missing_template_id", "template_id required")
        market = TalentMarket(self.context.opc_home, self.context.engine.config)
        template = next((item for item in market.list_templates() if getattr(item, "id", "") == template_id), None)
        if template is None:
            template = next((item for item in market.scan_local_talent() if getattr(item, "id", "") == template_id), None)
        if template is None:
            raise ServiceError("template_not_found", "Template not found", {"template_id": template_id})
        agent = await self.context.agent_store.create_agent(
            name=str(getattr(template, "name", "") or template_id),
            opc_role_id=str(role_id or template_id),
            office_id=office_id,
            org_engine=getattr(self.context.engine, "org_engine", None),
            description=str(getattr(template, "description", "") or ""),
            specialties=[*list(getattr(template, "domains", []) or []), *list(getattr(template, "tags", []) or [])],
        )
        if self.context.mode_state.exec_mode in {"org", "custom"}:
            await self.context.agent_store.sync_custom_shadow()
        return ServiceResult({"agent": agent}, [ServiceEvent("event", {"type": "agent_created", "agent_id": agent.get("agent_id"), "data": agent})])

    async def import_employee(self, *, employee_id: str, office_id: str = "office-0") -> ServiceResult:
        employee_id = str(employee_id or "").strip()
        if not employee_id:
            raise ServiceError("missing_employee_id", "employee_id required")
        org = getattr(self.context.engine, "org_engine", None)
        employee_obj = org.get_employee(employee_id) if org and hasattr(org, "get_employee") else None
        if employee_obj is None:
            employee_obj = next(
                (item for item in getattr(self.context.engine.config.org, "employees", []) or [] if getattr(item, "employee_id", "") == employee_id),
                None,
            )
        if employee_obj is None:
            raise ServiceError("employee_not_found", "Employee not found", {"employee_id": employee_id})
        employee = {
            "employee_id": getattr(employee_obj, "employee_id", ""),
            "name": getattr(employee_obj, "name", "") or employee_id,
            "role_id": getattr(employee_obj, "role_id", ""),
            "category": getattr(employee_obj, "category", ""),
            "domains": list(getattr(employee_obj, "domains", []) or []),
            "tags": list(getattr(employee_obj, "tags", []) or []),
        }
        agent = await self.context.agent_store.create_agent_from_employee(employee, office_id=office_id)
        if self.context.mode_state.exec_mode in {"org", "custom"}:
            await self.context.agent_store.sync_custom_shadow()
        agents = await self.context.agent_store.get_all()
        return ServiceResult(
            {"agent": agent, "agents": agents, "imported_employee_id": employee_id},
            [ServiceEvent("event", {"type": "agent_created", "agent_id": agent.get("agent_id"), "data": agent})],
        )

    async def delete(self, agent_id: str) -> ServiceResult:
        removed = await self.context.agent_store.remove_agent(agent_id)
        if not removed:
            raise ServiceError("agent_not_found", "Agent not found", {"agent_id": agent_id})
        role_id = removed.get("opc_role_id", agent_id)
        await self._clean_orphaned_assignments(role_id)
        employee_id = removed.get("employee_id")
        if self.context.mode_state.exec_mode in {"org", "custom"} and employee_id:
            org = getattr(self.context.engine, "org_engine", None)
            if org is not None:
                async with self.context.config_lock:
                    employee = org.get_employee(employee_id) if hasattr(org, "get_employee") else None
                    prompt_refs = list(getattr(employee, "prompt_refs", []) or []) if employee else []
                    remover = getattr(org, "remove_employee", None)
                    if callable(remover):
                        remover(employee_id)
                    ensure_default = getattr(org, "ensure_default_employee_for_role", None)
                    if callable(ensure_default) and role_id:
                        ensure_default(role_id, persist=False)
                    for ref in prompt_refs:
                        if str(ref).startswith("prompts/custom/"):
                            (Path(getattr(self.context.engine, "opc_home", self.context.opc_home)) / ref).unlink(missing_ok=True)
                    self._persist_config()
        if self.context.mode_state.exec_mode in {"org", "custom"}:
            if self.context.ensure_custom_role_agents is not None and removed.get("employee_id"):
                await self.context.ensure_custom_role_agents()
            await self.context.agent_store.sync_custom_shadow()
        if self.context.sync_role_map is not None:
            await self.context.sync_role_map()
        events = [ServiceEvent("event", {
            "event_id": str(uuid.uuid4()),
            "type": "agent_removed",
            "agent_id": agent_id,
            "data": {},
            "timestamp": time.time(),
        })]
        return ServiceResult({"agents": await self.context.agent_store.get_all(), "deleted": agent_id}, events)

    async def move(self, *, agent_id: str, office_id: str, seat_zone: str | None = None, desk_id: str | None = None) -> ServiceResult:
        agent = await self.context.agent_store.move_agent(agent_id, office_id, seat_zone, desk_id)
        if not agent:
            raise ServiceError("agent_not_found", "Agent not found", {"agent_id": agent_id})
        return ServiceResult({"agent": agent})

    async def detail(self, *, project_id: str, agent_id: str) -> ServiceResult:
        agents = await self.context.agent_store.get_all()
        agent = next((a for a in agents if a.get("agent_id") == agent_id), None)
        if not agent:
            raise ServiceError("agent_not_found", "Agent not found", {"agent_id": agent_id})
        engine = await self.context.engine_for_project(project_id)
        task_history: list[dict[str, str]] = []
        role_id = agent.get("opc_role_id", agent_id)
        if getattr(engine, "store", None):
            tasks = await engine.store.get_tasks(project_id=project_id)
            task_history = [
                {
                    "task_id": task.id,
                    "title": task.title,
                    "status": task.status.value if hasattr(task.status, "value") else str(task.status),
                }
                for task in tasks
                if getattr(task, "assigned_to", "") == role_id
                and not is_runtime_auxiliary_task(task)
            ]
        tracker = self.context.event_adapter.get_tracker(agent_id) if self.context.event_adapter else None
        detail: dict[str, Any] = {
            "agent_id": agent_id,
            "name": agent.get("name", ""),
            "role_name": agent.get("opc_role_id", ""),
            "office_id": agent.get("office_id", ""),
            "status": tracker.state.value if tracker else str(agent.get("status", "idle") or "idle"),
            "current_task_id": tracker.task_id if tracker else agent.get("current_task_id"),
            "current_tool": tracker.current_tool if tracker else agent.get("current_tool"),
            "task_history": task_history,
            "inbox_count": 0,
            **agent,
        }
        employee_id = agent.get("employee_id")
        if employee_id:
            detail["employee_id"] = employee_id
            org = getattr(engine, "org_engine", None)
            try:
                employee = org.get_employee(employee_id) if org and hasattr(org, "get_employee") else None
                if employee:
                    employee_info: dict[str, Any] = {
                        "domains": list(getattr(employee, "domains", []) or []),
                        "seniority": getattr(employee, "seniority", "junior"),
                        "tags": list(getattr(employee, "tags", []) or []),
                        "category": getattr(employee, "category", ""),
                    }
                    evolution = getattr(org, "employee_evolution", None) if org else None
                    if evolution:
                        try:
                            employee_info["experience_score"] = evolution.get_experience_score(
                                employee.employee_id,
                                role_id=employee.role_id,
                                domains=list(getattr(employee, "domains", []) or []),
                            )
                        except Exception:
                            pass
                    detail["employee_info"] = employee_info
            except Exception:
                pass
        return ServiceResult({"detail": detail})

    async def _clean_orphaned_assignments(self, role_id: str) -> None:
        store = getattr(self.context.engine, "store", None)
        if not store:
            return
        try:
            tasks = await store.get_tasks(project_id=getattr(self.context.engine, "project_id", None) or "default")
            for task in tasks:
                if getattr(task, "assigned_to", "") == role_id:
                    task.assigned_to = ""
                    await store.save_task(task)
        except Exception:
            pass

    def _persist_config(self) -> None:
        if self.context.persist_runtime_config is not None:
            self.context.persist_runtime_config()
        else:
            self.context.engine.config.save()
