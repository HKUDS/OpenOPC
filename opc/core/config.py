"""Configuration management for OPC system."""

from __future__ import annotations

import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, Field, PrivateAttr, field_validator

from opc.core.company_tools import COMPANY_APPROVAL_EXEMPT_TOOL_NAMES


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def _find_project_root() -> Path:
    """Walk up from cwd looking for pyproject.toml to locate the project root."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    return cwd


def get_opc_home() -> Path:
    """Return the OPC data directory.

    Resolution order:
      1. $OPC_HOME environment variable (explicit override)
      2. {project_root}/.opc  (project-local, the default)
    """
    env = os.environ.get("OPC_HOME")
    if env:
        return Path(env)
    return _find_project_root() / ".opc"


def get_default_workplace_root() -> Path:
    """Return the default shared workplace root beside the OpenOPC repo."""
    project_root = _find_project_root()
    return project_root.parent / f"{project_root.name}_workplace"


def get_project_workplace(project_id: str) -> Path:
    project = str(project_id or "default").strip() or "default"
    return get_default_workplace_root() / project


def get_project_config_dir(project_path: Path | None = None) -> Path:
    if project_path:
        return project_path / ".opc"
    return get_opc_home()


def get_project_config_workspace(config_dir: Path) -> Path | None:
    """Return the workspace owning a project-local config directory, if any."""

    from opc.core.workspace_trust import project_workspace_for_config

    return project_workspace_for_config(
        config_dir,
        active_project_root=_find_project_root(),
    )


def _atomic_write_text(path: Path, content: str) -> None:
    """Write text via fsync and same-directory atomic replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as f:
            tmp_path = Path(f.name)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass
        raise


def _atomic_write_yaml(path: Path, data: Any) -> None:
    _atomic_write_text(path, yaml.dump(data, default_flow_style=False))


# ---------------------------------------------------------------------------
# Company organization config layout
# ---------------------------------------------------------------------------

COMPANY_INDEX_FILENAME = "company_index.yaml"
COMPANY_ORGS_DIRNAME = "company_orgs"
COMPANY_ORG_KIND = "opc_org_architecture"
COMPANY_INDEX_SCHEMA_VERSION = 1
COMPANY_ORG_SCHEMA_VERSION = 2
DEFAULT_ORGANIZATION_ID = "corporate"
_COMPANY_ORG_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_COMPANY_ORG_FILE_RE = re.compile(r"^(?:company|org)_([a-z0-9_-]{1,64})_config\.yaml$")
_ORG_STRUCTURE_KEYS = ("roles", "employees", "escalation_rules")
_ORG_RUNTIME_KEYS = (
    "runtime_policies",
    "talent_templates",
    "teams",
    "team_runtime",
    "external_team_bindings",
    "installed_packages",
    "role_serial_queue_enabled",
)
_ORG_BEARING_KEYS = (
    "company",
    *_ORG_STRUCTURE_KEYS,
    *_ORG_RUNTIME_KEYS,
)


def company_index_path(config_dir: Path) -> Path:
    return Path(config_dir) / COMPANY_INDEX_FILENAME


def company_orgs_dir(config_dir: Path) -> Path:
    return Path(config_dir) / COMPANY_ORGS_DIRNAME


def slugify_organization_name(name: Any, *, fallback: str = "org") -> str:
    """Return a safe file id from a user-facing organization name."""
    raw = str(name or "").strip()
    normalized = unicodedata.normalize("NFKD", raw)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_text.lower()
    lowered = re.sub(r"\s+", "_", lowered)
    lowered = re.sub(r"[^a-z0-9_-]+", "_", lowered)
    lowered = re.sub(r"_+", "_", lowered).strip("_-")
    if not lowered:
        lowered = str(fallback or "org").strip().lower()
        lowered = re.sub(r"[^a-z0-9_-]+", "_", lowered)
        lowered = re.sub(r"_+", "_", lowered).strip("_-") or "org"
    return lowered[:64].strip("_-") or "org"


def validate_organization_id(value: Any) -> str:
    org_id = str(value or "").strip()
    if not _COMPANY_ORG_ID_RE.match(org_id):
        raise ValueError(f"Invalid organization_id: {org_id!r}")
    return org_id


def company_org_filename(organization_id: Any) -> str:
    return f"org_{validate_organization_id(organization_id)}_config.yaml"


def organization_id_from_company_org_filename(path: Path) -> str | None:
    match = _COMPANY_ORG_FILE_RE.match(Path(path).name)
    return match.group(1) if match else None


def company_org_path(config_dir: Path, organization_id: Any) -> Path:
    return company_orgs_dir(config_dir) / company_org_filename(organization_id)


def company_org_relative_path(organization_id: Any) -> str:
    return f"{COMPANY_ORGS_DIRNAME}/{company_org_filename(organization_id)}"


def _read_yaml_file(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected YAML mapping in {path}")
    return data


def _write_yaml_preserving_unicode(path: Path, data: Any) -> None:
    _atomic_write_text(
        path,
        yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True),
    )


def read_company_index(config_dir: Path) -> str | None:
    path = company_index_path(config_dir)
    if not path.exists():
        return None
    data = _read_yaml_file(path)
    schema_version = int(data.get("schema_version", COMPANY_INDEX_SCHEMA_VERSION) or COMPANY_INDEX_SCHEMA_VERSION)
    if schema_version > COMPANY_INDEX_SCHEMA_VERSION:
        raise ValueError(
            f"{COMPANY_INDEX_FILENAME} schema_version {schema_version} is not supported by this version of OpenOPC"
        )
    active_id = data.get("active_organization_id") or DEFAULT_ORGANIZATION_ID
    return validate_organization_id(active_id)


def write_company_index(config_dir: Path, organization_id: Any) -> None:
    org_id = validate_organization_id(organization_id)
    _write_yaml_preserving_unicode(
        company_index_path(config_dir),
        {
            "schema_version": COMPANY_INDEX_SCHEMA_VERSION,
            "active_organization_id": org_id,
        },
    )


def write_company_org_payload(config_dir: Path, organization_id: Any, payload: dict[str, Any]) -> Path:
    org_id = validate_organization_id(organization_id)
    path = company_org_path(config_dir, org_id)
    payload = dict(payload or {})
    raw_employees = list(payload.get("employees", []) or [])
    if raw_employees:
        from opc.core.employee_registry import load_employee_registry, write_employee_registry

        opc_home = Path(config_dir).parent
        existing = load_employee_registry(opc_home, org_id)
        write_employee_registry(opc_home, org_id, [*existing, *raw_employees])
    payload["organization_id"] = org_id
    payload.setdefault("schema_version", COMPANY_ORG_SCHEMA_VERSION)
    payload.setdefault("kind", COMPANY_ORG_KIND)
    payload["employees"] = []
    payload["talent_templates"] = []
    payload["metadata"] = {
        **dict(payload.get("metadata", {}) or {}),
        "organization_config_file": company_org_relative_path(org_id),
    }
    _write_yaml_preserving_unicode(path, payload)
    return path


def list_company_org_config_paths(config_dir: Path) -> list[Path]:
    orgs_dir = company_orgs_dir(config_dir)
    if not orgs_dir.is_dir():
        return []
    return sorted(path for path in orgs_dir.glob("org_*_config.yaml") if organization_id_from_company_org_filename(path))


def allocate_organization_id(config_dir: Path, organization_name: Any, *, preferred_id: Any = "") -> str:
    base = str(preferred_id or "").strip()
    if base and _COMPANY_ORG_ID_RE.match(base):
        candidate = base
    else:
        candidate = slugify_organization_name(organization_name)
    existing = {
        org_id
        for path in list_company_org_config_paths(config_dir)
        for org_id in [organization_id_from_company_org_filename(path)]
        if org_id
    }
    if candidate not in existing:
        return candidate
    suffix = 2
    while True:
        tail = f"_{suffix}"
        stem = candidate[: max(1, 64 - len(tail))].rstrip("_-") or "org"
        next_id = f"{stem}{tail}"
        if next_id not in existing:
            return next_id
        suffix += 1


# ---------------------------------------------------------------------------
# Config Models
# ---------------------------------------------------------------------------

class LLMConfig(BaseModel):
    default_model: str = "anthropic/claude-sonnet-4-20250514"
    api_base: str = ""
    api_key: str = ""
    api_key_env: str = ""
    routing: dict[str, str] = Field(default_factory=dict)
    fallback: dict[str, Any] = Field(default_factory=dict)
    temperature: float = 0.3
    max_tokens: int = 32768
    reasoning_effort: str | None = None
    # Total input context window (tokens) for the active model. Set this when
    # the model is not mapped in litellm (e.g. proxy/self-hosted models like
    # doubao/minimax/glm), so the context-usage ring and compaction thresholds
    # have a real denominator. 0 = auto-detect via litellm; unmapped models
    # fall back to 128000. Optional per-model overrides keyed by model name
    # take precedence over the scalar value.
    context_window: int = 0
    context_window_overrides: dict[str, int] = Field(default_factory=dict)


