from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opc.core.active_task_runs import ActiveTaskRunRegistry
from opc.core.config import OPCConfig, RoleConfig
from opc.core.org_config import (
    build_org_config_payload_from_config,
    write_org_config_payload,
    write_org_index,
)
from opc.engine import OPCEngine
from opc.layer2_organization.custom_runtime import CustomRuntimeRunner
from opc.plugins.office_ui.services.models import ServiceError


def test_requested_mode_normalization_keeps_core_company_router_main_compatible() -> None:
    assert OPCEngine._normalize_requested_mode("org") == "task"
    assert OPCEngine._normalize_requested_mode("custom") == "task"
    assert OPCEngine._normalize_requested_mode("company") == "company"
    assert OPCEngine._normalize_requested_mode("project") == "task"


def test_custom_runtime_runner_loads_org_storage_without_mutating_parent_config(tmp_path) -> None:
    config_dir = tmp_path / ".opc" / "config"
    org_config = OPCConfig()
    org_config.org.organization_id = "lab"
    org_config.org.organization_name = "Lab Org"
    org_config.org.company_profile = "custom"
    org_config.org.roles = [
        RoleConfig(
            id="director",
            name="Director",
            responsibility="Own final decisions",
            reports_to="owner",
        )
    ]
    payload = build_org_config_payload_from_config(
        org_config,
        organization_id="lab",
        organization_name="Lab Org",
    )
    write_org_config_payload(config_dir, "lab", payload)
    write_org_index(config_dir, "lab")

    engine = OPCEngine.__new__(OPCEngine)
    engine.opc_home = tmp_path / ".opc"
    engine.config = OPCConfig()

    loaded, resolved_org_id = CustomRuntimeRunner(engine)._build_org_config("")

    assert resolved_org_id == "lab"
    assert loaded.org.company_profile == "custom"
    assert loaded.org.organization_id == "lab"
    assert loaded.org.organization_name == "Lab Org"
    assert [role.id for role in loaded.org.roles] == ["director"]
    assert loaded.org.organization_config_file == "company_orgs/org_lab_config.yaml"
    assert engine.config.org.organization_id != "lab"
    assert not (config_dir / "company_index.yaml").exists()
    assert (config_dir / "company_orgs" / "org_lab_config.yaml").exists()


def test_process_message_rejects_org_mode_without_org_id() -> None:
    engine = OPCEngine.__new__(OPCEngine)
    engine.opc_home = None
    engine.config = OPCConfig()
    runner = CustomRuntimeRunner(engine)
    caught: ServiceError | None = None

    async def _run() -> None:
        await runner.process_message(
            "run org",
            project_id="default",
            session_id="session-1",
            org_id=None,
            preferred_agent=None,
            domains=None,
            origin_task_id=None,
            attachment_refs=None,
            message_metadata=None,
        )

    try:
        asyncio.run(_run())
    except ServiceError as exc:
        caught = exc
    assert caught is not None
    assert caught.code == "org_id_required"


def test_process_message_routes_org_mode_to_custom_runner(monkeypatch) -> None:
    engine = OPCEngine.__new__(OPCEngine)
    engine._initialized = True
    engine.project_id = "default"
    engine.memory = None
    engine._refresh_runtime_config_from_disk = AsyncMock()

    captured: dict[str, object] = {}

    async def fake_process(self, content: str, **kwargs):
        captured["content"] = content
        captured.update(kwargs)
        return "isolated org result"

    monkeypatch.setattr(CustomRuntimeRunner, "process_message", fake_process)

    result = asyncio.run(
        OPCEngine.process_message(
            engine,
            "run org",
            mode="org",
            org_id="lab",
            session_id="session-1",
            company_profile="custom",
        )
    )

    assert result == "isolated org result"
    assert captured["content"] == "run org"
    assert captured["org_id"] == "lab"
    assert captured["session_id"] == "session-1"
    engine._refresh_runtime_config_from_disk.assert_awaited_once()


