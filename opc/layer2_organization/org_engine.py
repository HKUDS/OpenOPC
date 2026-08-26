"""Organization engine — manages role-based agent orchestration."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from loguru import logger

from opc.core.company_tools import COMPANY_APPROVAL_EXEMPT_TOOL_NAMES
from opc.core.config import (
    EmployeeConfig,
    OPCConfig,
    RoleConfig,
)
from opc.core.models import (
    AgentInfo,
    AgentStatus,
    OrgAgent,
    Organization,
    OrgSnapshot,
    ReorgChangeSet,
)
from opc.layer2_organization.collaboration_policy import (
    collect_dynamic_contact_roles,
)
from opc.layer2_organization.company_runtime_profiles import (
    get_builtin_roles,
    get_builtin_runtime_policies,
    get_company_profile_descriptions,
)
from opc.layer2_organization.talent_market import resolve_prompt_refs
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    build_company_work_item_runtime_plan,
)
from opc.layer2_organization.external_team_compiler import (
    apply_external_team_bindings_to_plan,
)
from opc.layer5_memory.employee_evolution import EmployeeEvolutionManager

_DEFAULT_EMPLOYEE_TEMPLATE_ID = "general-default-employee"
_DEFAULT_EMPLOYEE_PROMPT_REF = "prompts/talent/general-default-employee.md"
_FALLBACK_EMPLOYEE_TEMPLATE_ID = "fallback-empty-employee"
TASK_MODE_GENERAL_ROLE_ID = "task_generalist"
TASK_MODE_COMPANY_ONLY_TOOLS = frozenset(COMPANY_APPROVAL_EXEMPT_TOOL_NAMES)
_DEFAULT_TASK_MODE_TOOLS = [
    "request_user_input",
    "shell_exec",
    "file_read",
    "file_write",
    "file_edit",
    "apply_patch",
    "file_search",
    "grep",
    "glob",
    "list_dir",
    "web_search",
    "web_fetch",
    "browser_navigate",
    "browser_navigate_back",
    "browser_click",
    "browser_snapshot",
    "browser_type",
    "browser_wait_for",
    "browser_scroll",
    "browser_select_option",
    "browser_take_screenshot",
    "browser_close",
    "git_status",
    "git_commit",
    "git_diff",
    "python_exec",
    "todo_write",
    "todo_read",
    "agent_spawn",
    "agent_wait",
    "agent_send",
    "agent_list",
]


class OrgEngine:
    """Manages the organizational structure and runtime role policies."""

    def __init__(
        self,
        config: OPCConfig,
        opc_home: Path | None = None,
        store: Any | None = None,
    ) -> None:
        self.config = config
        self.opc_home = opc_home
        self.store = store
        try:
            from opc.core.employee_registry import normalize_employee_records

            self.config.org.employees, _ = normalize_employee_records(list(self.config.org.employees))
        except Exception:
            pass
        self.employee_evolution = EmployeeEvolutionManager(opc_home) if opc_home else None
        self._agents: dict[str, AgentInfo] = {}
        self._task_mode_tool_names: list[str] = list(_DEFAULT_TASK_MODE_TOOLS)
        self._task_mode_role: AgentInfo | None = None
        self._org_version = 1
        self._runtime_topology_version = 1
        self._org_history: dict[int, dict[str, Any]] = {}
        self._initialize_roles()

    def _initialize_roles(self) -> None:
        self._agents = {}
        roles = self._effective_roles()

        for role in roles:
            tools = list(role.tools)
            self._agents[role.id] = AgentInfo(
                role_id=role.id,
                name=role.name,
                responsibility=role.responsibility,
                status=AgentStatus.IDLE,
                reports_to=role.reports_to,
                icon=role.icon,
                can_spawn=list(role.can_spawn),
                tools=tools,
                preferred_external_agent=role.preferred_external_agent,
                prompt_refs=self._resolve_prompt_refs(role.prompt_refs),
                skill_refs=list(role.skill_refs),
                handoff_template_ref=role.handoff_template_ref,
                memory_policy_ref=role.memory_policy_ref,
                artifact_contract_ref=role.artifact_contract_ref,
                runtime_policy=role.runtime_policy.model_dump(),
                capabilities=", ".join(
                    str(item).strip()
                    for item in list(role.capabilities or [])
                    if str(item).strip()
                ),
            )
        self._refresh_task_mode_role()
        logger.info(f"OrgEngine initialized with {len(self._agents)} roles")
        self._org_history[self._org_version] = {
            "roles": [role.model_dump() for role in roles],
            "company_profile": self.config.org.company_profile,
            "runtime_topology_version": self._runtime_topology_version,
        }

    def _effective_roles(self) -> list[RoleConfig]:
        profile = self.config.org.company_profile

        # Corporate: use builtin topology, but allow org_config role
        # definitions to override explicit fields like tools/runtime_policy.
        if profile == "corporate":
            return get_builtin_roles(profile, configured_roles=self.config.org.roles)

        # Task mode (profile="" or None): no org roles
        if not profile or profile not in {"custom"}:
            return []

        # Custom mode: return config.org.roles as-is
        return list(self.config.org.roles)

    # ── Task-mode dedicated role (independent of _effective_roles) ────

    @staticmethod
    def _normalize_task_mode_tools(tool_names: list[str] | None) -> list[str]:
        names = tool_names or list(_DEFAULT_TASK_MODE_TOOLS)
        normalized: list[str] = []
        seen: set[str] = set()
        for raw in names:
            name = str(raw or "").strip()
            if not name or name in TASK_MODE_COMPANY_ONLY_TOOLS or name in seen:
                continue
            normalized.append(name)
            seen.add(name)
        if "request_user_input" not in seen:
            normalized.insert(0, "request_user_input")
        return normalized

    def _refresh_task_mode_role(self) -> None:
        self._task_mode_tool_names = self._normalize_task_mode_tools(self._task_mode_tool_names)
        self._task_mode_role = AgentInfo(
            role_id=TASK_MODE_GENERAL_ROLE_ID,
            name="Task Generalist",
            responsibility=(
                "Primary session agent for task mode. Owns the full non-company toolset "
                "and solves standalone requests directly."
            ),
            status=AgentStatus.IDLE,
            reports_to="owner",
            can_spawn=[],
            tools=list(self._task_mode_tool_names),
            preferred_external_agent=None,
            prompt_refs=[],
            skill_refs=[],
            handoff_template_ref=None,
            memory_policy_ref=None,
            artifact_contract_ref=None,
            runtime_policy={
                "execution_strategy": "native",
                "allowed_downstream_roles": [],
                "review_role": None,
                "default_turn_type": "work",
            },
        )

    def configure_task_mode_tools(self, tool_names: list[str] | None) -> None:
        self._task_mode_tool_names = self._normalize_task_mode_tools(tool_names)
        self._refresh_task_mode_role()

    def get_task_mode_role(self) -> AgentInfo:
        if self._task_mode_role is None:
            self._refresh_task_mode_role()
        assert self._task_mode_role is not None
        return self._task_mode_role

    @staticmethod
    def _looks_like_path(ref: str) -> bool:
        """Heuristic: treat ref as a file path only if it looks like one."""
        if len(ref) > 260:          # too long for any filesystem
            return False
        if " " in ref and "/" not in ref and "\\" not in ref:
            return False            # contains spaces but no path separators → literal text
        if ref.startswith(("You ", "Act ", "Focus ", "Review ", "Write ")):
            return False            # common prompt sentence starters
        return True

    def _resolve_prompt_refs(self, refs: list[str]) -> list[str]:
        resolved: list[str] = []
        for ref in refs:
            if self.opc_home and self._looks_like_path(ref):
                try:
                    path = Path(ref)
                    if not path.is_absolute():
                        path = self.opc_home / path
                    if path.exists() and path.is_file():
                        resolved.append(path.read_text(encoding="utf-8").strip())
                        continue
                except OSError:
                    pass  # path too long or other OS error — treat as literal
            resolved.append(ref)
        return resolved

    def get_agent(self, role_id: str) -> AgentInfo | None:
        if role_id == TASK_MODE_GENERAL_ROLE_ID:
            return self.get_task_mode_role()
        return self._agents.get(role_id)

    def get_executor(self) -> AgentInfo:
        return self._agents.get("executor", list(self._agents.values())[0])

    def get_coordinator(self) -> AgentInfo:
        return self._agents.get("coordinator", list(self._agents.values())[0])

    def list_agents(self) -> list[AgentInfo]:
        return list(self._agents.values())

    def get_company_profile(self) -> str:
        return self.config.org.company_profile

    def get_execution_model(self) -> str:
        # ``execution_model`` is the organization-facing runtime contract.  It
        # is deliberately distinct from the internal company scheduler model
        # (currently ``multi_team_org``), so callers must observe the value
        # loaded from the active organization config instead of an internal
        # implementation label.
        return (
            str(getattr(self.config.org, "execution_model", "") or "").strip()
            or "actor_runtime"
        )

    def get_top_level_role_ids(self) -> list[str]:
        agent_ids = set(self._agents)
        top_level_role_ids = [
            role_id
            for role_id, agent in self._agents.items()
            if agent.reports_to == "owner" or agent.reports_to not in agent_ids
        ]
        return sorted(dict.fromkeys(top_level_role_ids))

    def get_final_decider_role_id(self, *, strict: bool = False) -> str | None:
        top_level_role_ids = self.get_top_level_role_ids()
        if not top_level_role_ids:
            return None
        configured = str(getattr(self.config.org, "final_decider_role_id", "") or "").strip()
        if configured:
            if configured in top_level_role_ids:
                return configured
            if strict:
                raise ValueError(
                    f"Configured final_decider_role_id '{configured}' is not a top-level role."
                )
        if len(top_level_role_ids) == 1:
            return top_level_role_ids[0]
        if strict:
            raise ValueError(
                "Multiple top-level roles exist; final_decider_role_id must be explicitly configured."
            )
        return configured or None

    def validate_company_runtime_setup(self) -> str | None:
        if not self._agents:
            return "Company mode requires at least one role."
        try:
            self.get_final_decider_role_id(strict=True)
        except ValueError as exc:
            return str(exc)
        return None

    def get_company_profile_descriptions(self) -> dict[str, str]:
        descriptions = get_company_profile_descriptions()
        enabled = set(self.config.org.company_profiles)
        descriptions.setdefault("custom", "A user-defined organization runtime driven by work items, role queues, teams, and seats.")
        return {name: descriptions.get(name, "") for name in enabled | {"custom"}}

    def get_runtime_policy(self, profile: str) -> dict[str, Any]:
        builtin = get_builtin_runtime_policies().get(profile)
        if profile == "custom" and builtin is not None:
            # Enhance the generic custom defaults with role-aware inference
            builtin = self._enhance_custom_policy(builtin)
        merged = builtin.model_dump() if builtin else {}
        custom = self.config.org.runtime_policies.get(profile)
        if custom:
            merged = self._deep_merge(merged, custom.model_dump())
        return merged

    def _enhance_custom_policy(self, base: Any) -> Any:
        """Refine the default custom policy using the current role structure."""
        from opc.core.config import (
            CommunicationPolicyConfig,
            GateHarnessPolicyConfig,
            HandoffPolicyConfig,
            ReviewPolicyConfig,
            RuntimePolicyConfig,
        )

        has_reviewer = any(
            a.runtime_policy.get("review_role")
            for a in self._agents.values()
        )
        depth = self._max_hierarchy_depth()
        has_external = any(
            a.preferred_external_agent
            for a in self._agents.values()
        )

        # Only override sub-policies that benefit from role-aware inference;
        # leave the rest at their Pydantic-model defaults so user overrides
        # in org_config.runtime_policies are not masked.
        enhanced = base.model_copy() if hasattr(base, "model_copy") else RuntimePolicyConfig()
        enhanced.communication = CommunicationPolicyConfig(
            default_mode="broadcast" if depth > 3 else "dm",
            blocking_default=has_reviewer,
            allow_broadcast=True,
        )
        enhanced.handoff = HandoffPolicyConfig(
            require_structured_handoff=depth > 1,
            require_ack=has_reviewer,
            include_risks=True,
            include_open_questions=True,
        )
        enhanced.review = ReviewPolicyConfig(
            strict_gate_inference=has_reviewer,
            require_reviewer_role=has_reviewer,
            allow_human_override=True,
        )
        enhanced.gate_harness = GateHarnessPolicyConfig(
            decision_mode="agent_first" if has_external else "hybrid",
            default_degrade_policy="allow",
            auto_infer_turn_kind=True,
            auto_infer_gate_profile=True,
            allow_pass_with_constraints=True,
        )
        return enhanced

    def _max_hierarchy_depth(self) -> int:
        """Return the longest reports_to chain length in the role hierarchy."""
        agent_ids = set(self._agents)

        def _depth(role_id: str, visited: set[str]) -> int:
            children = [
                rid for rid, a in self._agents.items()
                if a.reports_to == role_id and rid not in visited
            ]
            if not children:
                return 0
            return 1 + max(
                _depth(c, visited | {c}) for c in children
            )

        roots = [
            rid for rid, a in self._agents.items()
            if a.reports_to == "owner" or a.reports_to not in agent_ids
        ]
        if not roots:
            return 1
        return max(_depth(r, {r}) for r in roots)

    def build_company_work_item_runtime_plan(
        self,
        profile: str | None = None,
        *,
        runtime_topology: dict[str, Any] | None = None,
        original_request: str = "",
        compiled_external_teams: list[Any] | None = None,
    ) -> CompanyWorkItemRuntimePlan:
        """Build the company execution plan from org topology and work items."""

        normalized_profile = str(profile or self.config.org.company_profile or "corporate").strip() or "corporate"
        topology = dict(runtime_topology or self.build_runtime_delegation_topology())
        policy = self.get_runtime_policy(normalized_profile)
        policy_payload = policy.model_dump() if hasattr(policy, "model_dump") else dict(policy or {})
        plan = build_company_work_item_runtime_plan(
            self,
            profile=normalized_profile,
            runtime_topology=topology,
            original_request=original_request,
            runtime_policy=policy_payload,
        )
        return apply_external_team_bindings_to_plan(
            self,
            plan,
            runtime_topology=topology,
            compiled_bindings=compiled_external_teams,
        )

    def _build_runtime_delegation_topology_from_configured_teams(self) -> dict[str, Any]:
        final_decider_role_id = self.get_final_decider_role_id(strict=False) or ""
        top_level_role_ids = self.get_top_level_role_ids()
        agent_ids = set(self._agents)
        configured_teams = list(self.config.org.teams or [])
        teams: list[dict[str, Any]] = []
        seats: list[dict[str, Any]] = []
        team_by_lead: dict[str, str] = {}
        lead_seat_by_team: dict[str, dict[str, Any]] = {}
        seat_ids_by_team_and_role: dict[tuple[str, str], str] = {}

        def _seat_meta(raw_seat: Any) -> dict[str, Any]:
            metadata = dict(getattr(raw_seat, "metadata", {}) or {})
            if getattr(raw_seat, "seat_kind", ""):
                metadata.setdefault("seat_kind", str(getattr(raw_seat, "seat_kind", "") or "").strip())
            if getattr(raw_seat, "name", ""):
                metadata.setdefault("seat_name", str(getattr(raw_seat, "name", "") or "").strip())
            return metadata

        def _looks_like_lead(raw_seat: Any) -> bool:
            metadata = _seat_meta(raw_seat)
            if bool(metadata.get("is_team_lead", False)):
                return True
            seat_kind = str(metadata.get("seat_kind", "") or getattr(raw_seat, "seat_kind", "") or "").strip().lower()
            return seat_kind in {"lead", "leader", "manager", "root"}

        for team_cfg in configured_teams:
            team_id = str(getattr(team_cfg, "team_id", "") or "").strip()
            if not team_id:
                continue
            raw_seats = list(getattr(team_cfg, "seats", []) or [])
            if not raw_seats:
                continue
            lead_cfg = next((seat for seat in raw_seats if _looks_like_lead(seat)), None) or raw_seats[0]
            lead_role_id = str(getattr(lead_cfg, "role_id", "") or "").strip()
            if lead_role_id:
                team_by_lead[lead_role_id] = team_id
            member_role_ids: list[str] = []
            member_seat_ids: list[str] = []
            for raw_seat in raw_seats:
                seat_id = str(getattr(raw_seat, "seat_id", "") or "").strip()
                role_id = str(getattr(raw_seat, "role_id", "") or "").strip()
                if not seat_id or not role_id:
                    continue
                if role_id not in member_role_ids:
                    member_role_ids.append(role_id)
                member_seat_ids.append(seat_id)
                seat_ids_by_team_and_role[(team_id, role_id)] = seat_id
            team_metadata = dict(getattr(team_cfg, "metadata", {}) or {})
            parent_team_id = str(team_metadata.get("parent_team_id", "") or "").strip()
            lead_agent = self.get_agent(lead_role_id) if lead_role_id else None
            manager_role_id = str(getattr(lead_agent, "reports_to", "") or "").strip()
            if manager_role_id and manager_role_id != "owner" and not parent_team_id:
                parent_team_id = team_by_lead.get(manager_role_id, "")
            team_payload = {
                "team_id": team_id,
                "lead_role_id": lead_role_id,
                "parent_team_id": parent_team_id,
                "member_role_ids": member_role_ids,
                "member_seat_ids": member_seat_ids,
                "metadata": {
                    "lead_name": str(getattr(lead_agent, "name", "") or getattr(lead_cfg, "name", "") or lead_role_id).strip(),
                    "configured_team": True,
                    "final_decider_team": lead_role_id == final_decider_role_id,
                    **team_metadata,
                },
            }
            teams.append(team_payload)
            lead_seat_by_team[team_id] = {
                "seat_id": str(getattr(lead_cfg, "seat_id", "") or "").strip(),
                "role_id": lead_role_id,
            }

        for team_cfg in configured_teams:
            team_id = str(getattr(team_cfg, "team_id", "") or "").strip()
            if not team_id:
                continue
            lead_entry = lead_seat_by_team.get(team_id, {})
            lead_role_id = str(lead_entry.get("role_id", "") or "").strip()
            lead_seat_id = str(lead_entry.get("seat_id", "") or "").strip()
            parent_team_id = next(
                (
                    str(team.get("parent_team_id", "") or "").strip()
                    for team in teams
                    if str(team.get("team_id", "") or "").strip() == team_id
                ),
                "",
            )
            for raw_seat in list(getattr(team_cfg, "seats", []) or []):
                seat_id = str(getattr(raw_seat, "seat_id", "") or "").strip()
                role_id = str(getattr(raw_seat, "role_id", "") or "").strip()
                if not seat_id or not role_id:
                    continue
                agent = self.get_agent(role_id)
                metadata = _seat_meta(raw_seat)
                is_team_lead = seat_id == lead_seat_id
                manager_role_id = str(getattr(raw_seat, "manager_role_id", "") or "").strip()
                manager_seat_id = str(getattr(raw_seat, "manager_seat_id", "") or "").strip()
                if not manager_role_id:
                    if is_team_lead:
                        manager_role_id = str(getattr(agent, "reports_to", "") or "").strip()
                        if manager_role_id == "owner":
                            manager_role_id = ""
                    else:
                        manager_role_id = lead_role_id
                if not manager_seat_id:
                    if is_team_lead:
                        if parent_team_id:
                            manager_seat_id = (
                                seat_ids_by_team_and_role.get((parent_team_id, role_id), "")
                                or seat_ids_by_team_and_role.get((parent_team_id, manager_role_id), "")
                                or str(lead_seat_by_team.get(parent_team_id, {}).get("seat_id", "") or "").strip()
                            )
                    else:
                        manager_seat_id = lead_seat_id
                managed_team_id = team_by_lead.get(role_id, "")
                contact_role_ids = [
                    str(item).strip()
                    for item in list(metadata.get("contact_role_ids", []) or [])
                    if str(item).strip()
                ]
                if not contact_role_ids:
                    contact_role_ids = [
                        str(getattr(candidate, "role_id", "") or "").strip()
                        for candidate in list(getattr(team_cfg, "seats", []) or [])
                        if str(getattr(candidate, "role_id", "") or "").strip()
                        and str(getattr(candidate, "role_id", "") or "").strip() != role_id
                    ]
                if manager_role_id and manager_role_id not in contact_role_ids and manager_role_id in agent_ids:
                    contact_role_ids.append(manager_role_id)
                seats.append(
                    {
                        "seat_id": seat_id,
                        "team_id": team_id,
                        "role_id": role_id,
                        "employee_id": "",
                        "is_team_lead": is_team_lead,
                        "manager_role_id": manager_role_id,
                        "manager_seat_id": manager_seat_id,
                        "managed_team_id": managed_team_id,
                        "allowed_delegate_role_ids": [
                            str(item).strip()
                            for item in list(metadata.get("allowed_delegate_role_ids", []) or self.get_allowed_downstream_roles(role_id))
                            if str(item).strip()
                        ],
                        "contact_role_ids": contact_role_ids,
                        "metadata": {
                            "team_lead_role_id": lead_role_id,
                            "parent_team_id": parent_team_id,
                            "role_name": getattr(agent, "name", role_id) if agent else role_id,
                            "configured_seat": True,
                            **metadata,
                        },
                    }
                )

        return {
            "final_decider_role_id": final_decider_role_id,
            "top_level_role_ids": list(top_level_role_ids),
            "teams": teams,
            "seats": seats,
        }

    def get_role_prompt_context(self, role_id: str) -> str:
        agent = self.get_agent(role_id)
        if not agent or not agent.prompt_refs:
            return ""
        return "\n\n".join(ref.strip() for ref in agent.prompt_refs if ref.strip())

    def get_role_skill_refs(self, role_id: str) -> list[str]:
        agent = self.get_agent(role_id)
        if not agent:
            return []
        return list(agent.skill_refs)

    def get_role_review_target(self, role_id: str) -> str | None:
        agent = self.get_agent(role_id)
        if not agent:
            return None
        review_role = agent.runtime_policy.get("review_role")
        return review_role or None

    def get_allowed_downstream_roles(self, role_id: str) -> list[str]:
        if not role_id:
            return []
        agent = self.get_agent(role_id)
        if not agent:
            return []
        allowed = list(agent.runtime_policy.get("allowed_downstream_roles", []))
        if allowed:
            return allowed
        return list(agent.can_spawn)

    def get_allowed_contact_roles(self, role_id: str, task: Any | None = None) -> list[str]:
        task_meta = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
        seat_contacts = [
            str(candidate).strip()
            for candidate in list(task_meta.get("seat_contact_role_ids", []) or [])
            if str(candidate).strip()
        ]
        if seat_contacts:
            return [candidate for candidate in seat_contacts if candidate in self._agents and candidate != role_id]
        agent = self.get_agent(role_id)
        if not agent:
            return []

        explicit = list(agent.runtime_policy.get("allowed_contact_roles", []))
        if explicit:
            return [candidate for candidate in explicit if candidate in self._agents and candidate != role_id]

        allowed: list[str] = []

        def _add(candidate: str | None) -> None:
            if not candidate or candidate == role_id or candidate not in self._agents:
                return
            if candidate not in allowed:
                allowed.append(candidate)

        # Preserve existing downward delegation behavior.
        for candidate in self.get_allowed_downstream_roles(role_id):
            _add(candidate)

        # Allow escalation to the direct manager only.
        _add(agent.reports_to)

        # Allow peer collaboration with roles that share the same direct manager.
        manager_id = agent.reports_to
        if manager_id:
            for peer in self._agents.values():
                if peer.role_id != role_id and peer.reports_to == manager_id:
                    _add(peer.role_id)

        for candidate in collect_dynamic_contact_roles(task):
            _add(candidate)

        return allowed

    def build_runtime_delegation_topology(self) -> dict[str, Any]:
        """Build runtime team/seat topology from the org tree.

        The topology is org-first rather than runtime-plan-first: every manager role
        gets a team containing itself and its direct reports, while middle roles
        appear both as members of their manager's team and as the lead of their
        own team.
        """
        if list(self.config.org.teams or []):
            return self._build_runtime_delegation_topology_from_configured_teams()
        final_decider_role_id = self.get_final_decider_role_id(strict=False) or ""
        top_level_role_ids = self.get_top_level_role_ids()
        agent_ids = set(self._agents)
        team_order: list[str] = []
        for role_id in self._agents:
            if self.get_subordinates(role_id) or role_id == final_decider_role_id:
                team_order.append(role_id)
        # Preserve deterministic manager-before-child ordering.
        team_order.sort(key=lambda role_id: (len(self.get_chain_of_command(role_id)), role_id))

        teams: list[dict[str, Any]] = []
        seats: list[dict[str, Any]] = []
        team_by_lead: dict[str, str] = {}

        for lead_role_id in team_order:
            agent = self.get_agent(lead_role_id)
            if agent is None:
                continue
            team_id = f"team::{lead_role_id}"
            team_by_lead[lead_role_id] = team_id
            direct_reports = [
                subordinate.role_id
                for subordinate in self.get_subordinates(lead_role_id)
                if subordinate.role_id in agent_ids
            ]
            parent_team_id = ""
            manager_role_id = str(agent.reports_to or "").strip()
            if manager_role_id and manager_role_id != "owner":
                parent_team_id = f"team::{manager_role_id}"
            member_role_ids = [lead_role_id, *direct_reports]
            teams.append(
                {
                    "team_id": team_id,
                    "lead_role_id": lead_role_id,
                    "parent_team_id": parent_team_id,
                    "member_role_ids": member_role_ids,
                    "member_seat_ids": [
                        f"seat::{team_id}::{member_role_id}"
                        for member_role_id in member_role_ids
                    ],
                    "metadata": {
                        "lead_name": getattr(agent, "name", lead_role_id),
                        "final_decider_team": lead_role_id == final_decider_role_id,
                    },
                }
            )

        for team in teams:
            team_id = str(team["team_id"])
            lead_role_id = str(team["lead_role_id"])
            parent_team_id = str(team.get("parent_team_id", "") or "").strip()
            parent_lead_role_id = parent_team_id.replace("team::", "", 1) if parent_team_id else ""
            for member_role_id in list(team.get("member_role_ids", []) or []):
                agent = self.get_agent(member_role_id)
                if agent is None:
                    continue
                seat_id = f"seat::{team_id}::{member_role_id}"
                is_team_lead = member_role_id == lead_role_id
                managed_team_id = team_by_lead.get(member_role_id, "")
                manager_role_id = ""
                manager_seat_id = ""
                if is_team_lead:
                    if parent_lead_role_id and parent_lead_role_id in agent_ids:
                        manager_role_id = parent_lead_role_id
                        manager_seat_id = f"seat::{parent_team_id}::{member_role_id}"
                else:
                    manager_role_id = lead_role_id
                    manager_seat_id = f"seat::{team_id}::{lead_role_id}"
                contact_role_ids = []
                for candidate_role_id in list(team.get("member_role_ids", []) or []):
                    if candidate_role_id != member_role_id and candidate_role_id not in contact_role_ids:
                        contact_role_ids.append(candidate_role_id)
                if manager_role_id and manager_role_id not in contact_role_ids and manager_role_id in agent_ids:
                    contact_role_ids.append(manager_role_id)
                seats.append(
                    {
                        "seat_id": seat_id,
                        "team_id": team_id,
                        "role_id": member_role_id,
                        "employee_id": "",
                        "is_team_lead": is_team_lead,
                        "manager_role_id": manager_role_id,
                        "manager_seat_id": manager_seat_id,
                        "managed_team_id": managed_team_id if managed_team_id != team_id else managed_team_id,
                        "allowed_delegate_role_ids": self.get_allowed_downstream_roles(member_role_id),
                        "contact_role_ids": contact_role_ids,
                        "metadata": {
                            "team_lead_role_id": lead_role_id,
                            "parent_team_id": parent_team_id,
                            "role_name": getattr(agent, "name", member_role_id),
                        },
                    }
                )

        return {
            "final_decider_role_id": final_decider_role_id,
            "top_level_role_ids": list(top_level_role_ids),
            "teams": teams,
            "seats": seats,
        }

    def resolve_runtime_target_seat(
        self,
        topology: dict[str, Any],
        *,
        from_seat_id: str,
        target_role_id: str,
    ) -> dict[str, Any] | None:
        """Resolve the best seat for a delegation target inside runtime teams."""
        target_role_id = str(target_role_id or "").strip()
        if not from_seat_id or not target_role_id:
            return None
        seat_map = {
            str(seat.get("seat_id", "")).strip(): dict(seat)
            for seat in list(topology.get("seats", []) or [])
            if str(seat.get("seat_id", "")).strip()
        }
        from_seat = seat_map.get(from_seat_id)
        if from_seat is None:
            return None

        # Opaque external teams expose one canonical dispatchable seat even
        # when the org graph renders the boundary role in multiple teams.
        opaque_target = next(
            (
                candidate
                for candidate in seat_map.values()
                if str(candidate.get("role_id", "") or "").strip() == target_role_id
                and str(candidate.get("execution_unit_kind", "") or "").strip()
                == "opaque_external_team"
                and bool(candidate.get("dispatchable", False))
            ),
            None,
        )
        if opaque_target is not None:
            return opaque_target

        def _find_in_team(team_id: str) -> dict[str, Any] | None:
            for candidate in seat_map.values():
                if str(candidate.get("team_id", "") or "").strip() != team_id:
                    continue
                if str(candidate.get("role_id", "") or "").strip() != target_role_id:
                    continue
                if str(candidate.get("seat_id", "") or "").strip() == from_seat_id:
                    continue
                return candidate
            return None

        same_team = _find_in_team(str(from_seat.get("team_id", "") or "").strip())
        if same_team is not None:
            return same_team
        managed_team = str(from_seat.get("managed_team_id", "") or "").strip()
        if managed_team:
            managed_candidate = _find_in_team(managed_team)
            if managed_candidate is not None:
                return managed_candidate
        manager_seat_id = str(from_seat.get("manager_seat_id", "") or "").strip()
        if manager_seat_id:
            manager_seat = seat_map.get(manager_seat_id)
            if manager_seat is not None and str(manager_seat.get("role_id", "") or "").strip() == target_role_id:
                return manager_seat
        return None

    def build_contact_directory(self, role_id: str, task: Any | None = None) -> list[dict[str, str]]:
        agent = self.get_agent(role_id)
        if not agent:
            return []

        peers = {
            peer.role_id
            for peer in self._agents.values()
            if peer.role_id != role_id and peer.reports_to == agent.reports_to
        }
        downstream = set(self.get_allowed_downstream_roles(role_id))
        manager_id = agent.reports_to if agent.reports_to in self._agents else None

        directory: list[dict[str, str]] = []
        for contact_role_id in self.get_allowed_contact_roles(role_id, task=task):
            contact = self.get_agent(contact_role_id)
            if not contact:
                continue
            relation = "peer"
            if contact_role_id == manager_id:
                relation = "manager"
            elif contact_role_id in downstream:
                relation = "downstream"
            elif contact_role_id in peers:
                relation = "peer"
            directory.append({
                "role_id": contact.role_id,
                "name": contact.name,
                "responsibility": contact.responsibility,
                "relation": relation,
            })
        return directory

    def get_role_for_domain(self, domains: list[str]) -> AgentInfo:
        """Return the default executor instead of domain-based routing."""
        _ = domains
        return self.get_executor()

    def get_role_for_work_item(self, role_id: str, domains: list[str] | None = None) -> AgentInfo:
        agent = self.get_agent(role_id)
        if agent:
            return agent
        _ = domains
        return self.get_executor()

    def get_employee(self, employee_id: str) -> EmployeeConfig | None:
        normalized = str(employee_id or "").strip()
        if not normalized:
            return None
        for employee in self.config.org.employees:
            if employee.employee_id == normalized:
                return employee
            aliases = {
                str(item).strip()
                for item in list(dict(employee.metadata or {}).get("legacy_employee_ids", []) or [])
                if str(item).strip()
            }
            if normalized in aliases:
                return employee
        return None

    def get_default_employee_for_role(self, role_id: str) -> EmployeeConfig | None:
        if role_id == TASK_MODE_GENERAL_ROLE_ID:
            return None
        default_id = self._slugify_identifier(f"{role_id}-default-employee")
        direct = self.get_employee(default_id)
        if direct is not None:
            return direct
        for employee in self.list_employees(role_id=role_id):
            metadata = dict(employee.metadata or {})
            if metadata.get("is_default_employee") and metadata.get("auto_created_for_role") == role_id:
                return employee
        return None

    def get_fallback_employee_for_role(self, role_id: str) -> EmployeeConfig | None:
        fallback_id = self._slugify_identifier(f"{role_id}-fallback-empty-employee")
        direct = self.get_employee(fallback_id)
        if direct is not None:
            return direct
        for employee in self.list_employees(role_id=role_id):
            metadata = dict(employee.metadata or {})
            if metadata.get("is_fallback_employee") and metadata.get("auto_created_for_role") == role_id:
                return employee
        return None

    def _slugify_identifier(self, value: str) -> str:
        slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("-")
        return slug or "employee"

    def _format_role_label(self, role_id: str) -> str:
        agent = self.get_agent(role_id)
        if agent and agent.name.strip():
            return agent.name.strip()
        return re.sub(r"[-_]+", " ", role_id).strip().title() or "General Agent"

    def ensure_default_employee_for_role(
        self,
        role_id: str,
        *,
        persist: bool = True,
    ) -> EmployeeConfig | None:
        if role_id == TASK_MODE_GENERAL_ROLE_ID:
            return None
        existing_default = self.get_default_employee_for_role(role_id)
        if existing_default is not None:
            return existing_default

        role_label = self._format_role_label(role_id)
        employee_id = self._slugify_identifier(f"{role_id}-default-employee")
        already_created = self.get_employee(employee_id)
        if already_created is not None:
            return already_created

        prompt_refs: list[str] = []
        if self.opc_home:
            prompt_path = self.opc_home / _DEFAULT_EMPLOYEE_PROMPT_REF
            if prompt_path.is_file():
                prompt_refs.append(_DEFAULT_EMPLOYEE_PROMPT_REF)

        agent = self.get_agent(role_id)
        employee = EmployeeConfig(
            employee_id=employee_id,
            template_id=_DEFAULT_EMPLOYEE_TEMPLATE_ID,
            name=f"{role_label} Default Employee",
            role_id=role_id,
            description=(
                f"General-purpose default employee for the {role_label} role. "
                "Handles routine execution until a specialized employee is assigned."
            ),
            category="general",
            prompt_refs=prompt_refs,
            preferred_external_agent=agent.preferred_external_agent if agent else None,
            metadata={
                "is_default_employee": True,
                "auto_created_for_role": role_id,
                "employee_origin": "system_default",
                "default_prompt_ref": _DEFAULT_EMPLOYEE_PROMPT_REF,
            },
        )
        self.config.org.employees = [*self.config.org.employees, employee]
        if persist and self.opc_home:
            self.config.save(self.opc_home / "config")
        return employee

    def ensure_fallback_employee_for_role(
        self,
        role_id: str,
        *,
        persist: bool = True,
    ) -> EmployeeConfig | None:
        existing_fallback = self.get_fallback_employee_for_role(role_id)
        if existing_fallback is not None:
            return existing_fallback

        role_label = self._format_role_label(role_id)
        employee_id = self._slugify_identifier(f"{role_id}-fallback-empty-employee")
        already_created = self.get_employee(employee_id)
        if already_created is not None:
            return already_created

        agent = self.get_agent(role_id)
        employee = EmployeeConfig(
            employee_id=employee_id,
            template_id=_FALLBACK_EMPLOYEE_TEMPLATE_ID,
            name=f"{role_label} Fallback Empty Employee",
            role_id=role_id,
            description=(
                f"Empty placeholder employee for the {role_label} role when recruitment found no credible fit. "
                "Add prompt_refs in YAML to customize fallback execution."
            ),
            category="fallback",
            prompt_refs=[],
            preferred_external_agent=agent.preferred_external_agent if agent else None,
            metadata={
                "is_fallback_employee": True,
                "auto_created_for_role": role_id,
                "employee_origin": "recruitment_fallback",
            },
        )
        self.config.org.employees = [*self.config.org.employees, employee]
        if persist and self.opc_home:
            self.config.save(self.opc_home / "config")
        return employee

    def remove_employee(self, employee_id: str) -> bool:
        """Remove an employee from config. Returns True if found and removed."""
        before = len(self.config.org.employees)
        self.config.org.employees = [
            emp for emp in self.config.org.employees
            if emp.employee_id != employee_id
        ]
        return len(self.config.org.employees) < before

    @staticmethod
    def employee_role_ids(employee: EmployeeConfig) -> list[str]:
        metadata = dict(employee.metadata or {})
        role_ids: list[str] = []
        for value in [
            employee.role_id,
            metadata.get("home_role_id"),
            *list(metadata.get("home_role_ids", []) or []),
            *list(metadata.get("staffed_role_ids", []) or []),
        ]:
            role_id = str(value or "").strip()
            if role_id and role_id not in role_ids:
                role_ids.append(role_id)
        return role_ids

    def list_employees(self, role_id: str | None = None) -> list[EmployeeConfig]:
        employees = [employee for employee in self.config.org.employees if employee.status == "active"]
        if role_id:
            normalized_role_id = str(role_id or "").strip()
            employees = [employee for employee in employees if normalized_role_id in self.employee_role_ids(employee)]
        return sorted(employees, key=lambda item: (item.role_id, item.name.lower()))

    def resolve_employee_for_work_item(
        self,
        role_id: str,
        domains: list[str] | None = None,
        *,
        project_id: str | None = None,
        work_item_metadata: dict[str, Any] | None = None,
        preferred_employee_id: str | None = None,
        prefer_default_employee: bool = False,
    ) -> dict[str, Any] | None:
        if role_id == TASK_MODE_GENERAL_ROLE_ID:
            return None
        if preferred_employee_id:
            preferred = self.get_employee(preferred_employee_id)
            if preferred is not None:
                return self._build_employee_assignment(
                    preferred,
                    role_id=role_id,
                    domains=[],
                    project_id=project_id,
                )
        candidates = self.list_employees(role_id=role_id)
        if not candidates:
            default_employee = self.ensure_default_employee_for_role(role_id, persist=False)
            if default_employee is None:
                return None
            candidates = [default_employee]

        if prefer_default_employee:
            default_employee = self.ensure_default_employee_for_role(role_id, persist=False)
            if default_employee is not None:
                return self._build_employee_assignment(
                    default_employee,
                    role_id=role_id,
                    domains=[],
                    project_id=project_id,
                )

        rankable_candidates = [
            employee
            for employee in candidates
            if not dict(employee.metadata or {}).get("is_fallback_employee")
        ]
        if not rankable_candidates:
            default_employee = self.ensure_default_employee_for_role(role_id, persist=False)
            if default_employee is None:
                return None
            return self._build_employee_assignment(
                default_employee,
                role_id=role_id,
                domains=[],
                project_id=project_id,
            )

        work_item_metadata = dict(work_item_metadata or {})
        ranked = sorted(
            rankable_candidates,
            key=lambda employee: self._employee_score(
                employee,
                role_id=role_id,
                domains=[],
                project_id=project_id,
                work_item_metadata=work_item_metadata,
            ),
            reverse=True,
        )
        selected = ranked[0]
        return self._build_employee_assignment(
            selected,
            role_id=role_id,
            domains=[],
            project_id=project_id,
        )

    def _build_employee_assignment(
        self,
        selected: EmployeeConfig,
        *,
        role_id: str,
        domains: list[str],
        project_id: str | None,
        experience_mode: str = "with_experience",
    ) -> dict[str, Any]:
        normalized_experience_mode = (
            "template_only"
            if str(experience_mode or "").strip() == "template_only"
            else "with_experience"
        )
        experience_score = 0.0
        delta_context = ""
        organization_id = str(getattr(self.config.org, "organization_id", "") or "").strip()
        if self.employee_evolution and normalized_experience_mode != "template_only":
            experience_score = self.employee_evolution.get_experience_score(
                selected.employee_id,
                role_id=role_id,
                domains=[],
                project_id=project_id,
                organization_id=organization_id or None,
            )
            delta_context = self.employee_evolution.build_employee_delta_context(
                selected.employee_id,
                project_id=project_id,
                organization_id=organization_id or None,
            )
        prompt_context = ""
        metadata = dict(selected.metadata or {})
        if metadata.get("is_default_employee") and role_id != TASK_MODE_GENERAL_ROLE_ID:
            prompt_context = self._build_company_default_employee_prompt_context(role_id)
        elif self.opc_home:
            prompt_context = "\n\n".join(resolve_prompt_refs(selected.prompt_refs, self.opc_home))
        return {
            "employee_id": selected.employee_id,
            "template_id": selected.template_id,
            "name": selected.name,
            "role_id": role_id,
            "home_role_id": selected.role_id,
            "role_ids": self.employee_role_ids(selected),
            "description": selected.description,
            "category": selected.category,
            "domains": list(selected.domains),
            "tags": list(selected.tags),
            "prompt_refs": list(selected.prompt_refs),
            "prompt_context": prompt_context,
            "delta_context": delta_context,
            "preferred_external_agent": selected.preferred_external_agent,
            "seniority": selected.seniority,
            "metadata": dict(selected.metadata),
            "experience_score": experience_score,
            "experience_mode": normalized_experience_mode,
        }

    def _build_company_default_employee_prompt_context(self, role_id: str) -> str:
        agent = self.get_agent(role_id)
        role_name = self._format_role_label(role_id)
        responsibility = str(getattr(agent, "responsibility", "") or "").strip()
        downstream_roles = self.get_allowed_downstream_roles(role_id)
        manager_like = bool(downstream_roles or self.get_subordinates(role_id))
        lines = [
            f"You are the default employee for the {role_name} role in company mode.",
        ]
        if responsibility:
            lines.append(f"Role responsibility: {responsibility}")
        if manager_like:
            lines.extend(
                [
                    "Operate as a manager by default.",
                    "Clarify scope, delegate work to the most suitable direct reports, monitor progress, and roll results up.",
                    "Do not assume you should personally execute the whole request unless no suitable downstream role exists.",
                ]
            )
        else:
            lines.extend(
                [
                    "Operate as an execution-focused teammate by default.",
                    "Complete the assigned slice, coordinate when needed, and report a concise digest upward.",
                ]
            )
        return "\n".join(lines).strip()

    def get_escalation_rules(self) -> list[dict[str, str]]:
        return [r.model_dump() for r in self.config.org.escalation_rules]

    def current_org_version(self) -> int:
        return self._org_version

    def current_runtime_topology_version(self) -> int:
        return self._runtime_topology_version

    def snapshot_org(self, project_id: str = "default", active_tasks: list[dict[str, Any]] | None = None) -> OrgSnapshot:
        return OrgSnapshot(
            project_id=project_id,
            org_version=self._org_version,
            runtime_topology_version=self._runtime_topology_version,
            company_name=self.config.org.company_name,
            topology=self.config.org.topology,
            roles=[role.model_dump() for role in self.config.org.roles] or [
                {
                    "id": agent.role_id,
                    "name": agent.name,
                    "responsibility": agent.responsibility,
                    "reports_to": agent.reports_to,
                    "can_spawn": list(agent.can_spawn),
                    "tools": list(agent.tools),
                    "preferred_external_agent": agent.preferred_external_agent,
                    "prompt_refs": list(agent.prompt_refs),
                    "skill_refs": list(agent.skill_refs),
                    "handoff_template_ref": agent.handoff_template_ref,
                    "memory_policy_ref": agent.memory_policy_ref,
                    "artifact_contract_ref": agent.artifact_contract_ref,
                    "runtime_policy": dict(agent.runtime_policy),
                }
                for agent in self.list_agents()
            ],
            company_profile=self.config.org.company_profile,
            active_tasks=list(active_tasks or []),
            metadata={
                "history_versions": sorted(self._org_history.keys()),
                "organization_id": getattr(self.config.org, "organization_id", ""),
                "organization_name": getattr(self.config.org, "organization_name", ""),
                "organization_config_file": getattr(self.config.org, "organization_config_file", ""),
            },
        )

    def reload_from_config(self) -> None:
        self._initialize_roles()

    def validate_changeset(self, changeset: ReorgChangeSet) -> list[str]:
        errors: list[str] = []
        role_ids = {role.id for role in self._effective_roles()} or {agent.role_id for agent in self.list_agents()}
        for change in changeset.role_changes:
            if change.action in {"remove", "replace", "update"} and change.role_id not in role_ids:
                errors.append(f"Unknown role `{change.role_id}` for action `{change.action}`.")
            if change.action in {"add", "replace"}:
                target = change.replacement_role_id or change.role.get("id", "")
                if not target:
                    errors.append(f"Role change `{change.action}` requires a new role id.")
        return errors

    def add_role(self, role: RoleConfig) -> None:
        self.config.org.roles.append(role)
        self._org_version += 1
        self._initialize_roles()

    def remove_role(self, role_id: str) -> None:
        self.config.org.roles = [role for role in self.config.org.roles if role.id != role_id]
        self._org_version += 1
        self._initialize_roles()

    def replace_roles(self, roles: list[RoleConfig]) -> None:
        self.config.org.roles = [role.model_copy(deep=True) for role in roles]
        self._org_version += 1
        self._initialize_roles()

    def apply_changeset(self, changeset: ReorgChangeSet, persist: bool = True) -> dict[str, Any]:
        errors = self.validate_changeset(changeset)
        if errors:
            raise ValueError("; ".join(errors))

        old_org_version = self._org_version
        current_profile = self.config.org.company_profile
        builtin_profile = current_profile == "corporate"
        base_roles = self._effective_roles() if builtin_profile else self.config.org.roles
        roles = [role.model_copy(deep=True) for role in base_roles]
        role_index = {role.id: idx for idx, role in enumerate(roles)}
        role_mapping: dict[str, str] = {}

        for change in changeset.role_changes:
            role_payload = dict(change.role)
            target_role_id = change.replacement_role_id or role_payload.get("id") or change.role_id
            if change.action == "add":
                roles.append(RoleConfig.model_validate(role_payload))
            elif change.action == "remove":
                roles = [role for role in roles if role.id != change.role_id]
                role_mapping[change.role_id] = target_role_id if target_role_id != change.role_id else ""
            elif change.action == "replace":
                roles = [role for role in roles if role.id != change.role_id]
                if role_payload:
                    role_payload.setdefault("id", target_role_id)
                    roles.append(RoleConfig.model_validate(role_payload))
                role_mapping[change.role_id] = target_role_id
            elif change.action == "update":
                idx = role_index.get(change.role_id)
                if idx is None:
                    continue
                merged = roles[idx].model_dump()
                merged.update(role_payload)
                roles[idx] = RoleConfig.model_validate(merged)

        self.config.org.roles = roles
        if builtin_profile and changeset.role_changes:
            self.config.org.company_profile = "custom"
        self._org_version += 1
        self._initialize_roles()
        if persist and self.opc_home:
            self.config.save(self.opc_home / "config")
        return {
            "old_org_version": old_org_version,
            "new_org_version": self._org_version,
            "role_mapping": role_mapping,
        }

    def _deep_merge(self, base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key, value in overlay.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = self._deep_merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _employee_score(
        self,
        employee: EmployeeConfig,
        *,
        role_id: str,
        domains: list[str],
        project_id: str | None,
        work_item_metadata: dict[str, Any],
    ) -> float:
        score = 10.0 if employee.role_id == role_id else 0.0
        work_item_tokens = {
            token.lower()
            for token in (
                str(work_item_metadata.get("domain", "")),
                str(work_item_metadata.get("focus", "")),
                str(work_item_metadata.get("title", "")),
            )
            if token
        }
        _ = domains
        employee_tokens = {
            token.lower()
            for token in re.findall(
                r"[a-z0-9][a-z0-9+-]{2,}",
                f"{employee.name} {employee.description} {employee.category}",
            )
        }
        score += float(len(employee_tokens & work_item_tokens) * 2)
        if self.employee_evolution:
            score += self.employee_evolution.get_experience_score(
                employee.employee_id,
                role_id=role_id,
                domains=[],
                project_id=project_id,
                organization_id=str(getattr(self.config.org, "organization_id", "") or "").strip() or None,
            )
        return score

    async def create_organization(
        self,
        name: str,
        description: str = "",
        company_profile: str = "corporate",
        budget_monthly_cents: int = 0,
        metadata: dict | None = None,
    ) -> Organization:
        """Create and persist a new organization."""
        org = Organization(
            name=name,
            description=description,
            company_profile=company_profile,
            budget_monthly_cents=budget_monthly_cents,
            metadata=dict(metadata or {}),
        )
        if self.store:
            await self.store.save_organization(org)
        return org

    async def load_organization(self, org_id: str) -> Organization | None:
        """Load a persistent organization from the store."""
        if not self.store:
            return None
        return await self.store.get_organization(org_id)

    async def load_org_agents(self, org_id: str) -> list[OrgAgent]:
        """Load all agents belonging to an organization."""
        if not self.store:
            return []
        return await self.store.list_org_agents(org_id)

    def get_org_tree(self) -> list[dict]:
        """Build a tree from agents using reports_to relationships."""
        agents = list(self._agents.values())
        by_manager: dict[str | None, list] = {}
        for agent in agents:
            key = agent.reports_to if agent.reports_to != "owner" else None
            by_manager.setdefault(key, []).append(agent)

        def _build(manager_id: str | None) -> list[dict]:
            members = by_manager.get(manager_id, [])
            return [
                {"agent": m, "reports": _build(m.role_id)}
                for m in members
            ]

        return _build(None)

    def get_chain_of_command(self, agent_id: str) -> list:
        """Walk up the reports_to chain from agent to root."""
        chain = []
        seen: set[str] = set()
        current = self._agents.get(agent_id)
        while current and current.role_id not in seen:
            seen.add(current.role_id)
            chain.append(current)
            parent_id = current.reports_to
            if not parent_id or parent_id == "owner":
                break
            current = self._agents.get(parent_id)
        return chain

    def get_subordinates(self, agent_id: str) -> list:
        """Get all direct reports of an agent."""
        return [
            agent for agent in self._agents.values()
            if agent.reports_to == agent_id
        ]
