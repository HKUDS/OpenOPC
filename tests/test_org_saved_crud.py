"""CRUD tests for saved org architectures under .opc/config/company_orgs."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml


def test_name_regex_accepts_valid_ids():
    from opc.plugins.office_ui.ws_handler import _SAVED_ORG_NAME_RE

    for name in ["lab_org", "startup-v2", "a", "a1", "0" * 64, "foo_bar-baz"]:
        assert _SAVED_ORG_NAME_RE.match(name), f"should accept {name!r}"


def test_name_regex_rejects_invalid_ids():
    from opc.plugins.office_ui.ws_handler import _SAVED_ORG_NAME_RE

    for name in ["", "..", "../etc", "foo/bar", "has space", "x" * 65, "A1", "unicode_ß"]:
        assert not _SAVED_ORG_NAME_RE.match(name), f"should reject {name!r}"


def test_saved_org_path_valid_resolves_under_company_orgs(monkeypatch, tmp_path):
    from opc.plugins.office_ui import ws_handler as wh

    monkeypatch.setattr("opc.core.config._find_project_root", lambda: tmp_path)
    result = wh._saved_org_path("my_org")
    expected = tmp_path / ".opc" / "config" / "company_orgs" / "org_my_org_config.yaml"
    assert result == expected


def test_saved_org_path_lax_slugifies_display_name(monkeypatch, tmp_path):
    from opc.plugins.office_ui import ws_handler as wh

    monkeypatch.setattr("opc.core.config._find_project_root", lambda: tmp_path)
    result = wh._saved_org_path("HKU Lab", strict=False)
    assert result == tmp_path / ".opc" / "config" / "company_orgs" / "org_hku_lab_config.yaml"
    with pytest.raises(ValueError):
        wh._saved_org_path("../etc/passwd")


def _write_saved_org(config_dir: Path, organization_id: str, *, organization_name: str = "Test Co", roles_count: int = 2) -> Path:
    from opc.core.org_config import org_config_path
    from opc.plugins.office_ui.org_architecture_snapshot import ORG_ARCHITECTURE_KIND

    payload = {
        "schema_version": 2,
        "kind": ORG_ARCHITECTURE_KIND,
        "organization_id": organization_id,
        "organization_name": organization_name,
        "company": {
            "name": organization_name,
            "topology": "flat",
            "company_profile": "custom",
            "final_decider_role_id": None,
            "company_profiles": ["corporate", "custom"],
        },
        "roles": [
            {"id": f"role_{i}", "name": f"Role {i}", "responsibility": "Work.", "reports_to": "owner"}
            for i in range(roles_count)
        ],
        "employees": [],
        "escalation_rules": [],
        "runtime_policies": {},
        "talent_templates": [],
        "teams": [],
        "team_runtime": {},
        "installed_packages": [],
        "role_serial_queue_enabled": True,
        "metadata": {"source": "test"},
    }
    path = org_config_path(config_dir, organization_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(payload, default_flow_style=False, sort_keys=False), encoding="utf-8")
    return path


class _FakeServerStateStore:
    def __init__(self, state: dict[str, str] | None = None) -> None:
        self.state = dict(state or {})

    async def get_server_state(self, key: str, default: str = "") -> str:
        return self.state.get(key, default)

    async def set_server_state(self, key: str, value: str) -> None:
        self.state[key] = value


class _FakeWS:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


def _async_cm() -> AsyncMock:
    lock = AsyncMock()
    lock.__aenter__ = AsyncMock(return_value=None)
    lock.__aexit__ = AsyncMock(return_value=None)
    return lock


def test_saved_file_structure_is_loadable(tmp_path):
    from opc.plugins.office_ui.org_architecture_snapshot import ORG_ARCHITECTURE_KIND

    config_dir = tmp_path / ".opc" / "config"
    path = _write_saved_org(config_dir, "fixture_org", roles_count=3)
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == 2
    assert parsed["kind"] == ORG_ARCHITECTURE_KIND
    assert parsed["organization_id"] == "fixture_org"
    assert parsed["company"]["company_profile"] == "custom"
    assert len(parsed["roles"]) == 3


def test_saved_org_storage_rejects_reserved_corporate_id(tmp_path):
    from opc.core.org_config import (
        list_org_config_paths,
        org_config_path,
        read_org_index,
        write_org_config_payload,
        write_org_index,
    )

    config_dir = tmp_path / ".opc" / "config"
    config_dir.mkdir(parents=True)
    with pytest.raises(ValueError):
        org_config_path(config_dir, "corporate")
    with pytest.raises(ValueError):
        write_org_index(config_dir, "corporate")
    with pytest.raises(ValueError):
        write_org_config_payload(config_dir, "corporate", {"organization_id": "corporate"})

    orgs_dir = config_dir / "company_orgs"
    orgs_dir.mkdir(parents=True)
    (orgs_dir / "org_corporate_config.yaml").write_text("organization_id: corporate\n", encoding="utf-8")
    (config_dir / "org_index.yaml").write_text("active_organization_id: corporate\n", encoding="utf-8")

    assert read_org_index(config_dir) is None
    assert list_org_config_paths(config_dir) == []


def test_load_via_import_format_accepted_by_loader(tmp_path):
    from opc.core.config import OPCConfig
    from opc.plugins.office_ui.org_architecture_snapshot import apply_org_architecture_snapshot, parse_org_architecture_snapshot

    config_dir = tmp_path / ".opc" / "config"
    path = _write_saved_org(config_dir, "lab", roles_count=2)
    cfg = apply_org_architecture_snapshot(OPCConfig(), parse_org_architecture_snapshot(path.read_text()))
    assert cfg.org.organization_id == "lab"
    assert len(cfg.org.roles) == 2
    assert cfg.org.company_profile == "custom"


def test_write_active_org_config_never_writes_reserved_corporate_saved_org(tmp_path):
    from opc.core.config import OPCConfig
    from opc.plugins.office_ui import ws_handler as wh

    cfg = OPCConfig()
    cfg.org.company_profile = "custom"
    cfg.org.organization_id = "corporate"
    cfg.org.organization_name = "Corporate"
    cfg.org.company_name = "Corporate"

    handler = wh.WSHandler.__new__(wh.WSHandler)
    handler.engine = SimpleNamespace(config=cfg, opc_home=tmp_path / ".opc")

    handler._write_active_org_config(cfg)

    config_dir = tmp_path / ".opc" / "config"
    assert cfg.org.organization_id != "corporate"
    assert not (config_dir / "company_orgs" / "org_corporate_config.yaml").exists()
    assert (config_dir / "company_orgs" / f"org_{cfg.org.organization_id}_config.yaml").exists()


def test_project_activation_reloads_active_saved_org(tmp_path):
    from opc.core.config import OPCConfig
    from opc.core.org_config import write_org_index
    from opc.plugins.office_ui import ws_handler as wh

    config_dir = tmp_path / ".opc" / "config"
    _write_saved_org(config_dir, "splinter", organization_name="Splinter AI Company", roles_count=1)
    write_org_index(config_dir, "splinter")

    delegate_config = OPCConfig()
    delegate_config.org.company_profile = "corporate"
    delegate_config.org.organization_id = "corporate"
    delegate_org_engine = SimpleNamespace(
        config=delegate_config,
        reload_from_config=MagicMock(),
    )
    delegate = SimpleNamespace(
        project_id="shipdocs",
        opc_home=tmp_path / ".opc",
        config=delegate_config,
        org_engine=delegate_org_engine,
        talent_market=SimpleNamespace(config=delegate_config),
        _ensure_attachment_store=MagicMock(),
    )

    handler = wh.WSHandler.__new__(wh.WSHandler)
    handler.engine = SimpleNamespace(project_id="default")
    handler.chat_store = MagicMock()
    handler._exec_mode = "org"
    handler._company_profile = "custom"

    handler._on_service_engine_activated(delegate, "shipdocs")

    assert handler.engine is delegate
    assert delegate.config.org.company_profile == "custom"
    assert delegate.config.org.organization_id == "splinter"
    assert [role.id for role in delegate.config.org.roles] == ["role_0"]
    assert delegate.org_engine.config is delegate.config
    delegate.org_engine.reload_from_config.assert_called_once_with()
    delegate._ensure_attachment_store.assert_called_once_with()


def test_org_service_rejects_corporate_write_operations(tmp_path):
    from opc.core.config import OPCConfig
    from opc.plugins.office_ui.services.context import ModeState, OfficeServiceContext
    from opc.plugins.office_ui.services.models import ServiceError
    from opc.plugins.office_ui.services.org import OrgService

    async def run() -> None:
        engine = SimpleNamespace(
            config=OPCConfig(),
            opc_home=tmp_path / ".opc",
            org_engine=MagicMock(),
            talent_market=MagicMock(),
        )
        context = OfficeServiceContext(
            engine=engine,
            agent_store=MagicMock(),
            chat_store=MagicMock(),
            event_adapter=MagicMock(),
            mode_state=ModeState(exec_mode="company", company_profile="corporate"),
        )
        with pytest.raises(ServiceError) as exc:
            await OrgService(context).add_role({
                "role_id": "analyst",
                "name": "Analyst",
                "responsibility": "Analyze.",
            })
        assert exc.value.code == "org_read_only"

    asyncio.run(run())


def test_org_service_saved_create_from_corporate_mode(tmp_path):
    from opc.core.config import OPCConfig
    from opc.core.org_config import (
        apply_org_config_payload_to_config,
        load_org_config_payload,
        read_org_index,
        validate_runnable_org_config,
    )
    from opc.plugins.office_ui.services.context import ModeState, OfficeServiceContext
    from opc.plugins.office_ui.services.org import OrgService

    async def run() -> None:
        cfg = OPCConfig()
        org_engine = SimpleNamespace(config=cfg, reload_from_config=MagicMock())
        talent_market = SimpleNamespace(config=cfg)
        engine = SimpleNamespace(
            config=cfg,
            opc_home=tmp_path / ".opc",
            org_engine=org_engine,
            talent_market=talent_market,
        )
        context = OfficeServiceContext(
            engine=engine,
            agent_store=MagicMock(),
            chat_store=MagicMock(),
            event_adapter=MagicMock(),
            mode_state=ModeState(exec_mode="company", company_profile="corporate"),
        )
        context.set_active_saved_org_name = AsyncMock()

        result = await OrgService(context).saved_create(
            organization_name="HKU Research Lab",
            members=[
                {
                    "name": "Research Lead",
                    "responsibility": "Sets research direction.",
                    "prompt": "Lead with a careful research protocol.",
                },
                {
                    "name": "Data Scientist",
                    "responsibility": "Builds datasets.",
                    "prompt": "Prefer reproducible data checks.",
                    "reports_to_index": 0,
                },
            ],
        )

        config_dir = tmp_path / ".opc" / "config"
        assert result.payload["ok"] is True
        assert result.payload["organization_id"] == "hku_research_lab"
        assert result.payload["roles_count"] == 2
        assert result.payload["employees_count"] == 2
        assert read_org_index(config_dir) == "hku_research_lab"
        assert not (config_dir / "company_index.yaml").exists()
        path = config_dir / "company_orgs" / "org_hku_research_lab_config.yaml"
        assert path.exists()

        payload, _ = load_org_config_payload(config_dir, "hku_research_lab")
        assert payload["organization_name"] == "HKU Research Lab"
        assert len(payload["roles"]) == 2
        assert payload["employees"] == []
        assert payload["roles"][0]["id"] == "research_lead"
        assert payload["roles"][0]["prompt_refs"] == ["Lead with a careful research protocol."]
        assert payload["roles"][1]["reports_to"] == "research_lead"
        assert payload["roles"][1]["prompt_refs"] == ["Prefer reproducible data checks."]
        validated = apply_org_config_payload_to_config(OPCConfig(), payload, source_path=path)
        validate_runnable_org_config(validated, organization_id="hku_research_lab")
        from opc.layer2_organization.org_engine import OrgEngine

        runtime_org = OrgEngine(validated, opc_home=tmp_path / ".opc")
        assert runtime_org.get_role_prompt_context("research_lead") == "Lead with a careful research protocol."
        assert runtime_org.get_role_prompt_context("data_scientist") == "Prefer reproducible data checks."
        assert engine.config.org.organization_id == "hku_research_lab"
        assert engine.config is org_engine.config
        assert engine.config is talent_market.config
        org_engine.reload_from_config.assert_called_once()
        context.set_active_saved_org_name.assert_awaited_once_with("hku_research_lab")

    asyncio.run(run())


def test_org_service_saved_create_validates_minimum_members(tmp_path):
    from opc.core.config import OPCConfig
    from opc.plugins.office_ui.services.context import ModeState, OfficeServiceContext
    from opc.plugins.office_ui.services.models import ServiceError
    from opc.plugins.office_ui.services.org import OrgService

    async def run() -> None:
        cfg = OPCConfig()
        context = OfficeServiceContext(
            engine=SimpleNamespace(
                config=cfg,
                opc_home=tmp_path / ".opc",
                org_engine=SimpleNamespace(config=cfg, reload_from_config=MagicMock()),
                talent_market=SimpleNamespace(config=cfg),
            ),
            agent_store=MagicMock(),
            chat_store=MagicMock(),
            event_adapter=MagicMock(),
            mode_state=ModeState(exec_mode="task", company_profile="corporate"),
        )
        with pytest.raises(ServiceError) as exc:
            await OrgService(context).saved_create(
                organization_name="One Person Org",
                members=[{"name": "Solo"}],
            )
        assert exc.value.code == "org_members_required"

    asyncio.run(run())


def test_org_service_saved_create_validates_member_hierarchy(tmp_path):
    from opc.core.config import OPCConfig
    from opc.plugins.office_ui.services.context import ModeState, OfficeServiceContext
    from opc.plugins.office_ui.services.models import ServiceError
    from opc.plugins.office_ui.services.org import OrgService

    async def run() -> None:
        cfg = OPCConfig()
        context = OfficeServiceContext(
            engine=SimpleNamespace(
                config=cfg,
                opc_home=tmp_path / ".opc",
                org_engine=SimpleNamespace(config=cfg, reload_from_config=MagicMock()),
                talent_market=SimpleNamespace(config=cfg),
            ),
            agent_store=MagicMock(),
            chat_store=MagicMock(),
            event_adapter=MagicMock(),
            mode_state=ModeState(exec_mode="company", company_profile="corporate"),
        )
        with pytest.raises(ServiceError) as exc:
            await OrgService(context).saved_create(
                organization_name="Bad Hierarchy Org",
                members=[
                    {"name": "Lead"},
                    {"name": "Peer", "reports_to_index": 1},
                ],
            )
        assert exc.value.code == "invalid_org_member_hierarchy"

    asyncio.run(run())


def test_org_saved_create_handler_switches_to_new_org(tmp_path):
    from opc.plugins.office_ui import ws_handler as wh
    from opc.plugins.office_ui.services.models import ServiceResult

    async def run() -> None:
        org_service = SimpleNamespace(saved_create=AsyncMock(return_value=ServiceResult({
            "ok": True,
            "name": "hku_lab",
            "organization_id": "hku_lab",
            "organization_name": "HKU Lab",
            "filename": "org_hku_lab_config.yaml",
            "roles_count": 2,
            "employees_count": 2,
        })))
        handler = wh.WSHandler.__new__(wh.WSHandler)
        handler._ensure_office_services = MagicMock(return_value=SimpleNamespace(org=org_service))
        handler._apply_mode_switch = AsyncMock(return_value=True)
        handler._task_preferred_agent = "native"
        ws = _FakeWS()

        await handler._handle_org_saved_create(ws, {
            "organization_name": "HKU Lab",
            "members": [{"name": "Lead"}, {"name": "Analyst", "reports_to_index": 0}],
        })

        org_service.saved_create.assert_awaited_once()
        handler._apply_mode_switch.assert_awaited_once_with("org", "custom", "native", org_id="hku_lab")
        assert ws.messages[-1]["type"] == "org_saved_create"
        assert ws.messages[-1]["payload"]["ok"] is True
        assert ws.messages[-1]["payload"]["organization_id"] == "hku_lab"

    asyncio.run(run())


def test_org_service_rebinds_replaced_config_to_runtime_components(tmp_path):
    from opc.core.config import OPCConfig, RoleConfig
    from opc.core.org_config import build_org_config_payload_from_config
    from opc.plugins.office_ui.services.context import ModeState, OfficeServiceContext
    from opc.plugins.office_ui.services.org import OrgService

    async def run() -> None:
        original = OPCConfig()
        original.org.company_profile = "custom"
        original.org.organization_id = "lab"
        original.org.organization_name = "Lab"
        original.org.company_name = "Lab"
        org_engine = SimpleNamespace(config=original, reload_from_config=MagicMock())
        talent_market = SimpleNamespace(config=original)
        engine = SimpleNamespace(
            config=original,
            opc_home=tmp_path / ".opc",
            org_engine=org_engine,
            talent_market=talent_market,
        )
        context = OfficeServiceContext(
            engine=engine,
            agent_store=MagicMock(),
            chat_store=MagicMock(),
            event_adapter=MagicMock(),
            mode_state=ModeState(exec_mode="org", company_profile="custom"),
        )
        context.persist_runtime_config = lambda: None
        replacement = OPCConfig()
        replacement.org.company_profile = "custom"
        replacement.org.organization_id = "lab"
        replacement.org.organization_name = "Lab"
        replacement.org.company_name = "Lab"
        replacement.org.roles = [
            RoleConfig(id="director", name="Director", responsibility="Own direction."),
        ]

        payload = build_org_config_payload_from_config(
            replacement,
            organization_id="lab",
            organization_name="Lab",
        )
        await OrgService(context).import_config(payload)

        assert engine.config is org_engine.config
        assert engine.config is talent_market.config
        assert [role.id for role in engine.config.org.roles] == ["director"]
        org_engine.reload_from_config.assert_called()

    asyncio.run(run())


def test_saved_list_returns_active_id(monkeypatch, tmp_path):
    from opc.core.org_config import write_org_index
    from opc.plugins.office_ui import ws_handler as wh

    async def run() -> None:
        monkeypatch.setattr("opc.core.config._find_project_root", lambda: tmp_path)
        config_dir = tmp_path / ".opc" / "config"
        path = _write_saved_org(config_dir, "lab", organization_name="HKU Lab", roles_count=2)
        saved = yaml.safe_load(path.read_text(encoding="utf-8"))
        saved["employees"] = [
            {"employee_id": "ava", "name": "Ava Chen", "role_id": "role_0"},
            {
                "employee_id": "role-0-default-employee",
                "name": "Role 0 Default Employee",
                "role_id": "role_0",
                "metadata": {
                    "is_default_employee": True,
                    "auto_created_for_role": "role_0",
                    "employee_origin": "system_default",
                },
            },
        ]
        path.write_text(yaml.dump(saved, default_flow_style=False, sort_keys=False), encoding="utf-8")
        write_org_index(config_dir, "lab")

        handler = wh.WSHandler.__new__(wh.WSHandler)
        handler.engine = SimpleNamespace(opc_home=tmp_path / ".opc")
        handler.agent_store = _FakeServerStateStore()
        ws = _FakeWS()

        await handler._handle_org_saved_list(ws, {})
        payload = ws.messages[-1]["payload"]
        assert payload["active_name"] == "lab"
        assert payload["orgs"][0]["organization_id"] == "lab"
        assert payload["orgs"][0]["organization_name"] == "HKU Lab"
        assert payload["orgs"][0]["filename"] == "org_lab_config.yaml"
        assert payload["orgs"][0]["employees_count"] == 1
        assert not (config_dir / "company_index.yaml").exists()

    asyncio.run(run())


def test_saved_delete_rejects_active_org(monkeypatch, tmp_path):
    from opc.core.org_config import write_org_index
    from opc.plugins.office_ui import ws_handler as wh

    async def run() -> None:
        monkeypatch.setattr("opc.core.config._find_project_root", lambda: tmp_path)
        config_dir = tmp_path / ".opc" / "config"
        path = _write_saved_org(config_dir, "lab", roles_count=2)
        write_org_index(config_dir, "lab")

        handler = wh.WSHandler.__new__(wh.WSHandler)
        handler.engine = SimpleNamespace(opc_home=tmp_path / ".opc")
        handler.agent_store = _FakeServerStateStore()
        ws = _FakeWS()

        await handler._handle_org_saved_delete(ws, {"organization_id": "lab"})
        assert ws.messages[-1]["payload"]["ok"] is False
        assert ws.messages[-1]["payload"]["error"] == "cannot_delete_active"
        assert path.exists()

    asyncio.run(run())


def test_startup_restores_active_saved_org_when_org_config_empty(monkeypatch, tmp_path):
    from opc.core.config import OPCConfig
    from opc.core.org_config import write_org_index
    from opc.plugins.office_ui import ws_handler as wh

    async def run() -> None:
        monkeypatch.setattr("opc.core.config._find_project_root", lambda: tmp_path)
        config_dir = tmp_path / ".opc" / "config"
        _write_saved_org(config_dir, "lab", roles_count=2)
        write_org_index(config_dir, "lab")

        cfg = OPCConfig()
        cfg.org.company_profile = "custom"
        cfg.org.roles = []
        handler = wh.WSHandler.__new__(wh.WSHandler)
        handler.engine = SimpleNamespace(
            config=cfg,
            opc_home=tmp_path / ".opc",
            org_engine=MagicMock(),
            talent_market=MagicMock(),
        )
        handler.agent_store = _FakeServerStateStore()
        handler._exec_mode = "org"
        handler._config_lock = _async_cm()

        await handler._restore_active_saved_org_if_needed()

        assert [role.id for role in handler.engine.config.org.roles] == ["role_0", "role_1"]
        assert handler.engine.config.org.organization_id == "lab"
        on_disk = yaml.safe_load((config_dir / "company_orgs" / "org_lab_config.yaml").read_text())
        assert len(on_disk["roles"]) == 2
        assert not (config_dir / "company_index.yaml").exists()
        handler.engine.org_engine.reload_from_config.assert_called_once()

    asyncio.run(run())
