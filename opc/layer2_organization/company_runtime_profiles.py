"""Built-in company runtime profiles and helpers."""

from __future__ import annotations

from opc.core.config import (
    ArtifactPolicyConfig,
    CommunicationPolicyConfig,
    GateHarnessPolicyConfig,
    HandoffPolicyConfig,
    MemoryPolicyConfig,
    ReviewPolicyConfig,
    RoleConfig,
    RoleRuntimePolicyConfig,
    RuntimePolicyConfig,
)
from opc.core.models import CompanyProfile
from opc.layer2_organization.data_acquisition_policy import ACQUISITION_SPECIALIST_ROLE_ID

_BROWSER_RESEARCH_TOOLS = [
    "browser_navigate",
    "browser_navigate_back",
    "browser_snapshot",
    "browser_wait_for",
    "browser_scroll",
    "browser_take_screenshot",
]

_BROWSER_EXECUTION_TOOLS = [
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
]

_CORPORATE_COORDINATION_TOOLS = [
    "file_read",
    "file_write",
    "file_edit",
    "file_search",
    "list_dir",
    "todo_write",
    "todo_read",
]

_CORPORATE_BOOTSTRAP_TOOLS = [
    *_CORPORATE_COORDINATION_TOOLS,
    "shell_exec",
]

_CORPORATE_WEB_COORDINATION_TOOLS = [
    *_CORPORATE_COORDINATION_TOOLS,
    "web_search",
    "web_fetch",
]

_CORPORATE_EXECUTION_TOOLS = [
    "shell_exec",
    "file_read",
    "file_write",
    "file_edit",
    "file_search",
    "list_dir",
    "web_search",
    "web_fetch",
    "todo_write",
    "todo_read",
    *_BROWSER_EXECUTION_TOOLS,
]

_CORPORATE_QA_TOOLS = [
    "file_read",
    "file_write",
    "file_edit",
    "file_search",
    "list_dir",
    "shell_exec",
    "browser_navigate",
    "browser_navigate_back",
    "browser_snapshot",
    "browser_wait_for",
    "browser_scroll",
    "browser_select_option",
    "browser_take_screenshot",
]

_CORPORATE_ENV_TOOLS = [
    "shell_exec",
    "file_read",
    "file_write",
    "file_edit",
    "file_search",
    "list_dir",
    "web_search",
    "web_fetch",
    "todo_write",
    "todo_read",
]

_CORPORATE_DATA_ACQUISITION_TOOLS = [
    "shell_exec",
    "file_read",
    "file_write",
    "file_edit",
    "file_search",
    "list_dir",
    "web_search",
    "web_fetch",
    "todo_write",
    "todo_read",
    *_BROWSER_EXECUTION_TOOLS,
]


def get_company_profile_descriptions() -> dict[str, str]:
    return {
        CompanyProfile.CORPORATE.value: (
            "A hierarchical corporate runtime with CEO, C-suite executives (CTO/CMO/COO), and specialized workers. Execution is driven by work items and role queues."
        ),
        CompanyProfile.CUSTOM.value: (
            "A user-defined company runtime. Roles and work items drive execution."
        ),
    }


def get_builtin_runtime_policies() -> dict[str, RuntimePolicyConfig]:
    return {
        CompanyProfile.CORPORATE.value: RuntimePolicyConfig(
            communication=CommunicationPolicyConfig(
                default_mode="dm",
                blocking_default=False,
                meeting_required_for=["architecture", "cross_team_conflict"],
                allow_broadcast=True,
            ),
            memory=MemoryPolicyConfig(
                include_role_memory=True,
                include_project_memory=False,
                include_decision_log=True,
                include_artifact_index=True,
                recent_history_lines=12,
            ),
            handoff=HandoffPolicyConfig(
                require_structured_handoff=True,
                require_ack=False,
                include_risks=True,
                include_open_questions=True,
            ),
            artifact=ArtifactPolicyConfig(
                enforce_contract=False,
                require_artifact_index=True,
                required_kinds=[],
            ),
            review=ReviewPolicyConfig(
                enable_work_item_gates=False,
                strict_gate_inference=False,
                require_reviewer_role=True,
                allow_human_override=True,
            ),
            gate_harness=GateHarnessPolicyConfig(
                decision_mode="agent_first",
                default_degrade_policy="allow",
                allow_pass_with_constraints=True,
            ),
        ),
        CompanyProfile.CUSTOM.value: RuntimePolicyConfig(
            gate_harness=GateHarnessPolicyConfig(
                decision_mode="agent_first",
                default_degrade_policy="allow",
                allow_pass_with_constraints=True,
            ),
        ),
    }


