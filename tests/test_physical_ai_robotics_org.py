"""Physical AI / robotics domain pack: preset, saved org, and talent templates.

Covers the industry-generalized ``physical-ai-robotics-company`` architecture
preset, the RoboForce-Titan saved org instance generated from it, and the
robotics talent templates. See docs/physical-ai-founding-roles.md.
"""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
ROBOFORCE_ORG_CONFIG = (
    REPO_ROOT / ".opc" / "config" / "company_orgs" / "org_roboforce-titan_config.yaml"
)

EXPECTED_ROLE_IDS = [
    "founding_ai_native_lead",
    "robot_learning_lead",
    "foundation_model_scientist",
    "data_engine_engineer",
    "sim2real_engineer",
    "robotics_software_lead",
    "perception_engineer",
    "manipulation_engineer",
    "motion_planning_engineer",
    "deployment_field_engineer",
    "ai_infra_reliability_engineer",
]


def test_physical_ai_preset_loads_and_infers_final_decider() -> None:
    from opc.market.architecture_registry import get_preset

    preset = get_preset("physical-ai-robotics-company")
    assert preset is not None
    assert preset.category == "robotics"
    assert [r["id"] for r in preset.roles] == EXPECTED_ROLE_IDS
    # Only the founding lead reports to the owner -> it is the sole top-level role.
    top_level = [r["id"] for r in preset.roles if r.get("reports_to") == "owner"]
    assert top_level == ["founding_ai_native_lead"]
    # Every prompt_ref must be a plain string (guards the YAML colon gotcha).
    for role in preset.roles:
        for ref in role.get("prompt_refs", []) or []:
            assert isinstance(ref, str)
    # Work-item DAG closes the flywheel back into a learning-feedback step.
    template_ids = {t["id"] for t in preset.work_item_templates}
    assert {"teleop_data_plan", "policy_training", "sim2real_eval", "field_pilot",
            "learning_feedback", "capability_delivery"} <= template_ids
    # Every role mounts the physical_ai domain skill + the operating-loop doctrine.
    for role in preset.roles:
        refs = role.get("skill_refs") or []
        assert "physical_ai" in refs, role["id"]
        assert "physical_ai_operating_loop" in refs, role["id"]


def test_operating_loop_doctrine_skill_and_gates_present() -> None:
    from opc.market.architecture_registry import get_preset

    # The doctrine ships as a mountable role skill.
    skill = REPO_ROOT / "skills" / "core" / "physical_ai_operating_loop.md"
    body = skill.read_text(encoding="utf-8")
    for anchor in ("maker", "release ladder", "Human Burden", "safety is a gate"):
        assert anchor.lower() in body.lower(), anchor

    preset = get_preset("physical-ai-robotics-company")
    roles = {r["id"]: r for r in preset.roles}
    # The independent-referee role carries the maker!=checker + safety-terminal doctrine.
    referee = " ".join(roles["ai_infra_reliability_engineer"].get("prompt_refs") or [])
    assert "independent referee" in referee.lower()
    assert "terminal gate" in referee.lower()
    # The final decider carries the release-ladder promotion rule.
    lead = " ".join(roles["founding_ai_native_lead"].get("prompt_refs") or [])
    assert "release ladder" in lead.lower()


def test_physical_ai_preset_applies_to_config() -> None:
    from opc.core.config import OPCConfig
    from opc.market.architecture_registry import apply_architecture_preset_to_config

    config = OPCConfig()
    apply_architecture_preset_to_config(
        config,
        "physical-ai-robotics-company",
        strategy="overwrite",
        organization_id="roboforce-titan",
        organization_name="RoboForce Titan Robo-Labor",
    )
    assert [role.id for role in config.org.roles] == EXPECTED_ROLE_IDS
    assert config.org.final_decider_role_id == "founding_ai_native_lead"


def test_roboforce_titan_org_config_is_importable() -> None:
    from opc.core.config import OPCConfig
    from opc.core.org_config import (
        apply_org_config_payload_to_config,
        validate_org_config_payload,
    )

    raw = yaml.safe_load(ROBOFORCE_ORG_CONFIG.read_text(encoding="utf-8"))
    payload = validate_org_config_payload(ROBOFORCE_ORG_CONFIG, raw)
    config = apply_org_config_payload_to_config(
        OPCConfig(), payload, source_path=ROBOFORCE_ORG_CONFIG
    )

    assert config.org.organization_id == "roboforce-titan"
    assert config.org.organization_name == "RoboForce Titan Robo-Labor"
    assert config.org.company_profile == "custom"
    assert config.org.final_decider_role_id == "founding_ai_native_lead"
    assert [role.id for role in config.org.roles] == EXPECTED_ROLE_IDS
    assert config.org.employees == []


def test_robotics_talent_templates_present_and_unique() -> None:
    from opc.market.talent_presets import get_all_talent_presets, get_talent_preset

    templates = get_all_talent_presets()
    ids = [t["id"] for t in templates]
    assert len(ids) == len(set(ids)), "duplicate talent template ids"

    robotics = [t for t in templates if t["category"] == "robotics"]
    assert len(robotics) == 10
    anchor = get_talent_preset("founding-ai-native-lead")
    assert anchor is not None
    assert anchor["category"] == "robotics"