def test_custom_runtime_initializes_with_parent_store(monkeypatch, tmp_path) -> None:
    config_dir = tmp_path / ".opc" / "config"
    org_config = OPCConfig()
    org_config.org.organization_id = "lab"
    org_config.org.organization_name = "Lab Org"
    org_config.org.company_profile = "custom"
    org_config.org.roles = [
        RoleConfig(
            id="director",
            name="Director",
            responsibility="Own final decisions",
            reports_to="owner",
        )
    ]
    payload = build_org_config_payload_from_config(
        org_config,
        organization_id="lab",
        organization_name="Lab Org",
    )
    write_org_config_payload(config_dir, "lab", payload)
    write_org_index(config_dir, "lab")

    parent_store = object()
    captured: dict[str, object] = {}

    class FakeMessageBus:
        def __init__(self, owner):
            self.owner = owner

        async def process_single(self, message):
            captured["store_during_run"] = self.owner.store
            captured["metadata"] = dict(message.metadata)
            return SimpleNamespace(content="custom result")

    class FakeRuntime:
        instances: list["FakeRuntime"] = []

        def __init__(self, **kwargs):
            self.config = kwargs["config"]
            self.project_id = kwargs["project_id"]
            self.store = kwargs.get("store") or object()
            self.owns_store = kwargs.get("owns_store")
            self.run_startup_reconcile = kwargs.get("run_startup_reconcile")
            self.active_task_run_registry = kwargs.get("active_task_run_registry")
            self.owns_active_task_run_registry = kwargs.get(
                "owns_active_task_run_registry"
            )
            self.bound_stores = []
            self.message_bus = FakeMessageBus(self)
            self.company_executor = SimpleNamespace(_signal_dispatcher_wake=lambda: None)
            FakeRuntime.instances.append(self)

        async def initialize(self):
            return None

        def bind_store(self, store, **kwargs):
            self.bound_stores.append(store)
            self.store = store

        def _normalize_attachment_refs(self, refs):
            return refs or []

        async def shutdown(self):
            captured["store_at_shutdown"] = self.store

    import opc.engine as engine_module

    monkeypatch.setattr(engine_module, "OPCEngine", FakeRuntime)

    parent = OPCEngine.__new__(OPCEngine)
    parent.opc_home = tmp_path / ".opc"
    parent.config = OPCConfig()
    parent.project_id = None
    parent.store = parent_store
    parent._active_task_run_registry = ActiveTaskRunRegistry()
    parent.on_progress = None
    parent.on_runtime_event = None
    parent.on_escalation = None
    parent.on_company_runtime_children = None

    def kanban_callback_factory(runtime):
        captured["kanban_callback_runtime"] = runtime

        async def _callback():
            captured["kanban_callback_called"] = True

        return _callback

    parent.on_company_kanban_callback_factory = kanban_callback_factory

    result = asyncio.run(
        CustomRuntimeRunner(parent).process_message(
            "run custom",
            project_id="default",
            session_id="session-1",
            org_id="lab",
            preferred_agent="codex",
            domains=[],
            origin_task_id="task-1",
            attachment_refs=[],
            message_metadata={},
        )
    )

    runtime = FakeRuntime.instances[-1]
    assert result == "custom result"
    assert runtime.store is parent_store
    assert runtime.owns_store is False
    assert runtime.run_startup_reconcile is False
    assert runtime.active_task_run_registry is parent._active_task_run_registry
    assert runtime.owns_active_task_run_registry is False
    assert runtime.bound_stores == []
    assert captured["kanban_callback_runtime"] is runtime
    assert callable(runtime.company_executor.on_kanban_changed)
    assert captured["store_during_run"] is parent_store
    assert captured["store_at_shutdown"] is parent_store
    assert captured["metadata"]["mode"] == "company"
    assert captured["metadata"]["exec_mode"] == "org"
    assert captured["metadata"]["org_id"] == "lab"
