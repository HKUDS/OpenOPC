"""Serialize and apply Office UI organization architecture snapshots."""

from __future__ import annotations

from typing import Any

import yaml

from opc.core.config import COMPANY_ORG_KIND, OPCConfig, build_company_org_payload_from_config


ORG_ARCHITECTURE_SCHEMA_VERSION = 2
ORG_ARCHITECTURE_KIND = COMPANY_ORG_KIND

_STRUCTURE_KEYS = ("roles", "employees", "escalation_rules")
_RUNTIME_KEYS = (
    "runtime_policies",
    "talent_templates",
    "teams",
    "team_runtime",
    "external_team_bindings",
    "installed_packages",
    "role_serial_queue_enabled",
)


def _dump_item(item: Any) -> Any:
    if hasattr(item, "model_dump"):
        return item.model_dump()
    if isinstance(item, dict):
        return dict(item)
    return item


def _dump_list(items: list[Any]) -> list[Any]:
    return [_dump_item(item) for item in list(items or [])]


def _runtime_policy_payload(config: OPCConfig) -> dict[str, Any]:
    return {
        str(key): _dump_item(value)
        for key, value in dict(config.org.runtime_policies or {}).items()
    }


def build_active_company_payload(
    config: OPCConfig,
    *,
    force_profile: str | None = None,
) -> dict[str, Any]:
    """Build the legacy split company structure payload."""

    profile = force_profile if force_profile is not None else config.org.company_profile
    return {
        "schema_version": 1,
        "company": {
            "name": config.org.company_name,
            "topology": config.org.topology,
            "company_profile": profile,
            "final_decider_role_id": config.org.final_decider_role_id,
            "company_profiles": list(config.org.company_profiles),
        },
        "roles": [role.model_dump() for role in config.org.roles],
        "employees": [employee.model_dump() for employee in config.org.employees],
        "escalation_rules": [rule.model_dump() for rule in config.org.escalation_rules],
    }


def build_active_org_runtime_payload(config: OPCConfig) -> dict[str, Any]:
    """Build the .opc/config/org_config.yaml runtime-extra payload."""

    return {
        "talent_templates": [],
        "teams": [team.model_dump() for team in config.org.teams],
        "team_runtime": config.org.team_runtime.model_dump(),
        "external_team_bindings": [binding.model_dump() for binding in config.org.external_team_bindings],
        "installed_packages": _dump_list(config.org.installed_packages),
        "runtime_policies": _runtime_policy_payload(config),
        "role_serial_queue_enabled": bool(config.org.role_serial_queue_enabled),
    }


def build_org_architecture_snapshot(
    config: OPCConfig,
    *,
    force_profile: str | None = None,
) -> dict[str, Any]:
    """Build a complete saved/exported architecture snapshot."""
    snapshot = build_company_org_payload_from_config(config, force_profile=force_profile)
    snapshot["metadata"] = {**dict(snapshot.get("metadata", {}) or {}), "source": "office_ui"}
    return snapshot


def dump_org_architecture_snapshot(snapshot: dict[str, Any]) -> str:
    return yaml.dump(snapshot, default_flow_style=False, sort_keys=False, allow_unicode=True)


def parse_org_architecture_snapshot(raw_yaml: str) -> dict[str, Any]:
    parsed = yaml.safe_load(raw_yaml) or {}
    if not isinstance(parsed, dict):
        raise ValueError("Architecture YAML must contain a mapping.")

    schema_version = parsed.get("schema_version", 1)
    try:
        schema_version_int = int(schema_version)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid schema_version: {schema_version!r}") from exc
    if schema_version_int < 1:
        raise ValueError(f"Unsupported schema_version: {schema_version_int}")
    if schema_version_int > ORG_ARCHITECTURE_SCHEMA_VERSION:
        raise ValueError(f"Unsupported schema_version: {schema_version_int}")

    kind = str(parsed.get("kind", "") or "").strip()
    if schema_version_int >= 2 and kind and kind != ORG_ARCHITECTURE_KIND:
        raise ValueError(f"Unsupported architecture kind: {kind}")

    parsed["schema_version"] = schema_version_int
    return parsed


def apply_org_architecture_snapshot(
    existing_config: OPCConfig,
    snapshot: dict[str, Any],
) -> OPCConfig:
    """Return a validated config with a v1/v2 architecture snapshot applied.

    v1 snapshots preserve runtime extras that are not present in the file.
    v2 snapshots are complete architecture replacements, so missing runtime
    extras fall back to model defaults rather than stale in-memory state.
    """

    schema_version = int(snapshot.get("schema_version", 1))
    existing_dict = existing_config.model_dump()
    merged: dict[str, Any] = dict(existing_dict)
    merged_org: dict[str, Any] = (
        {} if schema_version >= ORG_ARCHITECTURE_SCHEMA_VERSION
        else dict(merged.get("org", {}) or {})
    )

    company = snapshot.get("company")
    if isinstance(company, dict):
        if "name" in company:
            merged_org["company_name"] = company.get("name")
        if "topology" in company:
            merged_org["topology"] = company.get("topology")
        if "company_profile" in company:
            merged_org["company_profile"] = company.get("company_profile")
        if "final_decider_role_id" in company:
            merged_org["final_decider_role_id"] = company.get("final_decider_role_id")
        if "company_profiles" in company:
            merged_org["company_profiles"] = company.get("company_profiles")
    if "organization_id" in snapshot:
        merged_org["organization_id"] = snapshot.get("organization_id")
    if "organization_name" in snapshot:
        merged_org["organization_name"] = snapshot.get("organization_name")

    for key in _STRUCTURE_KEYS:
        if key in snapshot:
            merged_org[key] = snapshot.get(key) or []
        elif schema_version >= ORG_ARCHITECTURE_SCHEMA_VERSION:
            merged_org[key] = []

    if schema_version >= ORG_ARCHITECTURE_SCHEMA_VERSION:
        merged_org["runtime_policies"] = snapshot.get("runtime_policies") or {}
        merged_org["talent_templates"] = []
        merged_org["teams"] = snapshot.get("teams") or []
        merged_org["team_runtime"] = snapshot.get("team_runtime") or {}
        merged_org["external_team_bindings"] = snapshot.get("external_team_bindings") or []
        merged_org["installed_packages"] = snapshot.get("installed_packages") or []
        merged_org["role_serial_queue_enabled"] = bool(
            snapshot.get("role_serial_queue_enabled", True),
        )
    else:
        for key in _RUNTIME_KEYS:
            if key == "talent_templates":
                merged_org[key] = []
                continue
            if key in snapshot:
                if key == "role_serial_queue_enabled":
                    merged_org[key] = bool(snapshot[key])
                else:
                    merged_org[key] = snapshot.get(key) or ({} if key in {"runtime_policies", "team_runtime"} else [])

    merged["org"] = merged_org
    return OPCConfig.model_validate(merged)