ExternalAgentApprovalMode = Literal["user-settings", "auto", "full-auto"]
_EXTERNAL_AGENT_APPROVAL_MODES = {"user-settings", "auto", "full-auto"}
_LEGACY_EXTERNAL_AGENT_APPROVAL_MODE_MIGRATIONS = {
    "delegate": "auto",
    "bypass": "auto",
}
_LEGACY_OPENCODE_DEFAULT_MODEL = "opencode/minimax-m2.5-free"


def _migrate_agent_config_approval_modes(path: Path, data: Any) -> Any:
    """Rewrite pre-three-mode external-agent approval config once on load."""
    if not isinstance(data, dict):
        return data
    external_agents = data.get("external_agents")
    if not isinstance(external_agents, dict):
        return data

    changed = False
    for agent_name, agent_data in external_agents.items():
        if agent_name == "preferred_order" or not isinstance(agent_data, dict):
            continue
        raw_mode = agent_data.get("approval_mode")
        mode = str(raw_mode or "").strip().lower()
        if mode in _EXTERNAL_AGENT_APPROVAL_MODES:
            continue
        migrated = _LEGACY_EXTERNAL_AGENT_APPROVAL_MODE_MIGRATIONS.get(mode)
        if not migrated:
            continue
        agent_data["approval_mode"] = migrated
        changed = True

    if changed:
        _atomic_write_yaml(path, data)
    return data


DEFAULT_EXTERNAL_AGENT_STARTUP_TIMEOUT_SECONDS = 300


def _migrate_agent_config_external_agent_defaults(path: Path, data: Any) -> Any:
    """Repair legacy external-agent defaults that break local CLI config."""
    if not isinstance(data, dict):
        return data
    external_agents = data.get("external_agents")
    if not isinstance(external_agents, dict):
        return data

    changed = False
    opencode = external_agents.get("opencode")
    if isinstance(opencode, dict) and str(opencode.get("model") or "").strip() == _LEGACY_OPENCODE_DEFAULT_MODEL:
        opencode["model"] = ""
        changed = True

    if changed:
        _atomic_write_yaml(path, data)
    return data


class ExternalAgentConfig(BaseModel):
    enabled: bool = True
    command: str = ""
    workspace_base: str = ""
    extra_args: list[str] = Field(default_factory=list)
    model: str = ""
    model_flag: str = ""
    session_mode: str = "auto"
    session_id: str = ""
    new_session_flag: str = ""
    resume_session_flag: str = ""
    run_mode: str = "batch"
    interactive_timeout_seconds: int = 900
    idle_timeout_seconds: int = 900
    startup_timeout_seconds: int = DEFAULT_EXTERNAL_AGENT_STARTUP_TIMEOUT_SECONDS
    status_heartbeat_seconds: int = 30
    approval_mode: ExternalAgentApprovalMode = "auto"
    show_thinking: bool = False
    # Optional provider-specific transport/runtime settings.  They are kept
    # on the shared external-agent model so project-local YAML can configure
    # Jiuwen without importing Jiuwen/OpenJiuwen into the OpenOPC process.
    transport: Literal["cli", "gateway"] = "cli"
    gateway_url: str = ""
    provider_mode: str = ""
    project_dir: str = ""
    trusted_dirs: list[str] = Field(default_factory=list)
    agent_group_name: str = ""


class AgentsConfig(BaseModel):
    preferred_order: list[str] = Field(default_factory=lambda: ["claude_code", "cursor", "codex", "opencode", "jiuwen", "jiuwenswarm"])
    agents: dict[str, ExternalAgentConfig] = Field(default_factory=lambda: {
        "claude_code": ExternalAgentConfig(command="claude", run_mode="interactive", approval_mode="full-auto"),
        "cursor": ExternalAgentConfig(command="cursor-agent", run_mode="interactive", approval_mode="full-auto"),
        "codex": ExternalAgentConfig(command="codex", run_mode="interactive"),
        "opencode": ExternalAgentConfig(
            command="opencode",
            model_flag="--model",
            run_mode="interactive",
            approval_mode="full-auto",
            show_thinking=True,
        ),
        # ``jiuwen`` is a normal single external agent. ``jiuwenswarm`` is an
        # opaque execution team: OpenOPC owns one Task/WorkItem while Jiuwen
        # owns every internal teammate and subtask.
        "jiuwen": ExternalAgentConfig(
            command="jiuwenswarm",
            run_mode="interactive",
            transport="gateway",
            gateway_url="ws://127.0.0.1:19001/tui",
            provider_mode="code.normal",
            interactive_timeout_seconds=21600,
        ),
        "jiuwenswarm": ExternalAgentConfig(
            command="jiuwenswarm",
            run_mode="interactive",
            transport="gateway",
            gateway_url="ws://127.0.0.1:19001/tui",
            provider_mode="team",
            interactive_timeout_seconds=21600,
        ),
    })
    native_subagents: dict[str, "NativeSubagentProfileConfig"] = Field(default_factory=dict)


class RoleRuntimePolicyConfig(BaseModel):
    execution_strategy: str = "auto"
    allowed_downstream_roles: list[str] = Field(default_factory=list)
    review_role: str | None = None
    default_turn_type: str = "work"
    shell_timeout_override: int | None = None
    setup_env_type: str | None = None
    coordination_hints: dict[str, Any] = Field(default_factory=dict)
    signal_capabilities: list[str] = Field(default_factory=list)
    parallelism_constraints: list[str] = Field(default_factory=list)
    gate_preferences: dict[str, Any] = Field(default_factory=dict)


class CommunicationPolicyConfig(BaseModel):
    default_mode: str = "dm"
    blocking_default: bool = False
    meeting_required_for: list[str] = Field(default_factory=list)
    allow_broadcast: bool = True


class MemoryPolicyConfig(BaseModel):
    include_role_memory: bool = True
    include_project_memory: bool = True
    include_decision_log: bool = True
    include_artifact_index: bool = True
    recent_history_lines: int = 12


class HandoffPolicyConfig(BaseModel):
    require_structured_handoff: bool = True
    require_ack: bool = False
    include_risks: bool = True
    include_open_questions: bool = True


class ArtifactPolicyConfig(BaseModel):
    enforce_contract: bool = False
    require_artifact_index: bool = True
    required_kinds: list[str] = Field(default_factory=list)


class ReviewPolicyConfig(BaseModel):
    enable_work_item_gates: bool = False
    strict_gate_inference: bool = False
    require_reviewer_role: bool = True
    allow_human_override: bool = True


def _default_gate_harness_blocker_map() -> dict[str, str]:
    return {
        "workspace_mismatch": "rework_same_work_item",
        "missing_required_artifact": "rework_same_work_item",
        "verification_missing": "rework_same_work_item",
        "dependency_not_ready": "rework_same_work_item",
        "cross_work_item_conflict": "replan",
        "external_secret_missing": "await_user_decision",
        "permission_missing": "escalate",
        "environment_capability_gap": "pass_with_constraints",
        "quality_gap": "rework_same_work_item",
        "goal_env_mismatch": "replan",
        "unresolved_user_visible_risk": "pass_with_constraints",
    }


class GateHarnessPolicyConfig(BaseModel):
    enabled: bool = True
    decision_mode: Literal["rule_first", "agent_first", "hybrid"] = "agent_first"
    default_degrade_policy: Literal["allow", "strict", "replan_first"] = "allow"
    auto_infer_turn_kind: bool = True
    auto_infer_gate_profile: bool = True
    max_rework_rounds_per_issue: int = 2
    stagnation_threshold: int = 2
    allow_pass_with_constraints: bool = True
    enable_delivery_constraints_propagation: bool = True
    agent_model: str = ""
    agent_confidence_threshold: float = 0.55
    fallback_to_rules_on_parse_error: bool = True
    builtin_blocker_map: dict[str, str] = Field(default_factory=_default_gate_harness_blocker_map)
    turn_kind_overrides: dict[str, str] = Field(default_factory=dict)
    gate_profile_overrides: dict[str, str] = Field(default_factory=dict)


class ParallelPolicyConfig(BaseModel):
    auto_dispatch: bool = True
    review_gate_enabled: bool = True
    max_workers: int = 10


class CoordinationPolicyConfig(BaseModel):
    inference_mode: Literal["llm_primary", "rules_first"] = "llm_primary"
    fallback_mode: Literal["conservative", "balanced"] = "conservative"
    strict_gate_turn_kinds: list[str] = Field(default_factory=lambda: ["verify", "deliver"])
    mixed_gate_turn_kinds: list[str] = Field(default_factory=lambda: ["synthesize", "review", "integration"])
    allow_manager_release_for_mixed_only: bool = True
    allow_custom_signals: bool = True


class HeartbeatConfig(BaseModel):
    enabled: bool = False
    default_interval_sec: int = 300
    max_concurrent_runs: int = 1


