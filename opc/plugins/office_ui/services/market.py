"""OPC Market service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from loguru import logger

from .context import OfficeServiceContext
from .models import ServiceError, ServiceEvent, ServiceResult


class MarketService:
    def __init__(self, context: OfficeServiceContext) -> None:
        self.context = context

    def _ensure_custom_org_editable(self) -> None:
        if not self.context.is_custom_org_editable():
            raise ServiceError(
                "org_read_only",
                "Corporate organization is read-only. Select or create a saved custom org before editing.",
            )

    async def browse(self) -> ServiceResult:
        from opc.market.architecture_registry import get_all_presets

        return ServiceResult({"presets": [
            item.to_display_card() if hasattr(item, "to_display_card") else self._preset_payload(item)
            for item in get_all_presets()
        ]})

    async def preview(self, preset_id: str) -> ServiceResult:
        from opc.market.architecture_registry import get_preset

        preset = get_preset(preset_id)
        if not preset:
            raise ServiceError("preset_not_found", "Preset not found", {"preset_id": preset_id})
        return ServiceResult(preset.to_detail() if hasattr(preset, "to_detail") else {"preset": self._preset_payload(preset)})

    async def apply_preset(self, *, preset_id: str, strategy: str = "overwrite") -> ServiceResult:
        from opc.market.architecture_registry import apply_architecture_preset_to_config, get_preset

        self._ensure_custom_org_editable()
        preset = get_preset(preset_id)
        if not preset:
            raise ServiceError("preset_not_found", f"Preset '{preset_id}' not found", {"preset_id": preset_id})
        if strategy not in {"namespace", "overwrite"}:
            raise ServiceError("invalid_strategy", "Strategy must be namespace or overwrite")
        async with self.context.config_lock:
            info = apply_architecture_preset_to_config(self.context.engine.config, preset_id, strategy=strategy, clear_existing=True)
            role_ids = list(info.role_ids)
            work_item_template_ids = list(info.work_item_template_ids or info.template_ids)
            employee_ids = self._ensure_default_employees(role_ids)
            self._persist_config()
        events = await self._reload_custom_agents_for_roles(role_ids)
        events.extend(await self._org_events())
        return ServiceResult({
            "ok": True,
            "action": "market_preset_applied",
            "package_id": preset_id,
            "name": getattr(preset, "name", preset_id),
            "roles": len(role_ids),
            "work_item_templates": len(work_item_template_ids),
            "employees": len(employee_ids),
        }, events)

    async def list_installed(self) -> ServiceResult:
        packages = []
        for package in list(getattr(self.context.engine.config.org, "installed_packages", []) or []):
            if hasattr(package, "model_dump"):
                packages.append(package.model_dump())
            elif isinstance(package, dict):
                packages.append(package)
        return ServiceResult({"packages": packages})

    async def export(self, *, package_id: str, name: str, description: str = "", version: str = "1.0.0", output_dir: str = ".") -> ServiceResult:
        from opc.market import PackageExporter

        if not package_id or not name:
            raise ServiceError("missing_package_fields", "package_id and name required")
        exporter = PackageExporter(self.context.engine.config, self.context.opc_home)
        package = exporter.export_current(package_id=package_id, name=name, description=description, version=version)
        out_path = exporter.write_to_path(package, Path(output_dir or self.context.opc_home / "exports"))
        return ServiceResult({
            "ok": True,
            "action": "market_exported",
            "path": str(out_path),
            "package_id": package_id,
            "roles": len(package.roles),
            "templates": len(package.talent_templates),
        })

    async def install(self, *, path: str, strategy: str = "namespace") -> ServiceResult:
        from opc.market import PackageLoader, SandboxChecker

        self._ensure_custom_org_editable()
        if not path:
            raise ServiceError("missing_path", "path required")
        loader = PackageLoader(self.context.engine.config, self.context.opc_home)
        package = loader.load_from_path(Path(path))
        report = SandboxChecker().validate(package)
        if not report.passed:
            raise ServiceError("package_security_failed", "Security check failed", {
                "sandbox_errors": list(report.errors),
                "sandbox_warnings": list(report.warnings),
            })
        async with self.context.config_lock:
            info = loader.install(package, strategy=strategy)
            employee_ids = self._ensure_default_employees(list(info.role_ids))
            self._persist_config()
        events = await self._reload_custom_agents_for_roles(list(info.role_ids))
        events.extend(await self._org_events())
        return ServiceResult({
            "ok": True,
            "action": "market_installed",
            "package_id": info.package_id,
            "name": info.name,
            "roles": len(info.role_ids),
            "templates": len(info.template_ids),
            "employees": len(employee_ids),
            "warnings": list(report.warnings),
        }, events)

    async def uninstall(self, package_id: str) -> ServiceResult:
        from opc.market import PackageLoader
        from opc.market.package_format import InstalledPackageInfo

        self._ensure_custom_org_editable()
        if not package_id:
            raise ServiceError("missing_package_id", "package_id required")
        removed_role_ids: set[str] = set()
        for package in self.context.engine.config.org.installed_packages:
            pid = package.package_id if isinstance(package, InstalledPackageInfo) else package.get("package_id", "")
            if pid == package_id:
                removed_role_ids = set(package.role_ids if isinstance(package, InstalledPackageInfo) else package.get("role_ids", []))
                break
        async with self.context.config_lock:
            success = PackageLoader(self.context.engine.config, self.context.opc_home).uninstall(package_id)
            if success:
                org = getattr(self.context.engine, "org_engine", None)
                if org:
                    org.reload_from_config()
                await self._clean_orphaned_assignments(removed_role_ids)
                self._persist_config()
        if not success:
            raise ServiceError("package_not_found", f"Package '{package_id}' not found", {"package_id": package_id})
        events: list[ServiceEvent] = []
        if removed_role_ids and self.context.agent_store is not None:
            try:
                agents = await self.context.agent_store.get_all()
                to_remove = [agent for agent in agents if agent.get("opc_role_id") in removed_role_ids]
                for agent in to_remove:
                    await self.context.agent_store.remove_agent(agent["agent_id"])
                if to_remove:
                    if self.context.sync_role_map is not None:
                        await self.context.sync_role_map()
                    sync = getattr(self.context.agent_store, "sync_custom_shadow", None)
                    if callable(sync):
                        await sync()
                    events.append(ServiceEvent("ack", {
                        "ok": True,
                        "action": "agents_spawned",
                        "agents": await self.context.agent_store.get_all(),
                    }))
            except Exception:
                pass
        events.extend(await self._org_events())
        return ServiceResult({"ok": True, "action": "market_uninstalled", "package_id": package_id}, events)

    def _persist_config(self) -> None:
        org = getattr(self.context.engine, "org_engine", None)
        if org:
            org.reload_from_config()
        if self.context.persist_runtime_config is not None:
            self.context.persist_runtime_config()
        else:
            self.context.engine.config.save()

    def _ensure_default_employees(self, role_ids: list[str]) -> list[str]:
        org = getattr(self.context.engine, "org_engine", None)
        if not org:
            return []
        employee_ids: list[str] = []
        for role_id in role_ids:
            try:
                employee = org.ensure_default_employee_for_role(role_id, persist=False)
                if employee:
                    employee_ids.append(employee.employee_id)
            except Exception:
                pass
        return employee_ids

    async def _reload_custom_agents_for_roles(self, role_ids: list[str]) -> list[ServiceEvent]:
        if self.context.mode_state.exec_mode not in {"org", "custom"}:
            return []
        org = getattr(self.context.engine, "org_engine", None)
        store = self.context.agent_store
        if org is None or store is None:
            return []
        try:
            db = getattr(store, "_db", None)
            if db is not None:
                await db.execute("DELETE FROM agents")
                await db.execute("DELETE FROM custom_agents_shadow")
                await db.commit()
            loader = getattr(store, "_load_from_role_infos", None)
            if callable(loader):
                installed = set(role_ids)
                roles_info = [role for role in org.list_agents() if role.role_id in installed]
                if roles_info:
                    await loader(roles_info, "custom")
            sync = getattr(store, "sync_custom_shadow", None)
            if callable(sync):
                await sync()
            if self.context.sync_role_map is not None:
                await self.context.sync_role_map()
            return [ServiceEvent("ack", {"ok": True, "action": "agents_spawned", "agents": await store.get_all()})]
        except Exception:
            return []

    async def _clean_orphaned_assignments(self, removed_role_ids: set[str]) -> None:
        """Clear ``assigned_to`` on every Task still pointing at a removed role.

        Each Task is saved independently: one Task failing to save must not
        abort the whole cleanup and leave the *rest* of the Tasks still
        pointing at a role that no longer exists. A single outer
        ``try/except Exception: pass`` around the loop used to do exactly
        that -- silently stopping after the first failure with no error
        surfaced anywhere, which defeats the purpose of this best-effort
        cleanup. (Same bug shape as ``AgentService._clean_orphaned_assignments``.)
        """
        if not removed_role_ids:
            return
        store = getattr(self.context.engine, "store", None)
        if not store:
            return
        try:
            tasks = await store.get_tasks(project_id=getattr(self.context.engine, "project_id", None) or "default")
        except Exception:
            logger.opt(exception=True).warning(
                f"_clean_orphaned_assignments: failed to list tasks for removed_role_ids={removed_role_ids!r}"
            )
            return
        for task in tasks:
            if getattr(task, "assigned_to", "") not in removed_role_ids:
                continue
            try:
                task.assigned_to = ""
                await store.save_task(task)
            except Exception:
                logger.opt(exception=True).warning(
                    "_clean_orphaned_assignments: failed to clear assigned_to "
                    f"on task {getattr(task, 'id', '')!r}"
                )

    async def _org_events(self) -> list[ServiceEvent]:
        from .org import OrgService

        return (await OrgService(self.context).info(include_events=True)).events

    @staticmethod
    def _preset_payload(preset: Any) -> dict[str, Any]:
        return {
            "id": getattr(preset, "id", ""),
            "name": getattr(preset, "name", ""),
            "description": getattr(preset, "description", ""),
            "category": getattr(preset, "category", ""),
            "roles": len(getattr(preset, "roles", []) or []),
            "templates": len(getattr(preset, "work_item_templates", []) or []),
            "collaboration_pattern": getattr(preset, "collaboration_pattern", ""),
        }

    @staticmethod
    def _installed_payload(package: Any) -> dict[str, Any]:
        if hasattr(package, "package_id"):
            return {
                "package_id": package.package_id,
                "name": package.name,
                "version": package.version,
                "role_ids": list(package.role_ids),
                "template_ids": list(package.template_ids),
            }
        return dict(package or {})
