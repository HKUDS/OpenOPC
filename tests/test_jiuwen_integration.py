from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from opc.core.config import ExternalAgentConfig, ExternalTeamBindingConfig, OPCConfig
from opc.core.execution_agents import EXECUTION_AGENTS, normalize_execution_agent
from opc.core.models import (
    DelegationWorkItem,
    ExecutionMode,
    RouterDecision,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.engine import OPCEngine
from opc.database.store import OPCStore
from opc.layer2_organization.company_mode import CompanyRuntimeSpec, CompanyWorkItemExecutor
from opc.layer2_organization.company_runtime_identity import requires_native_company_execution
from opc.layer2_organization.external_team_compiler import (
    apply_external_team_bindings_to_topology,
    compile_external_team_bindings,
    opaque_external_team_hidden_role_ids,
    runtime_execution_seats,
)
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.recruiter import (
    CompanyRecruiter,
    RECRUITMENT_AGENT_CHOICES,
    normalize_recruitment_agent_choice,
    resolve_effective_execution_agent,
)
from opc.layer3_agent.company_runtime_contract import build_company_work_item_contract
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer3_agent.adapters.jiuwen_adapter import JiuwenAdapter, JiuwenSwarmAdapter
from opc.layer3_agent.company_workspace_fence import (
    CompanyWorkspaceFenceError,
    capture_company_workspace,
    validate_company_workspace,
)
from opc.layer3_agent.external_broker import ExternalAgentBroker
from opc.layer3_agent.jiuwen_gateway_runner import _is_terminal
from opc.layer4_tools.collaboration import (
    _external_team_capability_match,
    _external_team_execution_metadata,
)
from opc.plugins.office_ui.snapshot_builder import build_snapshot


def _corporate_org(*bindings: ExternalTeamBindingConfig) -> OrgEngine:
    config = OPCConfig()
    config.org.company_profile = "corporate"
    config.org.external_team_bindings = list(bindings)
    return OrgEngine(config)


def test_default_config_exposes_both_jiuwen_execution_units() -> None:
    config = OPCConfig()
    assert {"jiuwen", "jiuwenswarm"}.issubset(config.agents.agents)
    assert config.agents.agents["jiuwen"].provider_mode == "code.normal"
    assert config.agents.agents["jiuwenswarm"].provider_mode == "team"

    normal = JiuwenAdapter(config=config.agents.agents["jiuwen"])
    team = JiuwenSwarmAdapter(config=config.agents.agents["jiuwenswarm"])
    assert normal.execution_unit_kind() == "external_agent"
    assert team.execution_unit_kind() == "opaque_external_team"
    assert normal.capabilities()["company_mode"] is True
    assert team.capabilities()["opaque_team"] is True
    assert normal.agent_isolation_home_slug() == "jiuwen"
    assert team.agent_isolation_home_slug() == "jiuwenswarm"


@pytest.mark.parametrize(
    "metadata",
    [
        {"execution_mode": "company_mode", "runtime_model": "multi_team_org"},
        {
            "execution_mode": "company_mode",
            "runtime_model": "multi_team_org",
            "execution_unit_kind": "opaque_external_team",
            "covered_role_ids": ["cto", "senior_engineer"],
        },
    ],
)
def test_company_contract_anchors_relative_time_for_native_and_team(
    metadata: dict[str, object],
) -> None:
    contract = build_company_work_item_contract(Task(title="Research", metadata=metadata))
    today = datetime.now().astimezone().date().isoformat()
    assert "## Runtime Clock" in contract
    assert f"Current local date: `{today}`" in contract
    assert "last three months" in contract


@pytest.mark.parametrize("agent", ["jiuwen", "jiuwenswarm"])
def test_jiuwen_identity_survives_every_shared_normalization_boundary(agent: str) -> None:
    assert RECRUITMENT_AGENT_CHOICES == EXECUTION_AGENTS
    assert normalize_execution_agent(agent) == agent
    assert normalize_recruitment_agent_choice(agent) == agent
    assert resolve_effective_execution_agent(agent) == (agent, agent, False)


@pytest.mark.parametrize(
    ("agent", "execution_unit_kind"),
    [
        ("jiuwen", "external_agent"),
        ("jiuwenswarm", "opaque_external_team"),
    ],
)
def test_company_topology_preserves_explicit_jiuwen_execution_unit(
    agent: str,
    execution_unit_kind: str,
) -> None:
    config = OPCConfig()
    org = OrgEngine(config)
    adapters = {
        "jiuwen": JiuwenAdapter(config=config.agents.agents["jiuwen"]),
        "jiuwenswarm": JiuwenSwarmAdapter(config=config.agents.agents["jiuwenswarm"]),
    }
    engine = OPCEngine(config=config, project_id="company-jiuwen")
    engine.org_engine = org
    engine.adapter_registry = SimpleNamespace(
        list_available=lambda: list(adapters),
        get=adapters.get,
    )

    topology = engine._enrich_runtime_delegation_topology(
        runtime_topology=org.build_runtime_delegation_topology(),
        decision=RouterDecision(
            mode=ExecutionMode.COMPANY_MODE,
            company_profile="corporate",
            domains=[],
            preferred_agent=agent,
        ),
        project_id="company-jiuwen",
    )

    dispatchable = [seat for seat in topology["seats"] if seat.get("dispatchable", True)]
    assert dispatchable
    assert all(seat["selected_execution_agent"] == agent for seat in dispatchable)
    assert all(seat["preferred_external_agent"] == agent for seat in dispatchable)
    assert all(seat["force_native_execution"] is False for seat in dispatchable)
    assert all(seat["company_external_execution_capable"] is True for seat in dispatchable)
    assert all(seat["execution_unit_kind"] == execution_unit_kind for seat in dispatchable)


@pytest.mark.parametrize(
    ("agent", "execution_unit_kind"),
    [
        ("jiuwen", "external_agent"),
        ("jiuwenswarm", "opaque_external_team"),
    ],
)
def test_company_root_work_item_keeps_jiuwen_workspace_fence(
    agent: str,
    execution_unit_kind: str,
) -> None:
    engine = OPCEngine(config=OPCConfig(), project_id="company-jiuwen")
    engine.store = SimpleNamespace(
        get_runtime_task_for_work_item=AsyncMock(return_value=None),
        save_delegation_work_item=AsyncMock(),
        save_task=AsyncMock(),
        link_work_item_runtime_task=AsyncMock(return_value=True),
    )
    engine.memory = SimpleNamespace(ensure_session=AsyncMock())
    engine.org_engine = SimpleNamespace(
        current_org_version=MagicMock(return_value=1),
        current_runtime_topology_version=MagicMock(return_value=1),
    )
    engine._requests_explicit_project_knowledge = MagicMock(return_value=False)
    work_item = DelegationWorkItem(
        work_item_id=f"wi-{agent}",
        run_id="run-jiuwen",
        cell_id="team::engineering",
        team_instance_id="team-instance-1",
        role_id="engineer",
        seat_id="seat-engineer",
        title="Engineering execution",
        summary="Implement the requested change.",
        kind="execute",
        projection_id="engineering-execute",
        metadata={
            "seat_id": "seat-engineer",
            "team_id": "team::engineering",
            "work_kind": "execute",
        },
    )

    task = asyncio.run(engine._ensure_runtime_work_item_task(
        work_item=work_item,
        parent_session_id="sess-company",
        original_message="Build the thing.",
        decision=RouterDecision(
            mode=ExecutionMode.COMPANY_MODE,
            domains=[],
            company_profile="corporate",
            preferred_agent=agent,
        ),
        runtime_topology={
            "final_decider_role_id": "lead",
            "seats": [{
                "seat_id": "seat-engineer",
                "team_id": "team::engineering",
                "role_id": "engineer",
                "preferred_external_agent": agent,
                "selected_execution_agent": agent,
                "execution_agent_locked": True,
                "company_external_execution_capable": True,
                "execution_unit_kind": execution_unit_kind,
                "employee_assignment": {"employee_id": "eng-1", "name": "Engineer"},
                "metadata": {"role_name": "Engineer"},
            }],
        },
        delegation_playbook={},
        secretary_context="",
        target_output_dir=None,
        origin_channel="cli",
        origin_chat_id="",
        origin_thread_id="",
        origin_task_id=None,
        attachment_refs=[],
        attachment_context="",
        force_native_execution=False,
    ))

    assert task.assigned_external_agent == agent
    assert task.metadata["selected_execution_agent"] == agent
    assert task.metadata["external_company_execution_allowed"] is True
    assert task.metadata["external_company_execution_fence"] == "validated_workspace"
    assert task.metadata["execution_unit_kind"] == execution_unit_kind
    assert task.metadata["force_native_execution"] is False
    assert requires_native_company_execution(task) is False
    selected = asyncio.run(engine._assign_task_execution_agent(task))
    assert selected == agent
    assert task.assigned_external_agent == agent
    assert task.metadata["agent_selection"]["selected"] == agent


def test_jiuwen_invocations_keep_single_and_team_modes_distinct(tmp_path: Path) -> None:
    task = Task(title="Ship", description="Do the work", project_id="p")
    normal = JiuwenAdapter(
        config=ExternalAgentConfig(
            command="jiuwenswarm",
            transport="gateway",
            provider_mode="code.normal",
            run_mode="interactive",
        )
    )
    team = JiuwenSwarmAdapter(
        config=ExternalAgentConfig(
            command="jiuwenswarm",
            transport="gateway",
            provider_mode="team",
            run_mode="interactive",
        )
    )

    normal_cmd, normal_meta = normal.build_invocation(task, str(tmp_path))
    team_cmd, team_meta = team.build_invocation(task, str(tmp_path))
    assert normal_cmd[normal_cmd.index("--mode") + 1] == "code.normal"
    assert team_cmd[team_cmd.index("--mode") + 1] == "team"
    assert normal_meta["execution_unit_kind"] == "external_agent"
    assert team_meta["execution_unit_kind"] == "opaque_external_team"
    assert normal.stdin_policy_for_process(normal_cmd, normal_meta) == "pipe_open"


def test_company_jiuwen_trust_surface_is_forced_to_workspace(tmp_path: Path) -> None:
    elsewhere = tmp_path.parent / "not-trusted"
    task = Task(
        title="Company work",
        assigned_external_agent="jiuwenswarm",
        metadata={
            "external_company_execution_allowed": True,
            "external_company_execution_fence": "validated_workspace",
            "execution_unit_kind": "opaque_external_team",
        },
    )
    adapter = JiuwenSwarmAdapter(
        config=ExternalAgentConfig(
            transport="gateway",
            project_dir=str(elsewhere),
            trusted_dirs=[str(elsewhere)],
            provider_mode="team",
        )
    )
    _cmd, metadata = adapter.build_invocation(task, str(tmp_path))
    assert metadata["project_dir"] == str(tmp_path.resolve())
    assert metadata["trusted_dirs"] == [str(tmp_path.resolve())]


def test_opaque_team_binding_collapses_cto_subtree_to_one_projection() -> None:
    binding = ExternalTeamBindingConfig(boundary_role_id="cto")
    org = _corporate_org(binding)
    raw_topology = org.build_runtime_delegation_topology()
    topology = apply_external_team_bindings_to_topology(org, raw_topology)
    plan = org.build_company_work_item_runtime_plan(
        "corporate",
        runtime_topology=topology,
        original_request="Build the product",
    )

    covered = {"cto", "senior_engineer", "devops_engineer", "env_engineer"}
    covered_specs = [projection for projection in plan.projections if projection.role_id in covered]
    assert len(covered_specs) == 1
    projection = covered_specs[0]
    assert projection.role_id == "cto"
    assert projection.preferred_external_agent == "jiuwenswarm"
    assert projection.metadata["execution_unit_kind"] == "opaque_external_team"
    assert set(projection.metadata["covered_role_ids"]) == covered
    assert projection.title == "CTO Team"
    assert "Jiuwen" not in projection.summary
    assert topology["external_execution_units"][0]["boundary_role_id"] == "cto"

    covered_seats = [seat for seat in topology["seats"] if seat.get("role_id") in covered]
    assert sum(bool(seat.get("dispatchable")) for seat in covered_seats) == 1


def test_external_team_bindings_reject_overlapping_subtrees() -> None:
    org = _corporate_org(
        ExternalTeamBindingConfig(boundary_role_id="cto"),
        ExternalTeamBindingConfig(boundary_role_id="senior_engineer"),
    )
    with pytest.raises(ValueError, match="overlap"):
        compile_external_team_bindings(
            org,
            runtime_topology=org.build_runtime_delegation_topology(),
        )

    provider_neutral = ExternalTeamBindingConfig(
        boundary_role_id="cto",
        external_agent="future_swarm",
        provider_mode="swarm",
    )
    assert provider_neutral.external_agent == "future_swarm"
    assert provider_neutral.provider_mode == "swarm"

    with pytest.raises(ValueError, match="non-empty external_agent"):
        ExternalTeamBindingConfig(boundary_role_id="cto", external_agent="")


def test_external_team_capability_manifest_is_compiled_from_live_org() -> None:
    binding = ExternalTeamBindingConfig(
        boundary_role_id="cto",
        metadata={
            "capabilities": ["release_governance"],
            "deliverables": ["verified_release"],
            "out_of_scope": ["brand_campaigns"],
        },
    )
    org = _corporate_org(binding)
    compiled = compile_external_team_bindings(
        org,
        runtime_topology=org.build_runtime_delegation_topology(),
    )[0]
    manifest = compiled.capability_manifest

    assert manifest["organizational_identity"] == "cto"
    assert {role["role_id"] for role in manifest["covered_roles"]} == {
        "cto",
        "senior_engineer",
        "devops_engineer",
        "env_engineer",
    }
    assert any(
        "architecture decisions" in role["responsibility"]
        for role in manifest["covered_roles"]
        if role["role_id"] == "cto"
    )
    assert "env_provisioning" in manifest["capabilities"]
    assert "technical_planning" in manifest["capabilities"]
    assert "software_implementation" in manifest["capabilities"]
    assert "deployment" in manifest["capabilities"]
    assert "release_governance" in manifest["capabilities"]
    assert manifest["deliverables"] == ["verified_release"]
    assert manifest["out_of_scope"] == ["brand_campaigns"]
    assert len(manifest["manifest_hash"]) == 64


def test_manager_contract_receives_team_routing_catalog_and_canonical_seat() -> None:
    binding = ExternalTeamBindingConfig(
        boundary_role_id="cto",
        metadata={"deliverables": ["source_code", "verification_report"]},
    )
    org = _corporate_org(binding)
    topology = apply_external_team_bindings_to_topology(
        org,
        org.build_runtime_delegation_topology(),
    )
    ceo_seat_id = "seat::team::ceo::ceo"
    target = org.resolve_runtime_target_seat(
        topology,
        from_seat_id=ceo_seat_id,
        target_role_id="cto",
    )
    assert target is not None
    assert target["seat_id"] == "seat::team::cto::cto"
    assert target["manager_seat_id"] == ceo_seat_id
    assert target["dispatchable"] is True
    assert target["allowed_delegate_role_ids"] == []
    assert target["direct_report_role_ids"] == []
    assert target["direct_report_seat_ids"] == []
    assert target["managed_team_id"] == ""
    assert not {
        "senior_engineer",
        "devops_engineer",
        "env_engineer",
    } & set(target["contact_role_ids"])

    execution_metadata = _external_team_execution_metadata(target)
    assert execution_metadata["selected_execution_agent"] == "jiuwenswarm"
    assert execution_metadata["execution_agent_locked"] is True
    assert execution_metadata["external_company_execution_allowed"] is True
    assert execution_metadata["external_company_execution_fence"] == "validated_workspace"
    assert execution_metadata["external_session_scope"] == "company_run"
    assert execution_metadata["external_provider_mode"] == "team"
    assert set(execution_metadata["covered_role_ids"]) == {
        "cto",
        "senior_engineer",
        "devops_engineer",
        "env_engineer",
    }

    task = Task(
        title="CEO intake",
        assigned_to="ceo",
        metadata={
            "runtime_model": "multi_team_org",
            "delegation_seat_id": ceo_seat_id,
            "allowed_delegate_role_ids": ["cto", "cmo", "coo"],
            "runtime_topology": topology,
        },
    )
    contract = build_company_work_item_contract(task)
    assert "## Delegation Capability Catalog" in contract
    assert "target_role_id=`cto`" in contract
    assert "source_code, verification_report" in contract
    assert "never delegate to the provider name" in contract


def test_three_c_level_teams_collapse_to_four_execution_roles() -> None:
    org = _corporate_org(
        ExternalTeamBindingConfig(boundary_role_id="cto"),
        ExternalTeamBindingConfig(boundary_role_id="cmo"),
        ExternalTeamBindingConfig(boundary_role_id="coo"),
    )
    topology = apply_external_team_bindings_to_topology(
        org,
        org.build_runtime_delegation_topology(),
    )

    execution_roles = {
        str(seat.get("role_id", "") or "")
        for seat in runtime_execution_seats(topology)
    }
    assert execution_roles == {"ceo", "cto", "cmo", "coo"}
    canonical_team_seats = {
        str(seat.get("role_id", "") or ""): seat
        for seat in runtime_execution_seats(topology)
        if str(seat.get("role_id", "") or "") in {"cto", "cmo", "coo"}
    }
    assert {
        str(seat.get("manager_seat_id", "") or "")
        for seat in canonical_team_seats.values()
    } == {"seat::team::ceo::ceo"}
    assert opaque_external_team_hidden_role_ids(org) == {
        "senior_engineer",
        "devops_engineer",
        "env_engineer",
        "content_specialist",
        "designer",
        "acquisition_specialist",
        "qa_analyst",
    }


def test_company_bootstrap_does_not_create_sessions_for_team_internal_roles(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        config = OPCConfig()
        config.org.company_profile = "corporate"
        config.org.external_team_bindings = [
            ExternalTeamBindingConfig(boundary_role_id="cto"),
            ExternalTeamBindingConfig(boundary_role_id="cmo"),
            ExternalTeamBindingConfig(boundary_role_id="coo"),
        ]
        org = OrgEngine(config, tmp_path)
        topology = apply_external_team_bindings_to_topology(
            org,
            org.build_runtime_delegation_topology(),
        )
        plan = org.build_company_work_item_runtime_plan(
            "corporate",
            runtime_topology=topology,
            original_request="Research recent agent progress",
        )
        store = OPCStore(tmp_path / "company.db")
        await store.initialize()
        try:
            engine = OPCEngine(
                config=config,
                opc_home=tmp_path,
                project_id="opaque-team-runtime",
            )
            engine.store = store
            engine.org_engine = org
            run_id, _ = await engine._bootstrap_runtime_delegation_run(
                session_id="opaque-team-session",
                project_id="opaque-team-runtime",
                runtime_spec=None,
                original_message="Research recent agent progress",
                runtime_topology=topology,
                work_item_plan=plan,
                delegation_playbook={},
                target_output_dir=None,
                comms_workspace_root="",
                force_native_execution=False,
            )

            seat_states = await store.list_delegation_seat_states(run_id)
            role_sessions = await store.list_delegation_role_sessions(run_id)
            assert {seat.role_id for seat in seat_states} == {"ceo", "cto", "cmo", "coo"}
            assert {session.role_id for session in role_sessions} == {
                "ceo",
                "cto",
                "cmo",
                "coo",
            }
        finally:
            await store.close()

    asyncio.run(run())


def test_office_snapshot_hides_provider_internal_team_roles() -> None:
    async def run() -> None:
        org = _corporate_org(
            ExternalTeamBindingConfig(boundary_role_id="cto"),
            ExternalTeamBindingConfig(boundary_role_id="cmo"),
            ExternalTeamBindingConfig(boundary_role_id="coo"),
        )
        agents = [
            {
                "agent_id": role.role_id,
                "opc_role_id": role.role_id,
                "name": role.name,
                "status": "idle",
            }
            for role in org.list_agents()
        ]
        templates = [
            {"id": role.role_id, "label": role.name}
            for role in org.list_agents()
        ]
        agent_store = SimpleNamespace(
            get_all=AsyncMock(return_value=agents),
            get_templates=AsyncMock(return_value=templates),
        )
        engine = SimpleNamespace(
            project_id="opaque-team-snapshot",
            org_engine=org,
            skills=None,
            event_bus=SimpleNamespace(get_history=lambda limit=50: []),
        )

        snapshot = await build_snapshot(engine, agent_store, None, None)

        assert set(snapshot["agents"]) == {"ceo", "cto", "cmo", "coo"}
        assert {template["id"] for template in snapshot["agent_templates"]} == {
            "ceo",
            "cto",
            "cmo",
            "coo",
        }

    asyncio.run(run())


def test_external_team_work_item_capability_check_is_auditable_and_non_blocking() -> None:
    org = _corporate_org(
        ExternalTeamBindingConfig(
            boundary_role_id="cto",
            metadata={"capabilities": ["architecture_design", "code_review"]},
        )
    )
    topology = apply_external_team_bindings_to_topology(
        org,
        org.build_runtime_delegation_topology(),
    )
    target = org.resolve_runtime_target_seat(
        topology,
        from_seat_id="seat::team::ceo::ceo",
        target_role_id="cto",
    )
    assert target is not None

    result = _external_team_capability_match(
        target,
        ["architecture-design", "brand_campaigns"],
    )

    assert result["status"] == "partially_matched"
    assert result["matched_capabilities"] == ["architecture-design"]
    assert result["undeclared_capabilities"] == ["brand_campaigns"]
    assert "Dispatch remains allowed" in result["warning"]
    assert len(result["manifest_hash"]) == 64


def test_opaque_team_roles_are_not_recruited_separately() -> None:
    org = _corporate_org(ExternalTeamBindingConfig(boundary_role_id="cto"))
    recruiter = CompanyRecruiter(llm=None, org_engine=org, talent_market=None)
    needs = recruiter._collect_needs(SimpleNamespace(metadata={}))
    recruited_role_ids = {need.role_id for need in needs}
    assert "ceo" in recruited_role_ids
    assert "cmo" in recruited_role_ids
    assert not {
        "cto",
        "senior_engineer",
        "devops_engineer",
        "env_engineer",
    } & recruited_role_ids


def test_manual_staffing_exposes_one_locked_team_boundary() -> None:
    binding = ExternalTeamBindingConfig(boundary_role_id="cto")
    config = OPCConfig()
    config.org.company_profile = "corporate"
    config.org.external_team_bindings = [binding]
    org = OrgEngine(config)
    engine = OPCEngine(config=config, project_id="company-team-staffing")
    engine.org_engine = org
    engine.talent_market = SimpleNamespace(list_available_templates=lambda: [])
    engine.adapter_registry = SimpleNamespace(
        list_available=lambda: ["jiuwenswarm"],
    )
    engine._load_project_company_staffing_defaults = lambda *args, **kwargs: {}
    payload = engine._build_manual_staffing_checkpoint_payload(
        RouterDecision(
            mode=ExecutionMode.COMPANY_MODE,
            company_profile="corporate",
            domains=[],
        ),
        "Build the product",
        CompanyRuntimeSpec(profile="corporate", original_request="Build the product"),
        session_id="staffing-session",
        origin_channel="ui",
        origin_chat_id="",
        origin_thread_id="",
    )
    assert payload is not None
    roles = {role["role_id"]: role for role in payload["staffing_roles"]}
    assert roles["cto"]["staffing_locked"] is True
    assert roles["cto"]["selected_agent"] == "jiuwenswarm"
    assert set(roles["cto"]["covered_role_ids"]) == {
        "cto",
        "senior_engineer",
        "devops_engineer",
        "env_engineer",
    }
    assert "senior_engineer" not in roles
    assert "devops_engineer" not in roles
    assert "env_engineer" not in roles
    assert "require no separate hires" in payload["summary"]


def test_company_global_jiuwenswarm_defaults_to_top_level_team_only() -> None:
    config = OPCConfig()
    config.org.company_profile = "corporate"
    engine = OPCEngine(config=config, project_id="company-global-team")
    engine.org_engine = OrgEngine(config)
    engine.talent_market = SimpleNamespace(list_available_templates=lambda: [])
    engine.adapter_registry = SimpleNamespace(list_available=lambda: ["jiuwenswarm"])
    engine._load_project_company_staffing_defaults = lambda *args, **kwargs: {}

    payload = engine._build_manual_staffing_checkpoint_payload(
        RouterDecision(
            mode=ExecutionMode.COMPANY_MODE,
            company_profile="corporate",
            preferred_agent="jiuwenswarm",
            domains=[],
        ),
        "Research agent technology",
        CompanyRuntimeSpec(profile="corporate", original_request="Research agent technology"),
        session_id="global-team-session",
        origin_channel="ui",
        origin_chat_id="",
        origin_thread_id="",
    )

    assert payload is not None
    roles = {role["role_id"]: role for role in payload["staffing_roles"]}
    assert roles["ceo"]["selected_agent"] == "jiuwenswarm"
    assert roles["cto"]["selected_agent"] == "native"
    assert roles["cmo"]["selected_agent"] == "native"
    assert roles["cto"]["reports_to"] == "ceo"


def test_staffing_team_selection_is_run_scoped_and_filters_descendants(tmp_path: Path) -> None:
    config = OPCConfig()
    config.org.company_profile = "corporate"
    engine = OPCEngine(
        config=config,
        opc_home=tmp_path,
        project_id="company-staffing-team-selection",
    )
    engine.org_engine = OrgEngine(config)
    payload = {
        "staffing_roles": [
            {"role_id": agent.role_id, "reports_to": agent.reports_to, "selected_agent": "native"}
            for agent in engine.org_engine.list_agents()
        ],
    }
    reply = {
        "recruitment_role_agents": {
            "cmo": "jiuwenswarm",
            "content_specialist": "codex",
            "designer": "claude_code",
        }
    }

    compiled, role_agents = engine._apply_staffing_external_team_bindings(payload, reply)
    covered, filtered_agents = engine._filter_staffing_for_external_teams(compiled, role_agents)

    assert [binding.boundary_role_id for binding in compiled] == ["cmo"]
    assert config.org.external_team_bindings == []
    assert {"cmo", "content_specialist", "designer"}.issubset(covered)
    assert filtered_agents["cmo"] == "jiuwenswarm"
    assert "content_specialist" not in filtered_agents
    assert "designer" not in filtered_agents

    topology = apply_external_team_bindings_to_topology(
        engine.org_engine,
        engine.org_engine.build_runtime_delegation_topology(),
        compiled_bindings=compiled,
    )
    plan = engine.org_engine.build_company_work_item_runtime_plan(
        "corporate",
        runtime_topology=topology,
        original_request="Research the market",
        compiled_external_teams=compiled,
    )
    assert {projection.role_id for projection in plan.projections}.isdisjoint(
        {"content_specialist", "designer"}
    )
    assert next(
        projection for projection in plan.projections if projection.role_id == "cmo"
    ).metadata["execution_unit_kind"] == "opaque_external_team"


def test_prior_run_team_binding_does_not_lock_new_staffing_checkpoint() -> None:
    config = OPCConfig()
    config.org.company_profile = "corporate"
    config.org.external_team_bindings = [
        ExternalTeamBindingConfig(
            boundary_role_id="cmo",
            metadata={"source": "company_staffing_selection"},
        )
    ]
    engine = OPCEngine(config=config, project_id="new-project")
    engine.org_engine = OrgEngine(config)
    engine.talent_market = SimpleNamespace(list_available_templates=lambda: [])
    engine.adapter_registry = SimpleNamespace(list_available=lambda: ["jiuwenswarm"])
    engine._load_project_company_staffing_defaults = lambda *args, **kwargs: {}

    payload = engine._build_manual_staffing_checkpoint_payload(
        RouterDecision(
            mode=ExecutionMode.COMPANY_MODE,
            company_profile="corporate",
            domains=[],
        ),
        "Research the market",
        CompanyRuntimeSpec(profile="corporate", original_request="Research the market"),
        session_id="new-session",
        origin_channel="ui",
        origin_chat_id="",
        origin_thread_id="",
    )

    assert payload is not None
    roles = {role["role_id"]: role for role in payload["staffing_roles"]}
    assert {"cmo", "content_specialist", "designer"}.issubset(roles)
    assert roles["cmo"].get("staffing_locked") is not True
    assert roles["cmo"]["selected_agent"] == "native"


def test_opaque_team_contract_includes_full_compiled_responsibilities() -> None:
    org = _corporate_org(ExternalTeamBindingConfig(boundary_role_id="cto"))
    topology = apply_external_team_bindings_to_topology(
        org,
        org.build_runtime_delegation_topology(),
    )
    plan = org.build_company_work_item_runtime_plan(
        "corporate",
        runtime_topology=topology,
        original_request="Ship the product",
    )
    projection = next(item for item in plan.projections if item.role_id == "cto")
    task = Task(
        title=projection.title,
        assigned_to="cto",
        metadata={
            "runtime_model": "multi_team_org",
            **projection.metadata,
        },
    )
    contract = build_company_work_item_contract(task, audience="external")
    assert "## OpenOPC WorkItem" in contract
    assert "Final response: one JSON object" in contract
    assert "Jiuwen" not in contract
    assert "Opaque External Team" not in contract
    assert "## Team Capability Manifest" in contract
    assert "### Covered Role Responsibilities" in contract
    assert "`senior_engineer` (Senior Engineer)" in contract
    assert "Code implementation, system development" in contract
    assert "env_provisioning" in contract


def test_external_team_work_item_contract_is_provider_neutral() -> None:
    org = _corporate_org(
        ExternalTeamBindingConfig(
            boundary_role_id="cto",
            external_agent="future_swarm",
            provider_mode="swarm",
        )
    )
    topology = apply_external_team_bindings_to_topology(
        org,
        org.build_runtime_delegation_topology(),
    )
    plan = org.build_company_work_item_runtime_plan(
        "corporate",
        runtime_topology=topology,
        original_request="Ship the product",
    )
    projection = next(item for item in plan.projections if item.role_id == "cto")
    task = Task(
        title=projection.title,
        assigned_to="cto",
        metadata={"runtime_model": "multi_team_org", **projection.metadata},
    )

    contract = build_company_work_item_contract(task, audience="external")

    assert projection.preferred_external_agent == "future_swarm"
    assert projection.metadata["external_provider_mode"] == "swarm"
    assert projection.title == "CTO Team"
    assert "future_swarm" not in contract
    assert "Jiuwen" not in contract
    assert "Opaque External Team" not in contract

def _team_envelope(**updates: object) -> str:
    payload: dict[str, object] = {
        "work_item_id": "wi-1",
        "attempt_id": "2",
        "status": "completed",
        "summary": "Implemented and verified",
        "deliverables": [{"name": "module", "path": "src/module.py", "status": "complete"}],
        "verification": {
            "verdict": "pass",
            "summary": "tests passed",
            "checks": [{"check": "pytest", "command": "pytest", "result": "PASS"}],
        },
        "risks": [],
        "open_questions": [],
        "handoff": {"next": "review"},
    }
    payload.update(updates)
    return json.dumps(payload)


def test_team_result_contract_validates_identity_and_maps_artifacts() -> None:
    adapter = JiuwenSwarmAdapter()
    task = Task(
        title="Team boundary",
        linked_work_item_id="wi-1",
        metadata={
            "execution_unit_kind": "opaque_external_team",
            "execution_mode": "company_mode",
            "runtime_model": "multi_team_org",
            "claimed_work_item_attempt_seq": 2,
        },
    )
    output = _team_envelope()
    assert adapter.validate_result_output(output, task) is None
    structured = adapter.extract_structured_result_fields(output)
    assert structured["opaque_external_team_result"]["handoff"] == {"next": "review"}
    assert structured["work_item_artifact_index"] == [
        {"kind": "deliverable", "label": "module", "value": "src/module.py"}
    ]
    assert structured["verification_evidence"]["verdict"] == "pass"

    # Jiuwen uses JSON null when a completed external-team boundary has no
    # downstream recipient.  This is a valid terminal handoff and must not
    # cause OpenOPC to retry the entire provider execution.
    assert adapter.validate_result_output(_team_envelope(handoff=None), task) is None
    assert adapter.validate_result_output(
        _team_envelope(handoff=[{"next": "review"}]),
        task,
    ) is None

    # Jiuwen leaders also emit a grouped deliverable object and prose/null for
    # semantically plural fields.  Normalize those shapes instead of retrying
    # an already completed external Team execution.
    grouped_output = _team_envelope(
        deliverables={
            "directory_path": "/workspace/research/",
            "files": ["sources.md", "events.md"],
        },
        verification={"requirements_met": True, "sources_count": 44},
        risks="No blocking risk",
        open_questions=None,
    )
    assert adapter.validate_result_output(grouped_output, task) is None
    grouped = adapter.extract_structured_result_fields(grouped_output)
    assert grouped["opaque_external_team_result"]["risks"] == ["No blocking risk"]
    assert grouped["opaque_external_team_result"]["open_questions"] == []
    assert grouped["verification_evidence"]["verdict"] == "pass"
    assert grouped["work_item_artifact_index"] == [
        {"kind": "directory", "label": "deliverable", "value": "/workspace/research/"},
        {"kind": "deliverable", "label": "deliverable", "value": "sources.md"},
        {"kind": "deliverable", "label": "deliverable", "value": "events.md"},
    ]

    # Team follow-up turns can include an unescaped quoted phrase inside an
    # otherwise complete JSON envelope.  Repair that transport defect at the
    # Jiuwen boundary instead of discarding the provider session and running
    # the whole work item again.
    malformed_followup = (
        "返工已完成："
        + _team_envelope(
            verification=['已标注"未公开"并完成检查'],
        ).replace(r'\"未公开\"', '"未公开"')
    )
    assert adapter.validate_result_output(malformed_followup, task) is None
    repaired_followup = adapter.extract_structured_result_fields(malformed_followup)
    assert repaired_followup["opaque_external_team_result"]["verification"] == [
        '已标注"未公开"并完成检查'
    ]

    # Jiuwen also emits a concise verification sentence (and occasionally
    # null when verification is not applicable).  These are transport-level
    # variants; evidence sufficiency is decided by the WorkItem gate.
    prose_verification = "检查确认交付文件已落盘并通过验收。"
    assert adapter.validate_result_output(
        _team_envelope(verification=prose_verification),
        task,
    ) is None
    assert adapter.validate_result_output(_team_envelope(verification=None), task) is None
    prose_structured = adapter.extract_structured_result_fields(
        _team_envelope(verification=prose_verification)
    )
    assert prose_structured["verification_evidence"]["verdict"] == "pass"

    checklist_structured = adapter.extract_structured_result_fields(
        _team_envelope(
            verification={
                "verification_status": "all_passed",
                "checklist": [{"item": "artifact exists", "status": "pass"}],
            }
        )
    )
    assert checklist_structured["verification_evidence"]["verdict"] == "pass"
    assert checklist_structured["verification_evidence"]["checks"] == [
        {"item": "artifact exists", "status": "pass"}
    ]

    assert "work_item_id mismatch" in str(
        adapter.validate_result_output(_team_envelope(work_item_id="wrong"), task)
    )
    assert "attempt_id mismatch" in str(
        adapter.validate_result_output(_team_envelope(attempt_id="3"), task)
    )
    task.metadata["execution_mode"] = "task_mode"
    task.metadata["runtime_model"] = "single_agent"
    assert adapter.validate_result_output("ordinary task-mode team answer", task) is None


def test_team_result_business_fields_are_owned_by_work_item() -> None:
    executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
    task = Task(
        title="Team boundary",
        metadata={
            "work_item_projection_id": "wi-1",
            "work_item_turn_type": "execute",
            "work_kind": "execute",
        },
    )
    set_linked_work_item_id(task, "wi-1")
    envelope = json.loads(
        _team_envelope(
            risks=["staged rollout required"],
            open_questions=["who owns rollout?"],
        )
    )
    bundle = executor._capture_work_item_outputs(
        task,
        TaskResult(
            status=TaskStatus.DONE,
            content=json.dumps(envelope),
            artifacts={
                "opaque_external_team_result": envelope,
                "work_item_artifact_index": [
                    {"kind": "deliverable", "label": "module", "value": "src/module.py"}
                ],
            },
        ),
    )
    assert bundle.summary == "Implemented and verified"
    assert bundle.work_item_updates["risks"] == ["staged rollout required"]
    assert bundle.work_item_updates["open_questions"] == ["who owns rollout?"]
    assert bundle.work_item_updates["handoff_context"] == {"next": "review"}
    assert bundle.work_item_updates["opaque_external_team_result"]["work_item_id"] == "wi-1"


def test_team_resume_policy_distinguishes_work_item_and_company_run_scope() -> None:
    task = Task(
        title="Team boundary",
        project_id="project-1",
        parent_session_id="company-run-1",
        metadata={
            "linked_work_item_id": "wi-1",
            "delegation_role_session_id": "role-session::run::cto",
            "external_session_scope": "work_item",
        },
    )
    set_linked_work_item_id(task, "wi-1")
    assert ExternalAgentBroker._external_role_resume_session_id(task) == ""
    assert (
        ExternalAgentBroker._external_work_item_resume_session_id(task)
        == "external-work-item::project-1::wi-1"
    )
    task.metadata["external_session_scope"] = "company_run"
    assert (
        ExternalAgentBroker._external_role_resume_session_id(task)
        == "role-session::run::cto"
    )
    assert ExternalAgentBroker._external_work_item_resume_session_id(task) == ""


def test_team_max_inflight_uses_one_limiter_per_org_binding() -> None:
    broker = ExternalAgentBroker.__new__(ExternalAgentBroker)
    broker._external_execution_limiters = {}
    task = Task(
        title="Team boundary",
        project_id="project-1",
        org_id="corporate",
        metadata={
            "execution_unit_kind": "opaque_external_team",
            "external_max_inflight": 2,
            "external_team_binding": {"binding_id": "cto-team"},
        },
    )
    first, key, limit = broker._external_execution_limiter(task)
    second, second_key, second_limit = broker._external_execution_limiter(task)
    assert first is second
    assert key == second_key == "project-1::corporate::cto-team"
    assert limit == second_limit == 2


def test_gateway_team_final_is_telemetry_until_processing_finishes() -> None:
    assert _is_terminal("chat.final", {"content": "done"}, team_mode=True) == (False, 0)
    assert _is_terminal(
        "chat.processing_status",
        {"is_processing": False},
        team_mode=True,
    ) == (True, 0)
    assert _is_terminal(
        "chat.final",
        {"event_type": "team.error", "content": "failed"},
        team_mode=True,
    ) == (True, 1)


def _gateway_progress_event(gateway_event: str, **payload: object) -> str:
    return json.dumps(
        {"type": "event", "event": gateway_event, "payload": payload},
        ensure_ascii=False,
    )


@pytest.mark.parametrize("adapter_type", [JiuwenAdapter, JiuwenSwarmAdapter])
def test_jiuwen_gateway_progress_exposes_tools_results_and_readable_text(adapter_type: type[JiuwenAdapter]) -> None:
    adapter = adapter_type()
    identity = {
        "session_id": "opc-jiuwen-progress",
        "rid": 7,
        "role": "leader",
    }

    # Token deltas are accumulated and force-flushed at the provider turn
    # boundary instead of surfacing isolated tokens.
    assert adapter.format_progress_update(
        _gateway_progress_event("chat.delta", content="现在搜索最近一个月的研究。", **identity),
        "stdout",
    ) is None

    thinking = adapter.format_progress_update(
        _gateway_progress_event("chat.final", event_type="chat.llm_usage", **identity),
        "stdout",
    )
    assert thinking == (
        f"[External:{adapter.agent_type}:thinking_snapshot] "
        "现在搜索最近一个月的研究。"
    )

    assert adapter.format_progress_update(
        _gateway_progress_event("chat.delta", content="准备调用命令。", **identity),
        "stdout",
    ) is None
    single_mode_boundary = adapter.format_progress_update(
        _gateway_progress_event("chat.usage_metadata", **identity),
        "stdout",
    )
    assert single_mode_boundary == (
        f"[External:{adapter.agent_type}:thinking_snapshot] 准备调用命令。"
    )

    tool_call = adapter.format_progress_update(
        _gateway_progress_event(
            "chat.tool_call",
            tool_call={
                "name": "bash",
                "arguments": json.dumps(
                    {"command": "gh search repos RSI", "description": "搜索 GitHub 仓库"},
                    ensure_ascii=False,
                ),
                "tool_call_id": "call-1",
            },
            **identity,
        ),
        "stdout",
    )
    assert tool_call == (
        f"[External:{adapter.agent_type}:tool] $ gh search repos RSI\n"
        "$ gh search repos RSI\n搜索 GitHub 仓库"
    )

    tool_result = adapter.format_progress_update(
        _gateway_progress_event(
            "chat.tool_result",
            tool_name="bash",
            tool_call_id="call-1",
            result="success=True data={'content': 'repo-a'} error=None",
            **identity,
        ),
        "stdout",
    )
    assert tool_result == (
        f"[External:{adapter.agent_type}:tool] bash result\n"
        "success=True data={'content': 'repo-a'} error=None"
    )


def test_jiuwen_progress_suppresses_gateway_noise_and_summarizes_plan() -> None:
    adapter = JiuwenSwarmAdapter()
    identity = {"session_id": "opc-team-progress", "rid": 1, "role": "leader"}
    for inner_type in ("keepalive", "chat.processing_status_deferred", "chat.tracer_agent"):
        assert adapter.format_progress_update(
            _gateway_progress_event("chat.final", event_type=inner_type, **identity),
            "stdout",
        ) is None

    plan = adapter.format_progress_update(
        _gateway_progress_event(
            "todo.updated",
            todos=[
                {"content": "搜索 GitHub", "status": "completed"},
                {"activeForm": "正在分析论文", "status": "in_progress"},
                {"content": "整理结论", "status": "pending"},
            ],
            **identity,
        ),
        "stdout",
    )
    assert plan == (
        "[External:jiuwenswarm:thinking_snapshot] "
        "Plan 1/3 complete · 正在分析论文"
    )

    ready = adapter.format_progress_update(
        _gateway_progress_event(
            "chat.final",
            event_type="team.runtime_ready",
            team_name="research_team",
            **identity,
        ),
        "stdout",
    )
    assert ready == "[External:jiuwenswarm:init] research_team ready"


def test_team_usage_boundary_flushes_deltas_that_omit_request_id() -> None:
    adapter = JiuwenSwarmAdapter()
    assert adapter.format_progress_update(
        _gateway_progress_event(
            "chat.delta",
            content="执行成功，命令输出为 telemetry-ok。",
            session_id="opc-team-progress",
            role="leader",
        ),
        "stdout",
    ) is None

    progress = adapter.format_progress_update(
        _gateway_progress_event(
            "chat.final",
            event_type="chat.llm_usage",
            session_id="opc-team-progress",
            rid=1,
            role="leader",
        ),
        "stdout",
    )
    assert progress == (
        "[External:jiuwenswarm:thinking_snapshot] "
        "执行成功，命令输出为 telemetry-ok。"
    )
    assert adapter.format_progress_update(
        _gateway_progress_event(
            "chat.final",
            event_type="chat.final",
            content="执行成功，命令输出为 telemetry-ok。",
            session_id="opc-team-progress",
        ),
        "stdout",
    ) is None


@pytest.mark.parametrize(
    ("adapter", "usage_event", "usage_payload"),
    [
        (JiuwenAdapter(), "chat.usage_metadata", {}),
        (
            JiuwenSwarmAdapter(),
            "chat.final",
            {"event_type": "chat.llm_usage", "rid": 1, "role": "leader"},
        ),
    ],
)
def test_jiuwen_result_keeps_markdown_and_returns_only_last_assistant_turn(
    adapter: JiuwenAdapter,
    usage_event: str,
    usage_payload: dict[str, object],
) -> None:
    session = "opc-markdown-result"
    frames = [
        _gateway_progress_event(
            "chat.delta",
            content="现在搜索",
            session_id=session,
            role="leader",
        ),
        _gateway_progress_event(
            "chat.delta",
            content=" GitHub 项目：",
            session_id=session,
            role="leader",
        ),
        _gateway_progress_event(
            usage_event,
            session_id=session,
            **usage_payload,
        ),
        _gateway_progress_event(
            "chat.tool_call",
            tool_call={"name": "bash", "arguments": "{}"},
            session_id=session,
            role="leader",
        ),
        _gateway_progress_event(
            "chat.delta",
            content="## 最新结论",
            session_id=session,
            role="leader",
        ),
        _gateway_progress_event(
            "chat.delta",
            content="\n\n",
            session_id=session,
            role="leader",
        ),
        _gateway_progress_event(
            "chat.delta",
            content="- **项目**: OpenRSI",
            session_id=session,
            role="leader",
        ),
        _gateway_progress_event(
            "chat.delta",
            content="\n- **结果**: 通过",
            session_id=session,
            role="leader",
        ),
        _gateway_progress_event(
            usage_event,
            session_id=session,
            **usage_payload,
        ),
    ]

    result = adapter.normalize_result_output("\n".join(frames))

    assert result == "## 最新结论\n\n- **项目**: OpenRSI\n- **结果**: 通过"
    assert "现在搜索" not in result


@pytest.mark.parametrize("adapter", [JiuwenAdapter(), JiuwenSwarmAdapter()])
def test_jiuwen_result_repairs_markdown_when_gateway_drops_newline_chunks(
    adapter: JiuwenAdapter,
) -> None:
    collapsed = (
        "由于网络访问限制，我无法直接获取实时数据。"
        "## 当前台风季节（2026年8月）"
        "8月是台风活跃期。"
        "### 中国官方台风信息来源"
        "1. **中央气象台** - http://typhoon.nmc.cn -权威发布"
        "2. **香港天文台** - https://www.hko.gov.hk -实时更新"
        "##如何查询："
        "1. **访问官方网站**"
        "2. **查看实时路径**"
        "##防护提示："
        "-随时关注官方预警"
        "-台风临近时避免前往沿海"
    )
    frame = _gateway_progress_event(
        "chat.delta",
        content=collapsed,
        session_id="opc-collapsed-markdown",
        role="leader",
    )

    result = adapter.normalize_result_output(frame)

    assert "。\n\n## 当前台风季节（2026年8月）\n\n8月是" in result
    assert "\n\n### 中国官方台风信息来源\n1. **中央气象台**" in result
    assert "\n2. **香港天文台**" in result
    assert "\n\n## 如何查询：\n1. **访问官方网站**" in result
    assert "\n2. **查看实时路径**" in result
    assert "\n\n## 防护提示：\n- 随时关注官方预警" in result
    assert "\n- 台风临近时避免前往沿海" in result


def test_jiuwen_markdown_repair_leaves_normal_text_and_valid_markdown_unchanged() -> None:
    adapter = JiuwenSwarmAdapter()
    prose = "版本 1.2 使用 https://example.com/a-b，并支持 C#。"
    markdown = "## 标题\n\n1. 第一项\n2. 第二项\n\n- A\n- B"

    assert adapter._restore_stream_markdown(prose) == prose
    assert adapter._restore_stream_markdown(markdown) == markdown


@pytest.mark.parametrize("adapter", [JiuwenAdapter(), JiuwenSwarmAdapter()])
def test_jiuwen_result_repairs_heading_scope_and_adjacent_lists(
    adapter: JiuwenAdapter,
) -> None:
    collapsed = (
        "根据配置，我可以访问以下路径："
        "## 当前项目工作空间"
        "**当前项目目录**：`/workspace/0000`"
        "这是主要工作目录。"
        "## OpenOPC系统目录"
        "**OpenOPC根目录**：`/workspace/OpenOPC`"
        "-技能目录：`/workspace/OpenOPC/.opc/skills/`"
        "-项目内存：`/workspace/OpenOPC/.opc/memory/`"
        "##团队工作空间（共享）"
        "**绝对路径**：`/team-workspace`"
        "##系统目录"
        "**系统工作空间**：`/agent/workspace`"
        "---"
        "请告诉我需要查看哪个文件？"
    )

    repaired = adapter._restore_stream_markdown(collapsed)

    assert "## 当前项目工作空间\n\n**当前项目目录**" in repaired
    assert "## OpenOPC系统目录\n\n**OpenOPC根目录**" in repaired
    assert "\n- 技能目录：" in repaired
    assert "\n- 项目内存：" in repaired
    assert "## 团队工作空间（共享）\n\n**绝对路径**" in repaired
    assert "## 系统目录\n\n**系统工作空间**" in repaired
    assert "\n\n---\n\n请告诉我" in repaired
    assert not any("**" in line for line in repaired.splitlines() if line.startswith("#"))


def test_jiuwen_result_repairs_emoji_bullets_and_gfm_table_rows() -> None:
    adapter = JiuwenSwarmAdapter()
    collapsed_bullets = (
        "可以修改代码。##我可以帮你做："
        "- ✅读取代码文件"
        "- ✅分析代码结构"
        "- ✅修复错误"
    )
    collapsed_table = (
        "工具如下：## Core Tools"
        "| Tool | Purpose ||------|---------|| `read_file` | Read files |"
        "|| `bash` | Run commands |"
    )

    bullets = adapter._restore_stream_markdown(collapsed_bullets)
    table = adapter._restore_stream_markdown(collapsed_table)

    assert "## 我可以帮你做：\n- ✅读取代码文件" in bullets
    assert "\n- ✅分析代码结构\n- ✅修复错误" in bullets
    assert "## Core Tools\n| Tool | Purpose |\n|------|---------|" in table
    assert "\n| `read_file` | Read files |\n| `bash` | Run commands |" in table


def test_jiuwen_result_repairs_isolated_heading_field_without_changing_valid_emphasis() -> None:
    adapter = JiuwenSwarmAdapter()

    assert adapter._restore_stream_markdown(
        "## 工作空间**路径**：`/workspace`"
    ) == "## 工作空间\n\n**路径**：`/workspace`"
    assert adapter._restore_stream_markdown(
        "## Release **v2**: notes"
    ) == "## Release **v2**: notes"


def test_company_external_capability_carves_out_native_only_guard() -> None:
    task = Task(
        title="External company boundary",
        assigned_external_agent="jiuwenswarm",
        metadata={
            "mode": "company",
            "runtime_model": "multi_team_org",
            "external_company_execution_allowed": True,
            "external_company_execution_fence": "validated_workspace",
        },
    )
    assert requires_native_company_execution(task) is False
    task.metadata["external_company_execution_allowed"] = False
    assert requires_native_company_execution(task) is True


def test_company_workspace_fence_records_changes_and_rejects_escape(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("before", encoding="utf-8")
    before = capture_company_workspace(tmp_path)
    source.write_text("after", encoding="utf-8")
    created = tmp_path / "artifact.txt"
    created.write_text("result", encoding="utf-8")
    attestation = validate_company_workspace(before, tmp_path)
    assert attestation["validated"] is True
    assert "source.txt" in attestation["modified_paths"]
    assert "artifact.txt" in attestation["created_paths"]

    escaping = tmp_path / "escape"
    escaping.symlink_to(tmp_path.parent)
    with pytest.raises(CompanyWorkspaceFenceError, match="escaping symlink"):
        capture_company_workspace(tmp_path)