class BrowserConfig(BaseModel):
    mode: Literal["embedded", "chrome", "auto"] = "embedded"
    headless: bool = True
    chrome_channel: str = "chrome"
    chrome_executable_path: str = ""
    user_data_dir: str = ""
    args: list[str] = Field(default_factory=list)


class TaskModeConfig(BaseModel):
    max_sub_agents: int = 8
    sub_agent_timeout_sec: int = 86400
    allow_parallel_dispatch: bool = True


ProjectModeConfig = TaskModeConfig


class PromptPrefixStabilityConfig(BaseModel):
    enabled: bool = True
    separate_dynamic_context: bool = True
    emit_cache_fingerprint_events: bool = True


class PromptHarnessConfig(BaseModel):
    enabled: bool = True
    split_static_dynamic: bool = True
    emit_delta_messages: bool = True
    cache_static_prefix_only: bool = True
    artifact_messages_enabled: bool = True
    section_dedup_enabled: bool = True
    reinject_after_compaction: bool = True


class ReactiveCompactionConfig(BaseModel):
    enabled: bool = True
    max_overflow_retries: int = 2
    circuit_breaker_failures: int = 2


class ContextUsageReportingConfig(BaseModel):
    enabled: bool = True
    emit_runtime_events: bool = True


class VerificationPolicyConfig(BaseModel):
    # Company mode already has its own manager-review/rework state machine.
    # Keep the optional extra verifier opt-in so a freshly-created config has
    # the same behaviour as the shipped system_config.yaml template.
    enabled: bool = False
    min_todos_for_verification: int = 3
    require_on_code_edits: bool = True
    require_on_risky_tools: bool = True
    verifier_profile: str = "verify"
    skip_metadata_key: str = "skip_verification"


class BackgroundSessionMemoryConfig(BaseModel):
    enabled: bool = True
    update_interval_messages: int = 4
    max_input_chars: int = 6_000


class PrefetchConfig(BaseModel):
    enabled: bool = True
    session_memory: bool = True
    focused_memory: bool = True
    skills_summary: bool = True
    project_memory_candidates: bool = True
    max_chars: int = 4_000


class ToolAwareMicrocompactConfig(BaseModel):
    enabled: bool = True
    preserve_recent_messages: int = 8
    tool_result_char_budget: int = 4_000
    assistant_char_budget: int = 3_000
    preserve_failure_outputs: bool = True


class TaskLedgerConfig(BaseModel):
    enabled: bool = True
    max_items: int = 24
    persist_to_runtime_session: bool = True
    persist_to_task_metadata: bool = True
    emit_runtime_events: bool = True


class StreamingToolStartConfig(BaseModel):
    enabled: bool = True
    safe_read_only_only: bool = True
    require_allow_prediction: bool = True


class StreamRenderingConfig(BaseModel):
    enabled: bool = True
    enter_depth: int = 8
    enter_age_ms: int = 120
    exit_depth: int = 2
    exit_age_ms: int = 40
    exit_hold_ms: int = 250
    reenter_hold_ms: int = 250
    tick_ms: int = 50


class ContextGuardConfig(BaseModel):
    enabled: bool = True
    # Below this usage ratio the history is never rewritten; at or above it
    # the runtime folds old messages into one LLM summary (durable compaction).
    hard_threshold: float = 0.90
    warn_remaining_pct: int = 15
    tool_output_char_budget: int = 12_000
    shell_stdout_char_budget: int = 12_000
    shell_stderr_char_budget: int = 6_000


class VerificationContractConfig(BaseModel):
    enabled: bool = True
    append_status_to_final: bool = True
    require_explicit_status: bool = True


class WorktreeVenvConfig(BaseModel):
    enabled: bool = False
    provider: Literal["auto", "uv", "venv"] = "auto"
    venv_dir: str = ".opc-venv"
    editable_project: bool = True
    requirements_files: list[str] = Field(default_factory=list)
    auto_detect_requirements: bool = True
    system_site_packages: bool = False
    fail_if_prepare_fails: bool = False


class SandboxPlatformConfig(BaseModel):
    mode: Literal["inherit", "off", "workspace-write", "elevated"] = "inherit"
    wrapper: Literal["auto", "none", "bwrap", "sandbox-exec"] = "auto"


class SandboxExecutionConfig(BaseModel):
    enabled: bool = False
    default_mode: Literal["off", "workspace-write", "elevated"] = "off"
    fail_if_unavailable: bool = False
    allow_direct_fallback: bool = True
    allow_network: bool = True
    windows: SandboxPlatformConfig = Field(
        default_factory=lambda: SandboxPlatformConfig(mode="elevated", wrapper="none")
    )
    linux: SandboxPlatformConfig = Field(
        default_factory=lambda: SandboxPlatformConfig(mode="workspace-write", wrapper="auto")
    )
    macos: SandboxPlatformConfig = Field(
        default_factory=lambda: SandboxPlatformConfig(mode="workspace-write", wrapper="auto")
    )


class ExecutionEnvironmentConfig(BaseModel):
    worktree_venv: WorktreeVenvConfig = Field(default_factory=WorktreeVenvConfig)
    sandbox: SandboxExecutionConfig = Field(default_factory=SandboxExecutionConfig)


class ArtifactCompactionConfig(BaseModel):
    enabled: bool = True
    session_memory_fast_path: bool = True
    reinject_tool_surface_delta: bool = True
    reinject_skills_delta: bool = True
    reinject_active_subagents: bool = True
    reinject_verification_state: bool = True
    reinject_permission_state: bool = True
    prompt_too_long_retry: bool = True
    max_prompt_too_long_retries: int = 3
    artifact_char_budget: int = 12_000


class NativeRuntimeConfig(BaseModel):
    enabled: bool = True
    stream_llm: bool = True
    emit_runtime_events: bool = True
    event_protocol_version: str = "v2"
    enable_tool_hooks: bool = True
    converge_on_parallel_failure: bool = True
    max_parallel_read_tools: int = 6
    tool_result_budget_chars: int = 20_000
    microcompact_chars: int = 8_000
    history_snip_trigger_messages: int = 40
    subagent_max_depth: int = 3
    auto_extract_durable_memory: bool = False
    durable_memory_extract_min_messages: int = 4
    durable_memory_max_input_chars: int = 12_000
    prompt_prefix_stability: PromptPrefixStabilityConfig = Field(default_factory=PromptPrefixStabilityConfig)
    prompt_harness: PromptHarnessConfig = Field(default_factory=PromptHarnessConfig)
    reactive_compaction: ReactiveCompactionConfig = Field(default_factory=ReactiveCompactionConfig)
    context_usage_reporting: ContextUsageReportingConfig = Field(default_factory=ContextUsageReportingConfig)
    verification_policy: VerificationPolicyConfig = Field(default_factory=VerificationPolicyConfig)
    background_session_memory: BackgroundSessionMemoryConfig = Field(default_factory=BackgroundSessionMemoryConfig)
    prefetch: PrefetchConfig = Field(default_factory=PrefetchConfig)
    tool_aware_microcompact: ToolAwareMicrocompactConfig = Field(default_factory=ToolAwareMicrocompactConfig)
    artifact_compaction: ArtifactCompactionConfig = Field(default_factory=ArtifactCompactionConfig)
    task_ledger: TaskLedgerConfig = Field(default_factory=TaskLedgerConfig)
    streaming_tool_start: StreamingToolStartConfig = Field(default_factory=StreamingToolStartConfig)
    stream_rendering: StreamRenderingConfig = Field(default_factory=StreamRenderingConfig)
    context_guard: ContextGuardConfig = Field(default_factory=ContextGuardConfig)
    verification_contract: VerificationContractConfig = Field(default_factory=VerificationContractConfig)
    execution_environment: ExecutionEnvironmentConfig = Field(default_factory=ExecutionEnvironmentConfig)


class NativeSubagentProfileConfig(BaseModel):
    enabled: bool = True
    model: str = ""
    max_iterations: int = 24
    default_isolation: Literal["shared", "worktree"] = "shared"
    background: bool = False
    allowed_tools: list[str] = Field(default_factory=list)


class DenialMemoryConfig(BaseModel):
    enabled: bool = True
    repeat_threshold: int = 2


class GuardianConfig(BaseModel):
    enabled: bool = True
    auto_allow_read_only: bool = True
    cache_upgrade_context: bool = True
    auto_retry_sandbox: bool = True
    max_sandbox_retries: int = 1


class PermissionsV2Config(BaseModel):
    """Runtime knobs for the unified permission predictor (ApprovalEngine.predict).

    Shell safe-command policy lives in ``autonomy.safe_command_prefixes`` plus
    the built-in flag-audited classifier (``shell_safety.py``); legacy
    duplicate fields (safe_shell_prefixes, classifier_*, sandbox_policy, ...)
    from the removed runtime-side resolver are ignored on load.
    """

    enabled: bool = True
    fail_closed: bool = True
    denial_memory: DenialMemoryConfig = Field(default_factory=DenialMemoryConfig)
    allow_tools: list[str] = Field(default_factory=list)
    deny_tools: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=list)
    denied_paths: list[str] = Field(default_factory=list)
    guardian: GuardianConfig = Field(default_factory=GuardianConfig)
    dangerous_shell_patterns: list[str] = Field(default_factory=lambda: [
        r"\brm\s+-rf\b",
        r"\bdrop\s+table\b",
        r"\btruncate\b",
        r"\bterraform\s+destroy\b",
        r"\bgit\s+push\s+--force\b",
    ])


