"""Round-trip tests for organization config schema.

Covers:
- save() writes schema_version: 2 org config under company_orgs/
- load() rejects schema_version > 1 with ValueError
- Export YAML round-trips roles/employees bitwise identical

All tests redirect _find_project_root to tmp_path so the real config is untouched.
"""
from __future__ import annotations

import yaml
import pytest


def _write_minimal_corporate(corporate_dir, schema_version: int | None = 1) -> None:
    """Seed a minimal valid corporate yaml in the given directory."""
    corporate_dir.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "company": {
            "name": "Test Co",
            "topology": "flat",
            "company_profile": "",
            "execution_model": "sequential",
            "final_decider_role_id": None,
            "company_profiles": [],
        },
        "roles": [],
        "employees": [],
        "escalation_rules": [],
    }
    if schema_version is not None:
        payload = {"schema_version": schema_version, **payload}
    (corporate_dir / "company_corporate_config.yaml").write_text(
        yaml.dump(payload, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def test_company_org_written_by_save(tmp_path, monkeypatch):
    """OPCConfig.save() must emit the corporate org config under company_orgs/."""
    from opc.core import config as cfg_module
    from opc.layer2_organization.company_runtime_profiles import get_builtin_roles

    monkeypatch.setattr(cfg_module, "_find_project_root", lambda: tmp_path)

    cfg = cfg_module.OPCConfig()
    cfg.save()

    config_dir = tmp_path / ".opc" / "config"
    index_path = config_dir / "company_index.yaml"
    org_path = config_dir / "company_orgs" / "org_corporate_config.yaml"
    assert org_path.exists(), "save() did not create corporate organization config"
    assert not index_path.exists(), "save() should not create legacy company_index.yaml"
    parsed = yaml.safe_load(org_path.read_text(encoding="utf-8"))
    assert parsed.get("schema_version") == 2
    assert parsed["organization_id"] == "corporate"
    assert parsed["company"]["final_decider_role_id"] == "ceo"
    assert [role["id"] for role in parsed["roles"]] == [role.id for role in get_builtin_roles("corporate")]
    assert "corporate" in parsed["runtime_policies"]
    assert parsed["employees"] == []
    assert parsed["talent_templates"] == []


def test_load_rejects_future_schema(tmp_path, monkeypatch):
    """OPCConfig.load() must raise ValueError for schema_version > 1."""
    from opc.core import config as cfg_module

    monkeypatch.setattr(cfg_module, "_find_project_root", lambda: tmp_path)
    corporate_dir = tmp_path / "config"
    _write_minimal_corporate(corporate_dir, schema_version=99)

    with pytest.raises(ValueError, match="schema_version 99"):
        cfg_module.OPCConfig.load(corporate_dir)


def test_load_accepts_current_and_missing_schema_version(tmp_path, monkeypatch):
    """load() must silently accept schema_version: 1 AND legacy files missing the key."""
    from opc.core import config as cfg_module

    monkeypatch.setattr(cfg_module, "_find_project_root", lambda: tmp_path)
    corporate_dir = tmp_path / "config"

    # schema_version: 1 — explicit
    _write_minimal_corporate(corporate_dir, schema_version=1)
    cfg1 = cfg_module.OPCConfig.load(corporate_dir)
    assert cfg1 is not None

    # missing schema_version — legacy compatible
    _write_minimal_corporate(corporate_dir, schema_version=None)
    cfg2 = cfg_module.OPCConfig.load(corporate_dir)
    assert cfg2 is not None


def test_load_uses_corporate_company_payload_even_when_legacy_index_points_custom(tmp_path, monkeypatch):
    """company_index.yaml must not let a saved custom org replace company mode."""
    from opc.core import config as cfg_module

    monkeypatch.setattr(cfg_module, "_find_project_root", lambda: tmp_path)
    config_dir = tmp_path / ".opc" / "config"
    orgs_dir = config_dir / "company_orgs"
    orgs_dir.mkdir(parents=True)

    corporate_payload = {
        "schema_version": 2,
        "kind": "opc_org_architecture",
        "organization_id": "corporate",
        "organization_name": "Corporate",
        "company": {
            "name": "Corporate",
            "company_profile": "corporate",
            "execution_model": "actor_runtime",
            "company_profiles": ["corporate", "custom"],
        },
        "roles": [],
        "employees": [],
        "escalation_rules": [],
    }
    quantum_payload = {
        **corporate_payload,
        "organization_id": "quantum_harbor",
        "organization_name": "Quantum Harbor",
        "company": {
            **corporate_payload["company"],
            "name": "Quantum Harbor",
            "company_profile": "custom",
        },
        "roles": [{"id": "founder_ceo", "name": "Founder CEO", "responsibility": "Lead"}],
    }
    (orgs_dir / "org_corporate_config.yaml").write_text(
        yaml.dump(corporate_payload, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    (orgs_dir / "org_quantum_harbor_config.yaml").write_text(
        yaml.dump(quantum_payload, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    (config_dir / "company_index.yaml").write_text(
        yaml.dump({"schema_version": 1, "active_organization_id": "quantum_harbor"}),
        encoding="utf-8",
    )

    cfg = cfg_module.OPCConfig.load(config_dir, trusted_source=True)

    assert cfg.org.organization_id == "corporate"
    assert cfg.org.company_profile == "corporate"
    assert [role.id for role in cfg.org.roles] == []


def test_company_orgs_does_not_use_company_style_fallback_files(tmp_path, monkeypatch):
    from opc.layer2_organization.company_runtime_profiles import get_builtin_roles
    from opc.core import config as cfg_module

    monkeypatch.setattr(cfg_module, "_find_project_root", lambda: tmp_path)
    config_dir = tmp_path / ".opc" / "config"
    company_orgs_dir = config_dir / "company_orgs"
    company_orgs_dir.mkdir(parents=True)

    corporate_payload = {
        "schema_version": 2,
        "kind": "opc_org_architecture",
        "organization_id": "corporate",
        "organization_name": "Corporate",
        "company": {
            "name": "Corporate",
            "company_profile": "corporate",
            "execution_model": "actor_runtime",
            "company_profiles": ["corporate", "custom"],
        },
        "roles": [],
        "employees": [],
        "escalation_rules": [],
    }
    corporate_payload["roles"] = [
        {"id": "company_style_legacy_role", "name": "Legacy Role", "responsibility": "Should not load"},
    ]
    (company_orgs_dir / "company_corporate_config.yaml").write_text(
        yaml.dump(corporate_payload, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )

    cfg = cfg_module.OPCConfig.load(config_dir, trusted_source=True)

    assert cfg.org.organization_id == "corporate"
    assert cfg.org.company_profile == "corporate"
    role_ids = [role.id for role in cfg.org.roles]
    assert role_ids == [role.id for role in get_builtin_roles("corporate")]
    assert "company_style_legacy_role" not in role_ids
    assert (company_orgs_dir / "org_corporate_config.yaml").exists()


def test_custom_org_save_writes_saved_org_storage_not_company_index(tmp_path, monkeypatch):
    from opc.core import config as cfg_module
    from opc.core.config import RoleConfig

    monkeypatch.setattr(cfg_module, "_find_project_root", lambda: tmp_path)
    config_dir = tmp_path / ".opc" / "config"
    cfg = cfg_module.OPCConfig()
    cfg.org.organization_id = "lab"
    cfg.org.organization_name = "Lab Org"
    cfg.org.organization_config_file = "company_orgs/org_lab_config.yaml"
    cfg.org.company_name = "Lab Org"
    cfg.org.company_profile = "custom"
    cfg.org.roles = [
        RoleConfig(id="director", name="Director", responsibility="Own final decisions"),
    ]

    cfg.save(config_dir)

    assert (config_dir / "company_orgs" / "org_lab_config.yaml").exists()
    assert yaml.safe_load((config_dir / "org_index.yaml").read_text(encoding="utf-8"))["active_organization_id"] == "lab"
    assert not (config_dir / "company_index.yaml").exists()
    assert not (config_dir / "company_orgs" / "company_lab_config.yaml").exists()


def test_org_payload_filters_runtime_placeholder_employees():
    from opc.core.config import EmployeeConfig, OPCConfig, RoleConfig, build_company_org_payload_from_config

    cfg = OPCConfig()
    cfg.org.roles = [
        RoleConfig(id="researcher", name="Researcher", responsibility="Research"),
    ]
    cfg.org.employees = [
        EmployeeConfig(employee_id="ava", name="Ava Chen", role_id="researcher"),
        EmployeeConfig(
            employee_id="researcher-default-employee",
            template_id="general-default-employee",
            name="Researcher Default Employee",
            role_id="researcher",
            metadata={
                "is_default_employee": True,
                "auto_created_for_role": "researcher",
                "employee_origin": "system_default",
            },
        ),
        EmployeeConfig(
            employee_id="researcher-fallback-empty-employee",
            template_id="fallback-empty-employee",
            name="Researcher Fallback Empty Employee",
            role_id="researcher",
            metadata={
                "is_fallback_employee": True,
                "auto_created_for_role": "researcher",
                "employee_origin": "recruitment_fallback",
            },
        ),
        EmployeeConfig(
            employee_id="saved-default",
            template_id="general-default-employee",
            name="Saved Default",
            role_id="researcher",
            metadata={
                "is_default_employee": True,
                "auto_created_for_role": "researcher",
                "employee_origin": "system_default",
                "persist_to_org": True,
            },
        ),
    ]

    payload = build_company_org_payload_from_config(cfg)

    assert payload["employees"] == []


def test_custom_org_save_drops_runtime_placeholder_employees(tmp_path, monkeypatch):
    from opc.core import config as cfg_module
    from opc.core.config import EmployeeConfig, RoleConfig

    monkeypatch.setattr(cfg_module, "_find_project_root", lambda: tmp_path)
    config_dir = tmp_path / ".opc" / "config"
    cfg = cfg_module.OPCConfig()
    cfg.org.organization_id = "lab"
    cfg.org.organization_name = "Lab Org"
    cfg.org.organization_config_file = "company_orgs/org_lab_config.yaml"
    cfg.org.company_name = "Lab Org"
    cfg.org.company_profile = "custom"
    cfg.org.roles = [
        RoleConfig(id="researcher", name="Researcher", responsibility="Research"),
    ]
    cfg.org.employees = [
        EmployeeConfig(employee_id="ava", name="Ava Chen", role_id="researcher"),
        EmployeeConfig(
            employee_id="researcher-default-employee",
            template_id="general-default-employee",
            name="Researcher Default Employee",
            role_id="researcher",
            metadata={
                "is_default_employee": True,
                "auto_created_for_role": "researcher",
                "employee_origin": "system_default",
            },
        ),
    ]

    cfg.save(config_dir)
    data = yaml.safe_load((config_dir / "company_orgs" / "org_lab_config.yaml").read_text(encoding="utf-8"))

    assert data["employees"] == []
    registry_path = tmp_path / ".opc" / "company_state" / "lab" / "employees" / "ava.yaml"
    assert registry_path.exists()


def test_org_config_load_ignores_runtime_placeholder_employees():
    from opc.core.config import OPCConfig
    from opc.core.org_config import apply_org_config_payload_to_config

    payload = {
        "schema_version": 2,
        "kind": "company_org",
        "organization_id": "lab",
        "organization_name": "Lab Org",
        "company": {"name": "Lab Org", "company_profile": "custom"},
        "roles": [{"id": "researcher", "name": "Researcher", "responsibility": "Research"}],
        "employees": [
            {"employee_id": "ava", "name": "Ava Chen", "role_id": "researcher"},
            {
                "employee_id": "researcher-default-employee",
                "template_id": "general-default-employee",
                "name": "Researcher Default Employee",
                "role_id": "researcher",
                "metadata": {
                    "is_default_employee": True,
                    "auto_created_for_role": "researcher",
                    "employee_origin": "system_default",
                },
            },
            {
                "employee_id": "saved-default",
                "template_id": "general-default-employee",
                "name": "Saved Default",
                "role_id": "researcher",
                "metadata": {
                    "is_default_employee": True,
                    "auto_created_for_role": "researcher",
                    "employee_origin": "system_default",
                    "persist_to_org": True,
                },
            },
        ],
        "escalation_rules": [],
    }

    cfg = apply_org_config_payload_to_config(OPCConfig(), payload)

    assert [employee.employee_id for employee in cfg.org.employees] == ["ava", "saved-default"]


def test_load_self_heals_legacy_company_org_employees_to_registry(tmp_path, monkeypatch):
    from opc.core import config as cfg_module

    monkeypatch.setattr(cfg_module, "_find_project_root", lambda: tmp_path)
    config_dir = tmp_path / ".opc" / "config"
    org_dir = config_dir / "company_orgs"
    org_dir.mkdir(parents=True)
    org_path = org_dir / "org_corporate_config.yaml"
    org_path.write_text(
        yaml.dump(
            {
                "schema_version": 2,
                "kind": "opc_org_architecture",
                "organization_id": "corporate",
                "organization_name": "Corporate",
                "company": {
                    "name": "Corporate",
                    "company_profile": "corporate",
                    "execution_model": "actor_runtime",
                    "company_profiles": ["corporate", "custom"],
                },
                "roles": [],
                "employees": [
                    {
                        "employee_id": "ceo-finance-investment-analyst",
                        "template_id": "finance-investment-analyst",
                        "name": "Investment Analyst",
                        "role_id": "ceo",
                        "description": "Investment research specialist.",
                        "category": "finance",
                    }
                ],
                "escalation_rules": [],
                "runtime_policies": {},
                "talent_templates": [{"id": "legacy-template", "name": "Legacy Template"}],
                "teams": [],
                "team_runtime": {},
                "installed_packages": [],
                "role_serial_queue_enabled": True,
            },
            default_flow_style=False,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    loaded = cfg_module.OPCConfig.load(config_dir, trusted_source=True)

    healed = yaml.safe_load(org_path.read_text(encoding="utf-8"))
    assert healed["employees"] == []
    assert healed["talent_templates"] == []
    registry_path = tmp_path / ".opc" / "company_state" / "corporate" / "employees" / "finance-investment-analyst.yaml"
    assert registry_path.exists()
    assert loaded.org.employees[0].employee_id == "finance-investment-analyst"
    assert loaded.org.employees[0].metadata["legacy_employee_ids"] == ["ceo-finance-investment-analyst"]


def test_export_yaml_roundtrips_roles_and_employees():
    """The export payload shape must preserve explicit org structure via YAML round-trip."""
    from opc.core.config import OPCConfig, build_company_org_payload_from_config
    from opc.layer2_organization.company_runtime_profiles import get_builtin_roles

    cfg = OPCConfig()
    corporate_data = build_company_org_payload_from_config(cfg)
    exported = yaml.dump(corporate_data, default_flow_style=False, sort_keys=False)
    parsed = yaml.safe_load(exported)

    assert parsed["schema_version"] == 2
    assert [role["id"] for role in parsed["roles"]] == [role.id for role in get_builtin_roles("corporate")]
    assert parsed["company"]["final_decider_role_id"] == "ceo"
    assert "corporate" in parsed["runtime_policies"]
    assert parsed["employees"] == []
    assert parsed["talent_templates"] == []
    assert parsed["escalation_rules"] == [e.model_dump() for e in cfg.org.escalation_rules]


def test_custom_org_payload_materializes_effective_policy_when_empty():
    from opc.core.config import OPCConfig, RoleConfig, build_company_org_payload_from_config

    cfg = OPCConfig()
    cfg.org.organization_id = "hkuds"
    cfg.org.organization_name = "HKUDS"
    cfg.org.company_name = "HKUDS"
    cfg.org.company_profile = "custom"
    cfg.org.final_decider_role_id = "chao"
    cfg.org.roles = [
        RoleConfig(id="chao", name="Chao", responsibility="leader", reports_to="owner"),
        RoleConfig(id="zongwei", name="Zongwei", responsibility="student", reports_to="chao"),
    ]
    cfg.org.runtime_policies = {}

    payload = build_company_org_payload_from_config(
        cfg,
        organization_id="hkuds",
        organization_name="HKUDS",
        force_profile="custom",
    )

    assert [role["id"] for role in payload["roles"]] == ["chao", "zongwei"]
    assert payload["company"]["final_decider_role_id"] == "chao"
    assert "custom" in payload["runtime_policies"]
    assert payload["runtime_policies"]["custom"]["gate_harness"]["decision_mode"] == "hybrid"
    assert payload["employees"] == []
    assert payload["talent_templates"] == []
