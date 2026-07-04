from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
ORG_DIR = REPO_ROOT / ".opc" / "config" / "company_orgs"
EXPECTED_ROLE_COUNTS = {
    "corporate": 11,
    "game-development-studio": 5,
    "research-report-studio": 3,
    "vc-investment-firm": 21,
    "roboforce-titan": 11,
}
OPTIONAL_LOCAL_ROLE_COUNTS = {
    "hkuds": 2,
}


def _load_org_payloads() -> dict[str, dict]:
    payloads: dict[str, dict] = {}
    for path in sorted(ORG_DIR.glob("org_*_config.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        payloads[str(data.get("organization_id") or "")] = data
    return payloads


def _org_graph_from_agents(agents) -> dict[str, list[str]]:
    role_ids = {agent.role_id for agent in agents}
    graph: dict[str, list[str]] = {}
    for agent in agents:
        if agent.reports_to in role_ids:
            graph.setdefault(agent.reports_to, []).append(agent.role_id)
    return {key: sorted(value) for key, value in sorted(graph.items())}


def test_company_org_yaml_files_share_aligned_structure() -> None:
    payloads = _load_org_payloads()
    expected_counts = dict(EXPECTED_ROLE_COUNTS)
    expected_counts.update({key: value for key, value in OPTIONAL_LOCAL_ROLE_COUNTS.items() if key in payloads})

    assert expected_counts.keys() <= payloads.keys()
    first_keys: list[str] | None = None
    for org_id, expected_count in expected_counts.items():
        payload = payloads[org_id]
        keys = list(payload.keys())
        if first_keys is None:
            first_keys = keys
        assert keys == first_keys
        assert len(payload["roles"]) == expected_count
        assert payload["employees"] == []
        assert payload["talent_templates"] == []
        assert payload["company"]["final_decider_role_id"]
        policy_key = "corporate" if org_id == "corporate" else "custom"
        assert policy_key in payload["runtime_policies"]


def test_corporate_org_config_materializes_builtin_runtime() -> None:
    from opc.core.config import OPCConfig
    from opc.layer2_organization.org_engine import OrgEngine

    cfg = OPCConfig.load(REPO_ROOT / ".opc" / "config")
    org = OrgEngine(cfg, REPO_ROOT / ".opc")
    agents = org.list_agents()

    assert len(agents) == 11
    assert org.get_final_decider_role_id(strict=True) == "ceo"
    assert _org_graph_from_agents(agents) == {
        "ceo": ["cmo", "coo", "cto"],
        "cmo": ["content_specialist", "designer"],
        "coo": ["acquisition_specialist", "qa_analyst"],
        "cto": ["devops_engineer", "env_engineer", "senior_engineer"],
    }


def test_custom_org_configs_are_runnable_when_present() -> None:
    from opc.core.config import OPCConfig
    from opc.core.org_config import apply_org_config_payload_to_config, validate_runnable_org_config
    from opc.layer2_organization.org_engine import OrgEngine

    payloads = _load_org_payloads()
    expected_counts = dict(EXPECTED_ROLE_COUNTS)
    expected_counts.update({key: value for key, value in OPTIONAL_LOCAL_ROLE_COUNTS.items() if key in payloads})
    expected_counts.pop("corporate", None)

    for org_id, expected_count in expected_counts.items():
        payload = payloads[org_id]
        path = ORG_DIR / f"org_{org_id}_config.yaml"
        cfg = apply_org_config_payload_to_config(OPCConfig(), payload, source_path=path)
        validate_runnable_org_config(cfg, organization_id=org_id)
        org = OrgEngine(cfg, REPO_ROOT / ".opc")
        assert len(org.list_agents()) == expected_count
        assert org.validate_company_runtime_setup() is None
        assert org.get_final_decider_role_id(strict=True) == payload["company"]["final_decider_role_id"]


def test_company_mode_catalogs_remain_external_to_org_yaml() -> None:
    from opc.core.config import OPCConfig
    from opc.layer2_organization.talent_market import TalentMarket

    cfg = OPCConfig.load(REPO_ROOT / ".opc" / "config")
    assert cfg.org.talent_templates == []
    market = TalentMarket(REPO_ROOT / ".opc", cfg)
    assert market.get_template("finance-finance-investment-researcher") is not None