def _default_native_subagents() -> dict[str, NativeSubagentProfileConfig]:
    return {
        "general": NativeSubagentProfileConfig(),
        "explore": NativeSubagentProfileConfig(default_isolation="shared"),
        "plan": NativeSubagentProfileConfig(default_isolation="shared"),
        "implement": NativeSubagentProfileConfig(default_isolation="worktree"),
        "verify": NativeSubagentProfileConfig(default_isolation="worktree", background=True),
    }


class RuntimePolicyConfig(BaseModel):
    communication: CommunicationPolicyConfig = Field(default_factory=CommunicationPolicyConfig)
    memory: MemoryPolicyConfig = Field(default_factory=MemoryPolicyConfig)
    handoff: HandoffPolicyConfig = Field(default_factory=HandoffPolicyConfig)
    artifact: ArtifactPolicyConfig = Field(default_factory=ArtifactPolicyConfig)
    review: ReviewPolicyConfig = Field(default_factory=ReviewPolicyConfig)
    gate_harness: GateHarnessPolicyConfig = Field(default_factory=GateHarnessPolicyConfig)
    # Only used within company-mode work-item runtime.
    parallel: ParallelPolicyConfig = Field(default_factory=ParallelPolicyConfig)
    coordination: CoordinationPolicyConfig = Field(default_factory=CoordinationPolicyConfig)


class CoordinatorPolicyConfig(BaseModel):
    synthesis_mode: str = "on_work_item_complete"  # "on_work_item_complete" | "on_inbox_threshold" | "periodic"
    inbox_threshold: int = 3
    auto_route: bool = True
    can_spawn_tasks: bool = True
    # Fraction of children that must be DONE before synthesize/deliver can start early
    partial_completion_threshold: float = 0.5
    # Auto-downgrade hard deps to soft after this many seconds of stall
    auto_downgrade_stall_seconds: int = 300
    # Force-advance after this many consecutive no-progress loop iterations
    max_stall_iterations: int = 3


class RoleConfig(BaseModel):
    id: str
    name: str
    responsibility: str
    reports_to: str = "owner"
    icon: str | None = None
    can_spawn: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    preferred_external_agent: str | None = None
    prompt_refs: list[str] = Field(default_factory=list)
    skill_refs: list[str] = Field(default_factory=list)
    handoff_template_ref: str | None = None
    memory_policy_ref: str | None = None
    artifact_contract_ref: str | None = None
    runtime_policy: RoleRuntimePolicyConfig = Field(default_factory=RoleRuntimePolicyConfig)
    capabilities: list[str] = Field(default_factory=list)
    role_type: str = "worker"  # "worker" | "coordinator" | "reviewer"
    coordinator_policy: CoordinatorPolicyConfig | None = None


class TalentTemplateConfig(BaseModel):
    id: str
    name: str
    description: str = ""
    category: str = ""
    domains: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    prompt_ref: str = ""
    preferred_external_agent: str | None = None
    source_repo: str = ""
    source_path: str = ""
    source_revision: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class EmployeeConfig(BaseModel):
    employee_id: str
    template_id: str = ""
    name: str
    role_id: str
    description: str = ""
    category: str = ""
    domains: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    prompt_refs: list[str] = Field(default_factory=list)
    skill_refs: list[str] = Field(default_factory=list)
    preferred_external_agent: str | None = None
    seniority: str = "junior"
    status: str = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class EscalationRule(BaseModel):
    condition: str
    action: str


