"""Canonical execution identity for Office UI sessions.

The UI groups saved organizations under the Company picker, but persisted
session identity still needs to distinguish the built-in corporate company
from a custom organization.  This module is the single normalization boundary
for that identity:

* task sessions clear company/org identity.
* company sessions always mean the built-in corporate company.
* custom organization sessions are stored as exec_mode=org,
  company_profile=custom, plus an org_id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from opc.core.config import validate_organization_id
from opc.layer2_organization.company_runtime_identity import is_company_runtime_task

PREFERRED_AGENTS: frozenset[str] = frozenset({
    "native",
    "codex",
    "claude_code",
    "cursor",
    "opencode",
})


@dataclass(frozen=True)
class ExecutionIdentity:
    exec_mode: str
    company_profile: str = "corporate"
    org_id: str = ""
    preferred_agent: str = "native"

    @property
    def is_task(self) -> bool:
        return self.exec_mode == "task"

    @property
    def is_company(self) -> bool:
        return self.exec_mode == "company"

    @property
    def is_custom_org(self) -> bool:
        return self.exec_mode == "org"

    @property
    def is_company_runtime(self) -> bool:
        return self.exec_mode in {"company", "org"}


def normalize_exec_mode(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"project", "single", "task"}:
        return "task"
    if normalized == "company":
        return "company"
    if normalized in {"org", "custom"}:
        return "org"
    return "task"


def normalize_company_profile(value: Any, *, default: str = "corporate") -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "classic":
        return "corporate"
    if normalized in {"corporate", "custom"}:
        return normalized

    fallback = str(default or "").strip().lower()
    if fallback == "classic":
        return "corporate"
    return fallback if fallback in {"corporate", "custom"} else "corporate"


def normalize_preferred_agent(value: Any, *, default: str = "native") -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in PREFERRED_AGENTS:
        return normalized
    fallback = str(default or "").strip().lower().replace("-", "_")
    return fallback if fallback in PREFERRED_AGENTS else "native"


def normalize_org_id(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return validate_organization_id(raw)
    except ValueError:
        return ""


def canonicalize_execution_identity(
    *,
    exec_mode: Any = None,
    company_profile: Any = None,
    org_id: Any = None,
    preferred_agent: Any = None,
    default_exec_mode: str = "task",
    default_company_profile: str = "corporate",
    default_preferred_agent: str = "native",
    explicit_exec_mode: bool | None = None,
) -> ExecutionIdentity:
    raw_exec = str(exec_mode or "").strip()
    has_explicit_mode = bool(raw_exec) if explicit_exec_mode is None else bool(explicit_exec_mode)
    requested_mode = normalize_exec_mode(raw_exec if has_explicit_mode else default_exec_mode)
    requested_profile = normalize_company_profile(company_profile, default=default_company_profile)
    requested_org_id = normalize_org_id(org_id)
    requested_agent = normalize_preferred_agent(preferred_agent, default=default_preferred_agent)

    if not has_explicit_mode and (requested_profile == "custom" or requested_org_id):
        requested_mode = "org"

    if requested_mode == "org":
        return ExecutionIdentity(
            exec_mode="org",
            company_profile="custom",
            org_id=requested_org_id,
            preferred_agent=requested_agent,
        )
    if requested_mode == "company":
        return ExecutionIdentity(
            exec_mode="company",
            company_profile="corporate",
            org_id="",
            preferred_agent=requested_agent,
        )
    return ExecutionIdentity(
        exec_mode="task",
        company_profile="corporate",
        org_id="",
        preferred_agent=requested_agent,
    )


def task_metadata(task: Any | None) -> dict[str, Any]:
    return dict(getattr(task, "metadata", {}) or {}) if task is not None else {}


def execution_identity_from_task(
    task: Any | None,
    *,
    default_exec_mode: str = "task",
    default_company_profile: str = "corporate",
    default_preferred_agent: str = "native",
    default_org_id: Any = "",
) -> ExecutionIdentity:
    metadata = task_metadata(task)
    raw_exec_mode = str(metadata.get("exec_mode", "") or "").strip()
    execution_mode = str(metadata.get("execution_mode", "") or "").strip().lower()
    company_profile = metadata.get("company_profile")
    metadata_profile = str(company_profile or "").strip().lower()
    metadata_org_id = (
        getattr(task, "org_id", None)
        or metadata.get("org_id")
        or metadata.get("organization_id")
        or ""
    )
    mode_hint = str(metadata.get("mode", "") or "").strip().lower()

    if raw_exec_mode:
        exec_mode = raw_exec_mode
        explicit = True
    elif metadata_profile == "custom":
        exec_mode = "org"
        explicit = True
    elif execution_mode == "company_mode" or metadata_profile:
        exec_mode = "company"
        explicit = True
    elif (
        mode_hint in {"company", "org", "custom"}
        or is_company_runtime_task(task)
    ):
        # Older company/runtime rows may only retain a mode marker or the
        # runtime marker itself.  Treat those rows as explicit company
        # identity so they cannot fall through to task-mode/global defaults.
        exec_mode = "org" if metadata_org_id else "company"
        explicit = True
    elif metadata_org_id:
        exec_mode = "org"
        explicit = True
    else:
        exec_mode = default_exec_mode
        explicit = False
    resolved_org_id = metadata_org_id if explicit else default_org_id

    return canonicalize_execution_identity(
        exec_mode=exec_mode,
        company_profile=company_profile if company_profile not in (None, "", [], {}) else default_company_profile,
        org_id=resolved_org_id,
        preferred_agent=metadata.get("preferred_agent"),
        default_exec_mode=default_exec_mode,
        default_company_profile=default_company_profile,
        default_preferred_agent=default_preferred_agent,
        explicit_exec_mode=explicit,
    )


def resolve_delivery_task_org_identity(
    task: Any | None,
    *,
    payload: Mapping[str, Any] | None = None,
    active_org_id: Any = "",
    default_org_id: Any = "",
) -> tuple[str, str]:
    """Validate the org identity of a delivery self-evolution task.

    Returns ``(organization_id, error)`` with at most one non-empty. Prefers
    ``Task.org_id``, then task metadata org fields; conflicting sources are
    rejected. Checkpoint-payload org fields are a last-resort legacy fallback
    and never override task/metadata identity. The active configuration org
    is only consulted for a confirmed corporate task; custom-org deliveries
    without a durable org id fail closed.
    """
    metadata = task_metadata(task)
    candidates: list[str] = []
    for value in (
        getattr(task, "org_id", None),
        metadata.get("org_id"),
        metadata.get("organization_id"),
    ):
        normalized = normalize_org_id(value)
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    if len(candidates) > 1:
        return "", "the delivery task org identity conflicts across task and metadata sources"
    task_org_id = candidates[0] if candidates else ""
    if task_org_id:
        return task_org_id, ""
    payload_org_id = normalize_org_id(
        (payload or {}).get("org_id")
        or (payload or {}).get("organization_id")
    )
    if payload_org_id:
        return payload_org_id, ""
    identity = execution_identity_from_task(task)
    if identity.is_company:
        return normalize_org_id(active_org_id) or normalize_org_id(default_org_id), ""
    return "", "the custom-organization delivery task has no durable org identity"