def _apply_configured_role_overrides(
    builtin_roles: list[RoleConfig],
    configured_roles: list[RoleConfig] | None = None,
) -> list[RoleConfig]:
    """Overlay explicit org_config role fields onto builtin role presets."""
    if not configured_roles:
        return builtin_roles

    configured_by_id = {role.id: role for role in configured_roles}
    merged: list[RoleConfig] = []
    for builtin_role in builtin_roles:
        configured_role = configured_by_id.get(builtin_role.id)
        if configured_role is None:
            merged.append(builtin_role)
            continue

        update_fields = {
            field_name: getattr(configured_role, field_name)
            for field_name in configured_role.model_fields_set
            if field_name != "id"
        }
        if not update_fields:
            merged.append(builtin_role)
            continue
        merged.append(builtin_role.model_copy(update=update_fields, deep=True))
    return merged


def get_builtin_roles(
    profile: str,
    configured_roles: list[RoleConfig] | None = None,
) -> list[RoleConfig]:
    # Corporate roles (also fallback for custom and unknown profiles)
    return _apply_configured_role_overrides([
        RoleConfig(
            id="ceo",
            name="CEO",
            icon="leader",
            responsibility="Strategic intake, high-level routing, final aggregation and delivery to the owner.",
            can_spawn=["cto", "cmo", "coo"],
            capabilities=[
                "strategic_planning",
                "work_delegation",
                "cross_team_coordination",
                "final_synthesis",
                "delivery_review",
            ],
            tools=list(_CORPORATE_BOOTSTRAP_TOOLS),
            prompt_refs=["Route tasks to the appropriate C-suite executive. Aggregate final results."],
        ),
        RoleConfig(
            id="cto",
            name="CTO",
            icon="code",
            responsibility="Technical planning, architecture decisions, code review, and engineering oversight.",
            reports_to="ceo",
            can_spawn=["senior_engineer", "devops_engineer"],
            capabilities=[
                "technical_planning",
                "architecture_design",
                "technical_research",
                "engineering_oversight",
                "code_review",
                "research_synthesis",
            ],
            tools=[*_CORPORATE_WEB_COORDINATION_TOOLS, "shell_exec"],
            prompt_refs=["Focus on technical feasibility, architecture quality, and engineering best practices."],
        ),
        RoleConfig(
            id="cmo",
            name="CMO",
            icon="marketing",
            responsibility="Marketing strategy, content planning, UX review, and brand oversight.",
            reports_to="ceo",
            can_spawn=["content_specialist", "designer"],
            capabilities=[
                "market_research",
                "industry_analysis",
                "marketing_strategy",
                "content_strategy",
                "user_research",
                "brand_strategy",
                "research_synthesis",
            ],
            tools=[*_CORPORATE_WEB_COORDINATION_TOOLS, *_BROWSER_RESEARCH_TOOLS],
            prompt_refs=["Optimize for audience fit, brand consistency, and content quality."],
        ),
        RoleConfig(
            id="coo",
            name="COO",
            icon="strategy",
            responsibility="Operations coordination, process management, cross-team alignment, and quality assurance.",
            reports_to="ceo",
            can_spawn=[ACQUISITION_SPECIALIST_ROLE_ID, "qa_analyst"],
            capabilities=[
                "operational_planning",
                "cross_team_coordination",
                "quality_assurance",
                "source_acquisition",
                "compliance_review",
                "research_synthesis",
            ],
            tools=[*_CORPORATE_WEB_COORDINATION_TOOLS, *_BROWSER_RESEARCH_TOOLS],
            prompt_refs=["Ensure operational efficiency, process compliance, and delivery quality."],
        ),
        RoleConfig(
            id=ACQUISITION_SPECIALIST_ROLE_ID,
            name="Acquisition Specialist",
            icon="target",
            responsibility="Discover, verify, prepare, and report task-critical external inputs inside the shared workspace.",
            reports_to="coo",
            preferred_external_agent="claude_code",
            capabilities=[
                "source_discovery",
                "source_verification",
                "data_acquisition",
                "workspace_preparation",
            ],
            tools=list(_CORPORATE_DATA_ACQUISITION_TOOLS),
            prompt_refs=[
                "Run data acquisition in four phases: Discover, Verify, Prepare, Report.",
                "For media tasks, HTML snapshots and URL lists never count as acquired binary assets.",
                "Use standard CLI download tools through shell_exec instead of ad hoc inline network scripts.",
            ],
        ),
        RoleConfig(
            id="senior_engineer",
            name="Senior Engineer",
            icon="terminal",
            responsibility="Code implementation, system development, and technical execution.",
            reports_to="cto",
            preferred_external_agent="codex",
            runtime_policy=RoleRuntimePolicyConfig(execution_strategy="auto"),
            capabilities=[
                "software_implementation",
                "system_development",
                "technical_execution",
                "testing",
            ],
            tools=list(_CORPORATE_EXECUTION_TOOLS),
            prompt_refs=["Write clean, tested code. Leave clear documentation for reviewers."],
        ),
        RoleConfig(
            id="devops_engineer",
            name="DevOps Engineer",
            icon="settings",
            responsibility="Infrastructure, deployment, CI/CD, monitoring, and operational hardening.",
            reports_to="cto",
            preferred_external_agent="cursor",
            capabilities=[
                "infrastructure_engineering",
                "ci_cd",
                "deployment",
                "monitoring",
                "operational_hardening",
            ],
            tools=list(_CORPORATE_EXECUTION_TOOLS),
            prompt_refs=["Prioritize operational safety, observability, and deployment readiness."],
        ),
        RoleConfig(
            id="content_specialist",
            name="Content Specialist",
            icon="writing",
            responsibility="Documentation, copywriting, presentations, and user-facing writing.",
            reports_to="cmo",
            capabilities=[
                "documentation",
                "copywriting",
                "presentation_design",
                "user_facing_writing",
            ],
            tools=list(_CORPORATE_EXECUTION_TOOLS),
            prompt_refs=["Write clearly for the target audience. Polish deliverables."],
        ),
        RoleConfig(
            id="designer",
            name="Designer",
            icon="design",
            responsibility="Visual design, UX artifacts, wireframes, and design system work.",
            reports_to="cmo",
            capabilities=[
                "visual_design",
                "ux_design",
                "wireframing",
                "design_systems",
            ],
            tools=list(_CORPORATE_EXECUTION_TOOLS),
            prompt_refs=["Focus on usability, visual consistency, and design quality."],
        ),
        RoleConfig(
            id="qa_analyst",
            name="QA Analyst",
            icon="bug",
            responsibility="Testing, security review, compliance checks, and acceptance validation.",
            reports_to="coo",
            capabilities=[
                "testing",
                "security_review",
                "compliance_review",
                "acceptance_validation",
                "quality_assurance",
            ],
            tools=list(_CORPORATE_QA_TOOLS),
            prompt_refs=["Test rigorously. Reject unclear or unsafe outputs."],
        ),
        RoleConfig(
            id="env_engineer",
            name="Environment Engineer",
            icon="database",
            responsibility=(
                "Probe the host environment, install required tools and dependencies, "
                "prepare the assigned target_output_dir and base workspace directories, "
                "configure runtime environments (conda/venv/docker/system packages), "
                "and produce a verified environment manifest for downstream execution work items. "
                "Supports any toolchain: video editing (FFmpeg, DaVinci), 3D engines (Unity, Unreal, Blender, Godot), "
                "audio processing (FMOD, Wwise, SoX), ML/AI frameworks (PyTorch, TensorFlow), "
                "game development SDKs, design tools, and any other software the task requires."
            ),
            reports_to="cto",
            skill_refs=["env_provisioning"],
            capabilities=[
                "environment_probing",
                "dependency_installation",
                "workspace_preparation",
                "runtime_configuration",
                "environment_verification",
            ],
            runtime_policy=RoleRuntimePolicyConfig(
                execution_strategy="native",
                default_turn_type="setup",
                shell_timeout_override=1800,
            ),
            tools=list(_CORPORATE_ENV_TOOLS),
            prompt_refs=[
                "Always probe what is already installed before attempting installation.",
                "Prepare the assigned target_output_dir before downstream work items run, including any missing parent directories and baseline workspace folders.",
                "Produce a structured environment_manifest JSON as your final artifact.",
                "Include verification commands that downstream work items can use to validate the environment.",
                "Prefer system package managers (apt, brew, dnf) for system tools, pip/conda/uv for Python packages.",
                "When GPU is needed, check CUDA/ROCm availability and driver versions.",
                "For complex environments, create isolated envs (conda/venv) rather than polluting the host.",
            ],
        ),
    ], configured_roles)