class SeatConfig(BaseModel):
    seat_id: str
    name: str = ""
    role_id: str = ""
    seat_kind: str = "workspace"
    manager_seat_id: str | None = None
    manager_role_id: str | None = None
    shared_executor: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class TeamConfig(BaseModel):
    team_id: str
    name: str = ""
    description: str = ""
    seat_ids: list[str] = Field(default_factory=list)
    seats: list[SeatConfig] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TeamRuntimeConfig(BaseModel):
    default_team_id: str = ""
    shared_role_session_scope: str = "team"
    allow_shared_role_sessions: bool = True
    seat_refresh_interval_seconds: int = 30
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExternalTeamBindingConfig(BaseModel):
    """Bind one organization boundary (optionally its subtree) to one opaque team."""

    binding_id: str = ""
    boundary_role_id: str
    external_agent: str = "jiuwenswarm"
    scope: Literal["role", "subtree"] = "subtree"
    enabled: bool = True
    collapse_subtree: bool = True
    session_scope: Literal["work_item", "company_run"] = "company_run"
    max_inflight: int = Field(default=1, ge=1)
    failure_policy: Literal["fail_closed", "fallback_native"] = "fail_closed"
    review_owner_role_id: str = ""
    artifact_isolation: Literal["validated_workspace"] = "validated_workspace"
    provider_mode: str = "team"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("boundary_role_id")
    @classmethod
    def _validate_external_team_boundary(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("external team boundary_role_id must not be empty")
        return normalized

    @field_validator("external_agent")
    @classmethod
    def _validate_external_team_agent(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized != "jiuwenswarm":
            raise ValueError("opaque external team bindings require external_agent=jiuwenswarm")
        return normalized

    @field_validator("provider_mode")
    @classmethod
    def _validate_external_team_provider_mode(cls, value: str) -> str:
        normalized = str(value or "team").strip().lower() or "team"
        if "team" not in normalized.split("."):
            raise ValueError("opaque external team bindings require a Jiuwen team provider_mode")
        return normalized


class OrgConfig(BaseModel):
    organization_id: str = DEFAULT_ORGANIZATION_ID
    organization_name: str = "My One-Person Company"
    organization_config_file: str = ""
    company_name: str = "My One-Person Company"
    topology: str = "Corporate Structure"
    default_mode: str = "task"  # "task" or "company"
    company_profile: str = "corporate"
    execution_model: str = "actor_runtime"
    final_decider_role_id: str | None = None
    company_profiles: list[str] = Field(default_factory=lambda: ["corporate", "custom"])
    runtime_policies: dict[str, RuntimePolicyConfig] = Field(default_factory=dict)
    roles: list[RoleConfig] = Field(default_factory=list)
    talent_templates: list[TalentTemplateConfig] = Field(default_factory=list)
    employees: list[EmployeeConfig] = Field(default_factory=list)
    teams: list[TeamConfig] = Field(default_factory=list)
    team_runtime: TeamRuntimeConfig = Field(default_factory=TeamRuntimeConfig)
    external_team_bindings: list[ExternalTeamBindingConfig] = Field(default_factory=list)
    escalation_rules: list[EscalationRule] = Field(default_factory=list)
    installed_packages: list[Any] = Field(default_factory=list)
    # Fix 5 PR3 feature flag. When ON, runnable work items for a role
    # whose session is already focused on something else are appended to
    # ``role_runtime_session.pending_work_item_ids`` instead of being
    # claimed immediately. Default OFF for a safe rollout — behaviour
    # without the flag matches the pre-PR3 concurrent-claim model.
    role_serial_queue_enabled: bool = True

    @field_validator("default_mode", mode="before")
    @classmethod
    def _normalize_default_mode(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip().lower() == "project":
            return "task"
        return value


class MCPServerConfig(BaseModel):
    name: str = ""
    type: str = "local"  # "local" or "remote"
    command: list[str] = Field(default_factory=list)  # local only
    url: str = ""  # remote only
    headers: dict[str, str] = Field(default_factory=dict)  # remote only
    enabled: bool = True
    env: dict[str, str] = Field(default_factory=dict)
    tools_filter: list[str] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    startup_timeout: float = 30.0


class SystemConfig(BaseModel):
    opc_home: str = ""
    default_channel: str = "cli"
    log_level: str = "INFO"
    max_agent_iterations: int = 50
    context_compression_threshold: float = 0.85
    auto_approve_below_cost: float = 10.0
    require_confirmation: list[str] = Field(
        default_factory=lambda: ["deploy to production", "send external emails", "modify database schema"]
    )
    mcp_servers: list[MCPServerConfig] = Field(default_factory=list)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    native_runtime: NativeRuntimeConfig = Field(default_factory=NativeRuntimeConfig)
    task_mode: TaskModeConfig = Field(
        default_factory=TaskModeConfig,
        validation_alias=AliasChoices("task_mode", "project_mode"),
        serialization_alias="task_mode",
    )


class AutonomyConfig(BaseModel):
    enabled: bool = True
    mode: str = "bounded"
    approval_model: str = ""
    approval_confidence_threshold: float = 0.7
    learned_policy_threshold: float = 0.8
    max_auto_approve_risk: str = "medium"
    allow_native_tool_auto_approval: bool = True
    allow_external_agent_auto_approval: bool = False
    learn_from_feedback: bool = True
    save_external_sessions: bool = True
    tool_first_use_approval: bool = True
    tool_approval_exemptions: list[str] = Field(default_factory=lambda: [
        *COMPANY_APPROVAL_EXEMPT_TOOL_NAMES,
        "request_user_input",
        "todo_read",
        "todo_write",
    ])
    command_review_window: int = 20
    sensitive_keywords: list[str] = Field(default_factory=lambda: [
        "token", "password", "secret", "api key", "credential", "private key",
        "payment", "invoice", "wire transfer", "database schema", "drop table",
        "truncate", "delete from", "rm -rf", "terraform destroy", "deploy to production",
        "send email", "external email", "publish", "post to", "webhook",
    ])
    safe_command_prefixes: list[str] = Field(default_factory=lambda: [
        "ls", "pwd", "echo", "rg", "find", "git status", "git diff", "python -V",
        "python3 -V", "node -v", "npm -v", "curl", "wget", "yt-dlp", "aria2c", "ffmpeg",
        # Standalone read-only commands. Shell control structures always route
        # through an explicit permission checkpoint, regardless of prefix.
        "cd", "cat", "head", "tail", "grep", "wc", "sort", "uniq", "cut", "tr",
        "stat", "file", "which", "date", "du", "df", "tree", "basename", "dirname",
        "realpath", "readlink", "uname", "nproc", "whoami", "hostname", "git log",
        "git show", "git rev-parse",
    ])
    permissions_v2: PermissionsV2Config = Field(default_factory=PermissionsV2Config)


class SkillHubConfig(BaseModel):
    enabled: bool = False
    api_base: str = "https://www.skillhub.club/api/v1"
    api_key: str = ""
    api_key_env: str = "SKILLHUB_API_KEY"
    search_limit: int = 5
    method: str = "hybrid"
    cache_remote_skills: bool = True
    promote_after_successes: int = 2


class CapabilityConfig(BaseModel):
    enable_recovery: bool = True
    local_first: bool = True
    attach_remote_skill_summaries: bool = True
    promote_remote_skills: bool = True
    remote_skill_source: str = "skillhub"
    max_remote_skill_results: int = 5
    tool_failure_threshold: int = 2
    skillhub: SkillHubConfig = Field(default_factory=SkillHubConfig)


class BaseChannelConfig(BaseModel):
    enabled: bool = False
    allow_from: list[str] = Field(default_factory=list)


class TelegramChannelConfig(BaseChannelConfig):
    token: str = ""
    proxy: str | None = None
    reply_to_message: bool = False


class WhatsAppChannelConfig(BaseChannelConfig):
    bridge_url: str = "ws://localhost:3001"
    bridge_token: str = ""


class DiscordChannelConfig(BaseChannelConfig):
    token: str = ""
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 37377
    group_policy: str = "mention"


class FeishuChannelConfig(BaseChannelConfig):
    app_id: str = ""
    app_secret: str = ""
    encrypt_key: str = ""
    verification_token: str = ""
    react_emoji: str = "THUMBSUP"


class MochatMentionConfig(BaseModel):
    require_in_groups: bool = False


class MochatGroupRule(BaseModel):
    require_mention: bool = False


class MochatChannelConfig(BaseChannelConfig):
    base_url: str = "https://mochat.io"
    socket_url: str = ""
    socket_path: str = "/socket.io"
    socket_disable_msgpack: bool = False
    socket_reconnect_delay_ms: int = 1000
    socket_max_reconnect_delay_ms: int = 10000
    socket_connect_timeout_ms: int = 10000
    refresh_interval_ms: int = 30000
    watch_timeout_ms: int = 25000
    watch_limit: int = 100
    retry_delay_ms: int = 500
    max_retry_attempts: int = 0
    claw_token: str = ""
    agent_user_id: str = ""
    sessions: list[str] = Field(default_factory=list)
    panels: list[str] = Field(default_factory=list)
    mention: MochatMentionConfig = Field(default_factory=MochatMentionConfig)
    groups: dict[str, MochatGroupRule] = Field(default_factory=dict)
    reply_delay_mode: str = "non-mention"
    reply_delay_ms: int = 120000


class DingTalkChannelConfig(BaseChannelConfig):
    client_id: str = ""
    client_secret: str = ""


class EmailChannelConfig(BaseChannelConfig):
    consent_granted: bool = False
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"
    imap_use_ssl: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    from_address: str = ""
    auto_reply_enabled: bool = True
    poll_interval_seconds: int = 30
    mark_seen: bool = True
    max_body_chars: int = 12000
    subject_prefix: str = "Re: "


class SlackDMChannelConfig(BaseModel):
    enabled: bool = True
    policy: str = "open"
    allow_from: list[str] = Field(default_factory=list)


class SlackChannelConfig(BaseChannelConfig):
    mode: str = "socket"
    webhook_path: str = "/slack/events"
    bot_token: str = ""
    app_token: str = ""
    user_token_read_only: bool = True
    reply_in_thread: bool = True
    react_emoji: str = "eyes"
    group_policy: str = "mention"
    group_allow_from: list[str] = Field(default_factory=list)
    dm: SlackDMChannelConfig = Field(default_factory=SlackDMChannelConfig)


class QQChannelConfig(BaseChannelConfig):
    app_id: str = ""
    secret: str = ""


class MatrixChannelConfig(BaseChannelConfig):
    homeserver: str = "https://matrix.org"
    access_token: str = ""
    user_id: str = ""
    device_id: str = ""
    e2ee_enabled: bool = True
    sync_stop_grace_seconds: int = 2
    max_media_bytes: int = 20 * 1024 * 1024
    group_policy: str = "open"
    group_allow_from: list[str] = Field(default_factory=list)
    allow_room_mentions: bool = False


class ChannelsConfig(BaseModel):
    send_progress: bool = True
    send_tool_hints: bool = False
    telegram: TelegramChannelConfig = Field(default_factory=TelegramChannelConfig)
    whatsapp: WhatsAppChannelConfig = Field(default_factory=WhatsAppChannelConfig)
    discord: DiscordChannelConfig = Field(default_factory=DiscordChannelConfig)
    feishu: FeishuChannelConfig = Field(default_factory=FeishuChannelConfig)
    mochat: MochatChannelConfig = Field(default_factory=MochatChannelConfig)
    dingtalk: DingTalkChannelConfig = Field(default_factory=DingTalkChannelConfig)
    email: EmailChannelConfig = Field(default_factory=EmailChannelConfig)
    slack: SlackChannelConfig = Field(default_factory=SlackChannelConfig)
    qq: QQChannelConfig = Field(default_factory=QQChannelConfig)
    matrix: MatrixChannelConfig = Field(default_factory=MatrixChannelConfig)


def _dump_config_item(item: Any) -> Any:
    if hasattr(item, "model_dump"):
        return item.model_dump()
    if isinstance(item, dict):
        return dict(item)
    return item


def _dump_config_list(items: Any) -> list[Any]:
    return [_dump_config_item(item) for item in list(items or [])]


def _runtime_policy_payload_from_org(org: OrgConfig) -> dict[str, Any]:
    return {str(key): _dump_config_item(value) for key, value in dict(org.runtime_policies or {}).items()}


def _materialized_roles_for_org_payload(config: Any, profile: str) -> list[RoleConfig]:
    org = config.org
    if profile == "corporate":
        from opc.layer2_organization.company_runtime_profiles import get_builtin_roles

        return get_builtin_roles("corporate", configured_roles=list(org.roles or []))
    return list(org.roles or [])


def _infer_final_decider_role_id(roles: list[RoleConfig], profile: str, configured: Any) -> str | None:
    configured_id = str(configured or "").strip()
    if configured_id:
        return configured_id
    role_ids = {str(role.id or "").strip() for role in roles if str(role.id or "").strip()}
    if profile == "corporate" and "ceo" in role_ids:
        return "ceo"
    top_level_role_ids = [
        str(role.id or "").strip()
        for role in roles
        if str(role.id or "").strip()
        and (
            str(role.reports_to or "owner").strip() == "owner"
            or str(role.reports_to or "").strip() not in role_ids
        )
    ]
    top_level_role_ids = sorted(dict.fromkeys(top_level_role_ids))
    if len(top_level_role_ids) == 1:
        return top_level_role_ids[0]
    return None


def _effective_runtime_policy_payload(config: Any, profile: str) -> dict[str, Any]:
    try:
        from opc.layer2_organization.org_engine import OrgEngine

        policy_config = config
        current_profile = str(getattr(config.org, "company_profile", "") or "").strip()
        if current_profile != profile and hasattr(config, "model_copy"):
            policy_config = config.model_copy(deep=True)
            policy_config.org.company_profile = profile
        effective = OrgEngine(policy_config).get_runtime_policy(profile)
        if hasattr(effective, "model_dump"):
            effective = effective.model_dump()
        return dict(effective or {})
    except Exception:
        try:
            from opc.layer2_organization.company_runtime_profiles import get_builtin_runtime_policies

            policy = get_builtin_runtime_policies().get(profile)
            return policy.model_dump() if policy else {}
        except Exception:
            return {}


def _runtime_policy_payload_for_org(config: Any, profile: str) -> dict[str, Any]:
    policies = _runtime_policy_payload_from_org(config.org)
    policy_key = "corporate" if profile == "corporate" else "custom" if profile == "custom" else profile
    if policy_key:
        effective = _effective_runtime_policy_payload(config, policy_key)
        if effective:
            policies[policy_key] = effective
    return policies


def _should_keep_org_employee_payload(employee: Any) -> bool:
    if isinstance(employee, EmployeeConfig):
        metadata = dict(employee.metadata or {})
    elif isinstance(employee, dict):
        metadata = dict(employee.get("metadata") or {})
    else:
        return True
    if metadata.get("persist_to_org") or metadata.get("user_saved_default"):
        return True
    if metadata.get("employee_origin") in {"system_default", "recruitment_fallback"}:
        return False
    if metadata.get("auto_created_for_role") and (
        metadata.get("is_default_employee") or metadata.get("is_fallback_employee")
    ):
        return False
    return True


def _should_persist_org_employee(employee: EmployeeConfig) -> bool:
    return _should_keep_org_employee_payload(employee)


def _organization_identity_from_org(org: OrgConfig) -> tuple[str, str]:
    display_name = str(org.organization_name or org.company_name or "My One-Person Company").strip()
    profile = str(org.company_profile or "").strip().lower()
    raw_id = str(org.organization_id or "").strip()
    if raw_id and _COMPANY_ORG_ID_RE.match(raw_id):
        org_id = raw_id
    elif profile == "custom":
        org_id = slugify_organization_name(display_name)
    else:
        org_id = DEFAULT_ORGANIZATION_ID
    if profile == "custom" and org_id == DEFAULT_ORGANIZATION_ID and display_name:
        org_id = slugify_organization_name(display_name)
    return validate_organization_id(org_id), display_name


def build_company_org_payload_from_config(
    config: Any,
    *,
    organization_id: str | None = None,
    organization_name: str | None = None,
    force_profile: str | None = None,
) -> dict[str, Any]:
    org = config.org
    resolved_id, resolved_name = _organization_identity_from_org(org)
    org_id = validate_organization_id(organization_id or resolved_id)
    org_name = str(organization_name or resolved_name or org.company_name or org_id).strip()
    profile = force_profile if force_profile is not None else org.company_profile
    profile = str(profile or "").strip() or ("custom" if org_id != DEFAULT_ORGANIZATION_ID else "corporate")
    roles = _materialized_roles_for_org_payload(config, profile)
    final_decider_role_id = _infer_final_decider_role_id(roles, profile, org.final_decider_role_id)
    return {
        "schema_version": COMPANY_ORG_SCHEMA_VERSION,
        "kind": COMPANY_ORG_KIND,
        "organization_id": org_id,
        "organization_name": org_name,
        "company": {
            "name": org.company_name or org_name,
            "topology": org.topology,
            "company_profile": profile,
            "execution_model": org.execution_model,
            "final_decider_role_id": final_decider_role_id,
            "company_profiles": list(org.company_profiles),
        },
        "roles": [role.model_dump() for role in roles],
        # Employees are stored in .opc/company_state/<org>/employees/*.yaml.
        # Keep org config focused on organization structure; load treats this
        # field as legacy input only.
        "employees": [],
        "escalation_rules": [rule.model_dump() for rule in org.escalation_rules],
        "runtime_policies": _runtime_policy_payload_for_org(config, profile),
        # Talent templates live in .opc/prompts/talent/*.md and builtin presets.
        # Keep org configs focused on organization structure, not talent indexes.
        "talent_templates": [],
        "teams": [team.model_dump() for team in org.teams],
        "team_runtime": org.team_runtime.model_dump(),
        "external_team_bindings": [binding.model_dump() for binding in org.external_team_bindings],
        "installed_packages": _dump_config_list(org.installed_packages),
        "role_serial_queue_enabled": bool(org.role_serial_queue_enabled),
        "metadata": {
            "source": "opc_config",
            "organization_config_file": company_org_relative_path(org_id),
        },
    }


def _default_company_org_payload() -> dict[str, Any]:
    cfg = OPCConfig()
    return build_company_org_payload_from_config(
        cfg,
        organization_id=DEFAULT_ORGANIZATION_ID,
        organization_name=cfg.org.company_name,
        force_profile="corporate",
    )


def _validate_company_org_payload(path: Path, data: dict[str, Any]) -> dict[str, Any]:
    schema_version = int(data.get("schema_version", 1) or 1)
    if schema_version > COMPANY_ORG_SCHEMA_VERSION:
        raise ValueError(
            f"{path.name} schema_version {schema_version} is not supported by this version of OpenOPC"
        )
    kind = str(data.get("kind", "") or "").strip()
    if schema_version >= COMPANY_ORG_SCHEMA_VERSION and kind and kind != COMPANY_ORG_KIND:
        raise ValueError(f"Unsupported organization config kind in {path.name}: {kind}")
    data["schema_version"] = schema_version
    return data


def _company_org_payload_to_org_mapping(data: dict[str, Any], *, source_path: Path | None = None) -> dict[str, Any]:
    company = data.get("company") if isinstance(data.get("company"), dict) else {}
    raw_org_id = data.get("organization_id") or DEFAULT_ORGANIZATION_ID
    org_id = validate_organization_id(raw_org_id)
    org_name = str(data.get("organization_name") or company.get("name") or org_id).strip()
    company_name = str(company.get("name") or org_name or "My OPC").strip()
    org: dict[str, Any] = {
        "organization_id": org_id,
        "organization_name": org_name or company_name,
        "organization_config_file": str(source_path or company_org_relative_path(org_id)),
        "company_name": company_name,
        "topology": company.get("topology", ""),
        "company_profile": company.get("company_profile") or ("custom" if org_id != DEFAULT_ORGANIZATION_ID else "corporate"),
        "execution_model": company.get("execution_model") or "actor_runtime",
        "final_decider_role_id": company.get("final_decider_role_id"),
        "company_profiles": company.get("company_profiles") or ["corporate", "custom"],
    }
    for key in _ORG_STRUCTURE_KEYS:
        if key in data:
            value = data.get(key) or []
            if key == "employees" and isinstance(value, list):
                value = [item for item in value if _should_keep_org_employee_payload(item)]
            org[key] = value
    for key in _ORG_RUNTIME_KEYS:
        if key == "role_serial_queue_enabled":
            if key in data:
                org[key] = bool(data.get(key))
            continue
        if key == "talent_templates":
            continue
        if key in data:
            org[key] = data.get(key) or ({} if key in {"runtime_policies", "team_runtime"} else [])
    return org


def _legacy_corporate_config_candidates(config_dir: Path) -> list[Path]:
    candidates: list[Path] = []
    project_root_candidate = _find_project_root() / "config" / "company_corporate_config.yaml"
    local_candidate = Path(config_dir) / "company_corporate_config.yaml"
    candidate_order = [local_candidate]
    try:
        if Path(config_dir).resolve() == (get_opc_home() / "config").resolve():
            candidate_order.insert(0, project_root_candidate)
    except Exception:
        pass
    for path in candidate_order:
        if path not in candidates:
            candidates.append(path)
    return candidates


def _read_legacy_corporate_config(config_dir: Path) -> dict[str, Any]:
    for path in _legacy_corporate_config_candidates(config_dir):
        if not path.exists():
            continue
        data = _read_yaml_file(path)
        schema_ver = int(data.get("schema_version", 1) or 1)
        if schema_ver > 1:
            raise ValueError(
                f"company_corporate_config.yaml schema_version {schema_ver} is not supported by this version of OpenOPC"
            )
        return data
    return {}


def _read_legacy_org_runtime_config(config_dir: Path) -> dict[str, Any]:
    path = Path(config_dir) / "org_config.yaml"
    if not path.exists():
        return {}
    return _read_yaml_file(path)


def _legacy_company_org_payload(config_dir: Path) -> dict[str, Any]:
    corporate_data = _read_legacy_corporate_config(config_dir)
    org_runtime_data = _read_legacy_org_runtime_config(config_dir)
    if not corporate_data and not org_runtime_data:
        return _default_company_org_payload()

    company = dict(corporate_data.get("company", {}) or org_runtime_data.get("company", {}) or {})
    company_name = str(company.get("name") or "My One-Person Company").strip()
    profile = str(company.get("company_profile") or "").strip() or "corporate"
    org_id = (
        slugify_organization_name(company_name)
        if profile == "custom"
        else DEFAULT_ORGANIZATION_ID
    )
    payload: dict[str, Any] = {
        "schema_version": COMPANY_ORG_SCHEMA_VERSION,
        "kind": COMPANY_ORG_KIND,
        "organization_id": org_id,
        "organization_name": company_name,
        "company": {
            "name": company_name,
            "topology": company.get("topology", ""),
            "company_profile": profile,
            "execution_model": company.get("execution_model") or "actor_runtime",
            "final_decider_role_id": company.get("final_decider_role_id"),
            "company_profiles": company.get("company_profiles") or ["corporate", "custom"],
        },
        "roles": corporate_data.get("roles") or org_runtime_data.get("roles") or [],
        "employees": corporate_data.get("employees") or org_runtime_data.get("employees") or [],
        "escalation_rules": corporate_data.get("escalation_rules") or org_runtime_data.get("escalation_rules") or [],
        "runtime_policies": org_runtime_data.get("runtime_policies") or {},
        "talent_templates": [],
        "teams": org_runtime_data.get("teams") or [],
        "team_runtime": org_runtime_data.get("team_runtime") or {},
        "external_team_bindings": org_runtime_data.get("external_team_bindings") or [],
        "installed_packages": org_runtime_data.get("installed_packages") or [],
        "role_serial_queue_enabled": bool(org_runtime_data.get("role_serial_queue_enabled", True)),
        "metadata": {"source": "legacy_migration"},
    }
    return payload


def _normalize_corporate_company_org_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Return a corporate-scoped company org payload.

    Company mode is the built-in corporate runtime. User-defined orgs are stored
    under ``company_orgs/`` and should not become the process-wide company architecture
    just because an old ``company_index.yaml`` still points at them.
    """
    payload = dict(data or {})
    company = dict(payload.get("company", {}) or {})
    org_name = str(
        payload.get("organization_name")
        or company.get("name")
        or "My One-Person Company"
    ).strip()
    payload["organization_id"] = DEFAULT_ORGANIZATION_ID
    payload["organization_name"] = org_name
    company["name"] = str(company.get("name") or org_name).strip() or org_name
    company["company_profile"] = "corporate"
    company.setdefault("topology", "")
    company.setdefault("execution_model", "actor_runtime")
    company.setdefault("final_decider_role_id", None)
    company.setdefault("company_profiles", ["corporate", "custom"])
    payload["company"] = company
    payload.setdefault("roles", [])
    payload.setdefault("employees", [])
    payload.setdefault("escalation_rules", [])
    payload.setdefault("runtime_policies", {})
    payload["talent_templates"] = []
    payload.setdefault("teams", [])
    payload.setdefault("team_runtime", {})
    payload.setdefault("external_team_bindings", [])
    payload.setdefault("installed_packages", [])
    payload.setdefault("role_serial_queue_enabled", True)
    payload.setdefault("metadata", {})
    return payload


def _company_payload_profile(data: dict[str, Any]) -> str:
    company = data.get("company") if isinstance(data.get("company"), dict) else {}
    return str(company.get("company_profile") or "").strip().lower()


def _company_payload_with_org_storage_path(data: dict[str, Any], org_id: str) -> dict[str, Any]:
    payload = dict(data or {})
    payload["organization_id"] = validate_organization_id(org_id)
    payload.setdefault("schema_version", COMPANY_ORG_SCHEMA_VERSION)
    payload.setdefault("kind", COMPANY_ORG_KIND)
    payload["metadata"] = {
        **dict(payload.get("metadata", {}) or {}),
        "organization_config_file": company_org_relative_path(org_id),
    }
    return payload


def _company_org_payload_needs_externalization(data: dict[str, Any]) -> bool:
    return bool(data.get("employees") or data.get("talent_templates"))


def _repoint_org_index_if_active_corporate(config_dir: Path, archive_id: str) -> None:
    path = Path(config_dir) / "org_index.yaml"
    if not path.exists():
        return
    try:
        data = _read_yaml_file(path)
    except Exception:
        return
    active_id = str(data.get("active_organization_id") or "").strip()
    if active_id != DEFAULT_ORGANIZATION_ID:
        return
    _write_yaml_preserving_unicode(
        path,
        {
            "schema_version": int(data.get("schema_version", 1) or 1),
            "active_organization_id": archive_id,
        },
    )


def _archive_conflicting_corporate_org_config(config_dir: Path, target_path: Path) -> str | None:
    """Preserve a custom ``org_corporate`` file before writing corporate config.

    Older org-mode builds allowed a user-defined custom architecture to use the
    id ``corporate``. With unified ``company_orgs/`` storage, that filename is now
    reserved for the built-in corporate company profile. If we see the old custom
    file, keep it under a fresh id instead of overwriting it.
    """

    if not target_path.exists():
        return None
    try:
        existing = _validate_company_org_payload(target_path, _read_yaml_file(target_path))
    except Exception:
        return None
    if _company_payload_profile(existing) != "custom":
        return None

    company = dict(existing.get("company", {}) or {})
    org_name = str(
        existing.get("organization_name")
        or company.get("name")
        or "Corporate Custom"
    ).strip()
    archive_id = allocate_organization_id(
        config_dir,
        org_name,
        preferred_id=f"{DEFAULT_ORGANIZATION_ID}_custom",
    )
    archived = _company_payload_with_org_storage_path(existing, archive_id)
    archived["organization_name"] = org_name
    company["company_profile"] = "custom"
    archived["company"] = company
    archived.setdefault("metadata", {})["source"] = "archived_custom_corporate_conflict"
    write_company_org_payload(config_dir, archive_id, archived)
    _repoint_org_index_if_active_corporate(config_dir, archive_id)
    return archive_id


def _should_persist_company_migration(config_dir: Path) -> bool:
    try:
        return Path(config_dir).resolve() == (get_opc_home() / "config").resolve()
    except Exception:
        return False


def _migrate_legacy_saved_orgs(config_dir: Path) -> None:
    legacy_dir = _find_project_root() / "config" / "orgs"
    if not legacy_dir.is_dir():
        return
    for legacy_path in sorted(legacy_dir.glob("*.yaml")):
        try:
            data = _read_yaml_file(legacy_path)
            company = dict(data.get("company", {}) or {})
            org_name = str(
                data.get("organization_name")
                or company.get("name")
                or legacy_path.stem
            ).strip()
            raw_org_id = str(data.get("organization_id") or "").strip()
            org_id = raw_org_id if _COMPANY_ORG_ID_RE.match(raw_org_id) else slugify_organization_name(legacy_path.stem)
            target_path = company_org_path(config_dir, org_id)
            if target_path.exists():
                continue
            company.setdefault("name", org_name)
            company.setdefault("company_profile", "custom")
            data.update({
                "schema_version": COMPANY_ORG_SCHEMA_VERSION,
                "kind": COMPANY_ORG_KIND,
                "organization_id": org_id,
                "organization_name": org_name,
                "company": company,
            })
            data.setdefault("roles", [])
            data.setdefault("employees", [])
            data.setdefault("escalation_rules", [])
            data.setdefault("runtime_policies", {})
            data["talent_templates"] = []
            data.setdefault("teams", [])
            data.setdefault("team_runtime", {})
            data.setdefault("installed_packages", [])
            data.setdefault("role_serial_queue_enabled", True)
            data.setdefault("metadata", {})["source"] = "legacy_saved_org_migration"
            write_company_org_payload(config_dir, org_id, data)
        except Exception:
            continue


def load_company_org_payload(
    config_dir: Path,
    organization_id: Any = DEFAULT_ORGANIZATION_ID,
) -> tuple[dict[str, Any], Path]:
    """Load a specific company-mode organization payload.

    ``OPCConfig.load`` intentionally calls this with ``corporate`` instead of
    consulting ``company_index.yaml``. The active org index is owned by org mode.
    """
    config_dir = Path(config_dir)
    org_id = validate_organization_id(organization_id or DEFAULT_ORGANIZATION_ID)
    if _should_persist_company_migration(config_dir):
        _migrate_legacy_saved_orgs(config_dir)
    path = company_org_path(config_dir, org_id)
    if path.exists():
        payload = _validate_company_org_payload(path, _read_yaml_file(path))
        if org_id == DEFAULT_ORGANIZATION_ID:
            if _company_payload_profile(payload) == "custom":
                if _should_persist_company_migration(config_dir):
                    _archive_conflicting_corporate_org_config(config_dir, path)
                    payload = _normalize_corporate_company_org_payload(_legacy_company_org_payload(config_dir))
                    write_company_org_payload(config_dir, org_id, _company_payload_with_org_storage_path(payload, org_id))
                    return payload, path
                payload = _legacy_company_org_payload(config_dir)
            payload = _normalize_corporate_company_org_payload(payload)
        if _should_persist_company_migration(config_dir) and _company_org_payload_needs_externalization(payload):
            write_company_org_payload(config_dir, org_id, payload)
            payload = _validate_company_org_payload(path, _read_yaml_file(path))
        return payload, path
    if org_id != DEFAULT_ORGANIZATION_ID:
        raise FileNotFoundError(f"Company organization config does not exist: {path}")

    payload = _normalize_corporate_company_org_payload(_legacy_company_org_payload(config_dir))
    if _should_persist_company_migration(config_dir):
        write_company_org_payload(config_dir, org_id, payload)
        payload = _read_yaml_file(path)
    return _validate_company_org_payload(path, payload), path


def load_active_company_org_payload(config_dir: Path) -> tuple[dict[str, Any], Path]:
    config_dir = Path(config_dir)
    if _should_persist_company_migration(config_dir):
        _migrate_legacy_saved_orgs(config_dir)
    active_id = read_company_index(config_dir)
    if active_id:
        path = company_org_path(config_dir, active_id)
        if not path.exists():
            raise FileNotFoundError(f"Active organization config does not exist: {path}")
        payload = _validate_company_org_payload(path, _read_yaml_file(path))
        if _should_persist_company_migration(config_dir) and _company_org_payload_needs_externalization(payload):
            write_company_org_payload(config_dir, active_id, payload)
            payload = _validate_company_org_payload(path, _read_yaml_file(path))
        return payload, path

    payload = _legacy_company_org_payload(config_dir)
    org_id = validate_organization_id(payload.get("organization_id") or DEFAULT_ORGANIZATION_ID)
    path = company_org_path(config_dir, org_id)
    if _should_persist_company_migration(config_dir):
        write_company_org_payload(config_dir, org_id, payload)
        payload = _read_yaml_file(path)
    return _validate_company_org_payload(path, payload), path


class OPCConfig(BaseModel):
    system: SystemConfig = Field(default_factory=SystemConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    org: OrgConfig = Field(default_factory=OrgConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    autonomy: AutonomyConfig = Field(default_factory=AutonomyConfig)
    capabilities: CapabilityConfig = Field(default_factory=CapabilityConfig)
    _trusted_workspace: Path | None = PrivateAttr(default=None)
    _trusted_config_dir: Path | None = PrivateAttr(default=None)

    def bind_workspace_trust(self, workspace: Path, config_dir: Path) -> None:
        """Attach the project trust provenance used for pre-sink rechecks."""

        self._trusted_workspace = Path(workspace).resolve(strict=False)
        self._trusted_config_dir = Path(config_dir).resolve(strict=False)

    def require_workspace_trust(self, *, include_effective: bool = False) -> None:
        """Revalidate bound project authority immediately before runtime use.

        Normal runtime checks bind the source files while allowing explicit
        host-side CLI overrides such as ``--model`` and ``--debug``.  Loading
        and trust-grant flows additionally validate the normalized declaration.
        """

        if self._trusted_workspace is None or self._trusted_config_dir is None:
            return
        from opc.core.workspace_trust import WorkspaceTrustStore

        WorkspaceTrustStore().require(
            self._trusted_workspace,
            self._trusted_config_dir,
            self if include_effective else None,
        )

    @classmethod
    def load(
        cls,
        config_dir: Path | None = None,
        *,
        trusted_source: bool = False,
    ) -> "OPCConfig":
        if config_dir is None:
            config_dir = get_opc_home() / "config"
        config_dir = Path(config_dir)
        trusted_workspace: Path | None = None
        trust_store: Any | None = None

        # Project-local configuration can select executables, endpoints, and
        # credential sources.  Check its user-owned trust record before any
        # parsing or migration: several legacy migrations intentionally write
        # normalized config back to disk during load.
        if not trusted_source and config_dir.exists():
            workspace = get_project_config_workspace(config_dir)
            if workspace is not None:
                from opc.core.workspace_trust import WorkspaceTrustStore

                trust_store = WorkspaceTrustStore()
                trust_store.require(workspace, config_dir)
                trusted_workspace = workspace

        merged: dict[str, Any] = {}
        for name in ("system_config", "llm_config", "agent_config", "channel_config"):
            path = config_dir / f"{name}.yaml"
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                if name == "agent_config":
                    data = _migrate_agent_config_approval_modes(path, data)
                    data = _migrate_agent_config_external_agent_defaults(path, data)
                merged.update(data)

        org_payload, org_payload_path = load_company_org_payload(config_dir, DEFAULT_ORGANIZATION_ID)

        mapping = {}
        if "system" in merged:
            mapping["system"] = merged["system"]
        if "llm" in merged:
            mapping["llm"] = merged["llm"]
        if "external_agents" in merged:
            agents_data = merged["external_agents"]
            mapping["agents"] = {
                "preferred_order": agents_data.get("preferred_order", []),
                "agents": {
                    k: v for k, v in agents_data.items() if k != "preferred_order"
                },
            }
        if "native_subagents" in merged:
            agent_mapping = mapping.get("agents", {"preferred_order": [], "agents": {}})
            if isinstance(agent_mapping, dict):
                agent_mapping["native_subagents"] = merged["native_subagents"]
                mapping["agents"] = agent_mapping
        mapping["org"] = _company_org_payload_to_org_mapping(org_payload, source_path=org_payload_path)
        if "channels" in merged:
            mapping["channels"] = merged["channels"]
        if "autonomy" in merged:
            mapping["autonomy"] = merged["autonomy"]
        if "capabilities" in merged:
            mapping["capabilities"] = merged["capabilities"]
        if "mcp_servers" in merged:
            system_data = mapping.get("system", {})
            if isinstance(system_data, dict):
                system_data["mcp_servers"] = merged["mcp_servers"]
                mapping["system"] = system_data
        agent_mapping = mapping.get("agents")
        if isinstance(agent_mapping, dict) and "native_subagents" not in agent_mapping:
            agent_mapping["native_subagents"] = {
                key: value.model_dump()
                for key, value in _default_native_subagents().items()
            }
            mapping["agents"] = agent_mapping

        config = cls.model_validate(mapping)
        config.org.talent_templates = []
        try:
            from opc.core.employee_registry import load_company_employees

            organization_id, _ = _organization_identity_from_org(config.org)
            config.org.employees = load_company_employees(
                Path(config_dir).parent,
                organization_id,
                list(config.org.employees),
            )
        except Exception:
            pass
        if trust_store is not None and trusted_workspace is not None:
            # Recheck both the source bytes and normalized effective authority
            # after parsing.  This catches migrations and concurrent changes
            # before the config can reach an engine sink.
            trust_store.require(trusted_workspace, config_dir, config)
            config.bind_workspace_trust(trusted_workspace, config_dir)
        return config

    def save(self, config_dir: Path | None = None) -> None:
        if config_dir is None:
            config_dir = get_opc_home() / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        system_path = config_dir / "system_config.yaml"
        _atomic_write_yaml(system_path, {
            "system": self.system.model_dump(),
            "autonomy": self.autonomy.model_dump(),
            "capabilities": self.capabilities.model_dump(),
        })

        llm_path = config_dir / "llm_config.yaml"
        _atomic_write_yaml(llm_path, {"llm": self.llm.model_dump()})

        organization_id, organization_name = _organization_identity_from_org(self.org)
        from opc.core.employee_registry import write_employee_registry

        self.org.employees, _ = write_employee_registry(
            Path(config_dir).parent,
            organization_id,
            list(self.org.employees),
        )
        profile = str(self.org.company_profile or "").strip().lower()
        if profile == "custom":
            from opc.core.org_config import (
                build_org_config_payload_from_config,
                org_config_path,
                write_org_config_payload,
                write_org_index,
            )

            payload = build_org_config_payload_from_config(
                self,
                organization_id=organization_id,
                organization_name=organization_name,
            )
            custom_path = org_config_path(config_dir, organization_id)
            if not self.org.roles and custom_path.exists():
                try:
                    with open(custom_path, encoding="utf-8") as f:
                        existing = yaml.safe_load(f) or {}
                    existing_roles = existing.get("roles") or []
                    if existing_roles:
                        import logging
                        import os as _os

                        logging.getLogger(__name__).error(
                            "OPCConfig.save(): REFUSED to wipe %d existing roles with "
                            "empty list in custom mode. pid=%d, path=%s. "
                            "If this save was intentional, use reset_architecture.",
                            len(existing_roles), _os.getpid(), custom_path,
                        )
                        return
                except Exception:
                    pass
            write_org_config_payload(config_dir, organization_id, payload)
            write_org_index(config_dir, organization_id)
        else:
            corporate_data = build_company_org_payload_from_config(
                self,
                organization_id=organization_id,
                organization_name=organization_name,
            )
            if organization_id == DEFAULT_ORGANIZATION_ID:
                corporate_path = company_org_path(config_dir, organization_id)
                _archive_conflicting_corporate_org_config(config_dir, corporate_path)
            write_company_org_payload(config_dir, organization_id, corporate_data)

        agent_path = config_dir / "agent_config.yaml"
        agent_data = {
            "external_agents": {
                "preferred_order": self.agents.preferred_order,
                **{k: v.model_dump() for k, v in self.agents.agents.items()},
            },
            "native_subagents": {
                key: value.model_dump()
                for key, value in (self.agents.native_subagents or _default_native_subagents()).items()
            },
        }
        _atomic_write_yaml(agent_path, agent_data)

        channel_path = config_dir / "channel_config.yaml"
        _atomic_write_yaml(channel_path, {"channels": self.channels.model_dump()})


AgentsConfig.model_rebuild()
