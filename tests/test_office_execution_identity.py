from __future__ import annotations

from types import SimpleNamespace

from opc.plugins.office_ui.execution_identity import (
    canonicalize_execution_identity,
    execution_identity_from_task,
)


def test_company_identity_clears_stale_custom_org_fields() -> None:
    identity = canonicalize_execution_identity(
        exec_mode="company",
        company_profile="custom",
        org_id="quantum_harbor",
        preferred_agent="codex",
        explicit_exec_mode=True,
    )

    assert identity.exec_mode == "company"
    assert identity.company_profile == "corporate"
    assert identity.org_id == ""
    assert identity.preferred_agent == "codex"


def test_legacy_custom_profile_becomes_custom_org_identity() -> None:
    identity = canonicalize_execution_identity(
        company_profile="custom",
        org_id="quantum_harbor",
        explicit_exec_mode=False,
    )

    assert identity.exec_mode == "org"
    assert identity.company_profile == "custom"
    assert identity.org_id == "quantum_harbor"


def test_custom_exec_mode_is_canonical_org_identity() -> None:
    identity = canonicalize_execution_identity(
        exec_mode="custom",
        company_profile="corporate",
        org_id="quantum_harbor",
        explicit_exec_mode=True,
    )

    assert identity.exec_mode == "org"
    assert identity.company_profile == "custom"
    assert identity.org_id == "quantum_harbor"


def test_task_identity_clears_company_and_org_fields() -> None:
    identity = canonicalize_execution_identity(
        exec_mode="task",
        company_profile="custom",
        org_id="quantum_harbor",
        preferred_agent="claude-code",
        explicit_exec_mode=True,
    )

    assert identity.exec_mode == "task"
    assert identity.company_profile == "corporate"
    assert identity.org_id == ""
    assert identity.preferred_agent == "claude_code"


def test_task_metadata_identity_prefers_explicit_company_over_stale_custom_profile() -> None:
    task = SimpleNamespace(
        metadata={
            "exec_mode": "company",
            "company_profile": "custom",
            "org_id": "quantum_harbor",
            "preferred_agent": "codex",
        },
        org_id="quantum_harbor",
    )

    identity = execution_identity_from_task(task)

    assert identity.exec_mode == "company"
    assert identity.company_profile == "corporate"
    assert identity.org_id == ""
    assert identity.preferred_agent == "codex"


def test_task_metadata_identity_keeps_custom_org_id() -> None:
    task = SimpleNamespace(
        metadata={
            "exec_mode": "org",
            "company_profile": "custom",
            "organization_id": "quantum_harbor",
        },
        org_id=None,
    )

    identity = execution_identity_from_task(task)

    assert identity.exec_mode == "org"
    assert identity.company_profile == "custom"
    assert identity.org_id == "quantum_harbor"


def test_task_org_id_field_is_org_identity_fallback() -> None:
    task = SimpleNamespace(
        metadata={},
        org_id="quantum_harbor",
    )

    identity = execution_identity_from_task(task)

    assert identity.exec_mode == "org"
    assert identity.company_profile == "custom"
    assert identity.org_id == "quantum_harbor"


def test_durable_task_org_id_wins_over_stale_metadata_org_id() -> None:
    task = SimpleNamespace(
        metadata={
            "exec_mode": "org",
            "company_profile": "custom",
            "organization_id": "active-org",
        },
        org_id="selected-org",
    )

    identity = execution_identity_from_task(task)

    assert identity.org_id == "selected-org"


def test_default_org_id_applies_only_when_task_has_no_persisted_identity() -> None:
    task = SimpleNamespace(metadata={}, org_id=None)

    identity = execution_identity_from_task(
        task,
        default_exec_mode="org",
        default_company_profile="custom",
        default_org_id="quantum_harbor",
    )

    assert identity.exec_mode == "org"
    assert identity.company_profile == "custom"
    assert identity.org_id == "quantum_harbor"


def test_explicit_company_identity_ignores_default_org_id() -> None:
    task = SimpleNamespace(
        metadata={
            "exec_mode": "company",
            "company_profile": "corporate",
        },
        org_id=None,
    )

    identity = execution_identity_from_task(
        task,
        default_exec_mode="org",
        default_company_profile="custom",
        default_org_id="quantum_harbor",
    )

    assert identity.exec_mode == "company"
    assert identity.company_profile == "corporate"
    assert identity.org_id == ""


def test_company_mode_marker_without_profile_is_company_identity() -> None:
    task = SimpleNamespace(
        metadata={"mode": "company"},
        org_id=None,
    )

    identity = execution_identity_from_task(task)

    assert identity.exec_mode == "company"
    assert identity.company_profile == "corporate"
    assert identity.org_id == ""


def test_work_item_runtime_marker_without_profile_is_company_identity() -> None:
    task = SimpleNamespace(
        metadata={"work_item_runtime": True},
        org_id=None,
    )

    identity = execution_identity_from_task(task)

    assert identity.exec_mode == "company"
    assert identity.company_profile == "corporate"
    assert identity.org_id == ""


def test_company_runtime_marker_with_custom_profile_is_org_identity() -> None:
    task = SimpleNamespace(
        metadata={"mode": "company", "work_item_runtime": True, "company_profile": "custom"},
        org_id=None,
    )

    identity = execution_identity_from_task(task)

    assert identity.exec_mode == "org"
    assert identity.company_profile == "custom"
    assert identity.org_id == ""
