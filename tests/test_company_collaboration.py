from __future__ import annotations

import contextlib
import json
import os
import shutil
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from opc.core.config import EmployeeConfig, ExternalAgentConfig, OPCConfig, OrgConfig
from opc.core.events import EventBus
from opc.core.models import (
    AgentMessage,
    ArtifactRecord,
    CompanyMemberSession,
    CommsSemanticType,
    CommsTransportKind,
    DelegationWorkItem,
    ExternalSession,
    ExecutionCheckpoint,
    ExecutionMode,
    MeetingStatus,
    MessageStatus,
    MessageUrgency,
    Phase,
    RecruitmentCandidateRecommendation,
    RecruitmentPlan,
    RecruitmentProposal,
    RoleMemoryRecord,
    RouterDecision,
    SeatState,
    Task,
    TaskResult,
    TaskStatus,
    WorkItemDecisionRecord,
)
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer1_perception.context_assembler import ContextAssembler
from opc.layer3_agent.adapters.codex_adapter import CodexAdapter
from opc.layer3_agent.adapters.opencode_adapter import OpenCodeAdapter
from opc.layer2_organization.communication import CommunicationManager
from opc.layer2_organization import comms as file_comms
from opc.layer2_organization.collaboration_service import CollaborationContext
from opc.layer2_organization.company_mode import (
    CompanyRuntimeSpecBuilder,
    CompanyWorkItemExecutor,
)
from opc.layer2_organization.company_runtime import CompanyRuntime
from opc.layer2_organization.company_runtime_profiles import get_builtin_roles
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemGatePolicy,
    WorkItemProjectionSpec,
    serialize_company_work_item_plan,
)
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task, set_linked_work_item_id
from opc.layer4_tools.collaboration import _serialize_board_item, create_collaboration_tools
from opc.layer4_tools.registry import ToolDefinition
from opc.layer5_memory.memory_manager import MemoryManager


@contextlib.contextmanager
def _workspace_tempdir() -> Path:
    base = Path.cwd() / ".tmp-test" / f"company-collab-{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


class DummyOrgEngine:
    def __init__(self) -> None:
        self._roles = {
            "reviewer": SimpleNamespace(
                role_id="reviewer",
                name="Reviewer",
                responsibility="Quality review, plan approval, and QA validation.",
                reports_to="owner",
                preferred_external_agent=None,
                runtime_policy={"execution_strategy": "native", "default_turn_type": "review"},
                handoff_template_ref=None,
                memory_policy_ref=None,
                artifact_contract_ref=None,
            ),
            "executor": SimpleNamespace(
                role_id="executor",
                name="Executor",
                responsibility="Engineering implementation, file edits, coding tasks, and data processing.",
                reports_to="reviewer",
                preferred_external_agent="codex",
                runtime_policy={"execution_strategy": "auto", "default_turn_type": "work"},
                handoff_template_ref=None,
                memory_policy_ref=None,
                artifact_contract_ref=None,
            ),
        }

    def get_role_for_work_item(self, role_id: str, domains: list[str] | None = None):
        return self._roles.get(role_id, self._roles["executor"])

    def get_role_for_domain(self, domains: list[str] | None = None):
        domains = domains or []
        if any(domain in {"review", "qa"} for domain in domains):
            return self._roles["reviewer"]
        return self._roles["executor"]

    def get_executor(self):
        return self._roles["executor"]

    def get_agent(self, role_id: str):
        return self._roles.get(role_id)


class DummyMemory:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    async def record_task_completion_async(self, task: Task, result_content: str, project: bool = False) -> None:
        self.calls.append((task.title, result_content, project))


class DummyFeedbackMemory:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.employee_evolution = SimpleNamespace(
            build_employee_delta_context=lambda employee_id, project_id="": ""
        )

    async def record_company_feedback_outcomes(
        self,
        *,
        delivery_task: Task,
        work_item_tasks: list[Task],
        feedback: dict[str, object],
        evaluation: dict[str, object],
    ) -> None:
        self.calls.append(
            {
                "delivery_task": delivery_task,
                "work_item_tasks": list(work_item_tasks),
                "feedback": dict(feedback),
                "evaluation": dict(evaluation),
            }
        )


class DummyRuntimeCommunication:
    def __init__(self, inbox_by_agent: dict[str, list[dict[str, object]]] | None = None) -> None:
        self.inbox_by_agent = dict(inbox_by_agent or {})

    async def read_inbox(
        self,
        *,
        agent_id: str,
        task_id: str | None = None,
        task_ids: list[str] | None = None,
        unread_only: bool = True,
        limit: int = 10,
        mark_read: bool = False,
        task: Task | None = None,
    ) -> list[dict[str, object]]:
        _ = (task_id, task_ids, unread_only, mark_read, task)
        return list(self.inbox_by_agent.get(agent_id, []))[:limit]


class DummyManagerNotificationCommunication:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send_manager_notification(
        self,
        *,
        from_agent: str,
        task: Task | None,
        semantic_type: CommsSemanticType,
        subject: str,
        body: str,
        metadata: dict[str, Any] | None = None,
        reply_needed: bool = False,
        requires_ack: bool = False,
    ) -> AgentMessage:
        record = {
            "from_agent": from_agent,
            "task": task,
            "semantic_type": semantic_type,
            "subject": subject,
            "body": body,
            "metadata": dict(metadata or {}),
            "reply_needed": reply_needed,
            "requires_ack": requires_ack,
        }
        self.calls.append(record)
        return AgentMessage(
            from_agent=from_agent,
            to_agents=["manager"],
            subject=subject,
            body=body,
            semantic_type=semantic_type,
            reply_needed=reply_needed,
            requires_ack=requires_ack,
            metadata=dict(metadata or {}),
        )


class DummyReorgProposal:
    def __init__(self, proposal_id: str, scope: str = "task_adjustment", status: str = "proposed") -> None:
        self.proposal_id = proposal_id
        self.scope = SimpleNamespace(value=scope)
        self.status = SimpleNamespace(value=status)


class DummyReorgManager:
    def __init__(self) -> None:
        self.adjust_calls: list[dict[str, Any]] = []
        self.replan_calls: list[dict[str, Any]] = []

    async def suggest_task_adjustment(self, **kwargs: Any) -> dict[str, Any]:
        self.adjust_calls.append(dict(kwargs))
        return {
            "proposal": DummyReorgProposal("proposal-adjust", scope="task_adjustment"),
            "auto_applied": False,
        }

    async def propose_reorg(self, **kwargs: Any) -> DummyReorgProposal:
        self.replan_calls.append(dict(kwargs))
        return DummyReorgProposal("proposal-replan", scope="runtime_replan")


class DummySelectionLLM:
    def __init__(self, selected_agent: str = "native", reasoning: str = "fits best", responses: list[object] | None = None, has_creds: bool = True) -> None:
        self.selected_agent = selected_agent
        self.reasoning = reasoning
        self.responses = list(responses or [])
        self.calls: list[dict] = []
        self._has_creds = has_creds

    def has_credentials(self) -> bool:
        return self._has_creds

    async def simple_chat(self, prompt: str, system: str | None = None, task_type: str | None = None) -> str:
        import json

        self.calls.append({
            "prompt": prompt,
            "system": system,
            "task_type": task_type,
        })
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return str(response)
        return json.dumps({
            "selected_agent": self.selected_agent,
            "reasoning": self.reasoning,
        }, ensure_ascii=False)

    def get_capabilities(self, task_type: str = "coding"):
        _ = task_type
        return SimpleNamespace(
            model="stub",
            supports_streaming=True,
            supports_tool_calling=True,
            supports_streaming_tool_calls=True,
            supports_multimodal=False,
        )


class CompanyCollaborationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _self_evolution_runtime_topology(
        *,
        run_id: str = "run-self-evolution",
        role_id: str = "ceo",
        employee_id: str = "ceo-1",
    ) -> dict[str, Any]:
        team_id = f"team::{role_id}"
        seat_id = f"seat::{team_id}::{role_id}"
        team_instance_id = f"team-instance::{run_id}::{team_id}"
        return {
            "run_id": run_id,
            "final_decider_role_id": role_id,
            "top_level_role_ids": [role_id],
            "teams": [{"team_id": team_id, "team_instance_id": team_instance_id}],
            "seats": [
                {
                    "role_id": role_id,
                    "cell_id": team_id,
                    "team_id": team_id,
                    "team_instance_id": team_instance_id,
                    "seat_id": seat_id,
                    "role_runtime_session_id": f"role-session::{run_id}::{role_id}::{team_instance_id}",
                    "employee_assignment": {
                        "employee_id": employee_id,
                        "employee_name": "Self Evolution Employee",
                        "role_id": role_id,
                    },
                }
            ],
        }

    @staticmethod
    async def _record_no_self_evolution_updates(
        store: Any,
        *,
        run_id: str,
        checkpoint_id: str,
    ) -> None:
        if not hasattr(store, "list_delegation_work_items"):
            return
        work_items = await store.list_delegation_work_items(run_id)
        for item in work_items:
            metadata = dict(getattr(item, "metadata", {}) or {})
            if metadata.get("self_evolution_checkpoint_id") != checkpoint_id:
                continue
            item.metadata = {
                **metadata,
                "self_evolution_recorded": [],
                "self_evolution_completed_at": datetime.now().isoformat(),
            }
            if hasattr(store, "update_delegation_work_item"):
                await store.update_delegation_work_item(
                    item.work_item_id,
                    metadata_updates={
                        "self_evolution_recorded": [],
                        "self_evolution_completed_at": item.metadata["self_evolution_completed_at"],
                    },
                )
            elif hasattr(store, "save_delegation_work_item"):
                await store.save_delegation_work_item(item)

    def test_company_task_timeout_default_is_24_hours(self) -> None:
        self.assertEqual(OPCConfig().system.task_mode.sub_agent_timeout_sec, 86400)

    async def test_company_bootstrap_only_materializes_root_work_item(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            config = OPCConfig()
            config.org.company_profile = "corporate"
            org = OrgEngine(config, Path(tmpdir))
            topology = org.build_runtime_delegation_topology()
            plan = org.build_company_work_item_runtime_plan(
                "corporate",
                runtime_topology=topology,
                original_request="Build a Gmail-style web app",
            )

            engine = OPCEngine(config=config, opc_home=Path(tmpdir), project_id="proj1")
            engine.store = store
            engine.org_engine = org

            run_id, root_work_item = await engine._bootstrap_runtime_delegation_run(
                session_id="sess-bootstrap-root-only",
                project_id="proj1",
                runtime_spec=None,
                original_message="Build a Gmail-style web app",
                runtime_topology=topology,
                work_item_plan=plan,
                delegation_playbook={},
                target_output_dir=None,
                comms_workspace_root="",
                force_native_execution=False,
            )
            work_items = await store.list_delegation_work_items(run_id)

            self.assertEqual([item.work_item_id for item in work_items], [root_work_item.work_item_id])
            self.assertEqual(root_work_item.role_id, "ceo")
            self.assertEqual(root_work_item.kind, "intake")
            self.assertEqual(root_work_item.metadata["allowed_delegate_role_ids"], ["cto", "cmo", "coo"])
            run = await store.get_delegation_run(run_id)
            assert run is not None
            self.assertEqual(run.metadata["company_work_item_plan"]["root_projection_id"], plan.root_projection_id)
            await store.close()

    async def test_company_runtime_persists_member_session_across_same_role_work_items(self) -> None:
        runtime = CompanyRuntime(
            org_engine=DummyOrgEngine(),
            communication=DummyRuntimeCommunication(
                {
                    "executor": [
                        {
                            "msg_id": "msg-manager",
                            "from_agent": "reviewer",
                            "subject": "Please prioritize rollback validation",
                            "body": "Focus on the rollback path first.",
                            "reply_needed": True,
                            "urgency": "blocking",
                        }
                    ]
                }
            ),
        )
        first = Task(
            id="work-item-one",
            title="Execution One",
            assigned_to="executor",
            status=TaskStatus.PENDING,
            project_id="proj1",
            metadata={
                "work_item_projection_id": "execution_one",
                "work_item_role_id": "executor",
                "employee_assignment": {"employee_id": "backend-architect", "role_id": "executor"},
            },
        )
        second = Task(
            id="work-item-two",
            title="Execution Two",
            assigned_to="executor",
            status=TaskStatus.PENDING,
            project_id="proj1",
            metadata={
                "work_item_projection_id": "execution_two",
                "work_item_role_id": "executor",
                "employee_assignment": {"employee_id": "backend-architect", "role_id": "executor"},
            },
        )

        await runtime.bootstrap([first, second])
        runtime.enqueue_runnable_tasks([first, second])
        claims = await runtime.claim_runnable_tasks([first, second])
        self.assertEqual(len(claims), 1)
        session, claimed_task = claims[0]
        self.assertEqual(claimed_task.id, "work-item-one")
        self.assertEqual(claimed_task.metadata["message_priority"], "manager")
        claimed_task.metadata["work_item_summary"] = "Execution One completed with rollback notes."
        await runtime.complete_claim(
            session,
            claimed_task,
            TaskResult(status=TaskStatus.DONE, content="Execution One completed with rollback notes."),
        )

        runtime.enqueue_runnable_tasks([second])
        second_claims = await runtime.claim_runnable_tasks([first, second])
        self.assertEqual(len(second_claims), 1)
        same_session, next_task = second_claims[0]
        self.assertEqual(session.member_session_id, same_session.member_session_id)
        self.assertEqual(next_task.metadata["member_session_id"], session.member_session_id)
        self.assertIn(
            "Execution One completed with rollback notes.",
            "\n".join(next_task.context_snapshot["member_working_memory"]),
        )
        self.assertGreaterEqual(next_task.metadata["member_session_state"]["inbox_cursor"], 1)

    async def test_company_runtime_scopes_member_sessions_by_top_level_session(self) -> None:
        runtime = CompanyRuntime(
            org_engine=DummyOrgEngine(),
            communication=DummyRuntimeCommunication(),
        )
        first = Task(
            id="work-item-one",
            title="Execution One",
            session_id="root-a",
            parent_session_id="root-a",
            assigned_to="executor",
            status=TaskStatus.PENDING,
            project_id="proj1",
            metadata={
                "work_item_projection_id": "execution_one",
                "work_item_role_id": "executor",
                "employee_assignment": {"employee_id": "backend-architect", "role_id": "executor"},
            },
        )
        second = Task(
            id="work-item-two",
            title="Execution Two",
            session_id="root-b",
            parent_session_id="root-b",
            assigned_to="executor",
            status=TaskStatus.PENDING,
            project_id="proj1",
            metadata={
                "work_item_projection_id": "execution_two",
                "work_item_role_id": "executor",
                "employee_assignment": {"employee_id": "backend-architect", "role_id": "executor"},
            },
        )

        await runtime.bootstrap([first])
        await runtime.bootstrap([second])

        first_session = runtime.session_for_task(first)
        second_session = runtime.session_for_task(second)

        self.assertEqual(first_session.member_session_id, "role-session::proj1::root-a::executor::backend-architect")
        self.assertEqual(second_session.member_session_id, "role-session::proj1::root-b::executor::backend-architect")
        self.assertNotEqual(first_session.member_session_id, second_session.member_session_id)
        self.assertNotEqual(first_session.role_session_id, second_session.role_session_id)

    async def test_company_runtime_enqueues_ready_work_item_with_session_scoped_queue(self) -> None:
        runtime = CompanyRuntime(
            org_engine=DummyOrgEngine(),
            communication=DummyRuntimeCommunication(),
        )
        task = Task(
            id="work-item-one",
            title="Execution One",
            session_id="root-a",
            parent_session_id="root-a",
            assigned_to="executor",
            status=TaskStatus.PENDING,
            project_id="proj1",
            metadata={
                "work_item_projection_id": "execution_one",
                "work_item_role_id": "executor",
                "employee_assignment": {"employee_id": "backend-architect", "role_id": "executor"},
            },
        )
        set_linked_work_item_id(task, "work-item-1")
        work_item = DelegationWorkItem(
            work_item_id="work-item-1",
            run_id="run-1",
            cell_id="cell-1",
            role_id="executor",
            projection_id="execution_one",
            phase=Phase.READY,
            metadata={},
        )

        await runtime.bootstrap([task])
        runtime.enqueue_runnable_work_items([work_item], task_by_work_item_id={"work-item-1": task})

        async def claim_work_item(*_args: object, **kwargs: object) -> DelegationWorkItem:
            role_session_id = str(kwargs["role_runtime_session_id"])
            work_item.phase = Phase.RUNNING
            work_item.role_runtime_session_id = role_session_id
            work_item.claimed_by_role_runtime_session_id = role_session_id
            work_item.claimed_by_seat_id = str(kwargs.get("seat_id", ""))
            work_item.metadata = {
                **dict(work_item.metadata or {}),
                "claimed_by_role_session_id": role_session_id,
                "claimed_task_id": str(kwargs["task_id"]),
            }
            return work_item

        runtime.store = SimpleNamespace(
            is_ready=True,
            claim_delegation_work_item_if_dispatchable=claim_work_item,
            save_delegation_role_session=AsyncMock(),
        )
        claims = await runtime.claim_runnable_tasks([task], work_items=[work_item])

        self.assertEqual(len(claims), 1)
        claimed_session, claimed_task = claims[0]
        self.assertEqual(claimed_task.id, task.id)
        self.assertEqual(claimed_session.member_session_id, "role-session::proj1::root-a::executor::backend-architect")

    async def test_company_runtime_does_not_spawn_after_atomic_claim_loses_to_hold(self) -> None:
        runtime = CompanyRuntime(
            org_engine=DummyOrgEngine(),
            communication=DummyRuntimeCommunication(),
        )
        task = Task(
            id="held-work-item-task",
            title="Held Work Item",
            session_id="root-a",
            parent_session_id="root-a",
            assigned_to="executor",
            status=TaskStatus.PENDING,
            project_id="proj1",
            metadata={
                "work_item_projection_id": "held_execution",
                "work_item_role_id": "executor",
                "employee_assignment": {
                    "employee_id": "backend-architect",
                    "role_id": "executor",
                },
            },
        )
        set_linked_work_item_id(task, "held-work-item")
        work_item = DelegationWorkItem(
            work_item_id="held-work-item",
            run_id="run-1",
            cell_id="cell-1",
            role_id="executor",
            projection_id="held_execution",
            phase=Phase.READY,
        )
        held_after_race = DelegationWorkItem(
            **{
                **work_item.__dict__,
                "metadata": {"dispatch_hold": "company_runtime_suspended"},
            }
        )
        claim = AsyncMock(return_value=None)
        runtime.store = SimpleNamespace(
            is_ready=True,
            claim_delegation_work_item_if_dispatchable=claim,
            get_delegation_work_item=AsyncMock(return_value=held_after_race),
        )
        await runtime.bootstrap([task])
        runtime.enqueue_runnable_work_items(
            [work_item],
            task_by_work_item_id={work_item.work_item_id: task},
        )

        claims = await runtime.claim_runnable_tasks([task], work_items=[work_item])

        self.assertEqual(claims, [])
        self.assertNotIn(work_item.work_item_id, runtime._claimed_work_item_ids)
        claim.assert_awaited_once()

    async def test_company_runtime_does_not_double_claim_same_work_item(self) -> None:
        runtime = CompanyRuntime(
            org_engine=DummyOrgEngine(),
            communication=DummyRuntimeCommunication(),
        )
        task = Task(
            id="single-work-item",
            title="Single Work Item",
            assigned_to="executor",
            status=TaskStatus.PENDING,
            project_id="proj1",
            metadata={
                "work_item_projection_id": "execution",
                "work_item_role_id": "executor",
                "employee_assignment": {"employee_id": "backend-architect", "role_id": "executor"},
            },
        )
        await runtime.bootstrap([task])
        runtime.enqueue_runnable_tasks([task])
        first = await runtime.claim_runnable_tasks([task])
        second = await runtime.claim_runnable_tasks([task])
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])

    def test_builtin_corporate_profile_is_work_item_runtime(self) -> None:
        org = OrgEngine(OPCConfig())
        corporate = org.build_company_work_item_runtime_plan(
            "corporate",
            runtime_topology=org.build_runtime_delegation_topology(),
        )
        self.assertGreater(len(corporate.projections), 0)
        self.assertTrue(corporate.metadata["work_item_driven"])
        self.assertEqual(corporate.metadata["runtime_model"], "multi_team_org")
        self.assertNotIn("work_item_projection_title_generation", corporate.metadata)

    def test_recovery_context_prefers_continue_or_narrower_follow_up(self) -> None:
        assembler = ContextAssembler(memory=SimpleNamespace())
        task = Task(
            id="task-1",
            title="Collect deployment details",
            description="Need the deployment target details.",
            status=TaskStatus.PENDING,
            context_snapshot={
                "user_supplied_input": "Use the staging environment first.",
                "requested_user_input": {
                    "reason": "Need deployment target details.",
                    "questions": ["Which environment and region should I deploy to?"],
                    "required_fields": ["environment", "region"],
                },
            },
        )

        brief = assembler.build_task_brief(task)
        recovery = assembler.build_recovery_context(task)

        self.assertIn("If it resolves the blocker, continue.", brief)
        self.assertIn("exact missing detail", brief)
        self.assertIn("Do not repeat the same broad question.", brief)
        self.assertIn("If it is enough, continue.", recovery)
        self.assertIn("narrower follow-up", recovery)
        self.assertIn("Previous User-Input Request", recovery)

    def test_company_executor_aligns_external_timeouts_with_work_item_budget(self) -> None:
        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            work_item_timeout=600,
        )
        task = Task(
            id="execution-timeout-budget",
            title="Execution",
            metadata={},
        )

        executor._configure_external_timeouts(task)

        self.assertEqual(task.metadata["external_hard_timeout_seconds"], 540)
        self.assertEqual(task.metadata["external_idle_timeout_seconds"], 200)
        self.assertEqual(task.metadata["external_startup_timeout_seconds"], 200)

    def test_corporate_profile_uses_work_item_runtime_without_fixed_projections(self) -> None:
        org = OrgEngine(OPCConfig())
        plan = org.build_company_work_item_runtime_plan(
            "corporate",
            runtime_topology=org.build_runtime_delegation_topology(),
        )
        self.assertGreater(len(plan.projections), 0)
        self.assertEqual(plan.metadata["source"], "company_work_item_runtime_plan")
        self.assertTrue(plan.metadata["work_item_driven"])

    def test_runtime_spec_builder_has_no_legacy_runtime_planner_entrypoints(self) -> None:
        builder = CompanyRuntimeSpecBuilder(DummyOrgEngine())

        self.assertFalse(hasattr(builder, "build_plan"))
        self.assertFalse(hasattr(builder, "build_" + "task_dicts"))

    def test_corporate_roles_include_acquisition_specialist_under_coo(self) -> None:
        role_by_id = {role.id: role for role in get_builtin_roles("corporate")}

        self.assertIn("acquisition_specialist", role_by_id)
        self.assertEqual(role_by_id["acquisition_specialist"].reports_to, "coo")
        self.assertIn("acquisition_specialist", role_by_id["coo"].can_spawn)
        # Every company role can author files (file_write/file_edit baseline).
        self.assertIn("file_edit", role_by_id["acquisition_specialist"].tools)

    def test_data_acquisition_turn_type_stays_execute_even_with_audit_language(self) -> None:
        builder = CompanyRuntimeSpecBuilder(DummyOrgEngine())
        workspace_bootstrap = WorkItemProjectionSpec(
            projection_id="workspace_bootstrap",
            turn_type="setup",
            title="Workspace Bootstrap",
            summary="Create and verify the shared runtime workspace.",
            role_id="ceo",
        )
        data_acquisition = WorkItemProjectionSpec(
            projection_id="data_acquisition",
            turn_type="execute",
            title="Data Acquisition",
            summary=(
                "Actively collect, validate, and prepare task-critical external inputs. "
                "Self-audit prepared results and publish a data_acquisition_report."
            ),
            role_id="acquisition_specialist",
        )

        self.assertEqual(builder._infer_work_item_turn_type(workspace_bootstrap), "setup")
        self.assertEqual(builder._infer_work_item_turn_type(data_acquisition), "execute")

    def test_data_acquisition_runtime_plan_prefers_web_discovery_before_shell_preparation(self) -> None:
        builder = CompanyRuntimeSpecBuilder(DummyOrgEngine())
        projection_spec = WorkItemProjectionSpec(
            projection_id="data_acquisition",
            turn_type="execute",
            title="Data Acquisition",
            summary=(
                "Actively collect, validate, and prepare task-critical external inputs. "
                "Self-audit prepared results and publish a data_acquisition_report."
            ),
            role_id="acquisition_specialist",
        )

        assignment = {
            "global_intent_summary": "Collect and prepare external inputs.",
            "your_responsibility": "Acquire and validate critical external sources.",
            "out_of_scope": ["Do not claim downstream editing is complete."],
            "inputs": ["Workspace manifest", "Upstream planning handoffs"],
            "deliverables": ["data_acquisition_report", "acquisition execution record"],
            "acceptance_criteria": ["Attempt real acquisition before blocking downstream work."],
        }
        runtime_plan = builder._build_work_item_runtime_plan(
            projection_spec=projection_spec,
            assignment=assignment,
            work_item_turn_type="execute",
            runtime_policy={"communication": {}},
        )

        expectations = "\n".join(runtime_plan["collaboration_expectations"])
        self.assertIn("web_search/web_fetch", expectations)
        self.assertIn("Use shell_exec only after concrete source URLs are identified", expectations)
        self.assertTrue(runtime_plan["execution_sequence"])
        self.assertTrue(runtime_plan["media_mode_triggers"])
        self.assertTrue(runtime_plan["media_mode_rules"])
        self.assertTrue(runtime_plan["download_priority"])

    def test_acquisition_specialist_employee_is_preferred_over_default_employee(self) -> None:
        config = OPCConfig(
            org=OrgConfig(
                company_profile="corporate",
                employees=[
                    EmployeeConfig(
                        employee_id="acquisition_specialist-specialized-source-acquisition-specialist",
                        template_id="specialized-source-acquisition-specialist",
                        name="Source Acquisition Specialist",
                        role_id="acquisition_specialist",
                        category="specialized",
                        preferred_external_agent="claude_code",
                    )
                ],
            )
        )
        engine = OrgEngine(config)

        assignment = engine.resolve_employee_for_work_item("acquisition_specialist", project_id="proj1")

        self.assertIsNotNone(assignment)
        assert assignment is not None
        self.assertEqual(assignment["employee_id"], "specialized-source-acquisition-specialist")
        self.assertEqual(assignment["template_id"], "specialized-source-acquisition-specialist")

    def test_data_acquisition_brief_includes_execution_sequence_and_media_rules(self) -> None:
        assembler = ContextAssembler(memory=SimpleNamespace(), store=None, communication=None)
        task = Task(
            id="data-acquisition-brief",
            title="Data Acquisition",
            description="Acquire media assets",
            assigned_to="acquisition_specialist",
            metadata={
                "work_item_projection_title": "Data Acquisition",
                "work_item_turn_type": "execute",
                "work_item_assignment": {
                    "global_intent_summary": "Collect and prepare source material.",
                    "your_responsibility": "Own data acquisition.",
                    "out_of_scope": ["Do not perform downstream editing."],
                    "inputs": ["Workspace manifest"],
                    "deliverables": ["data_acquisition_report"],
                    "acceptance_criteria": ["Attempt real acquisition before blocking."],
                },
                "work_item_runtime_plan": {
                    "turn_type": "execute",
                    "summary": "Acquire and prepare inputs.",
                    "execution_sequence": [
                        "Discover: identify candidate sources.",
                        "Verify: confirm source provenance.",
                        "Prepare: download into the workspace.",
                        "Report: publish manifests and readiness status.",
                    ],
                    "media_mode_triggers": ["Enable media mode when the request mentions trailer or video."],
                    "media_mode_rules": ["Parse raw HTML into work/source_candidates.json before searching it."],
                    "download_priority": ["Download/prepare: yt-dlp, curl, wget, aria2c"],
                    "collaboration_expectations": ["Leave reviewer-friendly manifests for QA."],
                },
            },
        )

        brief = assembler.build_task_brief(task)

        self.assertIn("## Work Item Runtime Plan", brief)
        self.assertIn("Execution Sequence:", brief)
        self.assertIn("Media Mode Rules:", brief)
        self.assertIn("Download Priority:", brief)

    async def test_setup_work_item_prepares_target_output_dir_before_execution(self) -> None:
        with _workspace_tempdir() as tmpdir:
            workspace = tmpdir / "target" / "nested"
            execute_task = AsyncMock(return_value=TaskResult(status=TaskStatus.DONE, content="prepared"))
            save_task = AsyncMock()
            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=SimpleNamespace(),
                approval_engine=SimpleNamespace(),
                memory=None,
                execute_task=execute_task,
                save_task=save_task,
            )
            task = Task(
                id="setup-task",
                title="Environment Provisioning",
                assigned_to="executor",
                status=TaskStatus.PENDING,
                metadata={
                    "work_item_projection_id": "env_provisioning",
                    "work_item_role_id": "executor",
                    "work_item_turn_type": "setup",
                    "work_item_execution_strategy": "native",
                    "target_output_dir": str(workspace),
                    "work_item_gate": None,
                    "progress_log": [],
                },
            )

            result = await executor._run_work_item(task, {task.id: task, "env_provisioning": task})

            self.assertEqual(result.status, TaskStatus.DONE)
            self.assertTrue(workspace.exists())
            self.assertEqual(task.metadata["setup_workspace_prepared"], str(workspace.resolve()))
            self.assertTrue(
                any(item.startswith("[Setup] Prepared workspace roots:") for item in task.metadata.get("progress_log", []))
            )

    async def test_non_setup_work_item_prepares_workspace_root_before_execution(self) -> None:
        with _workspace_tempdir() as tmpdir:
            workspace = tmpdir / "runtime-root"
            execute_task = AsyncMock(return_value=TaskResult(status=TaskStatus.DONE, content="prepared"))
            save_task = AsyncMock()
            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=SimpleNamespace(),
                approval_engine=SimpleNamespace(),
                memory=None,
                execute_task=execute_task,
                save_task=save_task,
            )
            task = Task(
                id="execute-task",
                title="CEO Intake",
                assigned_to="executor",
                status=TaskStatus.PENDING,
                metadata={
                    "work_item_projection_id": "ceo_intake",
                    "work_item_role_id": "executor",
                    "work_item_turn_type": "intake",
                    "work_item_execution_strategy": "native",
                    "workspace_root": str(workspace),
                    "comms_workspace_root": str(workspace),
                    "work_item_gate": None,
                    "progress_log": [],
                },
            )

            result = await executor._run_work_item(task, {task.id: task, "ceo_intake": task})

            self.assertEqual(result.status, TaskStatus.DONE)
            self.assertTrue(workspace.exists())
            self.assertEqual(task.metadata["setup_workspace_prepared"], str(workspace.resolve()))
            self.assertTrue(
                any(item.startswith("[Workspace] Prepared workspace roots:") for item in task.metadata.get("progress_log", []))
            )

    async def test_failed_work_item_transitions_out_of_running_phase(self) -> None:
        execute_task = AsyncMock(
            return_value=TaskResult(status=TaskStatus.FAILED, content="provider limit")
        )
        save_task = AsyncMock()
        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=execute_task,
            save_task=save_task,
        )
        task = Task(
            id="failed-work-item-task",
            title="Failed Work Item",
            assigned_to="executor",
            status=TaskStatus.PENDING,
            metadata={
                "work_item_projection_id": "failed_execution",
                "work_item_role_id": "executor",
                "work_item_turn_type": "execute",
                "work_item_execution_strategy": "native",
                "progress_log": [],
            },
        )

        with patch(
            "opc.layer2_organization.company_mode.transition_work_item_from_task",
            new_callable=AsyncMock,
        ) as transition:
            result = await executor._run_work_item(task, {task.id: task, "failed_execution": task})

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertTrue(
            any(
                call.kwargs.get("target_status_or_phase") == TaskStatus.FAILED
                and call.kwargs.get("reason") == "work_item_failed"
                for call in transition.await_args_list
            )
        )
        self.assertIn(
            "Work item ended with status failed.",
            task.metadata.get("progress_log", []),
        )
        save_task.assert_awaited()

    async def test_workspace_bootstrap_work_item_records_manifest_and_reserved_layout(self) -> None:
        with _workspace_tempdir() as tmpdir:
            workspace = tmpdir / "runtime-root"
            execute_task = AsyncMock(return_value=TaskResult(status=TaskStatus.DONE, content="bootstrap complete"))
            save_task = AsyncMock()
            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=SimpleNamespace(),
                approval_engine=SimpleNamespace(),
                memory=None,
                execute_task=execute_task,
                save_task=save_task,
            )
            task = Task(
                id="workspace-bootstrap-task",
                title="Workspace Bootstrap",
                assigned_to="executor",
                status=TaskStatus.PENDING,
                metadata={
                    "work_item_projection_id": "workspace_bootstrap",
                    "work_item_role_id": "executor",
                    "work_item_turn_type": "setup",
                    "work_item_execution_strategy": "native",
                    "target_output_dir": str(workspace),
                    "work_item_gate": None,
                    "progress_log": [],
                },
            )

            result = await executor._run_work_item(task, {task.id: task, "workspace_bootstrap": task})

            self.assertEqual(result.status, TaskStatus.DONE)
            manifest = dict(task.metadata.get("workspace_manifest", {}) or {})
            self.assertEqual(manifest.get("root_path"), str(workspace.resolve()))
            reserved = dict(manifest.get("reserved_paths", {}) or {})
            self.assertEqual(set(reserved), {"inputs", "deliverables", "work", ".openopc/manifests"})
            for path in reserved.values():
                self.assertTrue(Path(path).is_dir())
            self.assertTrue(Path(manifest.get("manifest_path", "")).is_file())

    async def test_same_role_dependency_runs_without_formal_handoff_contract(self) -> None:
        with _workspace_tempdir() as tmpdir:
            workspace = tmpdir / "runtime-root"
            execute_task = AsyncMock(return_value=TaskResult(status=TaskStatus.DONE, content="bootstrap complete"))
            save_task = AsyncMock()
            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=SimpleNamespace(),
                approval_engine=SimpleNamespace(),
                memory=None,
                execute_task=execute_task,
                save_task=save_task,
            )
            task = Task(
                id="workspace-bootstrap-contract-task",
                title="Workspace Bootstrap",
                assigned_to="reviewer",
                status=TaskStatus.PENDING,
                dependencies=["ceo-intake-task"],
                metadata={
                    "work_item_projection_id": "workspace_bootstrap",
                    "work_item_role_id": "reviewer",
                    "work_item_turn_type": "setup",
                    "work_item_execution_strategy": "native",
                    "target_output_dir": str(workspace),
                    "work_item_gate": None,
                    "progress_log": [],
                },
            )

            result = await executor._run_work_item(task, {task.id: task, "workspace_bootstrap": task})

            self.assertEqual(result.status, TaskStatus.DONE)
            self.assertNotIn(
                "Work-item contract enforcement failed.",
                "\n".join(task.metadata.get("progress_log", [])),
            )

    async def test_data_acquisition_gate_passes_on_ready_status(self) -> None:
        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
        )
        task = Task(
            id="data-ready-task",
            title="Data Acquisition",
            assigned_to="executor",
            status=TaskStatus.RUNNING,
            metadata={
                "work_item_projection_id": "data_acquisition",
                "data_acquisition_report": {
                    "status": "ready",
                    "designated_input_dir": "/tmp/inputs",
                    "prepared_assets": ["/tmp/inputs/source-01.mp4"],
                },
                "progress_log": [],
            },
        )
        gate = WorkItemGatePolicy(
            gate_type="automated_verification",
            on_reject="rework",
            rework_projection_id="data_acquisition",
            max_retries=2,
            metadata={
                "readiness_artifact": "data_acquisition_report",
                "allowed_statuses": ["ready", "already_present", "not_required"],
                "blocking_statuses": ["partial", "missing_critical"],
                "require_attempt_evidence_for_blocking": True,
            },
        )

        await executor._apply_data_acquisition_gate(task, gate)

        self.assertEqual(task.status, TaskStatus.DONE)
        self.assertIn("passed with status `ready`", "\n".join(task.metadata.get("progress_log", [])))

    async def test_data_acquisition_gate_rejects_media_ready_without_downloaded_binary_asset(self) -> None:
        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
        )
        task = Task(
            id="data-ready-media-missing-binary",
            title="Data Acquisition",
            assigned_to="acquisition_specialist",
            status=TaskStatus.RUNNING,
            metadata={
                "work_item_projection_id": "data_acquisition",
                "work_item_role_id": "acquisition_specialist",
                "original_message": "Collect official trailer video clips and subtitle assets for a movie recommendation video.",
                "data_acquisition_report": {
                    "status": "ready",
                    "designated_input_dir": "/tmp/inputs",
                    "source_candidates_path": "/tmp/work/source_candidates.json",
                    "download_manifest_path": "/tmp/work/download_manifest.json",
                    "prepared_assets": ["/tmp/work/source_candidates.json"],
                },
                "progress_log": [],
            },
        )
        gate = WorkItemGatePolicy(
            gate_type="automated_verification",
            on_reject="rework",
            rework_projection_id="data_acquisition",
            max_retries=1,
            metadata={
                "readiness_artifact": "data_acquisition_report",
                "allowed_statuses": ["ready", "already_present", "not_required"],
                "blocking_statuses": ["partial", "missing_critical"],
                "require_attempt_evidence_for_blocking": True,
            },
        )

        await executor._apply_data_acquisition_gate(task, gate)

        self.assertEqual(task.status, TaskStatus.PENDING)
        self.assertIn("download manifest", task.metadata["gate_review_feedback"])

    async def test_data_acquisition_gate_passes_media_ready_with_downloaded_binary_asset(self) -> None:
        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
        )
        with _workspace_tempdir() as tmpdir:
            workspace = tmpdir / "project"
            inputs = workspace / "inputs" / "trailers"
            workdir = workspace / "work"
            inputs.mkdir(parents=True, exist_ok=True)
            workdir.mkdir(parents=True, exist_ok=True)
            trailer = inputs / "clip.mp4"
            trailer.write_bytes(b"video")
            manifest_path = workdir / "download_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    [
                        {
                            "source_url": "https://example.com/trailer.mp4",
                            "local_path": str(trailer),
                            "tool": "yt-dlp",
                            "status": "downloaded",
                            "bytes": 5,
                            "media_kind": "video",
                        }
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            task = Task(
                id="data-ready-media",
                title="Data Acquisition",
                assigned_to="acquisition_specialist",
                status=TaskStatus.RUNNING,
                metadata={
                    "work_item_projection_id": "data_acquisition",
                    "work_item_role_id": "acquisition_specialist",
                    "original_message": "Collect official trailer video clips for a recommendation video.",
                    "data_acquisition_report": {
                        "status": "ready",
                        "designated_input_dir": str(workspace / "inputs"),
                        "download_manifest_path": str(manifest_path),
                        "prepared_assets": [str(trailer)],
                    },
                    "progress_log": [],
                },
            )
            gate = WorkItemGatePolicy(
                gate_type="automated_verification",
                on_reject="rework",
                rework_projection_id="data_acquisition",
                max_retries=1,
                metadata={
                    "readiness_artifact": "data_acquisition_report",
                    "allowed_statuses": ["ready", "already_present", "not_required"],
                    "blocking_statuses": ["partial", "missing_critical"],
                    "require_attempt_evidence_for_blocking": True,
                },
            )

            await executor._apply_data_acquisition_gate(task, gate)

            self.assertEqual(task.status, TaskStatus.DONE)

    async def test_data_acquisition_gate_requeues_same_work_item_on_blocking_status(self) -> None:
        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
        )
        task = Task(
            id="data-blocked-task",
            title="Data Acquisition",
            assigned_to="executor",
            status=TaskStatus.RUNNING,
            metadata={
                "work_item_projection_id": "data_acquisition",
                "data_acquisition_report": {
                    "status": "partial",
                    "attempted_sources": ["local archive scan", "browser search for licensed source footage"],
                    "blocked_reasons": ["No usable source footage was obtained yet."],
                    "acquisition_attempted": True,
                },
                "progress_log": [],
            },
        )
        gate = WorkItemGatePolicy(
            gate_type="automated_verification",
            on_reject="rework",
            rework_projection_id="data_acquisition",
            max_retries=1,
            metadata={
                "readiness_artifact": "data_acquisition_report",
                "allowed_statuses": ["ready", "already_present", "not_required"],
                "blocking_statuses": ["partial", "missing_critical"],
                "require_attempt_evidence_for_blocking": True,
            },
        )

        await executor._apply_data_acquisition_gate(task, gate)

        self.assertEqual(task.status, TaskStatus.PENDING)
        self.assertEqual(task.metadata["gate_rework_count"], 1)
        self.assertIn("partial", task.metadata["gate_review_feedback"])

    async def test_data_acquisition_gate_requeues_incomplete_blocking_status_without_attempt_evidence(self) -> None:
        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
        )
        task = Task(
            id="data-missing-evidence-task",
            title="Data Acquisition",
            assigned_to="executor",
            status=TaskStatus.RUNNING,
            metadata={
                "work_item_projection_id": "data_acquisition",
                "data_acquisition_report": {"status": "missing_critical"},
                "progress_log": [],
            },
        )
        gate = WorkItemGatePolicy(
            gate_type="automated_verification",
            on_reject="rework",
            rework_projection_id="data_acquisition",
            max_retries=1,
            metadata={
                "readiness_artifact": "data_acquisition_report",
                "allowed_statuses": ["ready", "already_present", "not_required"],
                "blocking_statuses": ["partial", "missing_critical"],
                "require_attempt_evidence_for_blocking": True,
            },
        )

        await executor._apply_data_acquisition_gate(task, gate)

        self.assertEqual(task.status, TaskStatus.PENDING)
        self.assertIn("require evidence of acquisition attempts", task.metadata["gate_review_feedback"])

    def test_data_acquisition_capture_reads_standard_report_and_log_files(self) -> None:
        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
        )
        with _workspace_tempdir() as tmpdir:
            workspace = tmpdir / "project"
            deliverables = workspace / "deliverables"
            inputs = workspace / "inputs"
            workdir = workspace / "work"
            manifests = workspace / ".openopc" / "manifests"
            deliverables.mkdir(parents=True, exist_ok=True)
            inputs.mkdir(parents=True, exist_ok=True)
            workdir.mkdir(parents=True, exist_ok=True)
            manifests.mkdir(parents=True, exist_ok=True)
            report_path = deliverables / "data_acquisition_report.json"
            log_path = deliverables / "data_acquisition_log.json"
            source_candidates_path = workdir / "source_candidates.json"
            download_manifest_path = workdir / "download_manifest.json"
            source_candidates_path.write_text(
                json.dumps(
                    [
                        {
                            "title": "Official Trailer",
                            "source_url": "https://example.com/trailer",
                            "host": "youtube.com",
                            "source_kind": "video",
                            "verified_official": True,
                            "verification_reason": "Official channel",
                        }
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            download_manifest_path.write_text(
                json.dumps(
                    [
                        {
                            "source_url": "https://example.com/trailer.mp4",
                            "local_path": str(inputs / "trailers" / "trailer.mp4"),
                            "tool": "yt-dlp",
                            "status": "downloaded",
                            "bytes": 1024,
                            "media_kind": "video",
                        }
                    ],
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            report_path.write_text(
                json.dumps(
                    {
                        "status": "partial",
                        "required_inputs": [
                            {"name": "Original source footage", "critical": True},
                            {"name": "Narration draft"},
                        ],
                        "present_inputs": [{"name": "Plot notes"}],
                        "missing_inputs": [{"name": "Original source footage", "critical": True}],
                        "attempted_tools": ["web_search", "yt-dlp"],
                        "source_candidates_path": str(source_candidates_path),
                        "download_manifest_path": str(download_manifest_path),
                        "provenance_summary": {"current_state": "not_ready", "rule": "Need episode and timecode traceability."},
                        "notes": ["Self-audit completed after acquisition attempts."],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            log_path.write_text(
                json.dumps(
                    {
                        "attempted_sources": ["browser search for 2003 series footage", "local archive scan"],
                        "attempted_tools": ["web_search", "yt-dlp"],
                        "prepared_assets": [str(inputs / "plot_notes.md")],
                        "blocked_reasons": ["No playable episode footage was acquired."],
                        "source_candidates_path": str(source_candidates_path),
                        "download_manifest_path": str(download_manifest_path),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            task = Task(
                id="data-capture-task",
                title="Data Acquisition",
                assigned_to="executor",
                status=TaskStatus.RUNNING,
                metadata={
                    "work_item_projection_id": "data_acquisition",
                    "workspace_manifest": {
                        "root_path": str(workspace),
                        "reserved_paths": {
                            "inputs": str(inputs),
                            "deliverables": str(deliverables),
                            "work": str(workdir),
                            ".openopc/manifests": str(manifests),
                        },
                    },
                },
            )

            executor._capture_data_acquisition_log(task, TaskResult(status=TaskStatus.DONE, content=""))
            executor._capture_data_acquisition_report(task, TaskResult(status=TaskStatus.DONE, content=""))
            executor._synthesize_data_acquisition_execution_record(task)

            report = task.metadata["data_acquisition_report"]
            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["designated_input_dir"], str(inputs))
            self.assertIn("Original source footage (critical)", report["required_inputs"])
            self.assertIn("Plot notes", report["present_inputs"])
            self.assertIn("browser search for 2003 series footage", report["attempted_sources"])
            self.assertIn("yt-dlp", report["attempted_tools"])
            self.assertIn(str(inputs / "plot_notes.md"), report["prepared_assets"])
            self.assertIn("No playable episode footage was acquired.", report["blocked_reasons"])
            self.assertTrue(report["acquisition_attempted"])
            self.assertEqual(report["report_path"], str(report_path))
            self.assertEqual(report["log_path"], str(log_path))
            self.assertEqual(report["source_candidates_path"], str(source_candidates_path))
            self.assertEqual(report["download_manifest_path"], str(download_manifest_path))
            execution_record = deliverables / "acquisition_execution_record.md"
            self.assertTrue(execution_record.is_file())
            self.assertIn("download_manifest.json", execution_record.read_text(encoding="utf-8"))

    async def test_external_resume_if_possible_uses_resume_mode_when_supported(self) -> None:
        engine = OPCEngine()
        engine.project_id = "proj1"
        engine.store = SimpleNamespace(
            get_latest_external_session_for_task=AsyncMock(
                return_value=ExternalSession(
                    agent_type="opencode",
                    project_id="proj1",
                    session_id="opencode-session-1",
                    task_id="task-1",
                    workspace_path="/tmp/work",
                    run_mode="interactive",
                    status="failed",
                    metadata={"resume_session_id": "resume-token-1"},
                    updated_at=datetime.now(),
                )
            )
        )
        task = Task(
            id="task-1",
            project_id="proj1",
            assigned_external_agent="opencode",
            metadata={
                "external_rework_strategy": "resume_if_possible",
                "gate_rework_request": {"feedback": "fix the missing artifact"},
            },
        )
        adapter = OpenCodeAdapter(config=ExternalAgentConfig(command="opencode"))

        run_adapter, resume_metadata = await engine._configure_external_adapter_for_task(task, adapter)

        self.assertEqual(run_adapter.config.session_mode, "resume")
        self.assertEqual(run_adapter.config.session_id, "resume-token-1")
        self.assertEqual(resume_metadata["resume_source_session"], "opencode-session-1")

    async def test_external_resume_if_possible_uses_saved_provider_session_for_codex(self) -> None:
        engine = OPCEngine()
        engine.project_id = "proj1"
        engine.store = SimpleNamespace(
            get_latest_external_session_for_task=AsyncMock(
                return_value=ExternalSession(
                    agent_type="codex",
                    project_id="proj1",
                    session_id="thread_1",
                    task_id="task-2",
                    workspace_path="/tmp/work",
                    run_mode="interactive",
                    status="failed",
                    metadata={},
                    updated_at=datetime.now(),
                )
            )
        )
        task = Task(
            id="task-2",
            project_id="proj1",
            assigned_external_agent="codex",
            metadata={
                "external_rework_strategy": "resume_if_possible",
                "contract_rework_request": {"issues": ["missing verification evidence"]},
            },
        )
        adapter = CodexAdapter(config=ExternalAgentConfig(command="codex"))

        run_adapter, resume_metadata = await engine._configure_external_adapter_for_task(task, adapter)

        self.assertEqual(run_adapter.config.session_mode, "resume")
        self.assertEqual(run_adapter.config.session_id, "thread_1")
        self.assertEqual(resume_metadata["resume_source_session"], "thread_1")

    async def test_top_level_company_session_forces_fresh_external_session_by_default(self) -> None:
        engine = OPCEngine()
        engine.project_id = "proj1"
        engine.store = SimpleNamespace(
            get_latest_external_session_for_task=AsyncMock(return_value=None),
            get_external_session=AsyncMock(return_value=None),
        )
        task = Task(
            id="task-top-level",
            project_id="proj1",
            session_id="sess-top",
            parent_session_id="sess-top",
            assigned_external_agent="codex",
            metadata={
                "work_item_runtime": True,
                "delegation_seat_id": "seat::team::ceo::ceo",
                "delegation_role_session_id": "role-runtime::run-1::seat::team::ceo::ceo",
                "external_resume_session_id": "stale-thread",
            },
        )
        adapter = CodexAdapter(config=ExternalAgentConfig(command="codex"))

        run_adapter, resume_metadata = await engine._configure_external_adapter_for_task(task, adapter)

        self.assertEqual(run_adapter.config.session_mode, "new")
        self.assertEqual(run_adapter.config.session_id, "")
        self.assertEqual(resume_metadata, {})
        self.assertNotIn("external_resume_session_id", task.metadata)
        self.assertNotIn("external_resume_session_scope_id", task.metadata)

    @unittest.skip("Filesystem handoff stack removed; see plans/task-cleanup-dead-comms.md"
    )
    async def test_structured_handoff_is_persisted_and_injected(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            memory = DummyMemory()

            async def execute_task(task: Task) -> TaskResult:
                result = TaskResult(
                    status=TaskStatus.DONE,
                    content="Decision: use SQLite\nRisk: guest posting must remain blocked",
                    artifacts={"workspace": "/tmp/demo", "files": ["src/app.py"]},
                )
                task.status = result.status
                task.result = {"content": result.content, "artifacts": result.artifacts}
                return result

            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=communication,
                approval_engine=SimpleNamespace(),
                memory=memory,
                execute_task=execute_task,
                save_task=store.save_task,
            )

            upstream = Task(
                id="planning-task",
                title="Planning",
                project_id="proj1",
                assigned_to="reviewer",
                status=TaskStatus.DONE,
                result={"content": "Plan approved with clear milestones.", "artifacts": {}},
                metadata={
                    "work_item_projection_id": "planning",
                    "work_item_summary_for_downstream": "Plan approved",
                    "decisions": ["Use SQLite for local persistence"],
                    "risks": ["Guest posting must remain blocked"],
                    "artifacts": ["doc: docs/plan.md"],
                    "acceptance_criteria": ["Implementation follows approved milestones"],
                },
            )
            downstream = Task(
                id="execution-task",
                title="Execution",
                project_id="proj1",
                assigned_to="executor",
                status=TaskStatus.PENDING,
                dependencies=["planning"],
                metadata={
                    "work_item_projection_id": "execution",
                    "work_item_role_id": "executor",
                    "work_item_execution_strategy": "native",
                    "work_item_gate": None,
                    "progress_log": [],
                },
            )
            await store.save_task(upstream)
            await store.save_task(downstream)

            await executor._run_work_item(downstream, {"planning": upstream, "execution": downstream})

            handoffs = await store.get_handoff_records(project_id="proj1", target_projection_id="execution")
            self.assertEqual(len(handoffs), 1)
            self.assertEqual(handoffs[0].payload["decisions"], ["Use SQLite for local persistence"])
            self.assertIn("Objective: Planning", downstream.metadata["handoff_context"])
            self.assertIn("Use SQLite for local persistence", downstream.metadata["handoff_context"])
            self.assertEqual(downstream.context_snapshot["handoff_payloads"][0]["source_projection_id"], "planning")
            self.assertEqual(memory.calls, [])
            await store.close()

    async def test_company_gate_prefers_structured_review_verdict_and_persists_work_item_state(self) -> None:
        memory = DummyMemory()

        async def execute_task(task: Task) -> TaskResult:
            result = TaskResult(
                status=TaskStatus.DONE,
                content=json.dumps(
                    {
                        "review_verdict": "reject",
                        "summary": "Execution lacks rollback coverage.",
                        "blocking_issues": ["Add failure-path tests before approval."],
                    }
                ),
                artifacts={
                    "artifact_index": [{"kind": "file", "label": "report", "value": "reports/review.md"}],
                },
            )
            task.status = result.status
            task.result = {"content": result.content, "artifacts": result.artifacts}
            return result

        saved: list[Task] = []

        async def save_task(task: Task) -> None:
            saved.append(task)

        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(refresh_waiting_tasks=AsyncMock(return_value=[]), detect_deadlocks=AsyncMock(return_value=[])),
            approval_engine=SimpleNamespace(),
            memory=memory,
            execute_task=execute_task,
            save_task=save_task,
        )
        projection_spec = WorkItemProjectionSpec(
            projection_id="qa_review",
            turn_type="review",
            title="QA Review",
            summary="Review the execution output.",
            role_id="reviewer",
            gate_policy=WorkItemGatePolicy(gate_type="review", on_reject="halt"),
        )
        plan = CompanyWorkItemRuntimePlan(
            profile="corporate",
            projections=[projection_spec],
            metadata={"runtime_policy": {"review": {"enable_work_item_gates": True}}},
        )
        task = Task(
            id="qa-task",
            title="QA Review",
            project_id="proj1",
            assigned_to="reviewer",
            status=TaskStatus.PENDING,
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_projection_id": "qa_review",
                "work_item_projection_title": "QA Review",
                "work_item_gate": {"type": "review", "on_reject": "halt", "metadata": {}},
                "runtime_policy": {"review": {"enable_work_item_gates": True}},
                "work_item_turn_type": "review",
                "work_item_runtime_plan": {"turn_type": "review", "summary": "Review the execution output."},
                "work_item_orchestration_profile": "company_review_fresh_eyes",
                "work_item_verification_required": False,
                "progress_log": [],
            },
        )

        result = await executor.execute(plan, [task])

        self.assertIn("[failed]", result.lower())
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.metadata["structured_review_verdict"]["label"], "reject")
        self.assertEqual(task.metadata["verification_status"]["label"], "review_reject")
        self.assertEqual(task.metadata["work_item_artifact_index"][0]["value"], "reports/review.md")
        self.assertEqual(task.metadata["work_item_summary"], task.result["content"][:800])

    async def test_company_review_gate_rejects_plaintext_verdict_without_legacy_flag(self) -> None:
        async def execute_task(task: Task) -> TaskResult:
            result = TaskResult(
                status=TaskStatus.DONE,
                content="APPROVED. The execution looks correct.",
                artifacts={"artifact_index": [{"kind": "file", "label": "report", "value": "reports/review.md"}]},
            )
            task.status = result.status
            task.result = {"content": result.content, "artifacts": result.artifacts}
            return result

        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(refresh_waiting_tasks=AsyncMock(return_value=[]), detect_deadlocks=AsyncMock(return_value=[])),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=execute_task,
            save_task=AsyncMock(),
        )
        projection_spec = WorkItemProjectionSpec(
            projection_id="qa_review",
            turn_type="review",
            title="QA Review",
            summary="Review the execution output.",
            role_id="reviewer",
            gate_policy=WorkItemGatePolicy(gate_type="review", on_reject="halt"),
        )
        task = Task(
            id="qa-plaintext",
            title="QA Review",
            project_id="proj1",
            assigned_to="reviewer",
            status=TaskStatus.PENDING,
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_projection_id": "qa_review",
                "work_item_projection_title": "QA Review",
                "work_item_gate": {"type": "review", "on_reject": "halt", "metadata": {}},
                "runtime_policy": {"review": {"enable_work_item_gates": True}},
                "work_item_turn_type": "review",
                "work_item_runtime_plan": {"turn_type": "review", "summary": "Review output."},
                "work_item_orchestration_profile": "company_review_fresh_eyes",
                "work_item_verification_required": False,
                "progress_log": [],
            },
        )

        result = await executor.execute(
            CompanyWorkItemRuntimePlan(profile="corporate", projections=[projection_spec], metadata={}),
            [task],
        )

        self.assertIn("[failed]", result.lower())
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.metadata["artifact_contract_status"], "not_required")
        self.assertEqual(task.metadata.get("structured_review_verdict", {}).get("label", ""), "")

    async def test_execute_work_item_without_verification_evidence_pauses_for_review(self) -> None:
        saved_statuses: list[TaskStatus] = []
        attempts = 0

        async def execute_task(task: Task) -> TaskResult:
            nonlocal attempts
            attempts += 1
            result = TaskResult(
                status=TaskStatus.DONE,
                content="Implementation complete without verifier evidence.",
                artifacts={
                    "runtime_session_id": "rt_execute",
                    "verification": {"completed": True, "passed": True, "verdict": "VERIFIED: looks good"},
                },
            )
            task.status = result.status
            task.result = {"content": result.content, "artifacts": result.artifacts}
            return result

        async def save_task(task: Task) -> None:
            saved_statuses.append(task.status)

        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(refresh_waiting_tasks=AsyncMock(return_value=[]), detect_deadlocks=AsyncMock(return_value=[])),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=execute_task,
            save_task=save_task,
        )
        task = Task(
            id="execute-no-evidence",
            title="Engineering Execution",
            project_id="proj1",
            assigned_to="executor",
            status=TaskStatus.PENDING,
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_projection_id": "execution",
                "work_item_projection_title": "Engineering Execution",
                "work_item_turn_type": "execute",
                "ownership_contract": {
                    "summary": "Implement the API slice.",
                    "write_scope": "assigned_workspace",
                    "expected_artifacts": ["Working API code"],
                    "downstream_consumer": ["reviewer"],
                    "allowed_collaboration_targets": ["reviewer"],
                    "status": "pending",
                    "issues": [],
                },
                "work_item_runtime_plan": {
                    "turn_type": "execute",
                    "summary": "Implement the API slice.",
                    "deliverables": ["Working API code"],
                    "acceptance_criteria": ["API matches the approved plan."],
                    "verification_required": True,
                },
                "work_item_verification_required": True,
                "progress_log": [],
                "work_item_gate": None,
            },
        )

        executor._active_plan = CompanyWorkItemRuntimePlan(profile="corporate", projections=[], metadata={})
        executor._active_tasks = [task]
        await executor._run_work_item(task, {task.id: task, "execution": task})

        self.assertEqual(attempts, 3)
        self.assertEqual(task.status, TaskStatus.AWAITING_MANAGER_REVIEW)
        self.assertEqual(task.metadata["artifact_contract_status"], "failed")
        self.assertEqual(task.metadata["contract_rework_count"], 2)
        self.assertIn("Verification evidence is missing", "\n".join(task.metadata["progress_log"]))
        self.assertIn(TaskStatus.AWAITING_MANAGER_REVIEW, saved_statuses)

    async def test_execute_work_item_contract_rework_retries_until_outputs_are_complete(self) -> None:
        attempts = 0

        async def execute_task(task: Task) -> TaskResult:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                result = TaskResult(
                    status=TaskStatus.DONE,
                    content="First pass omitted verification evidence.",
                    artifacts={
                        "runtime_session_id": "rt_execute",
                        "verification": {"completed": True, "passed": True, "verdict": "VERIFIED: looks good"},
                    },
                )
            else:
                result = TaskResult(
                    status=TaskStatus.DONE,
                    content="Implementation complete with verification evidence.",
                    artifacts={
                        "runtime_session_id": "rt_execute",
                        "verification": {"completed": True, "passed": True, "verdict": "VERIFIED: looks good"},
                        "verification_evidence": {
                            "status": "provided",
                            "verdict": "pass",
                            "summary": "Smoke checks passed.",
                            "checks": [{"check": "smoke", "command": "pytest -q", "observed_output": "1 passed", "result": "PASS"}],
                        },
                    },
                )
            task.status = result.status
            task.result = {"content": result.content, "artifacts": result.artifacts}
            return result

        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(refresh_waiting_tasks=AsyncMock(return_value=[]), detect_deadlocks=AsyncMock(return_value=[])),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=execute_task,
            save_task=AsyncMock(),
        )
        task = Task(
            id="execute-contract-rework",
            title="Engineering Execution",
            project_id="proj1",
            assigned_to="executor",
            status=TaskStatus.PENDING,
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_projection_id": "execution",
                "work_item_projection_title": "Engineering Execution",
                "work_item_turn_type": "execute",
                "ownership_contract": {
                    "summary": "Implement the API slice.",
                    "write_scope": "assigned_workspace",
                    "expected_artifacts": ["Working API code"],
                    "downstream_consumer": ["reviewer"],
                    "allowed_collaboration_targets": ["reviewer"],
                    "status": "pending",
                    "issues": [],
                },
                "work_item_runtime_plan": {
                    "turn_type": "execute",
                    "summary": "Implement the API slice.",
                    "deliverables": ["Working API code"],
                    "acceptance_criteria": ["API matches the approved plan."],
                    "verification_required": True,
                },
                "work_item_verification_required": True,
                "progress_log": [],
                "work_item_gate": None,
            },
        )

        executor._active_plan = CompanyWorkItemRuntimePlan(profile="corporate", projections=[], metadata={})
        executor._active_tasks = [task]
        result = await executor._run_work_item(task, {task.id: task, "execution": task})

        self.assertEqual(attempts, 2)
        self.assertEqual(task.status, TaskStatus.DONE)
        self.assertEqual(task.metadata["artifact_contract_status"], "satisfied")
        self.assertEqual(task.metadata["verification_status"]["label"], "verified")
        self.assertEqual(task.metadata["contract_rework_count"], 1)
        self.assertEqual(result.status, TaskStatus.DONE)

    async def test_execute_work_item_allows_unavailable_verification_evidence_without_reprompt_loop(self) -> None:
        async def execute_task(task: Task) -> TaskResult:
            result = TaskResult(
                status=TaskStatus.DONE,
                content="Implementation completed; verification tooling was unavailable in this runtime.",
                artifacts={
                    "runtime_session_id": "rt_execute",
                    "verification": {
                        "completed": True,
                        "passed": False,
                        "verdict": "Verification unavailable in this runtime.",
                    },
                    "verification_evidence": {
                        "status": "unavailable",
                        "verdict": "partial",
                        "summary": "Verification unavailable: verifier could not collect executable evidence in this runtime.",
                        "checks": [],
                    },
                },
            )
            task.status = result.status
            task.result = {"content": result.content, "artifacts": result.artifacts}
            return result

        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(refresh_waiting_tasks=AsyncMock(return_value=[]), detect_deadlocks=AsyncMock(return_value=[])),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=execute_task,
            save_task=AsyncMock(),
        )
        task = Task(
            id="execute-verification-unavailable",
            title="Engineering Execution",
            project_id="proj1",
            assigned_to="executor",
            status=TaskStatus.PENDING,
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_projection_id": "execution",
                "work_item_projection_title": "Engineering Execution",
                "work_item_turn_type": "execute",
                "ownership_contract": {
                    "summary": "Implement the API slice.",
                    "write_scope": "assigned_workspace",
                    "expected_artifacts": ["Working API code"],
                    "downstream_consumer": ["reviewer"],
                    "allowed_collaboration_targets": ["reviewer"],
                    "status": "pending",
                    "issues": [],
                },
                "work_item_runtime_plan": {
                    "turn_type": "execute",
                    "summary": "Implement the API slice.",
                    "deliverables": ["Working API code"],
                    "acceptance_criteria": ["API matches the approved plan."],
                    "verification_required": True,
                },
                "work_item_verification_required": True,
                "progress_log": [],
                "work_item_gate": None,
            },
        )

        executor._active_plan = CompanyWorkItemRuntimePlan(profile="corporate", projections=[], metadata={})
        executor._active_tasks = [task]
        result = await executor._run_work_item(task, {task.id: task, "execution": task})

        self.assertEqual(task.status, TaskStatus.DONE)
        self.assertEqual(task.metadata["artifact_contract_status"], "satisfied")
        self.assertEqual(task.metadata["verification_status"]["label"], "not_verified")
        self.assertEqual(result.status, TaskStatus.DONE)

    async def test_company_executor_pauses_before_learning_when_feedback_is_required(self) -> None:
        memory = DummyMemory()
        checkpoints: list[dict[str, object]] = []
        saved_statuses: list[TaskStatus] = []

        async def execute_task(task: Task) -> TaskResult:
            return TaskResult(
                status=TaskStatus.DONE,
                content="Final delivery is ready for user review.",
                artifacts={"workspace": "/tmp/demo"},
            )

        async def save_task(task: Task) -> None:
            saved_statuses.append(task.status)

        async def checkpoint_callback(data: dict[str, object]) -> None:
            checkpoints.append(dict(data))

        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=memory,
            execute_task=execute_task,
            save_task=save_task,
            checkpoint_callback=checkpoint_callback,
        )

        task = Task(
            id="delivery-task",
            title="CEO Final Delivery",
            project_id="proj1",
            assigned_to="executor",
            status=TaskStatus.DONE,
            result={
                "content": "Final delivery is ready for user review.",
                "artifacts": {"workspace": "/tmp/demo"},
            },
            metadata={
                "execution_mode": "company_mode",
                "company_profile": "corporate",
                "work_item_projection_id": "ceo_delivery",
                "work_item_projection_title": "CEO Final Delivery",
                "work_item_turn_type": "deliver",
                "authoritative_output": True,
                "user_visible": True,
                "requires_user_feedback": True,
                "feedback_scope": "final",
                "progress_log": [],
            },
        )

        executor._active_plan = CompanyWorkItemRuntimePlan(profile="corporate", projections=[])
        executor._active_tasks = [task]
        await executor._finalize_completed_work_item(task)

        self.assertEqual(task.status, TaskStatus.AWAITING_HUMAN)
        self.assertIn("Awaiting user feedback before learning from this delivery.", task.metadata["progress_log"])
        self.assertEqual(memory.calls, [])
        self.assertEqual(saved_statuses, [TaskStatus.AWAITING_HUMAN])
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["checkpoint_type"], "company_delivery_feedback")
        self.assertEqual(checkpoints[0]["payload"]["feedback_scope"], "final")
        self.assertEqual(checkpoints[0]["payload"]["review_level"], "human")

    async def test_company_executor_creates_feedback_checkpoint_without_active_plan_after_resume(self) -> None:
        checkpoints: list[dict[str, object]] = []

        async def execute_task(task: Task) -> TaskResult:
            return TaskResult(status=TaskStatus.DONE, content="Final delivery is ready.", artifacts={})

        async def save_task(task: Task) -> None:
            _ = task

        async def checkpoint_callback(data: dict[str, object]) -> None:
            checkpoints.append(dict(data))

        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=execute_task,
            save_task=save_task,
            checkpoint_callback=checkpoint_callback,
        )

        task = Task(
            id="delivery-task",
            title="CEO Final Delivery",
            project_id="proj1",
            assigned_to="executor",
            status=TaskStatus.DONE,
            session_id="parent-session:delivery-work-item",
            parent_session_id="parent-session",
            result={"content": "Final delivery is ready.", "artifacts": {}},
            metadata={
                "execution_mode": "company_mode",
                "company_profile": "corporate",
                "work_item_projection_id": "ceo_delivery",
                "work_item_projection_title": "CEO Final Delivery",
                "work_item_turn_type": "deliver",
                "authoritative_output": True,
                "user_visible": True,
                "review_owner_kind": "human",
                "requires_user_feedback": True,
                "feedback_scope": "final",
                "progress_log": [],
            },
        )

        executor._active_plan = None
        executor._active_tasks = []
        await executor._finalize_completed_work_item(task)

        self.assertEqual(task.status, TaskStatus.AWAITING_HUMAN)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["checkpoint_type"], "company_delivery_feedback")
        self.assertEqual(checkpoints[0]["session_id"], "parent-session:delivery-work-item")
        self.assertEqual(checkpoints[0]["payload"]["feedback_scope"], "final")
        self.assertEqual(checkpoints[0]["payload"]["task_ids"], ["delivery-task"])

    async def test_multi_team_final_delivery_completion_creates_feedback_checkpoint_from_work_item_metadata(self) -> None:
        checkpoints: list[dict[str, object]] = []

        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            try:
                async def execute_task(task: Task) -> TaskResult:
                    return TaskResult(
                        status=TaskStatus.DONE,
                        content="Final delivery is ready for user review.",
                        artifacts={"workspace": str(tmpdir)},
                    )

                async def save_task(task: Task) -> None:
                    await store.save_task(task)

                async def checkpoint_callback(data: dict[str, object]) -> None:
                    checkpoints.append(dict(data))

                executor = CompanyWorkItemExecutor(
                    org_engine=DummyOrgEngine(),
                    communication=SimpleNamespace(),
                    approval_engine=SimpleNamespace(),
                    memory=None,
                    execute_task=execute_task,
                    save_task=save_task,
                    checkpoint_callback=checkpoint_callback,
                    store=store,
                )

                task = Task(
                    id="delivery-task",
                    title="CEO Final Delivery",
                    project_id="proj1",
                    assigned_to="executor",
                    status=TaskStatus.PENDING,
                    metadata={
                        "execution_mode": "company_mode",
                        "company_profile": "corporate",
                        "runtime_model": "multi_team_org",
                        "delegation_run_id": "run-1",
                        "work_item_projection_id": "ceo_delivery",
                        "work_item_turn_type": "deliver",
                        "work_kind": "delivery",
                        "progress_log": [],
                    },
                )
                set_linked_work_item_id(task, "delivery-wi")
                work_item = DelegationWorkItem(
                    work_item_id="delivery-wi",
                    run_id="run-1",
                    cell_id="team::ceo",
                    team_id="team::ceo",
                    role_id="executor",
                    seat_id="seat::team::ceo::executor",
                    manager_role_id="owner",
                    manager_seat_id="seat::owner",
                    title="CEO Final Delivery",
                    kind="delivery",
                    projection_id="ceo_delivery",
                    phase=Phase.READY,
                    metadata={
                        "runtime_model": "multi_team_org",
                        "execution_mode": "company_mode",
                        "work_item_projection_id": "ceo_delivery",
                        "work_item_turn_type": "deliver",
                        "work_kind": "delivery",
                        "review_owner_kind": "human",
                        "user_visible": True,
                        "authoritative_output": True,
                        "requires_user_feedback": True,
                        "feedback_scope": "final",
                    },
                )
                await store.save_task(task)
                await store.save_delegation_work_item(work_item)
                await store.link_work_item_runtime_task(work_item.work_item_id, task.id)

                executor._active_plan = CompanyWorkItemRuntimePlan(profile="corporate", projections=[])
                executor._active_tasks = [task]
                result = await executor._run_work_item(task, {task.id: task, "ceo_delivery": task})

                refreshed_task = await store.get_task(task.id)
                refreshed_item = await store.get_delegation_work_item(work_item.work_item_id)
                self.assertEqual(result.status, TaskStatus.DONE)
                self.assertEqual(task.status, TaskStatus.AWAITING_HUMAN)
                self.assertEqual(refreshed_task.status, TaskStatus.AWAITING_HUMAN)
                self.assertEqual(refreshed_item.phase, Phase.AWAITING_HUMAN)
                self.assertEqual(task.metadata["feedback_scope"], "final")
                self.assertTrue(task.metadata["requires_user_feedback"])
                self.assertEqual(len(checkpoints), 1)
                self.assertEqual(checkpoints[0]["checkpoint_type"], "company_delivery_feedback")
                self.assertEqual(checkpoints[0]["payload"]["feedback_scope"], "final")
                self.assertEqual(checkpoints[0]["payload"]["waiting_task_id"], task.id)
            finally:
                await store.close()

    async def test_run_work_item_persists_human_waiting_result_to_task_and_work_item(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            try:
                async def execute_task(task: Task) -> TaskResult:
                    return TaskResult(
                        status=TaskStatus.AWAITING_HUMAN,
                        content="Questions:\n- Which product and audience should I use?",
                        artifacts={"requires_user_input": True},
                    )

                async def save_task(task: Task) -> None:
                    await store.save_task(task)

                executor = CompanyWorkItemExecutor(
                    org_engine=DummyOrgEngine(),
                    communication=SimpleNamespace(),
                    approval_engine=SimpleNamespace(),
                    memory=None,
                    execute_task=execute_task,
                    save_task=save_task,
                    store=store,
                )

                task = Task(
                    id="human-wait-task",
                    title="Clarify product audience",
                    project_id="proj1",
                    assigned_to="executor",
                    status=TaskStatus.PENDING,
                    metadata={
                        "execution_mode": ExecutionMode.COMPANY_MODE.value,
                        "runtime_model": "multi_team_org",
                        "delegation_run_id": "run-human-wait",
                        "work_item_projection_id": "content_execution",
                        "work_item_projection_title": "Content execution",
                        "work_item_turn_type": "execute",
                        "progress_log": [],
                    },
                )
                set_linked_work_item_id(task, "content-wi")
                work_item = DelegationWorkItem(
                    work_item_id="content-wi",
                    run_id="run-human-wait",
                    cell_id="team::marketing",
                    team_id="team::marketing",
                    role_id="executor",
                    seat_id="seat::team::marketing::executor",
                    manager_role_id="cmo",
                    manager_seat_id="seat::team::marketing::cmo",
                    title="Content execution",
                    kind="execution",
                    projection_id="content_execution",
                    phase=Phase.READY,
                    metadata={
                        "runtime_model": "multi_team_org",
                        "execution_mode": ExecutionMode.COMPANY_MODE.value,
                        "work_item_projection_id": "content_execution",
                        "work_item_turn_type": "execute",
                    },
                )
                await store.save_task(task)
                await store.save_delegation_work_item(work_item)
                await store.link_work_item_runtime_task(work_item.work_item_id, task.id)

                executor._active_plan = CompanyWorkItemRuntimePlan(profile="corporate", projections=[])
                executor._active_tasks = [task]
                result = await executor._run_work_item(task, {task.id: task, "content_execution": task})

                refreshed_task = await store.get_task(task.id)
                refreshed_item = await store.get_delegation_work_item(work_item.work_item_id)

                self.assertEqual(result.status, TaskStatus.AWAITING_HUMAN)
                self.assertEqual(task.status, TaskStatus.AWAITING_HUMAN)
                self.assertEqual(refreshed_task.status, TaskStatus.AWAITING_HUMAN)
                self.assertEqual(refreshed_item.phase, Phase.AWAITING_HUMAN)
                self.assertIn("Work item paused awaiting human review.", refreshed_item.metadata.get("progress_log", []))
            finally:
                await store.close()

    async def test_company_executor_withholds_authoritative_delivery_and_reuses_work_item_session_for_rework(self) -> None:
        llm = DummySelectionLLM(
            responses=[
                json.dumps(
                    {
                        "deliverable": False,
                        "summary": "Engineering still has unresolved issues.",
                        "rework_targets": [
                            {
                                "target_projection_id": "engineering_execution",
                                "role_id": "executor",
                                "feedback": "Fix the unresolved engineering issue before delivery.",
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            ]
        )
        checkpoints: list[dict[str, object]] = []

        async def execute_task(task: Task) -> TaskResult:
            _ = task
            return TaskResult(status=TaskStatus.DONE, content="unused", artifacts={})

        async def save_task(task: Task) -> None:
            _ = task

        async def checkpoint_callback(data: dict[str, object]) -> None:
            checkpoints.append(dict(data))

        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            llm=llm,
            execute_task=execute_task,
            save_task=save_task,
            checkpoint_callback=checkpoint_callback,
        )
        execution_task = Task(
            id="execution-task",
            title="Execution",
            status=TaskStatus.DONE,
            project_id="proj1",
            assigned_to="executor",
            result={"content": "Initial implementation.", "artifacts": {}},
            metadata={
                "execution_mode": "company_mode",
                "work_item_projection_id": "engineering_execution",
                "work_item_projection_title": "Engineering Execution",
                "work_item_turn_type": "execute",
                "work_item_summary": "Initial implementation completed.",
                "member_session_id": "member::proj1::executor::eng-1",
                "member_session_state": {
                    "member_session_id": "member::proj1::executor::eng-1",
                    "role_id": "executor",
                    "employee_id": "eng-1",
                    "working_memory": ["Initial implementation completed."],
                    "resume_state": {"resume_cursor": 3},
                },
                "adaptive": {
                    "normalized_state": "done",
                    "work_item_profile": {"turn_kind": "execute"},
                    "signals": [],
                },
                "employee_assignment": {
                    "employee_id": "eng-1",
                    "name": "Engineer One",
                    "role_id": "executor",
                },
                "progress_log": [],
            },
        )
        delivery_task = Task(
            id="delivery-task",
            title="CEO Final Delivery",
            status=TaskStatus.DONE,
            project_id="proj1",
            assigned_to="reviewer",
            result={"content": "Ready for delivery.", "artifacts": {}},
            dependencies=["engineering_execution"],
            metadata={
                "execution_mode": "company_mode",
                "company_profile": "corporate",
                "work_item_projection_id": "ceo_delivery",
                "work_item_projection_title": "CEO Final Delivery",
                "work_item_turn_type": "deliver",
                "authoritative_output": True,
                "user_visible": True,
                "requires_user_feedback": True,
                "feedback_scope": "final",
                "adaptive": {
                    "normalized_state": "done",
                    "work_item_profile": {"turn_kind": "deliver"},
                    "signals": [],
                },
                "progress_log": [],
            },
        )

        executor._active_plan = CompanyWorkItemRuntimePlan(
            profile="corporate",
            projections=[
                WorkItemProjectionSpec(projection_id="engineering_execution", turn_type="execute", title="Execution", summary="", role_id="executor"),
                WorkItemProjectionSpec(
                    projection_id="ceo_delivery",
                    turn_type="deliver",
                    title="CEO Final Delivery",
                    summary="",
                    role_id="reviewer",
                    dependency_projection_ids=["engineering_execution"],
                    metadata={"authoritative_output": True, "requires_user_feedback": True, "feedback_scope": "final"},
                ),
            ],
        )
        executor._active_tasks = [execution_task, delivery_task]

        await executor._finalize_completed_work_item(delivery_task)

        self.assertEqual(execution_task.status, TaskStatus.PENDING)
        self.assertEqual(delivery_task.status, TaskStatus.PENDING)
        self.assertEqual(execution_task.metadata["member_session_id"], "member::proj1::executor::eng-1")
        self.assertEqual(execution_task.context_snapshot["latest_ceo_rework"]["target_projection_id"], "engineering_execution")
        self.assertEqual(delivery_task.metadata["adaptive"]["normalized_state"], "invalidated")
        self.assertEqual(execution_task.result, None)
        self.assertEqual(checkpoints, [])
        prompt = json.loads(llm.calls[0]["prompt"])
        self.assertIn("role_task_map", prompt)
        self.assertIn("executor", prompt["role_task_map"])

    def test_summarize_results_prefers_authoritative_final_delivery(self) -> None:
        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            store=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
        )
        plan = CompanyWorkItemRuntimePlan(
            profile="corporate",
            projections=[
                WorkItemProjectionSpec(projection_id="engineering_execution", turn_type="execute", title="Engineering Execution", summary="", role_id="executor"),
                WorkItemProjectionSpec(projection_id="ceo_delivery", turn_type="deliver", title="CEO Final Delivery", summary="", role_id="reviewer"),
            ],
        )
        tasks = [
            Task(
                id="eng",
                title="Engineering Execution",
                project_id="proj1",
                status=TaskStatus.DONE,
                metadata={
                    "work_item_projection_id": "engineering_execution",
                    "work_item_summary": "Implemented the requested API behavior.",
                    "work_item_artifact_index": [{"kind": "code", "label": "api", "value": "src/api.py"}],
                },
                result={"content": "Implementation details", "artifacts": {}},
            ),
            Task(
                id="delivery",
                title="CEO Final Delivery",
                project_id="proj1",
                status=TaskStatus.DONE,
                metadata={
                    "work_item_projection_id": "ceo_delivery",
                    "user_visible": True,
                    "authoritative_output": True,
                    "work_item_turn_type": "deliver",
                    "delivery_package": {
                        "executive_summary": "The API change is complete and ready for handoff.",
                        "next_steps": ["Monitor rollout metrics."],
                    },
                },
                result={"content": "Do not fall back to naive concatenation.", "artifacts": {}},
            ),
        ]

        summary = executor._summarize_results(plan, tasks)

        self.assertIn("## Final Delivery", summary)
        self.assertIn("The API change is complete and ready for handoff.", summary)
        self.assertIn("Engineering Execution: Implemented the requested API behavior.", summary)
        self.assertNotIn("### CEO Final Delivery [done]", summary)

    async def test_run_work_item_without_gate_emits_completed_work_item_progress(self) -> None:
        progress_events: list[tuple[str, str | None]] = []
        saved_statuses: list[TaskStatus] = []

        async def execute_task(task: Task) -> TaskResult:
            result = TaskResult(
                status=TaskStatus.DONE,
                content="Implementation complete.",
                artifacts={},
            )
            task.status = result.status
            task.result = {"content": result.content, "artifacts": result.artifacts}
            return result

        async def save_task(task: Task) -> None:
            saved_statuses.append(task.status)

        async def progress_callback(message: str, task_id: str | None = None) -> None:
            progress_events.append((message, task_id))

        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=execute_task,
            save_task=save_task,
            progress_callback=progress_callback,
        )

        task = Task(
            id="implementation-task",
            title="Implementation",
            project_id="proj1",
            assigned_to="executor",
            status=TaskStatus.PENDING,
            metadata={
                "execution_mode": "company_mode",
                "company_profile": "corporate",
                "work_item_projection_id": "implementation",
                "progress_log": [],
            },
        )

        executor._active_plan = CompanyWorkItemRuntimePlan(profile="corporate", projections=[])
        executor._active_tasks = [task]
        await executor._run_work_item(task, {task.id: task, "implementation": task})

        self.assertEqual(task.status, TaskStatus.DONE)
        self.assertIn("Work item completed by role executor.", task.metadata["progress_log"])
        self.assertIn(("[Company:implementation] completed", "implementation-task"), progress_events)
        self.assertEqual(saved_statuses, [TaskStatus.RUNNING, TaskStatus.DONE])

    async def test_assign_task_execution_agent_prefers_llm_decision(self) -> None:
        engine = OPCEngine()
        engine.org_engine = DummyOrgEngine()
        adapter = SimpleNamespace(supports_interactive=lambda: True)
        engine.adapter_registry = SimpleNamespace(
            list_available=lambda: ["codex", "cursor"],
            get_ordered_available=lambda: [("codex", adapter), ("cursor", adapter)],
            get=lambda name: adapter,
        )
        engine.llm = DummySelectionLLM(selected_agent="cursor", reasoning="Cursor is better for this execution task")

        task = Task(
            title="Implement backend API",
            description="Implement the API, edit files, run tests, and update the repository artifacts.",
            project_id="proj1",
        )

        selected = await engine._assign_task_execution_agent(task)

        self.assertEqual(task.assigned_to, "executor")
        self.assertEqual(selected, "cursor")
        self.assertEqual(task.metadata["agent_selection"]["selected"], "cursor")
        self.assertEqual(task.metadata["agent_selection"]["selection_source"], "llm")
        self.assertEqual(task.metadata["agent_selection"]["llm_attempts"], 1)
        self.assertEqual(engine.llm.calls[0]["task_type"], "quick_tasks")

    async def test_assign_task_execution_agent_skips_llm_when_no_credentials(self) -> None:
        engine = OPCEngine()
        engine.org_engine = DummyOrgEngine()
        adapter = SimpleNamespace(supports_interactive=lambda: True)
        engine.adapter_registry = SimpleNamespace(
            list_available=lambda: ["codex", "cursor"],
            get_ordered_available=lambda: [("codex", adapter), ("cursor", adapter)],
            get=lambda name: adapter,
        )
        engine.llm = DummySelectionLLM(selected_agent="cursor", has_creds=False)

        task = Task(
            title="Implement backend API",
            description="Implement the API, edit files, run tests, and update the repository artifacts.",
            project_id="proj1",
        )

        selected = await engine._assign_task_execution_agent(task)

        # No LLM call attempted; selection comes from rule-based fallback.
        self.assertEqual(len(engine.llm.calls), 0)
        self.assertEqual(task.metadata["agent_selection"]["selection_source"], "fallback_rules")
        self.assertEqual(task.metadata["agent_selection"]["llm_attempts"], 0)

    async def test_assign_task_execution_agent_preserves_explicit_recruitment_override(self) -> None:
        engine = OPCEngine()
        engine.org_engine = DummyOrgEngine()
        adapter = SimpleNamespace(supports_interactive=lambda: True)
        engine.adapter_registry = SimpleNamespace(
            list_available=lambda: ["codex", "cursor"],
            get_ordered_available=lambda: [("codex", adapter), ("cursor", adapter)],
            get=lambda name: adapter,
        )
        engine.llm = DummySelectionLLM(selected_agent="cursor", reasoning="Cursor would normally win")

        task = Task(
            title="Implementation",
            description="Implement backend API",
            project_id="proj1",
            assigned_to="executor",
            assigned_external_agent="codex",
            metadata={
                "selected_execution_agent": "codex",
                "execution_agent_locked": True,
            },
        )

        selected = await engine._assign_task_execution_agent(task)

        self.assertEqual(selected, "codex")
        self.assertEqual(task.assigned_external_agent, "codex")
        self.assertEqual(task.metadata["agent_selection"]["selected"], "codex")
        self.assertEqual(task.metadata["agent_selection"]["selection_source"], "explicit_recruitment_override")
        self.assertEqual(len(engine.llm.calls), 0)

    async def test_assign_task_execution_agent_falls_back_to_native_when_no_external_agents(self) -> None:
        engine = OPCEngine()
        engine.org_engine = DummyOrgEngine()
        engine.adapter_registry = SimpleNamespace(list_available=lambda: [])
        engine.llm = DummySelectionLLM(selected_agent="codex", reasoning="Would use Codex if available")

        task = Task(
            title="Implement backend API",
            description="Implement the API, edit files, run tests, and update the repository artifacts.",
            project_id="proj1",
        )

        selected = await engine._assign_task_execution_agent(task)

        self.assertEqual(task.assigned_to, "executor")
        self.assertIsNone(selected)
        self.assertEqual(task.metadata["agent_selection"]["selected"], "native")
        self.assertEqual(task.metadata["agent_selection"]["selection_source"], "fallback_rules")
        self.assertEqual(task.metadata["agent_selection"]["llm_attempts"], 3)

    async def test_assign_task_execution_agent_retries_llm_before_fallback(self) -> None:
        engine = OPCEngine()
        engine.org_engine = DummyOrgEngine()
        adapter = SimpleNamespace(supports_interactive=lambda: True)
        engine.adapter_registry = SimpleNamespace(
            list_available=lambda: ["codex", "cursor"],
            get_ordered_available=lambda: [("codex", adapter), ("cursor", adapter)],
            get=lambda name: adapter,
        )
        engine.llm = DummySelectionLLM(
            responses=[
                '{"selected_agent":"unknown_agent","reasoning":"bad response"}',
                "not json at all",
                '{"selected_agent":"cursor","reasoning":"corrected after feedback"}',
            ]
        )

        task = Task(
            title="Implement backend API",
            description="Implement the API, edit files, run tests, and update the repository artifacts.",
            project_id="proj1",
        )

        selected = await engine._assign_task_execution_agent(task)

        self.assertEqual(selected, "cursor")
        self.assertEqual(task.metadata["agent_selection"]["selection_source"], "llm")
        self.assertEqual(task.metadata["agent_selection"]["llm_attempts"], 3)
        self.assertEqual(len(engine.llm.calls), 3)
        self.assertIn("retry_feedback", engine.llm.calls[1]["prompt"])
        self.assertIn("Invalid selected_agent", engine.llm.calls[1]["prompt"])
        self.assertIn("Response was not valid JSON", engine.llm.calls[2]["prompt"])

    async def test_assign_task_execution_agent_falls_back_after_three_llm_failures(self) -> None:
        engine = OPCEngine()
        engine.org_engine = DummyOrgEngine()
        adapter = SimpleNamespace(supports_interactive=lambda: True)
        engine.adapter_registry = SimpleNamespace(
            list_available=lambda: ["codex", "cursor"],
            get_ordered_available=lambda: [("codex", adapter), ("cursor", adapter)],
            get=lambda name: adapter,
        )
        engine.llm = DummySelectionLLM(
            responses=[
                '{"selected_agent":"wrong","reasoning":"bad"}',
                RuntimeError("temporary failure"),
                "still not json",
            ]
        )

        task = Task(
            title="Implement backend API",
            description="Implement the API, edit files, run tests, and update the repository artifacts.",
            project_id="proj1",
        )

        selected = await engine._assign_task_execution_agent(task)

        self.assertEqual(selected, "codex")
        self.assertEqual(task.metadata["agent_selection"]["selection_source"], "fallback_rules")
        self.assertEqual(task.metadata["agent_selection"]["llm_attempts"], 3)

    async def test_assign_task_execution_agent_prefers_native_for_company_execute_native_first(self) -> None:
        engine = OPCEngine()
        engine.org_engine = DummyOrgEngine()
        adapter = SimpleNamespace(supports_interactive=lambda: True)
        engine.adapter_registry = SimpleNamespace(
            list_available=lambda: ["codex", "cursor"],
            get_ordered_available=lambda: [("codex", adapter), ("cursor", adapter)],
            get=lambda name: adapter,
        )
        engine.llm = DummySelectionLLM(responses=[RuntimeError("temporary failure"), RuntimeError("temporary failure"), RuntimeError("temporary failure")])

        task = Task(
            title="Engineering Execution",
            description="Implement the backend API, edit files, run tests, and prepare the downstream review artifacts.",
            project_id="proj1",
            assigned_to="executor",
            assigned_external_agent="codex",
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "work_item_projection_title": "Engineering Execution",
                "work_item_turn_type": "execute",
                "work_item_orchestration_profile": "company_execute_native_first",
            },
        )

        selected = await engine._assign_task_execution_agent(task)

        self.assertIsNone(selected)
        self.assertEqual(task.metadata["agent_selection"]["decision_reason"], "company_execute_native_first")
        self.assertEqual(task.metadata["agent_selection"]["selection_source"], "fallback_rules")

    async def test_company_executor_uses_agent_selector_callback(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            memory = DummyMemory()
            selections: list[tuple[str, str]] = []

            async def agent_selector(task: Task, role) -> str | None:
                selections.append((task.id, role.role_id))
                task.assigned_external_agent = "codex"
                return "codex"

            async def execute_task(task: Task) -> TaskResult:
                self.assertEqual(task.assigned_external_agent, "codex")
                result = TaskResult(
                    status=TaskStatus.DONE,
                    content="executed with selected agent",
                    artifacts={},
                )
                task.status = result.status
                task.result = {"content": result.content, "artifacts": result.artifacts}
                return result

            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=communication,
                approval_engine=SimpleNamespace(),
                memory=memory,
                execute_task=execute_task,
                save_task=store.save_task,
                agent_selector=agent_selector,
            )

            task = Task(
                id="execution-task",
                title="Execution",
                description="Implement the approved plan.",
                project_id="proj1",
                assigned_to="executor",
                status=TaskStatus.PENDING,
                metadata={
                    "work_item_projection_id": "execution",
                    "work_item_role_id": "executor",
                    "work_item_execution_strategy": "auto",
                    "work_item_gate": None,
                    "progress_log": [],
                },
            )
            set_linked_work_item_id(task, "parent-item")
            await store.save_task(task)

            await executor._run_work_item(task, {"execution": task})

            self.assertEqual(selections, [("execution-task", "executor")])
            await store.close()

    def test_project_task_completion_no_longer_writes_project_memory_or_history(self) -> None:
        with _workspace_tempdir() as tmpdir:
            manager = MemoryManager(Path(tmpdir), "proj1")
            task = Task(
                title="Planning",
                status=TaskStatus.DONE,
                project_id="proj1",
                tags=["coding"],
                metadata={
                    "company_profile": "corporate",
                    "target_output_dir": "/tmp/out",
                    "decisions": ["Use SQLite"],
                    "artifacts": ["workspace: /tmp/out/app.py"],
                    "risks": ["Need auth review"],
                    "work_item_summary_for_downstream": "Plan approved",
                },
            )
            manager.record_task_completion(task, "Built planning deliverable", project=True)
            manager.append_autonomy_event(
                {
                    "action_kind": "tool",
                    "action_name": "shell_exec",
                    "decision": "auto_approve",
                    "risk_level": "low",
                    "policy_source": "heuristic",
                    "rationale": "safe",
                },
                project=True,
            )
            project_memory = manager.load_memory(project=True)
            project_history = manager.load_history(project=True)
            self.assertEqual(project_memory, "")
            self.assertEqual(project_history, "")

    async def test_resume_existing_company_runtime_keeps_live_external_work_item_active(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            plan = CompanyWorkItemRuntimePlan(
                profile="corporate",
                projections=[
                    WorkItemProjectionSpec(
                        projection_id="execution",
                        turn_type="execute",
                        title="Execution",
                        summary="Produce the main execution output.",
                        role_id="executor",
                    )
                ],
                metadata={"execution_model": "multi_team_org"},
            )
            running_task = Task(
                id="execution-task-live",
                title="Execution",
                session_id="sess-child-live",
                parent_session_id="sess-parent-live",
                status=TaskStatus.RUNNING,
                project_id="proj1",
                assigned_external_agent="codex",
                metadata={
                    "work_item_projection_id": "execution",
                    "company_work_item_plan": serialize_company_work_item_plan(plan),
                    "execution_model": "multi_team_org",
                    "work_item_runtime": True,
                    "progress_log": [],
                },
            )
            await store.save_task(running_task)
            await store.save_external_session(
                ExternalSession(
                    agent_type="codex",
                    project_id="proj1",
                    session_id="codex:proj1:execution-task-live",
                    opc_session_id="sess-child-live",
                    task_id=running_task.id,
                    workspace_path=str(tmpdir),
                    run_mode="interactive",
                    status="working",
                    metadata={"pid": os.getpid(), "status_heartbeat_seconds": 5},
                    updated_at=datetime.now(),
                )
            )

            class DummyExecutor:
                async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                    return "should not run"

            engine = OPCEngine()
            engine.project_id = "proj1"
            engine.store = store
            engine.company_executor = DummyExecutor()
            attempt_token = engine._active_task_run_registry.register("proj1", running_task.id)

            response = await engine._maybe_resume_existing_company_runtime("缁х画", "sess-parent-live")
            refreshed = await store.get_task(running_task.id)
            engine._active_task_run_registry.unregister("proj1", running_task.id, attempt_token)

            self.assertIn("already in progress", response)
            self.assertEqual(refreshed.status, TaskStatus.RUNNING)
            await store.close()

    async def test_reconcile_interrupted_project_tasks_suspends_stale_company_runtime(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            plan = CompanyWorkItemRuntimePlan(
                profile="corporate",
                projections=[
                    WorkItemProjectionSpec(
                        projection_id="execution",
                        turn_type="execute",
                        title="Execution",
                        summary="Produce the main execution output.",
                        role_id="executor",
                    )
                ],
                metadata={"execution_model": "multi_team_org"},
            )
            running_task = Task(
                id="execution-task-stale",
                title="Execution",
                session_id="sess-child-stale",
                parent_session_id="sess-parent-stale",
                status=TaskStatus.RUNNING,
                project_id="proj1",
                assigned_external_agent="codex",
                metadata={
                    "work_item_projection_id": "execution",
                    "company_work_item_plan": serialize_company_work_item_plan(plan),
                    "progress_log": [],
                },
            )
            await store.save_task(running_task)
            await store.save_external_session(
                ExternalSession(
                    agent_type="codex",
                    project_id="proj1",
                    session_id="codex:proj1:execution-task-stale",
                    opc_session_id="sess-child-stale",
                    task_id=running_task.id,
                    workspace_path=str(tmpdir),
                    run_mode="interactive",
                    status="cancelled",
                    metadata={"pid": 999999},
                    updated_at=datetime.now(),
                )
            )

            engine = OPCEngine()
            engine.project_id = "proj1"
            engine.store = store

            reconciled = await engine._reconcile_interrupted_project_tasks()
            refreshed = await store.get_task(running_task.id)
            checkpoints = await store.get_pending_checkpoints(
                project_id="proj1",
                session_id="sess-parent-stale",
                checkpoint_types=["company_runtime_interrupted"],
            )

            self.assertEqual(reconciled, 1)
            self.assertEqual(refreshed.status, TaskStatus.RUNNING)
            self.assertEqual(refreshed.metadata.get("company_runtime_suspend_checkpoint_type"), "company_runtime_interrupted")
            self.assertEqual(len(checkpoints), 1)
            await store.close()

    async def test_reconcile_recovers_stale_company_primary_session_anchor_without_failure(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            anchor = Task(
                id="ui-anchor-task",
                title="Company UI Anchor",
                session_id="sess-company-anchor",
                status=TaskStatus.RUNNING,
                execution_lock=True,
                execution_locked_at=datetime.fromtimestamp(0),
                project_id="proj1",
                metadata={
                    "exec_mode": "org",
                    "company_profile": "custom",
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "origin_task_id": "ui-anchor-task",
                },
            )
            await store.save_task(anchor)

            engine = OPCEngine()
            engine.project_id = "proj1"
            engine.store = store

            reconciled = await engine._reconcile_interrupted_project_tasks()
            refreshed = await store.get_task(anchor.id)

            self.assertEqual(reconciled, 1)
            self.assertEqual(refreshed.status, TaskStatus.IDLE)
            self.assertFalse(refreshed.execution_lock)
            self.assertIsNone(refreshed.execution_locked_at)
            self.assertIn("Recovered stale company session routing state", refreshed.metadata.get("progress_log", [])[-1])
            await store.close()

    def test_company_runtime_summary_prefers_final_delivery_over_intake_dispatch(self) -> None:
        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        created = datetime.now()
        intake = Task(
            id="intake-task",
            title="Chief Analyst Intake",
            status=TaskStatus.DONE,
            created_at=created,
            result={"content": "已创建下游 WorkItem：`wi-ppt`，交给 `report_producer`。", "artifacts": {}},
            metadata={
                "execution_model": "multi_team_org",
                "authoritative_output": True,
                "work_item_turn_type": "intake",
                "manager_board_mutation_performed": True,
                "delegation_wait_for_work_item_ids": ["wi-ppt"],
            },
        )
        delivery = Task(
            id="delivery-task",
            title="Deliver final result to user",
            status=TaskStatus.DONE,
            created_at=created + timedelta(seconds=1),
            result={"content": "最终交付：PPT 已完成。", "artifacts": {}},
            metadata={
                "execution_model": "multi_team_org",
                "authoritative_output": True,
                "user_visible": True,
                "requires_user_feedback": True,
                "feedback_scope": "final",
                "work_item_turn_type": "deliver",
            },
        )

        self.assertEqual(
            executor._summarize_multi_team_org_results([intake, delivery]),
            "最终交付：PPT 已完成。",
        )

    async def test_reconcile_preserves_stable_review_and_human_waiting_states(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            plan = CompanyWorkItemRuntimePlan(
                profile="corporate",
                projections=[
                    WorkItemProjectionSpec(
                        projection_id="review-wait",
                        turn_type="review",
                        title="Review wait",
                        summary="Waiting for review.",
                        role_id="manager",
                    )
                ],
                metadata={"execution_model": "multi_team_org"},
            )
            cases = [
                ("awaiting-manager-task", TaskStatus.AWAITING_MANAGER_REVIEW, Phase.AWAITING_MANAGER_REVIEW),
                ("awaiting-review-task", TaskStatus.AWAITING_REVIEW, Phase.AWAITING_MANAGER_REVIEW),
                ("awaiting-human-task", TaskStatus.AWAITING_HUMAN, Phase.AWAITING_HUMAN),
            ]
            for task_id, status, phase in cases:
                work_item_id = f"wi-{task_id}"
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id=work_item_id,
                        run_id="run-stable-wait",
                        cell_id="team::manager",
                        role_id="manager",
                        seat_id="seat::team::manager::manager",
                        title=task_id,
                        kind="review",
                        projection_id=task_id,
                        phase=phase,
                    )
                )
                task = Task(
                    id=task_id,
                    title=task_id,
                    session_id=f"sess-{task_id}",
                    parent_session_id="sess-parent-stable",
                    status=status,
                    project_id="proj1",
                    assigned_to="manager",
                    metadata={
                        "work_item_projection_id": task_id,
                        "company_work_item_plan": serialize_company_work_item_plan(plan),
                        "progress_log": [],
                    },
                )
                set_linked_work_item_id(task, work_item_id)
                await store.save_task(task)

            engine = OPCEngine()
            engine.project_id = "proj1"
            engine.store = store

            reconciled = await engine._reconcile_interrupted_project_tasks()

            self.assertEqual(reconciled, 3)
            for task_id, status, phase in cases:
                refreshed_task = await store.get_task(task_id)
                refreshed_item = await store.get_delegation_work_item(f"wi-{task_id}")
                assert refreshed_task is not None
                assert refreshed_item is not None
                self.assertEqual(refreshed_task.status, status)
                self.assertEqual(refreshed_item.phase, phase)
                self.assertIn("startup_reconcile_preserved_waiting_state", refreshed_task.metadata)
            await store.close()

    async def test_mark_task_interrupted_preserves_stable_waiting_work_item_phase(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="wi-human-wait",
                    run_id="run-human-wait",
                    cell_id="team::ceo",
                    role_id="ceo",
                    seat_id="seat::team::ceo::ceo",
                    title="Delivery",
                    kind="delivery",
                    projection_id="delivery",
                    phase=Phase.AWAITING_HUMAN,
                )
            )
            task = Task(
                id="human-wait-task",
                title="Delivery",
                session_id="sess-human-wait",
                parent_session_id="sess-parent-human-wait",
                status=TaskStatus.RUNNING,
                project_id="proj1",
                assigned_to="ceo",
                metadata={"progress_log": []},
            )
            set_linked_work_item_id(task, "wi-human-wait")
            await store.save_task(task)
            engine = OPCEngine()
            engine.project_id = "proj1"
            engine.store = store

            marked = await engine._mark_task_interrupted(task, reason="stale runtime")

            refreshed_task = await store.get_task(task.id)
            refreshed_item = await store.get_delegation_work_item("wi-human-wait")
            assert refreshed_task is not None
            assert refreshed_item is not None
            self.assertFalse(marked)
            self.assertEqual(refreshed_task.status, TaskStatus.RUNNING)
            self.assertEqual(refreshed_item.phase, Phase.AWAITING_HUMAN)
            await store.close()

    async def test_company_feedback_checkpoint_runs_self_evolution_only(self) -> None:
        runtime_topology = self._self_evolution_runtime_topology(
            run_id="run-self-evolution-feedback",
            role_id="reviewer",
            employee_id="ceo-1",
        )
        execution_task = Task(
            id="execution-task",
            title="Execution",
            status=TaskStatus.DONE,
            project_id="proj1",
            assigned_to="executor",
            result={"content": "Implemented the requested change.", "artifacts": {}},
            metadata={
                "execution_mode": "company_mode",
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
                "delegation_run_id": runtime_topology["run_id"],
                "runtime_topology": runtime_topology,
                "work_item_projection_id": "engineering_execution",
                "work_item_projection_title": "Engineering Execution",
                "employee_assignment": {
                    "employee_id": "eng-1",
                    "name": "Engineer One",
                    "role_id": "executor",
                },
                "progress_log": [],
            },
        )
        waiting_task = Task(
            id="delivery-task",
            title="Delivery",
            status=TaskStatus.AWAITING_REVIEW,
            project_id="proj1",
            assigned_to="reviewer",
            metadata={
                "execution_mode": "company_mode",
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
                "delegation_run_id": runtime_topology["run_id"],
                "runtime_topology": runtime_topology,
                "work_item_projection_id": "ceo_delivery",
                "work_item_projection_title": "CEO Final Delivery",
                "company_profile": "corporate",
                "feedback_scope": "final",
                "delivery_package": {"executive_summary": "Delivery ready."},
                "employee_assignment": {
                    "employee_id": "ceo-1",
                    "name": "CEO One",
                    "role_id": "reviewer",
                },
                "progress_log": [],
            },
        )
        checkpoint = ExecutionCheckpoint(
            project_id="proj1",
            checkpoint_type="company_delivery_feedback",
            task_id=waiting_task.id,
            payload={
                "waiting_task_id": waiting_task.id,
                "task_ids": [execution_task.id, waiting_task.id],
                "feedback_scope": "final",
                "plan": {"profile": "corporate", "final_decider_role_id": "reviewer", "projections": []},
            },
        )

        class DummyStore:
            def __init__(self, tasks: list[Task], checkpoint: ExecutionCheckpoint) -> None:
                self.tasks = {task.id: task for task in tasks}
                self.checkpoint = checkpoint
                self.work_items: dict[str, DelegationWorkItem] = {}

            async def get_task(self, task_id: str) -> Task | None:
                return self.tasks.get(task_id)

            async def save_task(self, task: Task) -> None:
                self.tasks[task.id] = task

            async def resolve_execution_checkpoint(self, checkpoint_id: str, status: str = "resolved") -> None:
                if checkpoint_id == self.checkpoint.checkpoint_id:
                    self.checkpoint.status = status

            async def save_execution_checkpoint(self, checkpoint: ExecutionCheckpoint) -> None:
                self.checkpoint = checkpoint

            async def get_latest_pending_checkpoint(self, project_id: str) -> ExecutionCheckpoint | None:
                if project_id == self.checkpoint.project_id and self.checkpoint.status == "pending":
                    return self.checkpoint
                return None

            async def save_delegation_work_item(self, item: DelegationWorkItem) -> None:
                self.work_items[item.work_item_id] = item

            async def list_delegation_work_items(self, run_id: str) -> list[DelegationWorkItem]:
                return [item for item in self.work_items.values() if item.run_id == run_id]

            async def get_delegation_work_item(self, work_item_id: str) -> DelegationWorkItem | None:
                return self.work_items.get(work_item_id)

        resumed_tasks: list[Task] = []

        class DummyExecutor:
            def __init__(self) -> None:
                self._active_plan = None
                self._active_tasks = []

            async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                resumed_tasks.extend(tasks)
                await CompanyCollaborationTests._record_no_self_evolution_updates(
                    engine.store,
                    run_id=runtime_topology["run_id"],
                    checkpoint_id=checkpoint.checkpoint_id,
                )
                return "runtime resumed"

            def _build_role_task_map(self, tasks: list[Task]) -> dict[str, object]:
                return {
                    "executor": {
                        "role_id": "executor",
                        "role_name": "Executor",
                        "employees": [{"employee_id": "eng-1", "employee_name": "Engineer One"}],
                        "work_items": [
                            {
                                "work_item_projection_id": "engineering_execution",
                                "projection_id": "engineering_execution",
                                "title": "Engineering Execution",
                                "status": "done",
                                "work_item_assignment": {},
                            }
                        ],
                    }
                }

        engine = OPCEngine()
        engine.project_id = "proj1"
        engine.store = DummyStore([execution_task, waiting_task], checkpoint)
        engine.memory = DummyFeedbackMemory()
        engine.company_executor = DummyExecutor()
        engine.llm = DummySelectionLLM(responses=[AssertionError("delivery feedback should not be classified")])
        engine._run_role_prompt_via_task_execution_agent = AsyncMock(return_value='{"patches":[]}')  # type: ignore[method-assign]

        response = await engine._resume_company_feedback_checkpoint(checkpoint, "Please continue with mobile-first refinements.")
        refreshed = await engine.store.get_task(waiting_task.id)

        self.assertIn("Self-evolution completed", response)
        self.assertEqual(refreshed.status, TaskStatus.DONE)
        self.assertTrue(refreshed.metadata["self_evolution_review_completed"])
        self.assertEqual(refreshed.metadata["latest_self_evolution_review"]["action"], "feedback")
        self.assertFalse(refreshed.metadata["requires_user_feedback"])
        self.assertTrue(refreshed.metadata["human_review_closed"])
        self.assertEqual(refreshed.metadata["feedback_resolution"], "self_evolution_review_completed")
        self.assertEqual(engine.memory.calls, [])
        self.assertEqual(engine.llm.calls, [])
        self.assertEqual([task.id for task in resumed_tasks], [execution_task.id, waiting_task.id])
        root_items = await engine.store.list_delegation_work_items(runtime_topology["run_id"])
        self.assertEqual([item.kind for item in root_items], ["self_evolution"])
        self.assertEqual(root_items[0].role_id, "reviewer")
        self.assertIsNone(await engine.store.get_latest_pending_checkpoint("proj1"))

    async def test_delivery_self_evolution_writes_employee_experience_sidecar(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            runtime_topology = self._self_evolution_runtime_topology(
                run_id="run-self-evolution-sidecar",
                role_id="ceo",
                employee_id="ceo-1",
            )
            waiting_task = Task(
                id="delivery-task",
                title="Delivery",
                session_id="sess-self-evolution-sidecar:delivery",
                parent_session_id="sess-self-evolution-sidecar",
                status=TaskStatus.AWAITING_HUMAN,
                project_id="proj1",
                assigned_to="ceo",
                org_id="corporate",
                metadata={
                    "execution_mode": "company_mode",
                    "execution_model": "multi_team_org",
                    "runtime_model": "multi_team_org",
                    "delegation_run_id": runtime_topology["run_id"],
                    "runtime_topology": runtime_topology,
                    "work_item_projection_id": "ceo_delivery",
                    "work_item_turn_type": "deliver",
                    "company_profile": "corporate",
                    "feedback_scope": "final",
                    "authoritative_output": True,
                    "requires_user_feedback": True,
                    "employee_assignment": {
                        "employee_id": "ceo-1",
                        "name": "CEO One",
                        "role_id": "ceo",
                    },
                },
                result={"content": "Delivery completed.", "artifacts": {}},
            )
            set_linked_work_item_id(waiting_task, "delivery-wi-sidecar")
            checkpoint = ExecutionCheckpoint(
                checkpoint_id="cp-self-evolution",
                project_id="proj1",
                checkpoint_type="company_delivery_feedback",
                task_id=waiting_task.id,
                status="pending",
                payload={
                    "waiting_task_id": waiting_task.id,
                    "task_ids": [waiting_task.id],
                    "feedback_scope": "final",
                    "plan": {"profile": "corporate", "final_decider_role_id": "ceo", "projections": []},
                },
            )
            try:
                await store.save_task(waiting_task)
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id="delivery-wi-sidecar",
                        run_id=runtime_topology["run_id"],
                        cell_id="team::ceo",
                        team_id="team::ceo",
                        team_instance_id="team-instance::run-self-evolution-sidecar::team::ceo",
                        role_id="ceo",
                        seat_id="seat::team::ceo::ceo",
                        role_runtime_session_id="role-session::run-self-evolution-sidecar::ceo::team-instance::run-self-evolution-sidecar::team::ceo",
                        title="CEO Final Delivery",
                        kind="delivery",
                        projection_id="ceo_delivery",
                        phase=Phase.AWAITING_HUMAN,
                        metadata={
                            "runtime_model": "multi_team_org",
                            "execution_mode": "company_mode",
                            "task_id": waiting_task.id,
                            "employee_assignment": waiting_task.metadata["employee_assignment"],
                            "authoritative_output": True,
                            "requires_user_feedback": True,
                            "feedback_scope": "final",
                        },
                    )
                )
                await store.link_work_item_runtime_task("delivery-wi-sidecar", waiting_task.id)
                await store.save_execution_checkpoint(checkpoint)

                patch_json = json.dumps({
                    "patches": [
                        {
                            "employee_id": "ceo-1",
                            "role_id": "ceo",
                            "summary": "Preserve concise final-delivery synthesis.",
                            "strengths": ["Preserve concise synthesis for owner-facing delivery."],
                            "adjustments": ["Ask for missing evidence before final delivery."],
                            "avoid_next_time": ["Avoid vague investment claims without evidence."],
                            "routing_notes": ["Good fit for final synthesis after analyst inputs."],
                            "confidence": 0.9,
                            "evidence_task_ids": ["delivery-task"],
                        }
                    ],
                })
                executed_self_evolution_tasks: list[Task] = []

                async def execute_task(task: Task) -> TaskResult:
                    executed_self_evolution_tasks.append(task)
                    return TaskResult(status=TaskStatus.DONE, content=patch_json, artifacts={})

                config = OPCConfig()
                config.org.company_profile = "corporate"
                org_engine = OrgEngine(config, Path(tmpdir))
                engine = OPCEngine(config=config, opc_home=tmpdir, project_id="proj1")
                engine.project_id = "proj1"
                engine.store = store
                engine.org_engine = org_engine
                engine.memory = MemoryManager(tmpdir, "proj1")
                engine.company_executor = CompanyWorkItemExecutor(
                    org_engine=org_engine,
                    communication=DummyRuntimeCommunication(),
                    approval_engine=SimpleNamespace(),
                    memory=engine.memory,
                    execute_task=execute_task,
                    save_task=store.save_task,
                    store=store,
                )

                response = await engine.run_company_delivery_self_evolution_checkpoint(
                    checkpoint,
                    action="approve",
                )
                refreshed = await store.get_task(waiting_task.id)
                checkpoints = await store.get_execution_checkpoints("proj1")
                refreshed_checkpoint = next(item for item in checkpoints if item.checkpoint_id == checkpoint.checkpoint_id)
                experience = engine.memory.employee_evolution.load_employee_experience("corporate", "ceo-1")
                context = engine.memory.employee_evolution.build_employee_delta_context(
                    "ceo-1",
                    project_id="proj1",
                    organization_id="corporate",
                )
            finally:
                await store.close()

        self.assertIn("Recorded 1 employee experience update", response)
        self.assertEqual(refreshed_checkpoint.status, "resolved")
        self.assertEqual(refreshed.status, TaskStatus.DONE)
        self.assertTrue(refreshed.metadata["self_evolution_review_completed"])
        self.assertTrue(refreshed.metadata["feedback_closed"])
        self.assertEqual(experience["employee_id"], "ceo-1")
        self.assertIn("Preserve concise synthesis", context)
        self.assertEqual(len(executed_self_evolution_tasks), 1)
        self.assertEqual(executed_self_evolution_tasks[0].metadata["work_kind"], "self_evolution")

    async def test_self_evolution_work_item_invalid_json_retries_then_fails(self) -> None:
        save_task = AsyncMock()
        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=save_task,
        )
        task = Task(
            id="self-evolution-task",
            title="Self-Evolution",
            status=TaskStatus.RUNNING,
            project_id="proj1",
            assigned_to="ceo",
            metadata={
                "work_kind": "self_evolution",
                "self_evolution_work_item": True,
                "self_evolution_patch_max_retries": 3,
                "employee_assignment": {"employee_id": "ceo-1", "role_id": "ceo"},
            },
        )

        retry = await executor._finalize_self_evolution_work_item(
            task,
            TaskResult(status=TaskStatus.DONE, content="not json", artifacts={}),
        )
        self.assertEqual(retry.status, TaskStatus.PENDING)
        self.assertEqual(task.metadata["self_evolution_patch_retry_count"], 1)
        self.assertIn("strict JSON", task.context_snapshot["self_evolution_patch_retry_feedback"])
        save_task.assert_awaited_once()

        task.metadata["self_evolution_patch_retry_count"] = 2
        failed = await executor._finalize_self_evolution_work_item(
            task,
            TaskResult(status=TaskStatus.DONE, content="still not json", artifacts={}),
        )
        self.assertEqual(failed.status, TaskStatus.FAILED)
        self.assertEqual(task.status, TaskStatus.FAILED)
        self.assertEqual(task.metadata["self_evolution_error"]["attempts"], 3)

    async def test_self_evolution_work_item_retries_patch_for_wrong_employee(self) -> None:
        save_task = AsyncMock()
        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=save_task,
        )
        task = Task(
            id="self-evolution-task",
            title="Self-Evolution",
            status=TaskStatus.RUNNING,
            project_id="proj1",
            assigned_to="ceo",
            metadata={
                "work_kind": "self_evolution",
                "self_evolution_work_item": True,
                "self_evolution_patch_max_retries": 3,
                "employee_assignment": {"employee_id": "ceo-1", "role_id": "ceo"},
            },
        )
        result = await executor._finalize_self_evolution_work_item(
            task,
            TaskResult(
                status=TaskStatus.DONE,
                content=json.dumps({"patches": [{"employee_id": "other-employee", "role_id": "ceo"}]}),
                artifacts={},
            ),
        )

        self.assertEqual(result.status, TaskStatus.PENDING)
        self.assertEqual(task.metadata["self_evolution_patch_retry_count"], 1)
        self.assertIn("other-employee", task.context_snapshot["self_evolution_patch_retry_feedback"])
        self.assertIn("ceo-1", task.context_snapshot["self_evolution_patch_retry_feedback"])

    async def test_close_human_review_tool_closes_awaiting_human_delivery_work_item(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            try:
                runtime_topology = self._self_evolution_runtime_topology(
                    run_id="run-human-review-close",
                    role_id="ceo",
                    employee_id="ceo-1",
                )
                waiting_task = Task(
                    id="delivery-task",
                    title="CEO Final Delivery",
                    status=TaskStatus.AWAITING_HUMAN,
                    project_id="proj1",
                    assigned_to="ceo",
                    metadata={
                        "execution_mode": "company_mode",
                        "execution_model": "multi_team_org",
                        "runtime_model": "multi_team_org",
                        "delegation_run_id": runtime_topology["run_id"],
                        "runtime_topology": runtime_topology,
                        "work_item_projection_id": "ceo_delivery",
                        "work_item_turn_type": "deliver",
                        "authoritative_output": True,
                        "requires_user_feedback": True,
                        "feedback_scope": "final",
                        "progress_log": [],
                    },
                )
                set_linked_work_item_id(waiting_task, "delivery-wi")
                await store.save_task(waiting_task)
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id="delivery-wi",
                        run_id=runtime_topology["run_id"],
                        cell_id="team::ceo",
                        team_id="team::ceo",
                        team_instance_id="team-instance::run-human-review-close::team::ceo",
                        role_id="ceo",
                        seat_id="seat::team::ceo::ceo",
                        role_runtime_session_id="role-session::run-human-review-close::ceo::team-instance::run-human-review-close::team::ceo",
                        title="CEO Final Delivery",
                        kind="delivery",
                        projection_id="ceo_delivery",
                        phase=Phase.AWAITING_HUMAN,
                        metadata={
                            "runtime_model": "multi_team_org",
                            "task_id": waiting_task.id,
                            "employee_assignment": {"employee_id": "ceo-1", "role_id": "ceo"},
                            "authoritative_output": True,
                            "requires_user_feedback": True,
                            "feedback_scope": "final",
                        },
                    )
                )
                await store.link_work_item_runtime_task("delivery-wi", waiting_task.id)
                checkpoint = ExecutionCheckpoint(
                    project_id="proj1",
                    checkpoint_type="company_delivery_feedback",
                    task_id=waiting_task.id,
                    payload={
                        "waiting_task_id": waiting_task.id,
                        "task_ids": [waiting_task.id],
                        "feedback_scope": "work_item",
                        "plan": {"profile": "corporate", "final_decider_role_id": "ceo", "projections": []},
                    },
                )
                await store.save_execution_checkpoint(checkpoint)

                class DummyExecutor:
                    def __init__(self) -> None:
                        self._active_plan = None
                        self._active_tasks = []

                    async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                        _ = (plan, tasks)
                        await CompanyCollaborationTests._record_no_self_evolution_updates(
                            store,
                            run_id=runtime_topology["run_id"],
                            checkpoint_id=checkpoint.checkpoint_id,
                        )
                        return "runtime resumed"

                    def _build_role_task_map(self, tasks: list[Task]) -> dict[str, object]:
                        _ = tasks
                        return {}

                engine = OPCEngine()
                engine.project_id = "proj1"
                engine.store = store
                engine.memory = DummyFeedbackMemory()
                engine.company_executor = DummyExecutor()
                engine.llm = DummySelectionLLM(responses=[AssertionError("approval should be handled by the final-decider runtime")])
                engine._run_role_prompt_via_task_execution_agent = AsyncMock(return_value='{"patches":[]}')  # type: ignore[method-assign]

                response = await engine._resume_company_feedback_checkpoint(checkpoint, "I approve this delivery.")
                refreshed_task = await store.get_task(waiting_task.id)
                refreshed_item = await store.get_delegation_work_item("delivery-wi")
                checkpoints = await store.get_execution_checkpoints("proj1")
                refreshed_checkpoint = next(
                    item for item in checkpoints if item.checkpoint_id == checkpoint.checkpoint_id
                )

                self.assertIn("Self-evolution completed", response)
                self.assertEqual(refreshed_task.status, TaskStatus.DONE)
                self.assertTrue(refreshed_task.metadata["self_evolution_review_completed"])
                self.assertEqual(refreshed_task.metadata["latest_self_evolution_review"]["action"], "approve")
                self.assertTrue(refreshed_task.metadata["feedback_closed"])
                self.assertFalse(refreshed_task.metadata["requires_user_feedback"])
                self.assertEqual(refreshed_task.metadata["feedback_scope"], "final")
                self.assertEqual(refreshed_item.phase, Phase.APPROVED)
                self.assertTrue(refreshed_item.metadata["feedback_closed"])
                self.assertEqual(refreshed_checkpoint.status, "resolved")
                self.assertEqual(engine.llm.calls, [])
            finally:
                await store.close()

    async def test_company_feedback_text_records_self_evolution_without_rework(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            try:
                runtime_topology = self._self_evolution_runtime_topology(
                    run_id="run-feedback-text",
                    role_id="ceo",
                    employee_id="ceo-1",
                )
                waiting_task = Task(
                    id="delivery-task",
                    title="CEO Final Delivery",
                    status=TaskStatus.AWAITING_HUMAN,
                    project_id="proj1",
                    assigned_to="ceo",
                    metadata={
                        "execution_mode": "company_mode",
                        "execution_model": "multi_team_org",
                        "runtime_model": "multi_team_org",
                        "delegation_run_id": runtime_topology["run_id"],
                        "runtime_topology": runtime_topology,
                        "work_item_projection_id": "ceo_delivery",
                        "work_item_turn_type": "deliver",
                        "authoritative_output": True,
                        "requires_user_feedback": True,
                        "feedback_scope": "final",
                        "progress_log": [],
                    },
                )
                set_linked_work_item_id(waiting_task, "delivery-wi")
                await store.save_task(waiting_task)
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id="delivery-wi",
                        run_id=runtime_topology["run_id"],
                        cell_id="team::ceo",
                        team_id="team::ceo",
                        team_instance_id="team-instance::run-feedback-text::team::ceo",
                        role_id="ceo",
                        seat_id="seat::team::ceo::ceo",
                        role_runtime_session_id="role-session::run-feedback-text::ceo::team-instance::run-feedback-text::team::ceo",
                        title="CEO Final Delivery",
                        kind="delivery",
                        projection_id="ceo_delivery",
                        phase=Phase.AWAITING_HUMAN,
                        metadata={
                            "runtime_model": "multi_team_org",
                            "task_id": waiting_task.id,
                            "employee_assignment": {"employee_id": "ceo-1", "role_id": "ceo"},
                            "authoritative_output": True,
                            "requires_user_feedback": True,
                            "feedback_scope": "final",
                        },
                    )
                )
                await store.link_work_item_runtime_task("delivery-wi", waiting_task.id)
                checkpoint = ExecutionCheckpoint(
                    project_id="proj1",
                    checkpoint_type="company_delivery_feedback",
                    task_id=waiting_task.id,
                    payload={
                        "waiting_task_id": waiting_task.id,
                        "task_ids": [waiting_task.id],
                        "feedback_scope": "final",
                        "plan": {"profile": "corporate", "final_decider_role_id": "ceo", "projections": []},
                    },
                )
                await store.save_execution_checkpoint(checkpoint)

                class DummyExecutor:
                    def __init__(self) -> None:
                        self._active_plan = None
                        self._active_tasks = []
                        self.execute_calls = 0
                        self.executed_task_ids: list[list[str]] = []

                    async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                        _ = (plan, tasks)
                        self.execute_calls += 1
                        self.executed_task_ids.append([task.id for task in tasks])
                        await CompanyCollaborationTests._record_no_self_evolution_updates(
                            store,
                            run_id=runtime_topology["run_id"],
                            checkpoint_id=checkpoint.checkpoint_id,
                        )
                        return "runtime resumed for final decider"

                    def _build_role_task_map(self, tasks: list[Task]) -> dict[str, object]:
                        _ = tasks
                        return {}

                executor = DummyExecutor()
                engine = OPCEngine()
                engine.project_id = "proj1"
                engine.store = store
                engine.memory = DummyFeedbackMemory()
                engine.company_executor = executor
                engine.llm = DummySelectionLLM(
                    responses=[
                        AssertionError("delivery feedback should not invoke a classifier."),
                    ]
                )
                engine._run_role_prompt_via_task_execution_agent = AsyncMock(return_value='{"patches":[]}')  # type: ignore[method-assign]

                response = await engine._resume_company_feedback_checkpoint(
                    checkpoint,
                    "I do not accept this delivery; I want changes.",
                )
                refreshed_task = await store.get_task(waiting_task.id)
                refreshed_item = await store.get_delegation_work_item("delivery-wi")
                checkpoints = await store.get_execution_checkpoints("proj1")
                refreshed_checkpoint = next(
                    item for item in checkpoints if item.checkpoint_id == checkpoint.checkpoint_id
                )

                self.assertIn("Self-evolution completed", response)
                self.assertEqual(refreshed_task.status, TaskStatus.DONE)
                self.assertTrue(refreshed_task.metadata["self_evolution_review_completed"])
                self.assertEqual(refreshed_task.metadata["latest_self_evolution_review"]["action"], "feedback")
                self.assertFalse(refreshed_task.metadata["requires_user_feedback"])
                self.assertTrue(refreshed_task.metadata["feedback_closed"])
                self.assertTrue(refreshed_task.metadata["feedback_resolved"])
                self.assertEqual(refreshed_item.phase, Phase.APPROVED)
                self.assertNotIn("resume_source", refreshed_item.metadata)
                self.assertEqual(refreshed_checkpoint.status, "resolved")
                self.assertIsNone(await store.get_latest_pending_checkpoint("proj1"))
                self.assertEqual(engine.memory.calls, [])
                self.assertEqual(executor.execute_calls, 1)
                self.assertEqual(executor.executed_task_ids, [[waiting_task.id]])
                self.assertEqual(engine.llm.calls, [])
            finally:
                await store.close()

    async def test_feedback_checkpoint_requires_final_authoritative_delivery(self) -> None:
        checkpoints: list[dict[str, object]] = []

        async def checkpoint_callback(data: dict[str, object]) -> None:
            checkpoints.append(dict(data))

        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            checkpoint_callback=checkpoint_callback,
        )
        executor._active_plan = CompanyWorkItemRuntimePlan(profile="corporate", projections=[])
        task = Task(
            id="delivery-task",
            title="CEO Final Delivery",
            status=TaskStatus.AWAITING_HUMAN,
            project_id="proj1",
            assigned_to="ceo",
            metadata={
                "execution_mode": "company_mode",
                "work_item_projection_id": "ceo_delivery",
                "work_item_turn_type": "deliver",
                "authoritative_output": True,
                "user_visible": True,
                "requires_user_feedback": True,
                "feedback_scope": "final",
                "delivery_revision": 7,
                "owner_directive_revision": 7,
                "latest_user_directive": "Add a visual PPT.",
            },
            result={"content": "Current round final delivery."},
        )
        set_linked_work_item_id(task, "wi-final-delivery")

        await executor._save_feedback_checkpoint(task)

        self.assertEqual(checkpoints[0]["checkpoint_type"], "company_delivery_feedback")
        payload = checkpoints[0]["payload"]
        self.assertEqual(payload["feedback_scope"], "final")
        self.assertEqual(payload["waiting_work_item_id"], "wi-final-delivery")
        self.assertEqual(payload["delivery_revision"], 7)
        self.assertEqual(payload["owner_directive_revision"], 7)
        self.assertEqual(payload["latest_user_directive"], "Add a visual PPT.")
        self.assertEqual(payload["result_content"], "Current round final delivery.")

    async def test_feedback_checkpoint_skips_non_final_delivery(self) -> None:
        checkpoints: list[dict[str, object]] = []

        async def checkpoint_callback(data: dict[str, object]) -> None:
            checkpoints.append(dict(data))

        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            checkpoint_callback=checkpoint_callback,
        )
        executor._active_plan = CompanyWorkItemRuntimePlan(profile="corporate", projections=[])
        task = Task(
            id="attention-delivery-task",
            title="Attention Delivery",
            status=TaskStatus.AWAITING_HUMAN,
            project_id="proj1",
            assigned_to="ceo",
            metadata={
                "execution_mode": "company_mode",
                "work_item_projection_id": "ceo_attention_delivery",
                "work_item_turn_type": "deliver",
                "attention_work_item": True,
                "authoritative_output": False,
                "user_visible": False,
            },
        )

        await executor._save_feedback_checkpoint(task)

        self.assertEqual(checkpoints, [])

    async def test_company_feedback_question_is_self_evolution_feedback(self) -> None:
        runtime_topology = self._self_evolution_runtime_topology(
            run_id="run-feedback-question",
            role_id="reviewer",
            employee_id="ceo-1",
        )
        execution_task = Task(
            id="execution-task",
            title="Execution",
            status=TaskStatus.DONE,
            project_id="proj1",
            assigned_to="executor",
            metadata={
                "execution_mode": "company_mode",
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
                "delegation_run_id": runtime_topology["run_id"],
                "runtime_topology": runtime_topology,
                "work_item_projection_id": "engineering_execution",
                "work_item_projection_title": "Engineering Execution",
                "employee_assignment": {
                    "employee_id": "eng-1",
                    "name": "Engineer One",
                    "role_id": "executor",
                },
                "progress_log": [],
            },
        )
        waiting_task = Task(
            id="delivery-task",
            title="Delivery",
            status=TaskStatus.AWAITING_REVIEW,
            project_id="proj1",
            assigned_to="reviewer",
            metadata={
                "execution_mode": "company_mode",
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
                "delegation_run_id": runtime_topology["run_id"],
                "runtime_topology": runtime_topology,
                "work_item_projection_id": "ceo_delivery",
                "work_item_projection_title": "CEO Final Delivery",
                "company_profile": "corporate",
                "feedback_scope": "final",
                "delivery_package": {"executive_summary": "Delivery ready."},
                "progress_log": [],
            },
        )
        checkpoint = ExecutionCheckpoint(
            project_id="proj1",
            checkpoint_type="company_delivery_feedback",
            task_id=waiting_task.id,
            payload={
                "waiting_task_id": waiting_task.id,
                "task_ids": [execution_task.id, waiting_task.id],
                "feedback_scope": "final",
                "plan": {"profile": "corporate", "final_decider_role_id": "reviewer", "projections": []},
            },
        )

        class DummyStore:
            def __init__(self, tasks: list[Task], checkpoint: ExecutionCheckpoint) -> None:
                self.tasks = {task.id: task for task in tasks}
                self.checkpoint = checkpoint
                self.work_items: dict[str, DelegationWorkItem] = {}

            async def get_task(self, task_id: str) -> Task | None:
                return self.tasks.get(task_id)

            async def save_task(self, task: Task) -> None:
                self.tasks[task.id] = task

            async def resolve_execution_checkpoint(self, checkpoint_id: str, status: str = "resolved") -> None:
                if checkpoint_id == self.checkpoint.checkpoint_id:
                    self.checkpoint.status = status

            async def save_execution_checkpoint(self, checkpoint: ExecutionCheckpoint) -> None:
                self.checkpoint = checkpoint

            async def get_latest_pending_checkpoint(self, project_id: str) -> ExecutionCheckpoint | None:
                if project_id == self.checkpoint.project_id and self.checkpoint.status == "pending":
                    return self.checkpoint
                return None

            async def save_delegation_work_item(self, item: DelegationWorkItem) -> None:
                self.work_items[item.work_item_id] = item

            async def list_delegation_work_items(self, run_id: str) -> list[DelegationWorkItem]:
                return [item for item in self.work_items.values() if item.run_id == run_id]

            async def get_delegation_work_item(self, work_item_id: str) -> DelegationWorkItem | None:
                return self.work_items.get(work_item_id)

        class DummyExecutor:
            def __init__(self) -> None:
                self._active_plan = None
                self._active_tasks = []
                self.execute_calls = 0

            async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                _ = (plan, tasks)
                self.execute_calls += 1
                await CompanyCollaborationTests._record_no_self_evolution_updates(
                    engine.store,
                    run_id=runtime_topology["run_id"],
                    checkpoint_id=checkpoint.checkpoint_id,
                )
                return "runtime resumed for question"

            def _build_role_task_map(self, tasks: list[Task]) -> dict[str, object]:
                return {}

        engine = OPCEngine()
        engine.project_id = "proj1"
        engine.store = DummyStore([execution_task, waiting_task], checkpoint)
        engine.memory = DummyFeedbackMemory()
        executor = DummyExecutor()
        engine.company_executor = executor
        engine.llm = DummySelectionLLM(responses=[AssertionError("question should resume the final-decider runtime")])
        engine._run_role_prompt_via_task_execution_agent = AsyncMock(return_value='{"patches":[]}')  # type: ignore[method-assign]

        response = await engine._resume_company_feedback_checkpoint(checkpoint, "Why is this still a known limitation?")
        refreshed = await engine.store.get_task(waiting_task.id)
        pending = await engine.store.get_latest_pending_checkpoint("proj1")

        self.assertIn("Self-evolution completed", response)
        self.assertEqual(refreshed.status, TaskStatus.DONE)
        self.assertTrue(refreshed.metadata["self_evolution_review_completed"])
        self.assertEqual(refreshed.metadata["latest_self_evolution_review"]["action"], "feedback")
        self.assertFalse(refreshed.metadata["requires_user_feedback"])
        self.assertIsNone(pending)
        self.assertEqual(executor.execute_calls, 1)
        self.assertEqual(engine.llm.calls, [])

    async def test_company_feedback_change_request_does_not_resume_work(self) -> None:
        runtime_topology = self._self_evolution_runtime_topology(
            run_id="run-change-request-self-evolution",
            role_id="reviewer",
            employee_id="ceo-1",
        )
        execution_task = Task(
            id="execution-task",
            title="Execution",
            status=TaskStatus.DONE,
            project_id="proj1",
            assigned_to="executor",
            result={"content": "Implemented the requested change.", "artifacts": {}},
            metadata={
                "execution_mode": "company_mode",
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
                "delegation_run_id": runtime_topology["run_id"],
                "runtime_topology": runtime_topology,
                "work_item_projection_id": "engineering_execution",
                "work_item_projection_title": "Engineering Execution",
                "work_item_turn_type": "execute",
                "member_session_id": "member::proj1::executor::eng-1",
                "member_session_state": {
                    "member_session_id": "member::proj1::executor::eng-1",
                    "role_id": "executor",
                    "employee_id": "eng-1",
                    "working_memory": ["Implemented the initial patch."],
                    "resume_state": {"resume_cursor": 2},
                },
                "employee_assignment": {
                    "employee_id": "eng-1",
                    "name": "Engineer One",
                    "role_id": "executor",
                },
                "progress_log": [],
            },
        )
        waiting_task = Task(
            id="delivery-task",
            title="Delivery",
            status=TaskStatus.AWAITING_REVIEW,
            project_id="proj1",
            assigned_to="reviewer",
            result={"content": "Final delivery package.", "artifacts": {}},
            dependencies=["engineering_execution"],
            metadata={
                "execution_mode": "company_mode",
                "execution_model": "multi_team_org",
                "runtime_model": "multi_team_org",
                "delegation_run_id": runtime_topology["run_id"],
                "runtime_topology": runtime_topology,
                "work_item_projection_id": "ceo_delivery",
                "work_item_projection_title": "CEO Final Delivery",
                "company_profile": "corporate",
                "work_item_turn_type": "deliver",
                "authoritative_output": True,
                "requires_user_feedback": True,
                "feedback_scope": "final",
                "progress_log": [],
            },
        )
        plan = {
            "profile": "corporate",
            "final_decider_role_id": "reviewer",
            "projections": [
                {
                    "projection_id": "engineering_execution",
                    "turn_type": "execute",
                    "title": "Execution",
                    "summary": "",
                    "role_id": "executor",
                    "dependency_projection_ids": [],
                    "execution_strategy": "auto",
                    "metadata": {},
                },
                {
                    "projection_id": "ceo_delivery",
                    "turn_type": "deliver",
                    "title": "Delivery",
                    "summary": "",
                    "role_id": "reviewer",
                    "dependency_projection_ids": ["engineering_execution"],
                    "execution_strategy": "auto",
                    "metadata": {
                        "authoritative_output": True,
                        "requires_user_feedback": True,
                        "feedback_scope": "final",
                    },
                },
            ],
        }
        checkpoint = ExecutionCheckpoint(
            project_id="proj1",
            checkpoint_type="company_delivery_feedback",
            task_id=waiting_task.id,
            payload={
                "waiting_task_id": waiting_task.id,
                "task_ids": [execution_task.id, waiting_task.id],
                "feedback_scope": "final",
                "plan": plan,
            },
        )

        class DummyStore:
            def __init__(self, tasks: list[Task], checkpoint: ExecutionCheckpoint) -> None:
                self.tasks = {task.id: task for task in tasks}
                self.checkpoint = checkpoint
                self.work_items: dict[str, DelegationWorkItem] = {}

            async def get_task(self, task_id: str) -> Task | None:
                return self.tasks.get(task_id)

            async def save_task(self, task: Task) -> None:
                self.tasks[task.id] = task

            async def resolve_execution_checkpoint(self, checkpoint_id: str, status: str = "resolved") -> None:
                if checkpoint_id == self.checkpoint.checkpoint_id:
                    self.checkpoint.status = status

            async def save_execution_checkpoint(self, checkpoint: ExecutionCheckpoint) -> None:
                self.checkpoint = checkpoint

            async def get_latest_pending_checkpoint(self, project_id: str) -> ExecutionCheckpoint | None:
                if project_id == self.checkpoint.project_id and self.checkpoint.status == "pending":
                    return self.checkpoint
                return None

            async def save_delegation_work_item(self, item: DelegationWorkItem) -> None:
                self.work_items[item.work_item_id] = item

            async def list_delegation_work_items(self, run_id: str) -> list[DelegationWorkItem]:
                return [item for item in self.work_items.values() if item.run_id == run_id]

            async def get_delegation_work_item(self, work_item_id: str) -> DelegationWorkItem | None:
                return self.work_items.get(work_item_id)

        engine = OPCEngine()
        engine.project_id = "proj1"
        engine.on_progress = AsyncMock()
        engine.store = DummyStore([execution_task, waiting_task], checkpoint)
        engine.memory = DummyFeedbackMemory()
        engine.company_executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            llm=None,
            execute_task=AsyncMock(),
            save_task=engine.store.save_task,
        )
        async def execute_self_evolution(plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
            _ = (plan, tasks)
            await CompanyCollaborationTests._record_no_self_evolution_updates(
                engine.store,
                run_id=runtime_topology["run_id"],
                checkpoint_id=checkpoint.checkpoint_id,
            )
            return "runtime resumed for final decider"

        engine.company_executor.execute = AsyncMock(side_effect=execute_self_evolution)  # type: ignore[method-assign]
        engine.llm = DummySelectionLLM(responses=[AssertionError("change request should resume the final-decider runtime")])
        engine._run_role_prompt_via_task_execution_agent = AsyncMock(return_value='{"patches":[]}')  # type: ignore[method-assign]

        response = await engine._resume_company_feedback_checkpoint(checkpoint, "Please fix the edge case before I accept this.")
        refreshed_execution = await engine.store.get_task(execution_task.id)
        refreshed_delivery = await engine.store.get_task(waiting_task.id)

        self.assertIn("Self-evolution completed", response)
        self.assertEqual(refreshed_execution.status, TaskStatus.DONE)
        self.assertEqual(refreshed_delivery.status, TaskStatus.DONE)
        self.assertEqual(refreshed_execution.metadata["member_session_id"], "member::proj1::executor::eng-1")
        self.assertNotIn("latest_ceo_rework", refreshed_execution.context_snapshot)
        self.assertTrue(refreshed_delivery.metadata["self_evolution_review_completed"])
        self.assertEqual(refreshed_delivery.metadata["latest_self_evolution_review"]["action"], "feedback")
        self.assertFalse(refreshed_delivery.metadata["requires_user_feedback"])
        engine.company_executor.execute.assert_awaited_once()  # type: ignore[attr-defined]
        self.assertEqual(engine.llm.calls, [])
        self.assertIsNone(await engine.store.get_latest_pending_checkpoint("proj1"))

    async def test_closed_delivery_review_does_not_block_next_company_turn(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            try:
                delivery_task = Task(
                    id="delivery-task",
                    title="Final Delivery",
                    session_id="sess-main:delivery",
                    parent_session_id="sess-main",
                    status=TaskStatus.AWAITING_HUMAN,
                    project_id="proj1",
                    assigned_to="ceo",
                    metadata={
                        "execution_mode": "company_mode",
                        "execution_model": "multi_team_org",
                        "runtime_model": "multi_team_org",
                        "work_item_runtime": True,
                        "work_item_projection_id": "ceo_delivery",
                        "work_item_turn_type": "deliver",
                        "authoritative_output": True,
                        "user_visible": True,
                        "requires_user_feedback": True,
                        "feedback_scope": "final",
                        "self_evolution_review_completed": True,
                        "self_evolution_review_completed_at": "2026-05-31T14:10:58",
                        "company_work_item_plan": {
                            "profile": "corporate",
                            "runtime_model": "multi_team_org",
                            "work_item_driven": True,
                            "projections": [],
                            "metadata": {
                                "execution_model": "multi_team_org",
                                "runtime_model": "multi_team_org",
                            },
                        },
                    },
                )
                await store.save_task(delivery_task)

                class DummyExecutor:
                    def __init__(self) -> None:
                        self._active_plan = None
                        self._active_tasks = []
                        self.execute_calls = 0

                    async def execute(self, plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                        _ = (plan, tasks)
                        self.execute_calls += 1
                        return "should not resume"

                    def _build_role_task_map(self, tasks: list[Task]) -> dict[str, object]:
                        _ = tasks
                        return {}

                executor = DummyExecutor()
                engine = OPCEngine()
                engine.project_id = "proj1"
                engine.store = store
                engine.company_executor = executor

                response = await engine._maybe_resume_existing_company_runtime(
                    "new independent company request",
                    "sess-main",
                )
                refreshed = await store.get_task(delivery_task.id)

                self.assertIsNone(response)
                self.assertEqual(executor.execute_calls, 0)
                self.assertEqual(refreshed.status, TaskStatus.DONE)
                self.assertFalse(refreshed.metadata["requires_user_feedback"])
                self.assertTrue(refreshed.metadata["feedback_closed"])
                self.assertEqual(refreshed.metadata["feedback_resolution"], "self_evolution_review_completed")
            finally:
                await store.close()

    async def test_detailed_company_feedback_defaults_to_partial_success_without_llm(self) -> None:
        engine = OPCEngine()
        evaluation = await engine._evaluate_company_feedback(
            delivery_task=Task(
                id="delivery-task",
                title="Delivery",
                project_id="proj1",
                metadata={},
            ),
            work_item_tasks=[
                Task(
                    id="execution-task",
                    title="Execution",
                    project_id="proj1",
                    metadata={
                        "employee_assignment": {
                            "employee_id": "eng-1",
                            "name": "Engineer One",
                            "role_id": "executor",
                        }
                    },
                )
            ],
            feedback={
                "scope": "final",
                "label": "detailed_feedback",
                "raw_feedback": "Overall direction is right, but the delivery quality is not stable enough and needs more testing.",
            },
        )

        self.assertEqual(evaluation["overall_outcome"], "partial_success")
        self.assertEqual(evaluation["employees"][0]["outcome"], "partial_success")

    async def test_open_peer_wait_now_delivers_async_mailbox_without_pausing(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            task = Task(
                id="exec-task",
                title="Execution",
                project_id="proj1",
                assigned_to="executor",
                status=TaskStatus.PENDING,
                metadata={"work_item_projection_id": "execution"},
            )
            await store.save_task(task)

            pause = await communication.open_peer_wait(
                task=task,
                to_agents=["reviewer"],
                subject="Need architecture clarification",
                body="Question: should we keep SQLite?",
                timeout_action="Assume SQLite",
            )
            self.assertFalse(pause["requires_peer_wait"])
            self.assertEqual(pause["delivery_mode"], "async_mailbox")
            self.assertEqual(task.status, TaskStatus.PENDING)
            await store.close()

    async def test_legacy_peer_message_wait_is_auto_released(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            planning_task = Task(
                id="cto-task",
                title="CTO Planning",
                project_id="proj1",
                assigned_to="cto",
                status=TaskStatus.AWAITING_PEER,
                metadata={"work_item_projection_id": "cto_planning"},
            )
            planning_task.metadata["peer_wait"] = {
                "kind": "peer_message",
                "message_id": "msg-legacy",
                "waiting_on_agents": ["devops_engineer"],
            }
            await store.save_task(planning_task)

            resumed = await communication.resolve_task_peer_wait(planning_task)

            self.assertTrue(resumed)
            self.assertEqual(planning_task.status, TaskStatus.PENDING)
            self.assertNotIn("peer_wait", planning_task.metadata)
            self.assertIn("peer_wait_released", planning_task.context_snapshot)
            await store.close()

    async def test_open_peer_wait_to_parallel_peer_remains_async(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            intake_task = Task(
                id="ceo-intake",
                title="CEO Intake",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.DONE,
                metadata={"work_item_projection_id": "ceo_intake"},
            )
            cmo_task = Task(
                id="cmo-task",
                title="CMO Planning",
                project_id="proj1",
                assigned_to="cmo",
                status=TaskStatus.RUNNING,
                dependencies=["ceo-intake"],
                metadata={"work_item_projection_id": "cmo_planning"},
            )
            coo_task = Task(
                id="coo-task",
                title="COO Coordination",
                project_id="proj1",
                assigned_to="coo",
                status=TaskStatus.PENDING,
                dependencies=["ceo-intake"],
                metadata={"work_item_projection_id": "coo_coordination"},
            )
            await store.save_task(intake_task)
            await store.save_task(cmo_task)
            await store.save_task(coo_task)

            pause = await communication.open_peer_wait(
                task=coo_task,
                to_agents=["cmo"],
                subject="Need CMO planning input",
                body="Can you share the current content direction?",
            )

            self.assertFalse(pause["requires_peer_wait"])
            self.assertEqual(pause["delivery_mode"], "async_mailbox")
            self.assertEqual(coo_task.status, TaskStatus.PENDING)
            await store.close()

    async def test_send_dm_to_standby_role_does_not_synthesize_reply(self) -> None:
        """A blocking DM to a standby role is delivered but NOT auto-replied
        by the main LLM. The old impersonation path was removed in favor of
        CommsReactivationSweeper re-opening the recipient's task so its own
        agent answers through the runtime mailbox. This test guards the new
        contract — delivery succeeds, no synthetic reply is generated, the
        sender is not marked REPLIED, and no ``auto_reply_generated`` flag
        appears in the message metadata."""
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            engineer_task = Task(
                id="eng-task",
                title="Engineering Execution",
                project_id="proj1",
                assigned_to="senior_engineer",
                status=TaskStatus.RUNNING,
                metadata={"work_item_projection_id": "engineering_execution"},
            )
            cto_task = Task(
                id="cto-plan",
                title="CTO Planning",
                project_id="proj1",
                assigned_to="cto",
                status=TaskStatus.DONE,
                result={"content": "Decision: keep SQLite for v1 and make schema changes additive.", "artifacts": {}},
                metadata={
                    "work_item_projection_id": "cto_planning",
                    "work_item_summary_for_downstream": "SQLite-first architecture approved",
                    "decisions": ["Decision: keep SQLite for v1"],
                    "risks": ["Risk: migrations must stay additive"],
                },
            )
            cto_follow_up = Task(
                id="cto-review",
                title="CTO Review",
                project_id="proj1",
                assigned_to="cto",
                status=TaskStatus.PENDING,
                dependencies=["eng-task"],
                metadata={"work_item_projection_id": "cto_review"},
            )
            await store.save_task(engineer_task)
            await store.save_task(cto_task)
            await store.save_task(cto_follow_up)

            message = AgentMessage(
                from_agent="senior_engineer",
                to_agents=["cto"],
                subject="DB clarification",
                body="Can we keep SQLite for v1?",
                context_ref=engineer_task.id,
                task_id=engineer_task.id,
                reply_needed=True,
            )

            await communication.send_dm(message)

            original = await store.get_message(message.msg_id)
            replies = await communication.read_inbox(
                agent_id="senior_engineer",
                task=engineer_task,
                unread_only=True,
                limit=5,
                mark_read=False,
            )

            assert original is not None
            self.assertEqual(original.status, MessageStatus.DELIVERED)
            self.assertNotIn("auto_reply_generated", original.metadata or {})
            self.assertEqual(len(replies), 0)
            await store.close()

    async def test_auto_resolve_stale_meeting_reaches_semantic_consensus(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())

            async def runner(_meeting: Any, participant: str, request: dict[str, Any]) -> str:
                self.assertEqual(request["mode"], "participant")
                if participant == "executor":
                    return json.dumps(
                        {
                            "stance": "agree",
                            "proposal": "Use SQLite",
                            "support_level": 0.9,
                            "vote": "support",
                            "reasoning": "SQLite keeps the stack local-first.",
                            "blocking_issues": [],
                            "assumptions": ["Additive migrations stay in place."],
                            "questions_for_others": [],
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {
                        "stance": "agree",
                        "proposal": "Use SQLite",
                        "support_level": 0.8,
                        "vote": "support",
                        "reasoning": "QA already validated the migration path.",
                        "blocking_issues": [],
                        "assumptions": [],
                        "questions_for_others": [],
                    },
                    ensure_ascii=False,
                )

            communication.set_meeting_turn_runner(runner)
            coordinator_task = Task(
                id="meeting-task",
                title="Architecture Sync",
                project_id="proj1",
                assigned_to="coordinator",
                status=TaskStatus.PENDING,
                metadata={"work_item_projection_id": "architecture_sync"},
            )
            await store.save_task(coordinator_task)

            pause = await communication.open_meeting_wait(
                task=coordinator_task,
                topic="Database selection",
                participants=["executor", "reviewer"],
                agenda=["Confirm the data store", "Confirm migration policy"],
                shared_context="We need a local-first storage decision.",
            )
            resolved = await communication.auto_resolve_stale_meetings([coordinator_task])
            meeting = await store.get_meeting(pause["meeting_room_id"])

            self.assertEqual(resolved, [pause["meeting_room_id"]])
            self.assertEqual(coordinator_task.status, TaskStatus.PENDING)
            assert meeting is not None
            self.assertEqual(meeting.decision_method, "semantic_consensus")
            self.assertEqual(coordinator_task.context_snapshot["meeting_outcome"]["decision"], "Use SQLite")
            self.assertEqual(coordinator_task.context_snapshot["meeting_decision_method"], "semantic_consensus")
            await store.close()

    async def test_auto_resolve_stale_meeting_uses_owner_override_for_short_conflicts(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())

            async def runner(_meeting: Any, participant: str, request: dict[str, Any]) -> str:
                if request["mode"] == "decision_owner":
                    return json.dumps(
                        {
                            "decision": "Use SQLite",
                            "action_items": ["Proceed with additive migrations."],
                            "reasoning": "The decision owner prefers the local-first option.",
                            "requires_human_input": False,
                            "follow_up_questions": [],
                        },
                        ensure_ascii=False,
                    )
                if participant == "executor":
                    return '{"stance":"agree","proposal":"SQLite","vote":"support","reasoning":"SQLite.","blocking_issues":[],"assumptions":[],"questions_for_others":[]}'
                return '{"stance":"disagree","proposal":"Postgres","vote":"oppose","reasoning":"Postgres.","blocking_issues":["Scalability concern"],"assumptions":[],"questions_for_others":[]}'

            communication.set_meeting_turn_runner(runner)
            coordinator_task = Task(
                id="meeting-task",
                title="Architecture Sync",
                project_id="proj1",
                assigned_to="coordinator",
                status=TaskStatus.PENDING,
                metadata={"work_item_projection_id": "architecture_sync"},
            )
            await store.save_task(coordinator_task)

            pause = await communication.open_meeting_wait(
                task=coordinator_task,
                topic="Database selection",
                participants=["executor", "reviewer"],
                agenda=["Choose a v1 datastore"],
                shared_context="Pick the v1 storage engine.",
                timeout_seconds=60,
            )
            meeting = await store.get_meeting(pause["meeting_room_id"])
            assert meeting is not None
            meeting.max_rounds = 1
            await store.save_meeting(meeting)

            resolved = await communication.auto_resolve_stale_meetings([coordinator_task])
            refreshed = await store.get_meeting(pause["meeting_room_id"])

            self.assertEqual(resolved, [pause["meeting_room_id"]])
            assert refreshed is not None
            self.assertEqual(refreshed.decision_method, "owner_override")
            self.assertNotEqual(refreshed.decision_method, "semantic_consensus")
            self.assertEqual(coordinator_task.context_snapshot["meeting_outcome"]["decision"], "Use SQLite")
            await store.close()

    async def test_auto_resolve_stale_meeting_escalates_to_human_when_vote_ties(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())

            async def runner(_meeting: Any, participant: str, request: dict[str, Any]) -> str:
                self.assertEqual(request["mode"], "participant")
                if participant == "executor":
                    return json.dumps(
                        {
                            "stance": "agree",
                            "proposal": "SQLite",
                            "vote": "support",
                            "reasoning": "SQLite keeps operations simple.",
                            "blocking_issues": [],
                            "assumptions": [],
                            "questions_for_others": [],
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {
                        "stance": "disagree",
                        "proposal": "Postgres",
                        "vote": "oppose",
                        "reasoning": "Postgres is safer for future scale.",
                        "blocking_issues": ["No consensus on scaling posture."],
                        "assumptions": [],
                        "questions_for_others": [],
                    },
                    ensure_ascii=False,
                )

            communication.set_meeting_turn_runner(runner)
            coordinator_task = Task(
                id="meeting-task",
                title="Architecture Sync",
                project_id="proj1",
                assigned_to="coordinator",
                status=TaskStatus.PENDING,
                metadata={"work_item_projection_id": "architecture_sync"},
            )
            await store.save_task(coordinator_task)

            pause = await communication.open_meeting_wait(
                task=coordinator_task,
                topic="Database selection",
                participants=["executor", "reviewer"],
                agenda=["Choose a v1 datastore"],
                shared_context="Pick the v1 storage engine.",
                decision_policy="majority_vote",
            )
            meeting = await store.get_meeting(pause["meeting_room_id"])
            assert meeting is not None
            meeting.max_rounds = 1
            await store.save_meeting(meeting)

            resolved = await communication.auto_resolve_stale_meetings([coordinator_task])
            refreshed = await store.get_meeting(pause["meeting_room_id"])

            self.assertEqual(resolved, [pause["meeting_room_id"]])
            assert refreshed is not None
            self.assertEqual(refreshed.decision_method, "human_escalation")
            self.assertEqual(coordinator_task.status, TaskStatus.AWAITING_HUMAN)
            self.assertTrue(coordinator_task.context_snapshot["meeting_outcome"]["requires_human_input"])
            await store.close()

    async def test_runtime_scoped_inbox_reads_cross_work_item_messages(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            execution_task_ids = ["cto-task", "coo-task", "cmo-task"]
            sender_task = Task(
                id="cto-task",
                title="CTO Planning",
                project_id="proj1",
                assigned_to="cto",
                status=TaskStatus.RUNNING,
                metadata={"work_item_projection_id": "cto_planning", "execution_task_ids": execution_task_ids},
            )
            recipient_task = Task(
                id="coo-task",
                title="COO Coordination",
                project_id="proj1",
                assigned_to="coo",
                status=TaskStatus.RUNNING,
                metadata={"work_item_projection_id": "coo_coordination", "execution_task_ids": execution_task_ids},
            )
            await store.save_task(sender_task)
            await store.save_task(recipient_task)

            await communication.send_dm(
                AgentMessage(
                    from_agent="cto",
                    to_agents=["coo"],
                    subject="Need coordination input",
                    body="Please align the execution handoff.",
                    task_id=sender_task.id,
                    context_ref=sender_task.id,
                )
            )

            inbox = await communication.read_inbox(
                agent_id="coo",
                task=recipient_task,
                unread_only=True,
                mark_read=False,
                limit=10,
            )

            self.assertEqual(len(inbox), 1)
            self.assertEqual(inbox[0]["from_agent"], "cto")
            self.assertEqual(inbox[0]["subject"], "Need coordination input")
            await store.close()

    async def test_send_dm_tool_blocking_flag_is_async_mailbox(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            tools = create_collaboration_tools(communication)
            send_dm_tool = next(tool for tool in tools if tool.name == "send_dm")
            task = Task(
                id="coo-task",
                title="COO Coordination",
                project_id="proj1",
                assigned_to="coo",
                status=TaskStatus.RUNNING,
                metadata={"work_item_projection_id": "coo_coordination", "execution_task_ids": ["coo-task", "cmo-task"]},
            )
            await store.save_task(task)

            result = await send_dm_tool.func(
                to_agent="cmo",
                subject="Need planning input",
                body="Share the current narrative direction.",
                blocking=True,
                task=task,
            )

            self.assertEqual(result["delivery_mode"], "async_mailbox")
            self.assertTrue(result["blocking_deprecated"])
            self.assertFalse(result.get("requires_peer_wait", False))
            self.assertEqual(task.status, TaskStatus.RUNNING)
            await store.close()

    async def test_delegate_work_resolves_semantic_dependencies_and_aliases(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            tools = create_collaboration_tools(communication)
            delegate_tool = next(tool for tool in tools if tool.name == "delegate_work")
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="parent-item",
                    run_id="run-1",
                    cell_id="team::ceo",
                    team_id="team::ceo",
                    role_id="ceo",
                    seat_id="seat::team::ceo::ceo",
                    title="CEO Intake",
                    summary="Dispatch the project.",
                    kind="intake",
                    projection_id="ceo-intake",
                    phase=Phase.RUNNING,
                    metadata={"dependency_work_item_ids": []},
                )
            )
            task = Task(
                id="ceo-task",
                title="CEO Dispatch",
                description="Dispatch work.",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "dispatch_required",
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::ceo::ceo",
                    "direct_report_role_ids": ["cto", "cmo", "coo"],
                    "allowed_delegate_role_ids": ["cto", "cmo", "coo"],
                    "runtime_topology": {
                        "seats": [
                            {
                                "role_id": "cto",
                                "seat_id": "seat::team::ceo::cto",
                                "team_id": "team::cto",
                                "allowed_delegate_role_ids": ["senior_engineer"],
                            },
                            {
                                "role_id": "cmo",
                                "seat_id": "seat::team::ceo::cmo",
                                "team_id": "team::cmo",
                            },
                            {
                                "role_id": "coo",
                                "seat_id": "seat::team::ceo::coo",
                                "team_id": "team::coo",
                            },
                        ]
                    },
                },
            )
            set_linked_work_item_id(task, "parent-item")

            result = await delegate_tool.func(
                planning_context="Deliver app, marketing, and integrated QA.",
                items=[
                    {
                        "role_id": "cto",
                        "title": "Build the app",
                        "task_brief": "Context: build the app. Mission: deliver runnable code.",
                        "brief": "Build app UI/audit summary.",
                        "outputs": ["app source"],
                        "done_when": ["app runs"],
                        "work_item_ref": "app",
                    },
                    {
                        "role_id": "cmo",
                        "title": "Prepare marketing structure",
                        "summary": "Context: create campaign scaffold. Mission: prepare the message architecture.",
                        "scope_key": "marketing-prep",
                        "deliverables": ["campaign scaffold"],
                        "acceptance_criteria": ["scaffold is ready for final copy"],
                    },
                    {
                        "role_id": "cmo",
                        "title": "Finalize marketing",
                        "brief": "Context: finalize campaign. Mission: deliver copy.",
                        "scope_key": "marketing-final",
                        "deliverables": ["copy"],
                        "acceptance_criteria": ["copy is ready"],
                        "depends_on": ["marketing-prep"],
                    },
                    {
                        "role_id": "coo",
                        "title": "Package and QA",
                        "brief": "Context: integrate outputs. Mission: verify and package.",
                        "depends_on": ["cto", "marketing-final"],
                    },
                ],
                task=task,
            )

            delegated = result["delegated"]
            self.assertEqual([item["role_id"] for item in delegated], ["cto", "cmo", "cmo", "coo"])
            self.assertTrue(delegated[0]["generated_scope_key"])
            self.assertTrue(delegated[0]["manager_outcome_dispatch"])
            cto_id = delegated[0]["work_item_id"]
            cmo_prep_id = delegated[1]["work_item_id"]
            cmo_final_id = delegated[2]["work_item_id"]
            coo_id = delegated[3]["work_item_id"]
            cmo_final_item = await store.get_delegation_work_item(cmo_final_id)
            self.assertEqual(cmo_final_item.metadata["dependency_work_item_ids"], [cmo_prep_id])
            coo_item = await store.get_delegation_work_item(coo_id)
            self.assertEqual(coo_item.metadata["dependency_work_item_ids"], [cto_id, cmo_final_id])
            self.assertEqual(
                [record["resolved_by"] for record in coo_item.metadata["resolved_dependencies"]],
                ["role_id", "scope_key"],
            )
            self.assertEqual(coo_item.metadata["dependency_specs"][0]["value"], "cto")
            cto_item = await store.get_delegation_work_item(cto_id)
            self.assertEqual(cto_item.team_instance_id, "team-instance::run-1::team::cto")
            self.assertEqual(cto_item.role_runtime_session_id, "role-runtime::run-1::cto")
            self.assertEqual(cto_item.metadata["brief"], "Build app UI/audit summary.")
            self.assertEqual(
                cto_item.metadata["prompt_contract"]["task_brief"],
                "Context: build the app. Mission: deliver runnable code.",
            )
            self.assertEqual(
                cto_item.metadata["prompt_contract"]["assignment_context"]["deliverables"],
                ["app source"],
            )
            self.assertEqual(cto_item.metadata["deliverables"], ["app source"])
            self.assertEqual(cto_item.metadata["acceptance_criteria"], ["app runs"])
            self.assertEqual(cto_item.metadata["owned_outcome_kind"], "execute")
            await store.close()

    async def test_delegate_work_resolves_same_call_scope_key_alias_before_saving(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            wake_calls: list[str] = []
            kanban_calls: list[str] = []
            communication.on_work_items_created = lambda: wake_calls.append("wake")

            async def on_kanban_changed() -> None:
                kanban_calls.append("kanban")

            communication.on_kanban_changed = on_kanban_changed
            delegate_tool = next(tool for tool in create_collaboration_tools(communication) if tool.name == "delegate_work")
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="parent-item",
                    run_id="run-1",
                    cell_id="team::chief",
                    team_id="team::chief",
                    role_id="chief_analyst",
                    seat_id="seat::team::chief::chief_analyst",
                    title="Chief Analyst Intake",
                    kind="intake",
                    projection_id="chief-intake",
                    phase=Phase.RUNNING,
                    metadata={"dependency_work_item_ids": []},
                )
            )
            task = Task(
                id="chief-task",
                title="Chief Analyst Dispatch",
                project_id="proj1",
                assigned_to="chief_analyst",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "dispatch_required",
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::chief::chief_analyst",
                    "direct_report_role_ids": ["researcher", "report_producer"],
                    "runtime_topology": {
                        "seats": [
                            {
                                "role_id": "researcher",
                                "seat_id": "seat::team::chief::researcher",
                                "team_id": "team::research",
                            },
                            {
                                "role_id": "report_producer",
                                "seat_id": "seat::team::chief::report_producer",
                                "team_id": "team::report",
                            },
                        ]
                    },
                },
            )
            set_linked_work_item_id(task, "parent-item")

            result = await delegate_tool.func(
                planning_context="Research first, then create the final report.",
                items=[
                    {
                        "role_id": "researcher",
                        "title": "Research engine feasibility",
                        "brief": "Research coding-agent game engine feasibility.",
                        "scope_key": "engine_feasibility_research",
                    },
                    {
                        "role_id": "report_producer",
                        "title": "Create feasibility report",
                        "brief": "Create the final report from the research brief.",
                        "scope_key": "engine_feasibility_report",
                        "depends_on": ["engine_feasibility_research"],
                    },
                ],
                task=task,
            )

            delegated = result["delegated"]
            self.assertEqual(len(delegated), 2)
            research_id = delegated[0]["work_item_id"]
            report_id = delegated[1]["work_item_id"]
            research = await store.get_delegation_work_item(research_id)
            report = await store.get_delegation_work_item(report_id)
            self.assertEqual(research.metadata["scope_key"], "engine-feasibility-research")
            self.assertEqual(report.metadata["dependency_work_item_ids"], [research_id])
            self.assertEqual(report.metadata["resolved_dependencies"][0]["resolved_by"], "scope_key")
            items = await store.list_delegation_work_items("run-1")
            self.assertEqual(
                [
                    item.work_item_id
                    for item in items
                    if str((item.metadata or {}).get("scope_key", "") or "") == "engine-feasibility-research"
                ],
                [research_id],
            )
            self.assertEqual(wake_calls, ["wake"])
            self.assertEqual(kanban_calls, ["kanban"])
            await store.close()

    async def test_delegate_work_batch_dependency_failure_has_no_partial_work_item_side_effect(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            delegate_tool = next(tool for tool in create_collaboration_tools(communication) if tool.name == "delegate_work")
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="parent-item",
                    run_id="run-1",
                    cell_id="team::chief",
                    team_id="team::chief",
                    role_id="chief_analyst",
                    seat_id="seat::team::chief::chief_analyst",
                    title="Chief Analyst Intake",
                    kind="intake",
                    projection_id="chief-intake",
                    phase=Phase.RUNNING,
                    metadata={"dependency_work_item_ids": []},
                )
            )
            task = Task(
                id="chief-task",
                title="Chief Analyst Dispatch",
                project_id="proj1",
                assigned_to="chief_analyst",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "dispatch_required",
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::chief::chief_analyst",
                    "direct_report_role_ids": ["researcher", "report_producer"],
                    "runtime_topology": {
                        "seats": [
                            {
                                "role_id": "researcher",
                                "seat_id": "seat::team::chief::researcher",
                                "team_id": "team::research",
                            },
                            {
                                "role_id": "report_producer",
                                "seat_id": "seat::team::chief::report_producer",
                                "team_id": "team::report",
                            },
                        ]
                    },
                },
            )
            set_linked_work_item_id(task, "parent-item")

            with self.assertRaisesRegex(ValueError, "could not resolve depends_on reference `missing_research`"):
                await delegate_tool.func(
                    planning_context="This batch must be atomic.",
                    items=[
                        {
                            "role_id": "researcher",
                            "title": "Research engine feasibility",
                            "brief": "Research first.",
                            "scope_key": "engine_feasibility_research",
                        },
                        {
                            "role_id": "report_producer",
                            "title": "Create feasibility report",
                            "brief": "Report later.",
                            "scope_key": "engine_feasibility_report",
                            "depends_on": ["missing_research"],
                        },
                    ],
                    task=task,
                )

            items = await store.list_delegation_work_items("run-1")
            self.assertEqual([item.work_item_id for item in items], ["parent-item"])
            await store.close()

    async def test_delegate_work_reuses_existing_scope_key_by_raw_slug_alias(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            delegate_tool = next(tool for tool in create_collaboration_tools(communication) if tool.name == "delegate_work")
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="parent-item",
                    run_id="run-1",
                    cell_id="team::chief",
                    team_id="team::chief",
                    role_id="chief_analyst",
                    seat_id="seat::team::chief::chief_analyst",
                    title="Chief Analyst Intake",
                    kind="intake",
                    projection_id="chief-intake",
                    phase=Phase.RUNNING,
                    metadata={"dependency_work_item_ids": []},
                )
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="research-existing",
                    run_id="run-1",
                    cell_id="team::research",
                    team_id="team::research",
                    role_id="researcher",
                    seat_id="seat::team::chief::researcher",
                    parent_work_item_id="parent-item",
                    manager_role_id="chief_analyst",
                    manager_seat_id="seat::team::chief::chief_analyst",
                    title="Research engine feasibility",
                    kind="execute",
                    projection_id="research-existing",
                    phase=Phase.APPROVED,
                    metadata={"scope_key": "engine-feasibility-research", "dependency_work_item_ids": []},
                )
            )
            task = Task(
                id="chief-task",
                title="Chief Analyst Dispatch",
                project_id="proj1",
                assigned_to="chief_analyst",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "dispatch_required",
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::chief::chief_analyst",
                    "direct_report_role_ids": ["researcher"],
                    "runtime_topology": {
                        "seats": [
                            {
                                "role_id": "researcher",
                                "seat_id": "seat::team::chief::researcher",
                                "team_id": "team::research",
                            },
                        ]
                    },
                },
            )
            set_linked_work_item_id(task, "parent-item")

            result = await delegate_tool.func(
                planning_context="Retry should reuse existing research work.",
                items=[
                    {
                        "role_id": "researcher",
                        "title": "Research engine feasibility",
                        "brief": "Retry research.",
                        "scope_key": "engine_feasibility_research",
                    }
                ],
                task=task,
            )

            self.assertTrue(result["delegated"][0]["reused"])
            self.assertEqual(result["delegated"][0]["work_item_id"], "research-existing")
            items = await store.list_delegation_work_items("run-1")
            self.assertEqual(
                [
                    item.work_item_id
                    for item in items
                    if str((item.metadata or {}).get("scope_key", "") or "").startswith("engine-feasibility-research")
                ],
                ["research-existing"],
            )
            await store.close()

    async def test_delegate_work_rejects_ambiguous_scope_key_alias(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            delegate_tool = next(tool for tool in create_collaboration_tools(communication) if tool.name == "delegate_work")
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="parent-item",
                    run_id="run-1",
                    cell_id="team::chief",
                    team_id="team::chief",
                    role_id="chief_analyst",
                    seat_id="seat::team::chief::chief_analyst",
                    title="Chief Analyst Intake",
                    kind="intake",
                    projection_id="chief-intake",
                    phase=Phase.RUNNING,
                    metadata={"dependency_work_item_ids": []},
                )
            )
            for work_item_id, scope_key in (
                ("research-a", "engine-feasibility-research"),
                ("research-b", "engine_feasibility_research"),
            ):
                await store.save_delegation_work_item(
                    DelegationWorkItem(
                        work_item_id=work_item_id,
                        run_id="run-1",
                        cell_id="team::research",
                        team_id="team::research",
                        role_id="researcher",
                        seat_id="seat::team::chief::researcher",
                        parent_work_item_id="parent-item",
                        manager_role_id="chief_analyst",
                        manager_seat_id="seat::team::chief::chief_analyst",
                        title="Research engine feasibility",
                        kind="execute",
                        projection_id=work_item_id,
                        phase=Phase.APPROVED,
                        metadata={"scope_key": scope_key, "dependency_work_item_ids": []},
                    )
                )
            task = Task(
                id="chief-task",
                title="Chief Analyst Dispatch",
                project_id="proj1",
                assigned_to="chief_analyst",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "dispatch_required",
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::chief::chief_analyst",
                    "direct_report_role_ids": ["researcher"],
                    "runtime_topology": {
                        "seats": [
                            {
                                "role_id": "researcher",
                                "seat_id": "seat::team::chief::researcher",
                                "team_id": "team::research",
                            },
                        ]
                    },
                },
            )
            set_linked_work_item_id(task, "parent-item")

            with self.assertRaisesRegex(ValueError, "matched multiple existing work items via aliases"):
                await delegate_tool.func(
                    planning_context="Ambiguous retries must be explicit.",
                    items=[
                        {
                            "role_id": "researcher",
                            "title": "Research engine feasibility",
                            "brief": "Retry research.",
                            "scope_key": "engine_feasibility_research",
                        }
                    ],
                    task=task,
                )
            await store.close()

    def test_monitor_children_attention_kind_is_not_dispatch(self) -> None:
        session = CompanyMemberSession(
            member_session_id="member-session-1",
            role_id="cto",
            seat_id="seat::team::cto::cto",
            current_turn_mode="monitor_children",
        )

        work_kind = CompanyWorkItemExecutor._attention_work_kind_for_session(session)

        self.assertEqual(work_kind, "monitor")
        self.assertEqual(
            CompanyWorkItemExecutor._attention_title_for_session(session, work_kind),
            "Monitor Children: cto",
        )

    async def test_attention_turn_reads_business_board_and_reuses_existing_scope_key(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            tools = create_collaboration_tools(communication)
            delegate_tool = next(tool for tool in tools if tool.name == "delegate_work")
            board_tool = next(tool for tool in tools if tool.name == "manager_board_read")
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="cto-parent",
                    run_id="run-1",
                    cell_id="team::cto",
                    team_id="team::cto",
                    role_id="cto",
                    seat_id="seat::team::cto::cto",
                    title="CTO Pipeline",
                    kind="execute",
                    projection_id="cto-parent",
                    phase=Phase.WAITING_FOR_CHILDREN,
                    metadata={"dependency_work_item_ids": ["existing-core", "stale-core"]},
                )
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="existing-core",
                    run_id="run-1",
                    cell_id="team::cto",
                    team_instance_id="team-instance::run-1::team::senior",
                    team_id="team::senior",
                    role_id="senior_engineer",
                    seat_id="seat::team::cto::senior_engineer",
                    parent_work_item_id="cto-parent",
                    title="Build pipeline core",
                    kind="execute",
                    projection_id="existing-core",
                    phase=Phase.READY,
                    manager_role_id="cto",
                    manager_seat_id="seat::team::cto::cto",
                    metadata={
                        "scope_key": "cto-pipeline-core",
                        "dependency_work_item_ids": [],
                    },
                )
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="stale-core",
                    run_id="run-1",
                    cell_id="team::senior",
                    team_instance_id="team-instance::run-1::team::senior",
                    team_id="team::senior",
                    role_id="senior_engineer",
                    seat_id="seat::team::cto::senior_engineer",
                    parent_work_item_id="cto-parent",
                    title="Stale pipeline branch",
                    kind="execute",
                    projection_id="stale-core",
                    phase=Phase.CANCELLED,
                    manager_role_id="cto",
                    manager_seat_id="seat::team::cto::cto",
                    metadata={
                        "scope_key": "stale-pipeline-core",
                        "deleted_by_manager_tool": True,
                        "hidden_from_company_kanban": True,
                        "upstream_visibility": "hidden",
                    },
                )
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="attention-monitor",
                    run_id="run-1",
                    cell_id="team::cto",
                    team_id="team::cto",
                    role_id="cto",
                    seat_id="seat::team::cto::cto",
                    parent_work_item_id="cto-parent",
                    title="Monitor Children: cto",
                    kind="monitor",
                    projection_id="attention-monitor",
                    phase=Phase.RUNNING,
                    metadata={
                        "attention_work_item": True,
                        "attention_key": "seat::team::cto::cto:monitor",
                    },
                )
            )
            task = Task(
                id="cto-attention-task",
                title="Monitor Children: cto",
                project_id="proj1",
                assigned_to="cto",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "monitor_children",
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::cto::cto",
                    "direct_report_role_ids": ["senior_engineer"],
                    "allowed_delegate_role_ids": ["senior_engineer"],
                    "runtime_topology": {
                        "seats": [
                            {
                                "role_id": "senior_engineer",
                                "seat_id": "seat::team::cto::senior_engineer",
                                "team_id": "team::senior",
                            }
                        ]
                    },
                },
            )
            set_linked_work_item_id(task, "attention-monitor")

            board = await board_tool.func(task=task)

            self.assertEqual(board["parent_work_item_id"], "cto-parent")
            self.assertEqual(board["attention_work_item_id"], "attention-monitor")
            self.assertEqual([item["work_item_id"] for item in board["items"]], ["existing-core"])
            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=communication,
                approval_engine=SimpleNamespace(),
                memory=None,
                execute_task=AsyncMock(),
                save_task=AsyncMock(),
                store=store,
            )
            await executor._inject_manager_board_into_context(task, None)
            self.assertEqual(task.metadata["manager_board_parent_work_item_id"], "cto-parent")
            self.assertEqual(task.context_snapshot["manager_board_attention_work_item_id"], "attention-monitor")
            self.assertEqual(task.context_snapshot["manager_board_children"][0]["scope_key"], "cto-pipeline-core")
            self.assertIn("Current Manager Board", task.description)
            self.assertIn("do not treat it as a fresh empty dispatch board", task.description)
            self.assertIn("scope=`cto-pipeline-core`", task.description)

            reused = await delegate_tool.func(
                planning_context="Monitor completed children without re-dispatching existing scopes.",
                items=[
                    {
                        "role_id": "senior_engineer",
                        "title": "Build pipeline core again",
                        "brief": "Context: duplicate request. Mission: should reuse.",
                        "scope_key": "cto-pipeline-core",
                    }
                ],
                task=task,
            )

            self.assertEqual(reused["parent_work_item_id"], "cto-parent")
            self.assertEqual(reused["attention_work_item_id"], "attention-monitor")
            self.assertEqual(reused["delegated"][0]["work_item_id"], "existing-core")
            self.assertTrue(reused["delegated"][0]["reused"])
            items = await store.list_delegation_work_items("run-1")
            self.assertEqual(
                [
                    item.work_item_id
                    for item in items
                    if str((item.metadata or {}).get("scope_key", "") or "") == "cto-pipeline-core"
                ],
                ["existing-core"],
            )

            created = await delegate_tool.func(
                planning_context="Create the missing demo slice.",
                items=[
                    {
                        "role_id": "senior_engineer",
                        "title": "Render pipeline demo",
                        "brief": "Context: new work. Mission: render demo.",
                        "scope_key": "cto-pipeline-demo",
                    }
                ],
                task=task,
            )

            self.assertFalse(created["delegated"][0]["reused"])
            created_item = await store.get_delegation_work_item(created["delegated"][0]["work_item_id"])
            self.assertEqual(created_item.parent_work_item_id, "cto-parent")
            parent_after = await store.get_delegation_work_item("cto-parent")
            self.assertNotIn("stale-core", parent_after.metadata["dependency_work_item_ids"])
            self.assertEqual(
                parent_after.metadata["dependency_work_item_ids"],
                ["existing-core", created_item.work_item_id],
            )
            await store.close()

    async def test_manager_board_read_invalid_parent_falls_back_to_current_board_with_warning(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            tools = create_collaboration_tools(communication)
            board_tool = next(tool for tool in tools if tool.name == "manager_board_read")
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="ceo-parent",
                    run_id="run-1",
                    cell_id="team::ceo",
                    team_id="team::ceo",
                    role_id="ceo",
                    seat_id="seat::team::ceo::ceo",
                    title="CEO Intake",
                    kind="intake",
                    projection_id="ceo-parent",
                    phase=Phase.RUNNING,
                    metadata={"dependency_work_item_ids": []},
                )
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="cto-child",
                    run_id="run-1",
                    cell_id="team::cto",
                    team_id="team::cto",
                    role_id="cto",
                    seat_id="seat::team::ceo::cto",
                    parent_work_item_id="ceo-parent",
                    title="Build feature",
                    kind="execute",
                    projection_id="cto-child",
                    phase=Phase.READY,
                    manager_role_id="ceo",
                    manager_seat_id="seat::team::ceo::ceo",
                )
            )
            task = Task(
                id="ceo-task",
                title="CEO Intake",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::ceo::ceo",
                },
            )
            set_linked_work_item_id(task, "ceo-parent")

            board = await board_tool.func(parent_work_item_id="missing-parent", task=task)

            self.assertEqual(board["parent_work_item_id"], "ceo-parent")
            self.assertEqual(board["requested_parent_work_item_id"], "missing-parent")
            self.assertIn("current manager board", board["warning"])
            self.assertEqual([item["work_item_id"] for item in board["items"]], ["cto-child"])
            await store.close()

    async def test_followup_manager_board_context_requires_reconciliation(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="ceo-parent",
                    run_id="run-1",
                    cell_id="team::ceo",
                    team_id="team::ceo",
                    role_id="ceo",
                    seat_id="seat::team::ceo::ceo",
                    title="CEO Intake",
                    kind="intake",
                    projection_id="ceo-parent",
                    phase=Phase.WAITING_FOR_CHILDREN,
                    metadata={"dependency_work_item_ids": ["old-space-game"]},
                )
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="old-space-game",
                    run_id="run-1",
                    cell_id="team::cto",
                    team_instance_id="team-instance::run-1::team::cto",
                    team_id="team::cto",
                    role_id="cto",
                    seat_id="seat::team::ceo::cto",
                    parent_work_item_id="ceo-parent",
                    title="Build spaceship game",
                    kind="execute",
                    projection_id="old-space-game",
                    phase=Phase.RUNNING,
                    manager_role_id="ceo",
                    manager_seat_id="seat::team::ceo::ceo",
                    metadata={
                        "scope_key": "space-game",
                        "dependency_work_item_ids": [],
                    },
                )
            )
            task = Task(
                id="ceo-followup-task",
                title="CEO Follow-up",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.RUNNING,
                description="Original request: build a spaceship game.",
                context_snapshot={
                    "user_supplied_input": "改成水下潜艇探险，不要继续太空船方向。",
                },
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "dispatch_required",
                    "followup_routed_to_final_decider": True,
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::ceo::ceo",
                    "direct_report_role_ids": ["cto"],
                    "allowed_delegate_role_ids": ["cto"],
                },
            )
            set_linked_work_item_id(task, "ceo-parent")
            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=communication,
                approval_engine=SimpleNamespace(),
                memory=None,
                execute_task=AsyncMock(),
                save_task=AsyncMock(),
                store=store,
            )

            await executor._inject_manager_board_into_context(task, None)

            self.assertIn("Latest user follow-up", task.description)
            self.assertIn("classify current children as keep, revise, delete, or replace", task.description)
            self.assertIn("delete_work_item", task.description)
            self.assertIn("replacement_dependency_work_item_ids", task.description)
            self.assertIn("stale running work and downstream delivery dependencies", task.description)
            await store.close()

    async def test_manager_board_context_renders_business_parent_and_latest_directive(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="cmo-sapphire-parent",
                    run_id="run-1",
                    cell_id="team::cmo",
                    team_id="team::cmo",
                    role_id="cmo",
                    seat_id="seat::team::ceo::cmo",
                    title="Theme, UX copy, and player-facing polish for Sapphire Tide Runner",
                    summary="Own the Sapphire Tide Runner visual direction and UX copy.",
                    kind="execute",
                    projection_id="cmo-sapphire-parent",
                    phase=Phase.WAITING_FOR_CHILDREN,
                    metadata={
                        "latest_user_directive": "Stop Amber Gear Orchard and build Sapphire Tide Runner instead.",
                        "dependency_work_item_ids": ["sapphire-copy"],
                    },
                )
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="sapphire-copy",
                    run_id="run-1",
                    cell_id="team::copy",
                    team_id="team::copy",
                    role_id="copywriter",
                    seat_id="seat::team::cmo::copywriter",
                    parent_work_item_id="cmo-sapphire-parent",
                    title="Sapphire Tide Runner UX copy",
                    kind="execute",
                    projection_id="sapphire-copy",
                    phase=Phase.READY,
                    manager_role_id="cmo",
                    manager_seat_id="seat::team::ceo::cmo",
                    metadata={"scope_key": "sapphire-copy", "dependency_work_item_ids": []},
                )
            )
            task = Task(
                id="cmo-monitor",
                title="Monitor Children: cmo",
                project_id="proj1",
                assigned_to="cmo",
                status=TaskStatus.RUNNING,
                description="Monitor child work.",
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "monitor_children",
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::ceo::cmo",
                },
            )
            set_linked_work_item_id(task, "cmo-sapphire-parent")
            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=communication,
                approval_engine=SimpleNamespace(),
                memory=None,
                execute_task=AsyncMock(),
                save_task=AsyncMock(),
                store=store,
            )

            await executor._inject_manager_board_into_context(task, None)

            self.assertIn("Business parent title: Theme, UX copy", task.description)
            self.assertIn("Sapphire Tide Runner visual direction", task.description)
            self.assertIn("Latest user directive for this business parent", task.description)
            self.assertEqual(
                task.context_snapshot["manager_board_parent"]["latest_user_directive"],
                "Stop Amber Gear Orchard and build Sapphire Tide Runner instead.",
            )
            await store.close()

    async def test_manager_board_context_rehydrates_latest_directive_for_attention_turn(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="cmo-sapphire-parent",
                    run_id="run-1",
                    cell_id="team::cmo",
                    team_id="team::cmo",
                    role_id="cmo",
                    seat_id="seat::team::ceo::cmo",
                    title="Theme, UX copy, and player-facing polish for Sapphire Tide Runner",
                    summary="Own the Sapphire Tide Runner visual direction and UX copy.",
                    kind="execute",
                    projection_id="cmo-sapphire-parent",
                    phase=Phase.WAITING_FOR_CHILDREN,
                    metadata={
                        "latest_user_directive": "Stop Amber Gear Orchard and build Sapphire Tide Runner instead.",
                        "prompt_contract": {
                            "version": 2,
                            "task_brief": "Own the Sapphire Tide Runner visual direction and UX copy.",
                            "assignment_context": {
                                "upstream_intent_summary": "Latest user directive: Stop Amber Gear Orchard and build Sapphire Tide Runner instead.",
                                "owned_outcome_kind": "execute",
                            },
                            "turn_profiles": {},
                        },
                    },
                )
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="attention-cmo-monitor",
                    run_id="run-1",
                    cell_id="team::cmo",
                    team_id="team::cmo",
                    role_id="cmo",
                    seat_id="seat::team::ceo::cmo",
                    parent_work_item_id="cmo-sapphire-parent",
                    title="Monitor Children: cmo",
                    summary="Monitor child work.",
                    kind="monitor",
                    projection_id="attention-cmo-monitor",
                    phase=Phase.RUNNING,
                    metadata={
                        "attention_work_item": True,
                        "attention_key": "seat::team::ceo::cmo:monitor",
                        "work_kind": "monitor",
                    },
                )
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="sapphire-copy",
                    run_id="run-1",
                    cell_id="team::copy",
                    team_id="team::copy",
                    role_id="copywriter",
                    seat_id="seat::team::cmo::copywriter",
                    parent_work_item_id="cmo-sapphire-parent",
                    title="Sapphire Tide Runner UX copy",
                    kind="execute",
                    projection_id="sapphire-copy",
                    phase=Phase.READY,
                    manager_role_id="cmo",
                    manager_seat_id="seat::team::ceo::cmo",
                    metadata={"scope_key": "sapphire-copy", "dependency_work_item_ids": []},
                )
            )
            task = Task(
                id="cmo-monitor",
                title="Monitor Children: cmo",
                project_id="proj1",
                assigned_to="cmo",
                status=TaskStatus.RUNNING,
                description="Monitor child work.",
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "monitor_children",
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::ceo::cmo",
                    "original_message": "Build Amber Gear Orchard as the requested browser game.",
                },
            )
            set_linked_work_item_id(task, "attention-cmo-monitor")
            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=communication,
                approval_engine=SimpleNamespace(),
                memory=None,
                execute_task=AsyncMock(),
                save_task=AsyncMock(),
                store=store,
            )

            await executor._inject_manager_board_into_context(task, None)

            self.assertEqual(
                task.metadata["latest_user_directive"],
                "Stop Amber Gear Orchard and build Sapphire Tide Runner instead.",
            )
            self.assertIn(
                "Latest user directive is authoritative",
                task.metadata["prompt_contract"]["task_brief"],
            )
            brief = ContextAssembler(memory=SimpleNamespace()).build_task_brief(task)
            self.assertIn("## Latest User Directive (AUTHORITATIVE)", brief)
            self.assertLess(
                brief.index("Latest User Directive"),
                brief.index("Original User Request"),
            )
            await store.close()

    def test_build_task_brief_marks_latest_directive_authoritative_for_reworked_subtask(self) -> None:
        assembler = ContextAssembler(memory=SimpleNamespace())
        task = Task(
            id="cmo-monitor",
            title="Theme, UX copy, and player-facing polish for Sapphire Tide Runner",
            description="Monitor Sapphire Tide Runner child work and synthesize the UX result.",
            project_id="proj1",
            assigned_to="cmo",
            metadata={
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "original_message": "Build Amber Gear Orchard as the requested browser game.",
                "latest_user_directive": "Stop Amber Gear Orchard and build Sapphire Tide Runner instead.",
            },
        )

        brief = assembler.build_task_brief(task)

        self.assertIn("## Latest User Directive (AUTHORITATIVE)", brief)
        self.assertIn("supersedes conflicting details from the original request", brief)
        self.assertIn("## Original User Request (background; superseded if conflicting)", brief)
        self.assertIn("## Your Current Work Item", brief)
        self.assertLess(
            brief.index("Latest User Directive"),
            brief.index("Original User Request"),
        )
        self.assertLess(
            brief.index("Original User Request"),
            brief.index("Your Current Work Item"),
        )

    async def test_attention_work_item_inherits_parent_latest_directive_and_contract(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            root_task = Task(
                id="root",
                title="CEO Intake",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.RUNNING,
                description="Build Amber Gear Orchard as the requested browser game.",
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "delegation_run_id": "run-1",
                    "original_message": "Build Amber Gear Orchard as the requested browser game.",
                    "runtime_topology": {
                        "seats": [
                            {
                                "role_id": "cmo",
                                "seat_id": "seat::team::ceo::cmo",
                                "team_id": "team::cmo",
                                "team_instance_id": "team-inst::cmo",
                            }
                        ]
                    },
                },
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="cmo-sapphire-parent",
                    run_id="run-1",
                    cell_id="team::cmo",
                    team_instance_id="team-inst::cmo",
                    team_id="team::cmo",
                    role_id="cmo",
                    seat_id="seat::team::ceo::cmo",
                    title="Theme, UX copy, and player-facing polish for Sapphire Tide Runner",
                    summary="Own the Sapphire Tide Runner visual direction and UX copy.",
                    kind="execute",
                    projection_id="cmo-sapphire-parent",
                    phase=Phase.WAITING_FOR_CHILDREN,
                    metadata={
                        "latest_user_directive": "Stop Amber Gear Orchard and build Sapphire Tide Runner instead.",
                        "manager_mutation_user_input": "Stop Amber Gear Orchard and build Sapphire Tide Runner instead.",
                        "prompt_contract": {
                            "version": 2,
                            "task_brief": "Own the Sapphire Tide Runner visual direction and UX copy.",
                            "assignment_context": {
                                "upstream_intent_summary": "Latest user directive: Stop Amber Gear Orchard and build Sapphire Tide Runner instead.",
                                "owned_outcome_kind": "execute",
                                "deliverables": ["Sapphire Tide Runner UX direction"],
                                "acceptance_criteria": ["No Amber Gear Orchard direction remains."],
                            },
                            "turn_profiles": {},
                        },
                    },
                )
            )
            work_items = await store.list_delegation_work_items("run-1")
            session = CompanyMemberSession(
                role_session_id="role-session::cmo",
                team_instance_id="team-inst::cmo",
                team_id="team::cmo",
                role_id="cmo",
                seat_id="seat::team::ceo::cmo",
                focused_work_item_id="cmo-sapphire-parent",
                current_turn_mode="monitor_children",
                metadata={"seat_id": "seat::team::ceo::cmo", "role_id": "cmo"},
            )
            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=communication,
                approval_engine=SimpleNamespace(),
                memory=None,
                execute_task=AsyncMock(),
                save_task=AsyncMock(),
                store=store,
            )

            updated_tasks, updated_items = await executor._upsert_attention_work_item(
                root_task=root_task,
                tasks=[root_task],
                work_items=work_items,
                session=session,
                source_message={
                    "msg_id": "msg-1",
                    "from_agent": "engineer",
                    "subject": "Child finished",
                    "body": "A child work item completed and needs manager monitoring.",
                },
            )

            attention_item = next(
                item for item in updated_items if dict(item.metadata or {}).get("attention_work_item")
            )
            self.assertEqual(
                attention_item.metadata["latest_user_directive"],
                "Stop Amber Gear Orchard and build Sapphire Tide Runner instead.",
            )
            self.assertIn(
                "Latest user directive is authoritative",
                attention_item.metadata["prompt_contract"]["task_brief"],
            )
            attention_task = next(
                task
                for task in updated_tasks
                if linked_work_item_id_for_task(task) == attention_item.work_item_id
            )
            self.assertEqual(
                attention_task.metadata["latest_user_directive"],
                "Stop Amber Gear Orchard and build Sapphire Tide Runner instead.",
            )
            brief = ContextAssembler(memory=SimpleNamespace()).build_task_brief(attention_task)
            self.assertIn("## Latest User Directive (AUTHORITATIVE)", brief)
            self.assertLess(
                brief.index("Latest User Directive"),
                brief.index("Original User Request"),
            )
            await store.close()

    async def test_delegate_work_propagates_latest_user_directive_to_new_children(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            tools = create_collaboration_tools(communication)
            delegate_tool = next(tool for tool in tools if tool.name == "delegate_work")
            board_tool = next(tool for tool in tools if tool.name == "manager_board_read")
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="ceo-parent",
                    run_id="run-1",
                    cell_id="team::ceo",
                    team_id="team::ceo",
                    role_id="ceo",
                    seat_id="seat::team::ceo::ceo",
                    title="CEO Intake",
                    summary="Original Amber Gear Orchard intake.",
                    kind="intake",
                    projection_id="ceo-parent",
                    phase=Phase.RUNNING,
                    metadata={"dependency_work_item_ids": []},
                )
            )
            task = Task(
                id="ceo-followup",
                title="CEO Follow-up",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.RUNNING,
                context_snapshot={
                    "user_supplied_input": "Stop Amber Gear Orchard and build Sapphire Tide Runner instead.",
                },
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "dispatch_required",
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::ceo::ceo",
                    "direct_report_role_ids": ["cmo"],
                    "global_intent_summary": "Build Amber Gear Orchard as a browser game.",
                    "runtime_topology": {
                        "seats": [
                            {
                                "role_id": "cmo",
                                "seat_id": "seat::team::ceo::cmo",
                                "team_id": "team::cmo",
                            }
                        ]
                    },
                },
            )
            set_linked_work_item_id(task, "ceo-parent")

            delegated = await delegate_tool.func(
                planning_context="Replace the old game direction with Sapphire Tide Runner.",
                items=[
                    {
                        "role_id": "cmo",
                        "title": "Theme, UX copy, and player-facing polish for Sapphire Tide Runner",
                        "brief": "Create the Sapphire Tide Runner UX direction.",
                        "scope_key": "sapphire-ux",
                    }
                ],
                task=task,
            )

            created_id = delegated["delegated"][0]["work_item_id"]
            created_item = await store.get_delegation_work_item(created_id)
            self.assertIsNotNone(created_item)
            assert created_item is not None
            self.assertEqual(
                created_item.metadata["latest_user_directive"],
                "Stop Amber Gear Orchard and build Sapphire Tide Runner instead.",
            )
            upstream_intent = created_item.metadata["prompt_contract"]["assignment_context"]["upstream_intent_summary"]
            self.assertIn("Latest user directive", upstream_intent)
            self.assertIn("Background intent before/latest alongside this directive", upstream_intent)
            self.assertLess(upstream_intent.index("Latest user directive"), upstream_intent.index("Background intent"))
            board = await board_tool.func(task=task)
            self.assertEqual(board["parent_item"]["title"], "CEO Intake")
            self.assertEqual(board["items"][0]["latest_user_directive"], "Stop Amber Gear Orchard and build Sapphire Tide Runner instead.")
            await store.close()

    async def test_delegate_work_rejects_unresolved_semantic_dependency(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            delegate_tool = next(tool for tool in create_collaboration_tools(communication) if tool.name == "delegate_work")
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="parent-item",
                    run_id="run-1",
                    cell_id="team::ceo",
                    team_id="team::ceo",
                    role_id="ceo",
                    seat_id="seat::team::ceo::ceo",
                    title="CEO Intake",
                    summary="Dispatch the project.",
                    kind="intake",
                    projection_id="ceo-intake",
                    phase=Phase.RUNNING,
                    metadata={"dependency_work_item_ids": []},
                )
            )
            task = Task(
                id="ceo-task",
                title="CEO Dispatch",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "dispatch_required",
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::ceo::ceo",
                    "direct_report_role_ids": ["coo"],
                    "runtime_topology": {
                        "seats": [
                            {
                                "role_id": "coo",
                                "seat_id": "seat::team::ceo::coo",
                                "team_id": "team::coo",
                            },
                        ]
                    },
                },
            )
            set_linked_work_item_id(task, "parent-item")

            with self.assertRaisesRegex(ValueError, "could not resolve depends_on reference `missing-scope`"):
                await delegate_tool.func(
                    planning_context="Plan",
                    items=[
                        {
                            "role_id": "coo",
                            "title": "QA",
                            "brief": "Context: QA.",
                            "depends_on": ["missing-scope"],
                        }
                    ],
                    task=task,
                )

            items = await store.list_delegation_work_items("run-1")
            self.assertEqual([item.work_item_id for item in items], ["parent-item"])
            await store.close()

    async def test_delegate_work_rejects_unknown_item_fields_atomically(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            delegate_tool = next(tool for tool in create_collaboration_tools(communication) if tool.name == "delegate_work")
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="parent-item",
                    run_id="run-1",
                    cell_id="team::ceo",
                    team_id="team::ceo",
                    role_id="ceo",
                    seat_id="seat::team::ceo::ceo",
                    title="CEO Intake",
                    summary="Dispatch the project.",
                    kind="intake",
                    projection_id="ceo-intake",
                    phase=Phase.RUNNING,
                    metadata={"dependency_work_item_ids": []},
                )
            )
            task = Task(
                id="ceo-task",
                title="CEO Dispatch",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "dispatch_required",
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::ceo::ceo",
                    "direct_report_role_ids": ["coo", "cmo"],
                    "runtime_topology": {
                        "seats": [
                            {
                                "role_id": "coo",
                                "seat_id": "seat::team::ceo::coo",
                                "team_id": "team::coo",
                            },
                            {
                                "role_id": "cmo",
                                "seat_id": "seat::team::ceo::cmo",
                                "team_id": "team::cmo",
                            },
                        ]
                    },
                },
            )
            set_linked_work_item_id(task, "parent-item")

            with self.assertRaisesRegex(ValueError, "depends_on_scope_keys.*depends_on"):
                await delegate_tool.func(
                    planning_context="Plan",
                    items=[
                        {
                            "role_id": "coo",
                            "title": "QA",
                            "brief": "Context: QA.",
                        },
                        {
                            "role_id": "cmo",
                            "title": "Finalize messaging",
                            "brief": "Context: messaging.",
                            "depends_on_scope_keys": ["qa"],
                        },
                    ],
                    task=task,
                )

            items = await store.list_delegation_work_items("run-1")
            self.assertEqual([item.work_item_id for item in items], ["parent-item"])
            await store.close()

    async def test_delegate_work_blocks_duplicate_same_role_followup_without_parallel_guard(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            delegate_tool = next(tool for tool in create_collaboration_tools(communication) if tool.name == "delegate_work")
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="ceo-parent",
                    run_id="run-1",
                    cell_id="team::ceo",
                    team_id="team::ceo",
                    role_id="ceo",
                    seat_id="seat::team::ceo::ceo",
                    title="CEO Intake",
                    kind="intake",
                    projection_id="ceo-parent",
                    phase=Phase.WAITING_FOR_CHILDREN,
                    metadata={"dependency_work_item_ids": ["old-cto"]},
                )
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="old-cto",
                    run_id="run-1",
                    cell_id="team::cto",
                    team_id="team::cto",
                    role_id="cto",
                    seat_id="seat::team::ceo::cto",
                    parent_work_item_id="ceo-parent",
                    title="Old CTO implementation",
                    kind="execute",
                    projection_id="old-cto",
                    phase=Phase.RUNNING,
                    manager_role_id="ceo",
                    manager_seat_id="seat::team::ceo::ceo",
                    metadata={"scope_key": "old-implementation", "dependency_work_item_ids": []},
                )
            )
            task = Task(
                id="ceo-followup",
                title="CEO Follow-up",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.RUNNING,
                context_snapshot={"user_supplied_input": "Change the game to a watercolor garden stealth puzzle."},
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "dispatch_required",
                    "followup_routed_to_final_decider": True,
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::ceo::ceo",
                    "direct_report_role_ids": ["cto"],
                    "runtime_topology": {
                        "seats": [
                            {
                                "role_id": "cto",
                                "seat_id": "seat::team::ceo::cto",
                                "team_id": "team::cto",
                            }
                        ]
                    },
                },
            )
            set_linked_work_item_id(task, "ceo-parent")

            with self.assertRaisesRegex(ValueError, "modify_work_item.*delete_work_item"):
                await delegate_tool.func(
                    planning_context="Replace the old implementation direction.",
                    items=[
                        {
                            "role_id": "cto",
                            "title": "New CTO implementation",
                            "scope_key": "new-watercolor-implementation",
                            "task_brief": "Build the watercolor garden stealth puzzle.",
                            "deliverables": ["new implementation"],
                            "acceptance_criteria": ["old direction removed"],
                            "non_overlap_guard": "Would replace the old implementation.",
                        }
                    ],
                    task=task,
                )

            items = await store.list_manager_board(
                "run-1",
                manager_seat_id="seat::team::ceo::ceo",
                parent_work_item_id="ceo-parent",
            )
            self.assertEqual([item.work_item_id for item in items], ["old-cto"])
            await store.close()

    async def test_delegate_work_allows_explicit_parallel_same_role_followup(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            delegate_tool = next(tool for tool in create_collaboration_tools(communication) if tool.name == "delegate_work")
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="ceo-parent",
                    run_id="run-1",
                    cell_id="team::ceo",
                    team_id="team::ceo",
                    role_id="ceo",
                    seat_id="seat::team::ceo::ceo",
                    title="CEO Intake",
                    kind="intake",
                    projection_id="ceo-parent",
                    phase=Phase.WAITING_FOR_CHILDREN,
                    metadata={"dependency_work_item_ids": ["old-cto"]},
                )
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="old-cto",
                    run_id="run-1",
                    cell_id="team::cto",
                    team_id="team::cto",
                    role_id="cto",
                    seat_id="seat::team::ceo::cto",
                    parent_work_item_id="ceo-parent",
                    title="Old CTO implementation",
                    kind="execute",
                    projection_id="old-cto",
                    phase=Phase.RUNNING,
                    manager_role_id="ceo",
                    manager_seat_id="seat::team::ceo::ceo",
                    metadata={"scope_key": "old-implementation", "dependency_work_item_ids": []},
                )
            )
            task = Task(
                id="ceo-followup",
                title="CEO Follow-up",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.RUNNING,
                context_snapshot={"user_supplied_input": "Add a separate accessibility audit work item."},
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "dispatch_required",
                    "followup_routed_to_final_decider": True,
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::ceo::ceo",
                    "direct_report_role_ids": ["cto"],
                    "runtime_topology": {
                        "seats": [
                            {
                                "role_id": "cto",
                                "seat_id": "seat::team::ceo::cto",
                                "team_id": "team::cto",
                            }
                        ]
                    },
                },
            )
            set_linked_work_item_id(task, "ceo-parent")

            result = await delegate_tool.func(
                planning_context="Keep implementation running; add a separate technical audit lane.",
                items=[
                    {
                        "role_id": "cto",
                        "title": "Accessibility audit",
                        "scope_key": "accessibility-audit",
                        "task_brief": "Audit keyboard and screen-reader accessibility.",
                        "deliverables": ["audit report"],
                        "acceptance_criteria": ["audit is separate from implementation"],
                        "non_overlap_guard": "Parallel audit only; do not edit implementation files.",
                        "allow_parallel_same_role": True,
                    }
                ],
                task=task,
            )

            self.assertEqual(result["delegated"][0]["role_id"], "cto")
            created = await store.get_delegation_work_item(result["delegated"][0]["work_item_id"])
            self.assertIsNotNone(created)
            assert created is not None
            self.assertTrue(created.metadata["allow_parallel_same_role"])
            await store.close()

    async def test_modify_work_item_rewrites_existing_child_contract(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            tools = create_collaboration_tools(communication)
            modify_tool = next(tool for tool in tools if tool.name == "modify_work_item")
            board_tool = next(tool for tool in tools if tool.name == "manager_board_read")
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="parent-item",
                    run_id="run-1",
                    cell_id="team::ceo",
                    team_id="team::ceo",
                    role_id="ceo",
                    seat_id="seat::team::ceo::ceo",
                    title="CEO Intake",
                    kind="intake",
                    projection_id="ceo-intake",
                    phase=Phase.WAITING_FOR_CHILDREN,
                    metadata={"dependency_work_item_ids": ["child-item"]},
                )
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="child-item",
                    run_id="run-1",
                    cell_id="team::cto",
                    team_instance_id="team-instance::run-1::team::cto",
                    team_id="team::cto",
                    role_id="cto",
                    seat_id="seat::team::ceo::cto",
                    parent_work_item_id="parent-item",
                    title="Old CTO slice",
                    summary="Old summary",
                    kind="execute",
                    projection_id="cto-old",
                    phase=Phase.RUNNING,
                    deliverable_summary="Old stale output",
                    manager_role_id="ceo",
                    manager_seat_id="seat::team::ceo::ceo",
                    claimed_by_role_runtime_session_id="role-runtime::run-1::cto",
                    claimed_by_seat_id="seat::team::ceo::cto",
                    metadata={
                        "scope_key": "cto-site",
                        "brief": "Old brief",
                        "prompt_assignment": {
                            "primary_task_brief": "Old task brief",
                            "deliverables": ["old artifact"],
                            "acceptance_criteria": ["old acceptance"],
                            "scope_key": "cto-site",
                        },
                        "prompt_contract": {
                            "task_brief": "Old task brief",
                            "assignment_context": {
                                "deliverables": ["old artifact"],
                                "acceptance_criteria": ["old acceptance"],
                                "scope_key": "cto-site",
                            },
                        },
                        "dependency_work_item_ids": [],
                        "work_item_summary": "stale",
                    },
                )
            )
            task = Task(
                id="ceo-task",
                title="CEO Follow-up",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.RUNNING,
                context_snapshot={"user_supplied_input": "Change the site to a brutalist UI."},
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "monitor_children",
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::ceo::ceo",
                },
            )
            set_linked_work_item_id(task, "parent-item")
            await store.save_task(task)

            result = await modify_tool.func(
                work_item_id="child-item",
                title="Rebuild CTO slice",
                task_brief="Build the website with a brutalist UI.",
                brief="Rebuild UI as brutalist.",
                deliverables=["brutalist website implementation"],
                acceptance_criteria=["site clearly uses brutalist visual language"],
                reason="User changed the design direction.",
                task=task,
            )

            self.assertEqual(result["action"], "modify_work_item")
            updated = await store.get_delegation_work_item("child-item")
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.phase, Phase.READY)
            self.assertEqual(updated.title, "Rebuild CTO slice")
            self.assertEqual(updated.summary, "Rebuild UI as brutalist.")
            self.assertEqual(updated.deliverable_summary, "")
            self.assertEqual(updated.claimed_by_role_runtime_session_id, "")
            self.assertEqual(updated.metadata["manager_mutation_revision"], 1)
            self.assertEqual(updated.metadata["manager_mutation_action"], "modify")
            self.assertEqual(updated.metadata["manager_mutation_user_input"], "Change the site to a brutalist UI.")
            self.assertEqual(updated.metadata["latest_user_directive"], "Change the site to a brutalist UI.")
            self.assertEqual(updated.metadata["prompt_contract"]["task_brief"], "Build the website with a brutalist UI.")
            self.assertEqual(updated.metadata["deliverables"], ["brutalist website implementation"])
            self.assertNotIn("work_item_summary", updated.metadata)
            board = await board_tool.func(task=task)
            self.assertEqual([item["work_item_id"] for item in board["items"]], ["child-item"])
            await store.close()

    async def test_delete_work_item_cancels_child_hides_board_and_rewrites_dependencies(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            tools = create_collaboration_tools(communication)
            delete_tool = next(tool for tool in tools if tool.name == "delete_work_item")
            board_tool = next(tool for tool in tools if tool.name == "manager_board_read")
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="parent-item",
                    run_id="run-1",
                    cell_id="team::ceo",
                    team_id="team::ceo",
                    role_id="ceo",
                    seat_id="seat::team::ceo::ceo",
                    title="CEO Intake",
                    kind="intake",
                    projection_id="ceo-intake",
                    phase=Phase.WAITING_FOR_CHILDREN,
                    metadata={
                        "dependency_work_item_ids": ["bad-child", "dependent-child"],
                        "waiting_on_work_item_ids": ["bad-child", "dependent-child"],
                    },
                )
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="bad-child",
                    run_id="run-1",
                    cell_id="team::cto",
                    team_instance_id="team-instance::run-1::team::cto",
                    team_id="team::cto",
                    role_id="cto",
                    seat_id="seat::team::ceo::cto",
                    parent_work_item_id="parent-item",
                    title="Wrong child",
                    kind="execute",
                    projection_id="bad-child",
                    phase=Phase.READY,
                    manager_role_id="ceo",
                    manager_seat_id="seat::team::ceo::ceo",
                    metadata={"scope_key": "wrong", "dependency_work_item_ids": []},
                )
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="dependent-child",
                    run_id="run-1",
                    cell_id="team::coo",
                    team_instance_id="team-instance::run-1::team::coo",
                    team_id="team::coo",
                    role_id="coo",
                    seat_id="seat::team::ceo::coo",
                    parent_work_item_id="parent-item",
                    title="Dependent child",
                    kind="execute",
                    projection_id="dependent-child",
                    phase=Phase.WAITING_DEPENDENCIES,
                    manager_role_id="ceo",
                    manager_seat_id="seat::team::ceo::ceo",
                    metadata={
                        "scope_key": "dependent",
                        "dependency_work_item_ids": ["bad-child"],
                        "waiting_on_work_item_ids": ["bad-child"],
                    },
                )
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="grandchild-item",
                    run_id="run-1",
                    cell_id="team::cto",
                    team_instance_id="team-instance::run-1::team::cto",
                    team_id="team::cto",
                    role_id="senior_engineer",
                    seat_id="seat::team::cto::senior_engineer",
                    parent_work_item_id="bad-child",
                    title="Nested obsolete implementation",
                    kind="execute",
                    projection_id="grandchild-item",
                    phase=Phase.RUNNING,
                    manager_role_id="cto",
                    manager_seat_id="seat::team::ceo::cto",
                    metadata={"scope_key": "nested-obsolete"},
                )
            )
            task = Task(
                id="ceo-task",
                title="CEO Follow-up",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": "company_mode",
                    "runtime_model": "multi_team_org",
                    "current_turn_mode": "monitor_children",
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::ceo::ceo",
                },
            )
            set_linked_work_item_id(task, "parent-item")
            await store.save_task(task)
            nested_task = Task(
                id="nested-task",
                title="Nested obsolete implementation",
                project_id="proj1",
                assigned_to="senior_engineer",
                status=TaskStatus.RUNNING,
                metadata={"runtime_model": "multi_team_org", "work_item_runtime": True},
            )
            set_linked_work_item_id(nested_task, "grandchild-item")
            await store.save_task(nested_task)
            await store.link_work_item_runtime_task("grandchild-item", "nested-task")

            result = await delete_tool.func(
                work_item_id="bad-child",
                reason="This work item is wrong for the revised plan.",
                task=task,
            )

            self.assertEqual(result["action"], "delete_work_item")
            deleted = await store.get_delegation_work_item("bad-child")
            dependent = await store.get_delegation_work_item("dependent-child")
            grandchild = await store.get_delegation_work_item("grandchild-item")
            nested_task_after = await store.get_task("nested-task")
            self.assertIsNotNone(deleted)
            self.assertIsNotNone(dependent)
            self.assertIsNotNone(grandchild)
            self.assertIsNotNone(nested_task_after)
            assert deleted is not None
            assert dependent is not None
            assert grandchild is not None
            assert nested_task_after is not None
            self.assertEqual(deleted.phase, Phase.CANCELLED)
            self.assertTrue(deleted.metadata["hidden_from_company_kanban"])
            self.assertEqual(deleted.metadata["upstream_visibility"], "hidden")
            self.assertEqual(grandchild.phase, Phase.CANCELLED)
            self.assertEqual(grandchild.metadata["manager_mutation_action"], "delete")
            self.assertEqual(grandchild.metadata["cascade_deleted_by_work_item_id"], "bad-child")
            self.assertEqual(nested_task_after.status, TaskStatus.CANCELLED)
            self.assertIn("grandchild-item", result["cascade_deleted_work_item_ids"])
            self.assertEqual(dependent.metadata["dependency_work_item_ids"], [])
            self.assertEqual(dependent.metadata["waiting_on_work_item_ids"], [])
            self.assertEqual(dependent.phase, Phase.READY)
            board = await board_tool.func(task=task)
            self.assertEqual([item["work_item_id"] for item in board["items"]], ["dependent-child"])
            await store.close()

    async def test_read_inbox_tool_is_not_exposed(self) -> None:
        tools = create_collaboration_tools(DummyRuntimeCommunication())
        self.assertNotIn("read_inbox", [tool.name for tool in tools])

    def test_manager_board_read_schema_exposes_only_canonical_arguments(self) -> None:
        tools = create_collaboration_tools(DummyRuntimeCommunication())
        board_tool = next(tool for tool in tools if tool.name == "manager_board_read")
        properties = dict((board_tool.parameters or {}).get("properties", {}) or {})

        self.assertEqual(
            set(properties.keys()),
            {"parent_work_item_id", "include_children"},
        )

    async def test_send_dm_tool_uses_work_item_role_id_when_assigned_to_missing(self) -> None:
        captured: dict[str, Any] = {}

        class _StubCommunication:
            async def send_dm(self, message: AgentMessage, task: Task | None = None) -> None:
                captured["message"] = message
                captured["task"] = task

            def _serialize_message(self, message: AgentMessage) -> dict[str, Any]:
                return {
                    "from_agent": message.from_agent,
                    "to_agents": list(message.to_agents),
                    "subject": message.subject,
                }

        tools = create_collaboration_tools(_StubCommunication())
        send_tool = next(tool for tool in tools if tool.name == "send_dm")
        task = Task(
            id="env-task",
            title="Environment Provisioning",
            project_id="proj1",
            assigned_to="",
            status=TaskStatus.RUNNING,
            metadata={"work_item_role_id": "env_engineer", "execution_task_ids": ["env-task"]},
        )

        result = await send_tool.func(
            to_agent="cto",
            subject="Need dependency clarification",
            body="Please confirm the FFmpeg baseline.",
            task=task,
        )

        self.assertEqual(captured["message"].from_agent, "env_engineer")
        self.assertEqual(result["message"]["from_agent"], "env_engineer")

    async def test_propose_task_adjustment_tool_marks_task_for_reorg_checkpoint(self) -> None:
        reorg_manager = DummyReorgManager()
        tools = create_collaboration_tools(DummyRuntimeCommunication(), reorg_manager=reorg_manager)
        tool = next(item for item in tools if item.name == "propose_task_adjustment")
        task = Task(
            id="eng-task",
            title="Engineering Execution",
            project_id="proj1",
            assigned_to="senior_engineer",
            status=TaskStatus.RUNNING,
            session_id="child-1",
            parent_session_id="parent-1",
            metadata={"execution_mode": "company_mode", "work_item_projection_id": "engineering_execution"},
        )

        result = await tool.func(
            summary="We need to add a validation step before implementation continues.",
            changeset={"task_adjustments": [{"task_id": "eng-task", "action": "request_review"}]},
            task=task,
        )

        self.assertTrue(result["requires_user_input"])
        self.assertEqual(task.metadata["pending_reorg_proposal_id"], "proposal-adjust")
        self.assertEqual(task.metadata["pending_reorg_scope"], "task_adjustment")
        self.assertEqual(reorg_manager.adjust_calls[0]["session_id"], "parent-1")

    async def test_meeting_wait_resumes_after_outcome(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            task = Task(
                id="design-task",
                title="Design",
                project_id="proj1",
                assigned_to="coordinator",
                status=TaskStatus.PENDING,
                metadata={"work_item_projection_id": "planning"},
            )
            await store.save_task(task)
            pause = await communication.open_meeting_wait(
                task=task,
                topic="Database selection",
                participants=["executor", "reviewer"],
                agenda=["Choose data store", "Document reason"],
                shared_context="Need a local-first database.",
            )
            self.assertTrue(pause["requires_peer_wait"])
            room_id = pause["meeting_room_id"]
            interim = await communication.respond_to_meeting(
                room_id,
                "coordinator",
                '{"stance":"agree","proposal":"sqlite","vote":"support","reasoning":"likely fine","blocking_issues":[],"assumptions":[],"questions_for_others":[]}',
            )
            self.assertNotEqual(interim.status, MeetingStatus.DECIDED)
            await communication.respond_to_meeting(room_id, "executor", '{"decision":"sqlite"}')
            await communication.respond_to_meeting(room_id, "coordinator", '{"decision":"sqlite","reasoning":"local-first"}', finalize=True)
            resolved = await communication.resolve_task_meeting_wait(task)
            self.assertTrue(resolved)
            self.assertEqual(task.status, TaskStatus.PENDING)
            self.assertEqual(task.context_snapshot["meeting_outcome"]["decision"], "sqlite")
            await store.close()

    def test_build_task_brief_prefers_structured_work_item_assignment(self) -> None:
        assembler = ContextAssembler(memory=SimpleNamespace())
        task = Task(
            id="task-assignment",
            title="Engineering Execution",
            description="legacy description",
            assigned_to="senior_engineer",
            metadata={
                "original_message": "Build a Flask API and write docs.",
                "work_item_projection_title": "Engineering Execution",
                "work_item_turn_type": "execute",
                "work_item_assignment": {
                    "global_intent_summary": "Deliver the requested API runtime cleanly.",
                    "your_responsibility": "Implement the API portion only.",
                    "out_of_scope": ["Do not write the final user-facing documentation."],
                    "inputs": ["Use the approved technical plan from upstream."],
                    "deliverables": ["Working API code and concise implementation notes."],
                    "acceptance_criteria": ["The API behavior matches the approved plan."],
                },
                "work_item_assignment_status": "resolved_from_manager_handoff",
                "work_item_assignment_source_projection_id": "cto_planning",
                "work_item_runtime_plan": {
                    "turn_type": "execute",
                    "summary": "Implement the approved API slice.",
                    "deliverables": ["Working API code"],
                    "acceptance_criteria": ["The API behavior matches the approved plan."],
                    "collaboration_expectations": ["Leave reviewer-friendly artifacts for QA."],
                    "verification_required": True,
                },
                "work_item_artifact_index": [
                    {"kind": "file", "label": "api_module", "value": "src/api.py"},
                ],
            },
        )

        brief = assembler.build_task_brief(task)

        self.assertIn("## Global Intent Summary", brief)
        self.assertIn("Implement the API portion only.", brief)
        self.assertIn("## Work Item Assignment Status", brief)
        self.assertIn("resolved_from_manager_handoff", brief)
        self.assertIn("## Work Item Assignment Source", brief)
        self.assertIn("cto_planning", brief)
        self.assertIn("## Work Item Runtime Plan", brief)
        self.assertIn("Leave reviewer-friendly artifacts for QA.", brief)
        self.assertIn("## Work Item Artifact Index", brief)
        self.assertIn("api_module: src/api.py", brief)
        self.assertNotIn("## Original User Request", brief)

    async def test_external_prompt_context_contains_collaboration_sections(self) -> None:
        with _workspace_tempdir() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                memory = MemoryManager(root, "proj1", store=store)
                communication = CommunicationManager(store, EventBus())
                task = Task(
                    id="external-task",
                    title="Execution",
                    description="Implement the feature",
                    project_id="proj1",
                    assigned_to="executor",
                    metadata={
                        "execution_mode": "company_mode",
                        "work_item_projection_id": "execution",
                        "handoff_context": "Objective: Planning",
                        "employee_assignment": {
                            "employee_id": "backend-architect",
                            "name": "Backend Architect",
                            "role_id": "executor",
                            "domains": ["coding", "api"],
                            "category": "engineering",
                            "experience_score": 7,
                        },
                        "employee_prompt_context": "Backend specialist focused on APIs.",
                        "employee_delta_context": "## Default Checklists\n- Include validation or test evidence before requesting review.",
                        "employee_skill_refs": ["backend-architect-executor-coding-playbook"],
                        "runtime_policy": {
                            "memory": {
                                "include_role_memory": True,
                                "include_decision_log": True,
                                "include_artifact_index": True,
                            }
                        },
                    },
                )
                await store.save_task(task)
                await store.record_role_memory(
                    RoleMemoryRecord(project_id="proj1", role_id="executor", summary="Prefer reviewer-friendly artifacts")
                )
                await store.record_work_item_decision(
                    WorkItemDecisionRecord(project_id="proj1", projection_id="planning", summary="Decision: use SQLite")
                )
                await store.record_artifact(
                    ArtifactRecord(project_id="proj1", projection_id="planning", name="plan.md", location="docs/plan.md")
                )
                await communication.send_dm(
                    AgentMessage(
                        from_agent="reviewer",
                        to_agents=["executor"],
                        subject="Clarify migration scope",
                        body="Question: should the migration include seed data?",
                        task_id=task.id,
                        context_ref=task.id,
                    )
                )
                await store.append_task_comment(
                    task.id,
                    {"from": "reviewer", "body": "Please double-check auth edge cases."},
                )
                refreshed_task = await store.get_task(task.id)
                assert refreshed_task is not None

                assembler = ContextAssembler(memory=memory, store=store, communication=communication)
                enriched = await assembler.build_external_context(refreshed_task, role_id="executor")
                self.assertIn("Role Local Memory", enriched)
                self.assertIn("Decision Log", enriched)
                self.assertIn("Inbox", enriched)
                self.assertIn("Task Annotations", enriched)
                # Self bucket: the previously-separate Assigned
                # Employee / Employee Persona / Employee Delta Profile
                # sections now live inside a single ``## Self``
                # section with H3 sub-blocks (Role / Employee /
                # Employee Persona / Learned Working Profile). See
                # ``ContextAssembler._build_self_section``.
                self.assertIn("## Self", enriched)
                self.assertIn("### Role", enriched)
                self.assertIn("- Role: executor", enriched)
                self.assertIn("### Employee", enriched)
                self.assertIn("- Employee: Backend Architect", enriched)
                self.assertIn("### Employee Persona", enriched)
                self.assertIn("Backend specialist focused on APIs.", enriched)
                self.assertIn("### Learned Working Profile", enriched)
            finally:
                await store.close()

    async def test_corporate_contact_rules_allow_manager_peers_and_direct_reports_only(self) -> None:
        with _workspace_tempdir() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                org_engine = OrgEngine(OPCConfig(), root)
                communication = CommunicationManager(store, EventBus(), org_engine=org_engine)

                await communication.send_dm(
                    AgentMessage(
                        from_agent="cto",
                        to_agents=["ceo"],
                        subject="Escalation",
                        body="Need a decision on architecture tradeoffs.",
                        task_id="task-up",
                    )
                )
                await communication.send_dm(
                    AgentMessage(
                        from_agent="cto",
                        to_agents=["cmo"],
                        subject="Peer sync",
                        body="Need content timing aligned with API scope.",
                        task_id="task-peer",
                    )
                )
                await communication.send_dm(
                    AgentMessage(
                        from_agent="cto",
                        to_agents=["senior_engineer"],
                        subject="Execution handoff",
                        body="Please implement the approved API plan.",
                        task_id="task-down",
                    )
                )

                with self.assertRaisesRegex(ValueError, "cannot message recipient"):
                    await communication.send_dm(
                        AgentMessage(
                            from_agent="cto",
                            to_agents=["unknown_role"],
                            subject="Cross-level reach",
                            body="This should be blocked because the recipient is outside the allowed contact graph.",
                            task_id="task-blocked",
                        )
                    )
            finally:
                await store.close()

    async def test_external_context_includes_contact_directory_with_responsibilities(self) -> None:
        with _workspace_tempdir() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                org_engine = OrgEngine(OPCConfig(), root)
                communication = CommunicationManager(store, EventBus(), org_engine=org_engine)
                assembler = ContextAssembler(
                    memory=SimpleNamespace(
                        build_memory_context=AsyncMock(return_value=""),
                        build_agent_memory_context=AsyncMock(return_value=""),
                    ),
                    store=store,
                    communication=communication,
                )
                task = Task(
                    id="task-contact-directory",
                    title="CTO Planning",
                    description="Create the technical plan.",
                    project_id="proj1",
                    assigned_to="cto",
                    metadata={
                        "execution_mode": "company_mode",
                        "work_item_projection_id": "cto_planning",
                        "work_item_projection_title": "CTO Planning",
                    },
                )
                await store.save_task(task)

                enriched = await assembler.build_external_context(task, role_id="cto")

                # Topology bucket: ownership contract + direct
                # contacts now live inside a single ``## Topology``
                # section with H3 sub-blocks. The old independent
                # ``## Contact Directory`` header was removed — see
                # ContextAssembler._build_topology_section.
                self.assertIn("## Topology", enriched)
                self.assertIn("### Direct Contacts", enriched)
                self.assertIn("ceo (CEO) [manager]", enriched)
                self.assertIn("cmo (CMO) [peer]", enriched)
                self.assertIn("senior_engineer (Senior Engineer) [downstream]", enriched)
                self.assertIn("Code implementation, system development, and technical execution.", enriched)
            finally:
                await store.close()

    async def test_multi_team_external_context_renders_direct_manager_and_reports(self) -> None:
        with _workspace_tempdir() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                org_engine = OrgEngine(OPCConfig(), root)
                communication = CommunicationManager(store, EventBus(), org_engine=org_engine)
                assembler = ContextAssembler(
                    memory=SimpleNamespace(
                        build_memory_context=AsyncMock(return_value=""),
                        build_agent_memory_context=AsyncMock(return_value=""),
                    ),
                    store=store,
                    communication=communication,
                )
                runtime_topology = {
                    "teams": [
                        {"team_id": "team::ceo", "lead_role_id": "ceo"},
                        {"team_id": "team::cto", "lead_role_id": "cto"},
                    ],
                    "seats": [
                        {
                            "seat_id": "seat::team::ceo::ceo",
                            "team_id": "team::ceo",
                            "role_id": "ceo",
                            "metadata": {"role_name": "CEO"},
                        },
                        {
                            "seat_id": "seat::team::ceo::cto",
                            "team_id": "team::ceo",
                            "role_id": "cto",
                            "manager_seat_id": "seat::team::ceo::ceo",
                            "managed_team_id": "team::cto",
                            "metadata": {"role_name": "CTO"},
                        },
                        {
                            "seat_id": "seat::team::cto::senior_engineer",
                            "team_id": "team::cto",
                            "role_id": "senior_engineer",
                            "manager_role_id": "cto",
                            "employee_assignment": {"name": "Senior Engineer Default Employee"},
                            "selected_execution_agent": "codex",
                            "metadata": {"role_name": "Senior Engineer"},
                        },
                    ],
                }
                task = Task(
                    id="multi-team-cto",
                    title="CTO Delegation",
                    description="Split work across engineering.",
                    project_id="proj1",
                    assigned_to="cto",
                    metadata={
                        "execution_mode": "company_mode",
                        "runtime_model": "multi_team_org",
                        "delegation_run_id": "run-multi-team",
                        "delegation_seat_id": "seat::team::ceo::cto",
                        "managed_team_id": "team::cto",
                        "manager_seat_id": "seat::team::ceo::ceo",
                        "runtime_topology": runtime_topology,
                    },
                )
                await store.save_task(task)
                await store.save_seat_state(
                    SeatState(
                        seat_state_id="seat-state-se",
                        team_instance_id="team-instance::run-multi-team::seat::team::ceo::cto::root",
                        run_id="run-multi-team",
                        project_id="proj1",
                        team_id="team::cto",
                        seat_id="seat::team::cto::senior_engineer",
                        role_id="senior_engineer",
                        status="running",
                        resident_status="running",
                    )
                )

                enriched = await assembler.build_external_context(task, role_id="cto")

                self.assertIn("## Topology", enriched)
                self.assertIn("### Direct Manager", enriched)
                self.assertIn("ceo (CEO)", enriched)
                self.assertIn("Strategic intake, high-level routing, final aggregation and delivery to the owner.", enriched)
                self.assertIn("### Direct Reports", enriched)
                self.assertIn("senior_engineer (Senior Engineer)", enriched)
                self.assertIn("Code implementation, system development, and technical execution.", enriched)
                self.assertIn("employee=Senior Engineer Default Employee", enriched)
                self.assertIn("agent=codex", enriched)
                self.assertIn("status=idle", enriched)
                self.assertNotIn("## Parallel Peer Work Items", enriched)
                self.assertNotIn("## Migration Handoff", enriched)
            finally:
                await store.close()

    async def test_memory_context_does_not_include_legacy_structured_project_knowledge(self) -> None:
        with _workspace_tempdir() as tmpdir:
            manager = MemoryManager(Path(tmpdir), "proj1")
            manager.employee_evolution.preferences.update_project_knowledge(
                "proj1",
                {
                    "constraints": ["Keep the app local-only."],
                    "confirmed_tech_stack": ["Python", "uv", "vanilla JS"],
                },
            )
            default_context = await manager.build_memory_context(project_id="proj1")
            explicit_context = await manager.build_memory_context(
                project_id="proj1",
                include_project_knowledge=True,
            )
            self.assertNotIn("Project Knowledge (proj1)", default_context)
            self.assertNotIn("Project Knowledge (proj1)", explicit_context)
            self.assertNotIn("Keep the app local-only.", explicit_context)
            self.assertNotIn("vanilla JS", explicit_context)

    async def test_employee_evolution_uses_runtime_state_not_structured_project_memory(self) -> None:
        with _workspace_tempdir() as tmpdir:
            root = Path(tmpdir)
            manager = MemoryManager(root, "proj1")

            def build_execution_task(run_id: str) -> Task:
                return Task(
                    id=f"execution-{run_id}",
                    title="Execution",
                    status=TaskStatus.DONE,
                    project_id="proj1",
                    assigned_to="executor",
                    tags=["coding", "api"],
                    metadata={
                        "execution_mode": "company_mode",
                        "work_item_projection_id": "execution",
                        "execution_task_ids": [f"execution-{run_id}", f"delivery-{run_id}"],
                        "work_item_summary_for_downstream": "Implemented reliable API delivery",
                        "decisions": ["Decision: use explicit API validation before handoff"],
                        "artifacts": ["tests: pytest -q", "file: src/api.py"],
                        "employee_assignment": {
                            "employee_id": "backend-architect",
                            "template_id": "engineering-backend-architect",
                            "name": "Backend Architect",
                            "role_id": "executor",
                            "domains": ["coding", "api"],
                            "category": "engineering",
                            "preferred_external_agent": "cursor",
                        },
                    },
                    result={"content": "Implemented reliable API delivery with reviewer-friendly artifacts.", "artifacts": {}},
                )

            def build_delivery_task(run_id: str) -> Task:
                return Task(
                    id=f"delivery-{run_id}",
                    title="Delivery",
                    status=TaskStatus.DONE,
                    project_id="proj1",
                    assigned_to="coordinator",
                    tags=["coding", "api"],
                    metadata={
                        "execution_mode": "company_mode",
                        "work_item_projection_id": "delivery",
                        "execution_task_ids": [f"execution-{run_id}", f"delivery-{run_id}"],
                    },
                    result={"content": "Final delivery prepared.", "artifacts": {}},
                )

            reflection_results: list[dict[str, Any]] = []
            for run_id in ("one", "two"):
                execution_task = build_execution_task(run_id)
                manager.employee_evolution.record_work_item_completion(
                    execution_task,
                    "Implemented reliable API delivery with reviewer-friendly artifacts.",
                )
                delivery_task = build_delivery_task(run_id)
                reflection_results.extend(
                    manager.employee_evolution.record_project_reflections(
                        delivery_task,
                        [execution_task, delivery_task],
                    )
                )

            global_profile = manager.employee_evolution.preferences.load_global()
            project_profile = manager.employee_evolution.preferences.load_project("proj1")
            self.assertEqual(global_profile, {})
            self.assertEqual(project_profile, {})
            evolution_global = manager.employee_evolution.load_evolution_profile()
            evolution_project = manager.employee_evolution.load_evolution_profile("proj1")
            self.assertIn("backend-architect", evolution_global.get("employees", {}))
            self.assertIn("backend-architect", evolution_project.get("employees", {}))
            self.assertGreater(
                manager.employee_evolution.get_experience_score(
                    "backend-architect",
                    role_id="executor",
                    domains=["coding", "api"],
                    project_id="proj1",
                ),
                0,
            )
            self.assertIn(
                "backend-architect-executor-coding-playbook",
                manager.employee_evolution.get_learned_skill_refs("backend-architect", project_id="proj1"),
            )
            self.assertEqual(len(reflection_results), 2)
            self.assertTrue(all(result["status"] == "recorded" for result in reflection_results))
            self.assertTrue(all(result["reflection_path"].endswith("projects/proj1/employee_evolution.json") for result in reflection_results))
            self.assertFalse((root / "projects" / "proj1" / "MEMORY.md").exists())
            self.assertTrue((root / "projects" / "proj1" / "skills" / "backend-architect-executor-coding-playbook" / "SKILL.md").exists())

    async def test_ask_peer_and_wait_times_out_and_escalates_to_manager(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            org_engine = OrgEngine(OPCConfig(), Path(tmpdir))
            communication = CommunicationManager(store, EventBus(), org_engine=org_engine)
            task = Task(
                id="eng-task",
                title="Engineering Execution",
                project_id="proj1",
                assigned_to="senior_engineer",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": "company_mode",
                    "work_item_projection_id": "engineering_execution",
                    "work_item_turn_type": "execute",
                },
            )
            peer_task = Task(
                id="devops-task",
                title="DevOps Support",
                project_id="proj1",
                assigned_to="devops_engineer",
                status=TaskStatus.PENDING,
                metadata={
                    "execution_mode": "company_mode",
                    "work_item_projection_id": "devops_support",
                    "work_item_turn_type": "execute",
                },
            )
            await store.save_task(task)
            await store.save_task(peer_task)

            wait = await communication.ask_peer_and_wait(
                task=task,
                to_agent="devops_engineer",
                subject="Need quick QA clarification",
                body="Should rollback steps be explicit in the handoff?",
                timeout_action="Assume rollback steps stay explicit.",
                timeout_seconds=1,
                on_timeout="manager",
            )
            self.assertTrue(wait["requires_peer_wait"])
            task.metadata["peer_wait"]["timeout_at"] = (datetime.now() - timedelta(seconds=5)).isoformat()
            resumed = await communication.resolve_task_peer_wait(task)
            inbox = await communication.read_inbox(
                agent_id="cto",
                task_id=task.id,
                unread_only=True,
                limit=10,
                mark_read=False,
            )

            self.assertTrue(resumed)
            self.assertEqual(task.status, TaskStatus.PENDING)
            self.assertEqual(task.context_snapshot["peer_timeout_escalation"]["mode"], "manager")
            self.assertEqual(len(inbox), 1)
            self.assertIn("Peer timeout escalation", inbox[0]["subject"])
            await store.close()

    async def test_ask_peer_and_wait_rejects_recipient_without_active_work_package(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            org_engine = OrgEngine(OPCConfig(), Path(tmpdir))
            communication = CommunicationManager(store, EventBus(), org_engine=org_engine)
            task = Task(
                id="ceo-task",
                title="CEO Intake",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": "company_mode",
                    "work_item_projection_id": "ceo_intake",
                    "work_item_turn_type": "intake",
                    "work_item_runtime": True,
                },
            )
            await store.save_task(task)

            with self.assertRaises(ValueError):
                await communication.ask_peer_and_wait(
                    task=task,
                    to_agent="cto",
                    subject="Need startup clarification",
                    body="Should we use Three.js?",
                    timeout_seconds=60,
                )
            await store.close()

    @unittest.skip("Filesystem handoff stack removed; see plans/task-cleanup-dead-comms.md"
    )
    async def test_required_handoff_records_are_persisted_as_sent_and_received(self) -> None:
        # The agent-facing `ack_handoff` / `review_handoff` tools were
        # deleted; the underlying handoff record is still created and
        # transitions sent → received on inbox read. The former
        # acked/accepted transitions used to require tool calls and are
        # now obsolete.
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            target_task = Task(
                id="review-task",
                title="Review",
                project_id="proj1",
                assigned_to="reviewer",
                metadata={
                    "work_item_projection_id": "review",
                    "execution_mode": "company_mode",
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                },
            )
            await store.save_task(target_task)

            message = await communication.send_handoff(
                task_id=target_task.id,
                from_agent="executor",
                to_agent="reviewer",
                subject="Execution handoff",
                body="Please review the implementation package.",
                handoff={
                    "handoff_id": "handoff-1",
                    "summary": "Execution package ready",
                    "source_projection_id": "execution",
                    "target_projection_id": "review",
                },
                requires_ack=True,
            )
            sent = await store.get_handoff_record("handoff-1")
            assert sent is not None
            self.assertEqual(sent.status, "sent")
            self.assertEqual(message.metadata["handoff_id"], "handoff-1")

            _ = await communication.read_inbox(
                agent_id="reviewer",
                task=target_task,
                unread_only=True,
                limit=10,
                mark_read=True,
            )
            received = await store.get_handoff_record("handoff-1")
            assert received is not None
            self.assertEqual(received.status, "received")
            await store.close()

    async def test_send_dm_writes_file_comms_and_read_inbox_projects_from_file(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            task = Task(
                id="eng-task",
                session_id="sess-eng",
                parent_session_id="root-session",
                title="Engineering Execution",
                project_id="proj1",
                assigned_to="senior_engineer",
                status=TaskStatus.RUNNING,
                metadata={
                    "work_item_projection_id": "engineering_execution",
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                },
            )
            await store.save_task(task)

            message = await communication.send_dm(
                AgentMessage(
                    from_agent="senior_engineer",
                    to_agents=["cto"],
                    subject="Need architecture confirmation",
                    body="Please confirm the migration boundary.",
                    task_id=task.id,
                    context_ref=task.id,
                ),
                task=task,
            )

            inbox = await communication.read_inbox(
                agent_id="cto",
                task=task,
                unread_only=True,
                limit=10,
                mark_read=False,
            )

            self.assertEqual(len(inbox), 1)
            self.assertEqual(inbox[0]["subject"], "Need architecture confirmation")
            self.assertEqual(inbox[0]["msg_id"], message.msg_id)
            message_path = Path(tmpdir) / ".opc-comms" / "proj1" / "root-session" / "inbox" / "cto" / "new"
            self.assertTrue(any(path.suffix == ".md" for path in message_path.iterdir()))
            await store.close()

    async def test_send_dm_projects_typed_comms_fields_into_store_and_frontmatter(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            task = Task(
                id="approval-task",
                session_id="sess-approval",
                parent_session_id="root-session",
                title="Approval Work Item",
                project_id="proj1",
                assigned_to="executor",
                status=TaskStatus.RUNNING,
                metadata={
                    "work_item_projection_id": "planning",
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                },
            )
            await store.save_task(task)

            message = await communication.send_dm(
                AgentMessage(
                    from_agent="executor",
                    to_agents=["reviewer"],
                    subject="Need plan approval",
                    body="Please approve the rollout order.",
                    task_id=task.id,
                    context_ref=task.id,
                    transport_kind=CommsTransportKind.SYSTEM,
                    semantic_type=CommsSemanticType.APPROVAL_REQUEST,
                    refs={"checkpoint_id": "chk-1"},
                ),
                task=task,
            )
            stored = await store.get_message(message.msg_id)
            assert stored is not None
            self.assertEqual(stored.semantic_type, CommsSemanticType.APPROVAL_REQUEST)
            self.assertEqual(stored.transport_kind, CommsTransportKind.SYSTEM)
            header = file_comms.read_header(Path(message.metadata["comms_path"]))
            assert header is not None
            self.assertEqual(header.raw_frontmatter["semantic_type"], CommsSemanticType.APPROVAL_REQUEST.value)
            self.assertEqual(header.raw_frontmatter["transport_kind"], CommsTransportKind.SYSTEM.value)
            self.assertEqual(header.raw_frontmatter["refs"]["checkpoint_id"], "chk-1")
            await store.close()

    async def test_send_dm_projects_manager_board_mailbox_fields_into_frontmatter_and_inbox(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            task = Task(
                id="manager-task",
                session_id="sess-manager",
                parent_session_id="root-session",
                title="Manager Work Item",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.RUNNING,
                metadata={
                    "work_item_projection_id": "ceo__delegate",
                    "delegation_parent_work_item_id": "root-item",
                    "delegation_seat_id": "seat::team::ceo::ceo",
                    "manager_seat_id": "seat::team::owner::owner",
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                },
            )
            set_linked_work_item_id(task, "parent-item")
            await store.save_task(task)

            message = await communication.send_dm(
                AgentMessage(
                    from_agent="ceo",
                    to_agents=["engineer"],
                    subject="Start implementation",
                    body="Proceed with the approved scope.",
                    task_id=task.id,
                    context_ref=task.id,
                    semantic_type=CommsSemanticType.WORK_UPDATE,
                    metadata={
                        "work_item_id": "parent-item",
                        "parent_work_item_id": "root-item",
                        "source_message_id": "msg-source",
                        "manager_seat_id": "seat::team::ceo::ceo",
                        "target_seat_id": "seat::team::ceo::engineer",
                        "action_hint": "take_assignment",
                    },
                ),
                task=task,
            )
            target_task = Task(
                id="engineer-task",
                session_id="sess-engineer",
                parent_session_id="root-session",
                title="Engineer Work Item",
                project_id="proj1",
                assigned_to="engineer",
                status=TaskStatus.RUNNING,
                metadata={
                    "work_item_projection_id": "engineer__execute",
                    "delegation_seat_id": "seat::team::ceo::engineer",
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                },
            )
            header = file_comms.read_header(Path(message.metadata["comms_path"]))
            assert header is not None
            self.assertEqual(header.raw_frontmatter["work_item_id"], "parent-item")
            self.assertEqual(header.raw_frontmatter["parent_work_item_id"], "root-item")
            self.assertEqual(header.raw_frontmatter["source_message_id"], "msg-source")
            self.assertEqual(header.raw_frontmatter["action_hint"], "take_assignment")

            inbox = await communication.read_inbox(
                agent_id="engineer",
                task=target_task,
                unread_only=True,
                limit=10,
                mark_read=False,
            )
            self.assertEqual(inbox[0]["work_item_id"], "parent-item")
            self.assertEqual(inbox[0]["parent_work_item_id"], "root-item")
            self.assertEqual(inbox[0]["source_message_id"], "msg-source")
            self.assertEqual(inbox[0]["metadata"]["action_hint"], "take_assignment")
            await store.close()

    async def test_read_inbox_mark_read_archives_messages_to_seen(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            task = Task(
                id="archive-task",
                session_id="sess-archive",
                parent_session_id="root-session",
                title="Archive inbox reads",
                project_id="proj1",
                assigned_to="executor",
                status=TaskStatus.RUNNING,
                metadata={
                    "work_item_projection_id": "execution",
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                },
            )
            await store.save_task(task)
            await communication.send_dm(
                AgentMessage(
                    from_agent="reviewer",
                    to_agents=["executor"],
                    subject="Archive this read",
                    body="Once visible, this should leave new/.",
                    task_id=task.id,
                    context_ref=task.id,
                ),
                task=task,
            )

            _ = await communication.read_inbox(
                agent_id="executor",
                task=task,
                unread_only=True,
                limit=10,
                mark_read=True,
            )

            new_dir = Path(tmpdir) / ".opc-comms" / "proj1" / "root-session" / "inbox" / "executor" / "new"
            seen_dir = Path(tmpdir) / ".opc-comms" / "proj1" / "root-session" / "inbox" / "executor" / "seen"
            self.assertFalse(any(path.suffix == ".md" for path in new_dir.iterdir()))
            self.assertTrue(any(path.suffix == ".md" for path in seen_dir.iterdir()))
            await store.close()

    async def test_inbox_peek_does_not_archive_and_ack_archives_selected_messages(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            task = Task(
                id="inbox-tool-task",
                session_id="sess-inbox-tool",
                parent_session_id="root-session",
                title="Inbox tool task",
                project_id="proj1",
                assigned_to="executor",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                },
            )
            await store.save_task(task)
            message = await communication.send_dm(
                AgentMessage(
                    from_agent="reviewer",
                    to_agents=["executor"],
                    subject="Please check this",
                    body="Acknowledge when handled.",
                    task_id=task.id,
                    context_ref=task.id,
                ),
                task=task,
            )

            peek = await communication.inbox(
                agent_id="executor",
                task=task,
                action="peek",
                limit=10,
            )
            self.assertEqual(peek["action"], "peek")
            self.assertEqual(peek["actionable_count"], 1)
            self.assertEqual(peek["messages"][0]["msg_id"], message.msg_id)

            new_dir = Path(tmpdir) / ".opc-comms" / "proj1" / "root-session" / "inbox" / "executor" / "new"
            seen_dir = Path(tmpdir) / ".opc-comms" / "proj1" / "root-session" / "inbox" / "executor" / "seen"
            self.assertTrue(any(path.suffix == ".md" for path in new_dir.iterdir()))

            wrong_role_ack = await communication.inbox(
                agent_id="designer",
                task=task,
                action="ack",
                message_ids=[message.msg_id],
            )
            self.assertEqual(wrong_role_ack["acked"], [])
            self.assertEqual(wrong_role_ack["missing"], [message.msg_id])
            self.assertTrue(any(path.suffix == ".md" for path in new_dir.iterdir()))

            ack = await communication.inbox(
                agent_id="executor",
                task=task,
                action="ack",
                message_ids=[message.msg_id],
            )
            self.assertEqual(ack["acked"], [message.msg_id])
            self.assertFalse(any(path.suffix == ".md" for path in new_dir.iterdir()))
            self.assertTrue(any(path.suffix == ".md" for path in seen_dir.iterdir()))
            stored = await store.get_message(message.msg_id)
            assert stored is not None
            self.assertEqual(stored.status, MessageStatus.READ)
            await store.close()

    async def test_reply_message_acknowledges_original_file_message(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            task = Task(
                id="reply-ack-task",
                session_id="sess-reply-ack",
                parent_session_id="root-session",
                title="Reply ack task",
                project_id="proj1",
                assigned_to="executor",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                },
            )
            await store.save_task(task)
            message = await communication.send_dm(
                AgentMessage(
                    from_agent="reviewer",
                    to_agents=["executor"],
                    subject="Need answer",
                    body="Should we proceed?",
                    task_id=task.id,
                    context_ref=task.id,
                    reply_needed=True,
                    metadata={"wait_mode": "ask_peer_and_wait"},
                ),
                task=task,
            )
            await communication.reply_to_message(
                original_msg_id=message.msg_id,
                from_agent="executor",
                body="Yes, proceed.",
                task_id=task.id,
            )

            new_dir = Path(tmpdir) / ".opc-comms" / "proj1" / "root-session" / "inbox" / "executor" / "new"
            seen_dir = Path(tmpdir) / ".opc-comms" / "proj1" / "root-session" / "inbox" / "executor" / "seen"
            self.assertFalse(any(path.suffix == ".md" for path in new_dir.iterdir()))
            self.assertTrue(any(path.suffix == ".md" for path in seen_dir.iterdir()))
            stored = await store.get_message(message.msg_id)
            assert stored is not None
            self.assertEqual(stored.status, MessageStatus.REPLIED)
            await store.close()

    async def test_reply_message_bypasses_contact_policy_only_for_received_message(self) -> None:
        class PolicyOrg:
            def get_allowed_contact_roles(self, role_id: str, task: Task | None = None):
                return {
                    "env_engineer": ["cto"],
                    "cto": ["ceo"],
                }.get(role_id, [])

        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus(), org_engine=PolicyOrg())
            task = Task(
                id="reply-policy-task",
                session_id="sess-reply-policy",
                parent_session_id="root-session",
                title="Reply policy task",
                project_id="proj1",
                assigned_to="cto",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "delegation_seat_id": "seat::team::cto::cto",
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                },
            )
            await store.save_task(task)
            original = await communication.send_dm(
                AgentMessage(
                    from_agent="env_engineer",
                    to_agents=["cto"],
                    subject="Manifest complete",
                    body="Environment manifest is ready.",
                    task_id=task.id,
                    context_ref=task.id,
                ),
                task=task,
            )

            with self.assertRaises(ValueError):
                await communication.send_dm(
                    AgentMessage(
                        from_agent="cto",
                        to_agents=["env_engineer"],
                        subject="Direct DM should fail",
                        body="This is not a reply.",
                        task_id=task.id,
                        context_ref=task.id,
                    ),
                    task=task,
                )

            reply = await communication.reply_to_message(
                original_msg_id=original.msg_id,
                from_agent="cto",
                body="Thanks, acknowledged.",
                task_id=task.id,
            )

            self.assertEqual(reply.to_agents, ["env_engineer"])
            stored = await store.get_message(original.msg_id)
            assert stored is not None
            self.assertEqual(stored.status, MessageStatus.REPLIED)
            new_dir = Path(tmpdir) / ".opc-comms" / "proj1" / "root-session" / "inbox" / "cto" / "new"
            seen_dir = Path(tmpdir) / ".opc-comms" / "proj1" / "root-session" / "inbox" / "cto" / "seen"
            self.assertFalse(any(path.suffix == ".md" for path in new_dir.iterdir()))
            self.assertTrue(any(path.suffix == ".md" for path in seen_dir.iterdir()))
            await store.close()

    async def test_lifecycle_ack_by_refs_archives_seat_mismatched_protocol_message(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            task = Task(
                id="review-task",
                session_id="sess-review",
                parent_session_id="root-session",
                title="Review task",
                project_id="proj1",
                assigned_to="cto",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "delegation_seat_id": "seat::team::cto::cto",
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                },
            )
            await store.save_task(task)
            message = await communication.send_dm(
                AgentMessage(
                    from_agent="engineer",
                    to_agents=["cto"],
                    subject="Review needed: Report #1",
                    body="Please review the report.",
                    task_id=task.id,
                    context_ref=task.id,
                    transport_kind=CommsTransportKind.SYSTEM,
                    semantic_type=CommsSemanticType.APPROVAL_REQUEST,
                    metadata={
                        "message_class": "protocol",
                        "protocol_type": "approval_request",
                        "actionable": True,
                        "target_seat_id": "seat::team::ceo::cto",
                        "work_item_id": "report::wi-child::v1",
                        "target_work_item_id": "report::wi-child::v1",
                        "parent_work_item_id": "wi-child",
                    },
                ),
                task=task,
            )

            normal_status = await communication.inbox(
                agent_id="cto",
                task=task,
                action="status",
            )
            self.assertEqual(normal_status["unread_count"], 0)

            acked = await communication._collaboration_service().ack_inbox_messages_by_refs(
                CollaborationContext.from_task(task, role_id="cto"),
                agent_id="cto",
                work_item_ids=["wi-child", "report::wi-child::v1"],
                semantic_types=["approval_request"],
                task=task,
            )

            self.assertEqual(acked["acked"], [message.msg_id])
            new_dir = Path(tmpdir) / ".opc-comms" / "proj1" / "root-session" / "inbox" / "cto" / "new"
            seen_dir = Path(tmpdir) / ".opc-comms" / "proj1" / "root-session" / "inbox" / "cto" / "seen"
            self.assertFalse(any(path.suffix == ".md" for path in new_dir.iterdir()))
            self.assertTrue(any(path.suffix == ".md" for path in seen_dir.iterdir()))
            stored = await store.get_message(message.msg_id)
            assert stored is not None
            self.assertEqual(stored.status, MessageStatus.READ)
            await store.close()

    async def test_review_lifecycle_cleanup_archives_matching_report_request(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            review_task = Task(
                id="review-cleanup-task",
                session_id="sess-review-cleanup",
                parent_session_id="root-session",
                title="Review #1",
                project_id="proj1",
                assigned_to="cto",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "delegation_seat_id": "seat::team::cto::cto",
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                    "review_target_work_item_id": "wi-child",
                    "report_attempt": 1,
                    "review_attempt": 1,
                },
            )
            await store.save_task(review_task)
            message = await communication.send_dm(
                AgentMessage(
                    from_agent="engineer",
                    to_agents=["cto"],
                    subject="Review needed: report::wi-child::v1",
                    body="Please review report attempt 1.",
                    task_id=review_task.id,
                    context_ref=review_task.id,
                    transport_kind=CommsTransportKind.SYSTEM,
                    semantic_type=CommsSemanticType.APPROVAL_REQUEST,
                    metadata={
                        "message_class": "protocol",
                        "protocol_type": "approval_request",
                        "actionable": True,
                        "work_item_id": "report::wi-child::v1",
                        "target_work_item_id": "report::wi-child::v1",
                        "parent_work_item_id": "wi-child",
                    },
                ),
                task=review_task,
            )
            child_item = DelegationWorkItem(
                work_item_id="wi-child",
                run_id="run-review-cleanup",
                cell_id="cell-review-cleanup",
                role_id="engineer",
                seat_id="seat::team::engineer::engineer",
                manager_role_id="cto",
                manager_seat_id="seat::team::cto::cto",
                title="Child work",
                kind="execute",
                phase=Phase.AWAITING_MANAGER_REVIEW,
                metadata={"report_attempt_count": 1, "review_attempt_count": 1},
            )
            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=communication,
                approval_engine=SimpleNamespace(),
                memory=None,
                llm=None,
                execute_task=AsyncMock(),
                save_task=store.save_task,
                store=store,
            )

            await executor._ack_lifecycle_inbox_for_review(
                review_task=review_task,
                review_work_item_id="review::wi-child::v1",
                target_work_item_id="wi-child",
                child_item=child_item,
            )

            new_dir = Path(tmpdir) / ".opc-comms" / "proj1" / "root-session" / "inbox" / "cto" / "new"
            seen_dir = Path(tmpdir) / ".opc-comms" / "proj1" / "root-session" / "inbox" / "cto" / "seen"
            self.assertFalse(any(path.suffix == ".md" for path in new_dir.iterdir()))
            self.assertTrue(any(path.suffix == ".md" for path in seen_dir.iterdir()))
            stored = await store.get_message(message.msg_id)
            assert stored is not None
            self.assertEqual(stored.status, MessageStatus.READ)
            await store.close()

    async def test_review_lifecycle_cleanup_archives_descendant_review_messages_for_reviewer_only(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            review_task = Task(
                id="ceo-review-turn-task",
                session_id="sess-ceo-review-turn",
                parent_session_id="root-session",
                title="Review #1: Review Turn: cto",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "delegation_run_id": "run-descendant-cleanup",
                    "delegation_seat_id": "seat::team::ceo::ceo",
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                    "review_target_work_item_id": "wi-cto-review-turn",
                    "report_attempt": 1,
                    "review_attempt": 1,
                },
            )
            await store.save_task(review_task)
            parent_item = DelegationWorkItem(
                work_item_id="wi-cto-aggregate",
                run_id="run-descendant-cleanup",
                cell_id="cell-descendant-cleanup",
                role_id="cto",
                seat_id="seat::team::cto::cto",
                manager_role_id="ceo",
                manager_seat_id="seat::team::ceo::ceo",
                title="CTO aggregate",
                kind="execute",
                phase=Phase.APPROVED,
            )
            manager_review_turn = DelegationWorkItem(
                work_item_id="wi-cto-review-turn",
                run_id="run-descendant-cleanup",
                cell_id="cell-descendant-cleanup",
                role_id="cto",
                seat_id="seat::team::cto::cto",
                parent_work_item_id="wi-cto-aggregate",
                manager_role_id="ceo",
                manager_seat_id="seat::team::ceo::ceo",
                title="Review Turn: cto",
                kind="review",
                phase=Phase.APPROVED,
            )
            leaf_item = DelegationWorkItem(
                work_item_id="wi-leaf",
                run_id="run-descendant-cleanup",
                cell_id="cell-descendant-cleanup",
                role_id="senior_engineer",
                seat_id="seat::team::senior_engineer::senior_engineer",
                parent_work_item_id="wi-cto-aggregate",
                manager_role_id="cto",
                manager_seat_id="seat::team::cto::cto",
                title="Leaf implementation",
                kind="execute",
                phase=Phase.APPROVED,
                metadata={"report_attempt_count": 1, "review_attempt_count": 1},
            )
            leaf_review_item = DelegationWorkItem(
                work_item_id="review::wi-leaf::v1",
                run_id="run-descendant-cleanup",
                cell_id="cell-descendant-cleanup",
                role_id="cto",
                seat_id="seat::team::cto::cto",
                parent_work_item_id="wi-leaf",
                manager_role_id="cto",
                manager_seat_id="seat::team::cto::cto",
                title="Review #1: Leaf implementation",
                kind="review",
                phase=Phase.APPROVED,
            )
            for item in (parent_item, manager_review_turn, leaf_item, leaf_review_item):
                await store.save_delegation_work_item(item)

            ceo_message = await communication.send_dm(
                AgentMessage(
                    from_agent="cto",
                    to_agents=["ceo"],
                    subject="Review needed: Review #1: Leaf implementation",
                    body="Please review the descendant review card.",
                    task_id=review_task.id,
                    context_ref=review_task.id,
                    transport_kind=CommsTransportKind.SYSTEM,
                    semantic_type=CommsSemanticType.APPROVAL_REQUEST,
                    metadata={
                        "message_class": "protocol",
                        "protocol_type": "approval_request",
                        "actionable": True,
                        "work_item_id": "review::wi-leaf::v1",
                        "target_work_item_id": "review::wi-leaf::v1",
                        "parent_work_item_id": "wi-leaf",
                    },
                ),
                task=review_task,
            )
            owner_message = await communication.send_dm(
                AgentMessage(
                    from_agent="cto",
                    to_agents=["owner"],
                    subject="Review needed: Review #1: Leaf implementation",
                    body="Owner should not be cleaned by CEO lifecycle cleanup.",
                    task_id=review_task.id,
                    context_ref=review_task.id,
                    transport_kind=CommsTransportKind.SYSTEM,
                    semantic_type=CommsSemanticType.APPROVAL_REQUEST,
                    metadata={
                        "message_class": "protocol",
                        "protocol_type": "approval_request",
                        "actionable": True,
                        "work_item_id": "review::wi-leaf::v1",
                        "target_work_item_id": "review::wi-leaf::v1",
                        "parent_work_item_id": "wi-leaf",
                    },
                ),
                task=review_task,
            )
            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=communication,
                approval_engine=SimpleNamespace(),
                memory=None,
                llm=None,
                execute_task=AsyncMock(),
                save_task=store.save_task,
                store=store,
            )

            await executor._ack_lifecycle_inbox_for_review(
                review_task=review_task,
                review_work_item_id="review::wi-cto-review-turn::v1",
                target_work_item_id="wi-cto-review-turn",
                child_item=manager_review_turn,
            )

            ceo_new = Path(tmpdir) / ".opc-comms" / "proj1" / "root-session" / "inbox" / "ceo" / "new"
            ceo_seen = Path(tmpdir) / ".opc-comms" / "proj1" / "root-session" / "inbox" / "ceo" / "seen"
            owner_new = Path(tmpdir) / ".opc-comms" / "proj1" / "root-session" / "inbox" / "owner" / "new"
            self.assertFalse(any(path.suffix == ".md" for path in ceo_new.iterdir()))
            self.assertTrue(any(path.suffix == ".md" for path in ceo_seen.iterdir()))
            self.assertTrue(any(path.suffix == ".md" for path in owner_new.iterdir()))
            stored_ceo = await store.get_message(ceo_message.msg_id)
            stored_owner = await store.get_message(owner_message.msg_id)
            assert stored_ceo is not None
            assert stored_owner is not None
            self.assertEqual(stored_ceo.status, MessageStatus.READ)
            self.assertEqual(stored_owner.status, MessageStatus.DELIVERED)
            await store.close()

    async def test_manager_notification_targets_recipient_review_owner_seat(self) -> None:
        class PolicyOrg:
            def get_agent(self, role_id: str):
                return {
                    "cto": SimpleNamespace(role_id="cto", reports_to="ceo"),
                    "ceo": SimpleNamespace(role_id="ceo", reports_to="owner"),
                }.get(role_id)

        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus(), org_engine=PolicyOrg())
            task = Task(
                id="manager-notice-task",
                session_id="sess-manager-notice",
                parent_session_id="root-session",
                title="Review #1",
                project_id="proj1",
                assigned_to="cto",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "delegation_seat_id": "seat::team::cto::cto",
                    "manager_role_id": "cto",
                    "manager_seat_id": "seat::team::cto::cto",
                    "review_owner_role_id": "ceo",
                    "review_owner_seat_id": "seat::team::ceo::ceo",
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                },
            )
            await store.save_task(task)

            message = await communication.send_manager_notification(
                from_agent="cto",
                task=task,
                semantic_type=CommsSemanticType.APPROVAL_REQUEST,
                subject="Review needed: Review #1",
                body="Review result needs CEO attention.",
                metadata={"work_item_id": "review::wi-child::v1"},
                reply_needed=True,
            )

            assert message is not None
            header = file_comms.read_header(Path(message.metadata["comms_path"]))
            assert header is not None
            self.assertEqual(header.to_role, "ceo")
            self.assertEqual(header.raw_frontmatter["target_seat_id"], "seat::team::ceo::ceo")
            await store.close()

    async def test_inbox_completion_gate_blocks_actionable_unread(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            task = Task(
                id="gate-inbox-task",
                session_id="sess-gate-inbox",
                parent_session_id="root-session",
                title="Gate inbox task",
                project_id="proj1",
                assigned_to="executor",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                },
            )
            await store.save_task(task)
            message = await communication.send_dm(
                AgentMessage(
                    from_agent="reviewer",
                    to_agents=["executor"],
                    subject="Resolve before finishing",
                    body="Please acknowledge this before completion.",
                    task_id=task.id,
                    context_ref=task.id,
                ),
                task=task,
            )
            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=communication,
                approval_engine=SimpleNamespace(),
                memory=None,
                llm=None,
                execute_task=AsyncMock(),
                save_task=store.save_task,
                store=store,
            )

            blocked = await executor._block_completion_for_unread_inbox(task)
            self.assertTrue(blocked)
            self.assertEqual(task.status, TaskStatus.PENDING)
            self.assertEqual(task.metadata["inbox_gate_pending_message_ids"], [message.msg_id])
            self.assertIn("inbox_completion_gate", task.context_snapshot)
            await store.close()

    async def test_inbox_completion_gate_allows_notification_unread(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            task = Task(
                id="gate-notification-task",
                session_id="sess-gate-notification",
                parent_session_id="root-session",
                title="Gate notification task",
                project_id="proj1",
                assigned_to="executor",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": ExecutionMode.COMPANY_MODE.value,
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                },
            )
            await store.save_task(task)
            await communication.send_dm(
                AgentMessage(
                    from_agent="reviewer",
                    to_agents=["executor"],
                    subject="FYI only",
                    body="Status update, no action needed.",
                    task_id=task.id,
                    context_ref=task.id,
                    metadata={
                        "message_class": "notification",
                        "notification_kind": "status_digest",
                        "actionable": False,
                    },
                ),
                task=task,
            )
            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=communication,
                approval_engine=SimpleNamespace(),
                memory=None,
                llm=None,
                execute_task=AsyncMock(),
                save_task=store.save_task,
                store=store,
            )

            blocked = await executor._block_completion_for_unread_inbox(task)
            self.assertFalse(blocked)
            self.assertEqual(task.status, TaskStatus.RUNNING)
            self.assertNotIn("inbox_gate_pending_message_ids", task.metadata)
            self.assertNotIn("inbox_completion_gate", task.context_snapshot)
            await store.close()

    async def test_team_memory_path_is_session_scoped(self) -> None:
        with _workspace_tempdir() as tmpdir:
            layout_a = file_comms.resolve_layout(str(tmpdir), "proj1", "sess-a")
            layout_b = file_comms.resolve_layout(str(tmpdir), "proj1", "sess-b")

            expected_a = Path(tmpdir) / ".opc-comms" / "proj1" / "sess-a" / "_shared" / "team_memory" / "TEAM_MEMORY.md"
            expected_b = Path(tmpdir) / ".opc-comms" / "proj1" / "sess-b" / "_shared" / "team_memory" / "TEAM_MEMORY.md"
            self.assertEqual(layout_a.team_memory_path, expected_a)
            self.assertEqual(layout_b.team_memory_path, expected_b)

    async def test_company_scratchpad_is_session_scoped(self) -> None:
        with _workspace_tempdir() as tmpdir:
            layout_a = file_comms.resolve_layout(str(tmpdir), "proj1", "sess-a")
            layout_b = file_comms.resolve_layout(str(tmpdir), "proj1", "sess-b")
            file_comms.ensure_layout(layout_a, ["executor"])
            file_comms.ensure_layout(layout_b, ["executor"])
            layout_a.scratchpad_path.write_text("session-a scratchpad", encoding="utf-8")
            layout_b.scratchpad_path.write_text("session-b scratchpad", encoding="utf-8")
            global_scratchpad = Path(tmpdir) / ".opc-comms" / "_shared" / "scratchpad.md"
            global_scratchpad.parent.mkdir(parents=True, exist_ok=True)
            global_scratchpad.write_text("global scratchpad", encoding="utf-8")

            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=DummyRuntimeCommunication(),
                approval_engine=SimpleNamespace(),
                memory=None,
                llm=None,
                execute_task=AsyncMock(),
                save_task=AsyncMock(),
            )
            task = Task(
                id="execution-work-item",
                title="Execution",
                session_id="sess-b",
                parent_session_id="sess-b",
                assigned_to="executor",
                status=TaskStatus.PENDING,
                project_id="proj1",
                metadata={
                    "work_item_projection_id": "execution",
                    "work_item_role_id": "executor",
                    "work_item_turn_type": "execute",
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                },
            )

            executor._inject_scratchpad_into_context(task)

            self.assertEqual(task.context_snapshot["team_scratchpad"], "session-b scratchpad")
            self.assertNotIn("global scratchpad", task.context_snapshot["team_scratchpad"])

    async def test_company_runtime_keeps_cold_role_mail_queued_until_first_assignment(self) -> None:
        with _workspace_tempdir() as tmpdir:
            layout = file_comms.resolve_layout(str(tmpdir), "proj1", "default")
            file_comms.ensure_layout(layout, ["executor", "reviewer"])
            layout.team_memory_path.write_text("# Team Memory\n\n- Shared constraint: rollback first.\n", encoding="utf-8")
            runtime = CompanyRuntime(
                org_engine=DummyOrgEngine(),
                communication=DummyRuntimeCommunication(
                    {
                        "executor": [
                            {
                                "msg_id": "msg-prestart",
                                "from_agent": "reviewer",
                                "subject": "Wait for rollback validation",
                                "body": "Do not start without the rollback checklist.",
                                "reply_needed": True,
                                "urgency": "blocking",
                            }
                        ]
                    }
                ),
            )
            task = Task(
                id="execution-work-item",
                title="Execution",
                assigned_to="executor",
                status=TaskStatus.PENDING,
                project_id="proj1",
                metadata={
                    "work_item_projection_id": "execution",
                    "work_item_role_id": "executor",
                    "work_item_turn_type": "execute",
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "employee_assignment": {"employee_id": "backend-architect", "role_id": "executor"},
                },
            )

            await runtime.bootstrap([task])
            session = runtime.session_for_task(task)
            self.assertEqual(session.status, "idle")
            self.assertEqual(session.pending_inbox, [])
            self.assertEqual(len(session.queued_inbox), 1)

            runtime.enqueue_runnable_tasks([task])
            claims = await runtime.claim_runnable_tasks([task])
            self.assertEqual(len(claims), 1)
            claimed_session, claimed_task = claims[0]
            self.assertEqual(claimed_session.status, "idle")
            self.assertTrue(claimed_task.context_snapshot["company_member_inbox"])
            assignment = dict(claimed_task.metadata.get("resident_assignment", {}) or {})
            self.assertEqual(assignment["manager_role_id"], "reviewer")
            self.assertEqual(assignment["pending_inbox"][0]["msg_id"], "msg-prestart")
            self.assertIn("rollback first", assignment["team_memory_digest"].lower())
            self.assertEqual(assignment["metadata"]["team_memory_path"], str(layout.team_memory_path))
            self.assertFalse(assignment["metadata"]["team_memory_truncated"])
            self.assertEqual(assignment["metadata"]["team_memory_omitted_chars"], 0)

    async def test_manager_board_item_uses_preview_with_optional_full_deliverable_summary(self) -> None:
        long_summary = "Delivered section\n" + ("details " * 400)
        item = DelegationWorkItem(
            work_item_id="wi-preview",
            run_id="run-preview",
            role_id="cto",
            seat_id="cto-seat",
            title="Build preview",
            phase=Phase.AWAITING_MANAGER_REVIEW,
            deliverable_summary=long_summary,
        )

        preview = _serialize_board_item(item)
        full = _serialize_board_item(item, include_full_summaries=True)

        self.assertTrue(preview["deliverable_summary_truncated"])
        self.assertEqual(preview["deliverable_summary_chars"], len(long_summary.strip()))
        self.assertIn("deliverable summary preview truncated", preview["deliverable_summary"])
        self.assertNotIn("deliverable_summary_full", preview)
        self.assertEqual(full["deliverable_summary_full"], long_summary.strip())

    async def test_company_runtime_separates_protocol_and_notification_backlogs(self) -> None:
        runtime = CompanyRuntime(
            org_engine=DummyOrgEngine(),
            communication=DummyRuntimeCommunication(
                {
                    "executor": [
                        {
                            "msg_id": "msg-chat",
                            "from_agent": "reviewer",
                            "subject": "Need an implementation update",
                            "body": "Share the latest status.",
                            "reply_needed": True,
                            "urgency": "normal",
                        },
                        {
                            "msg_id": "msg-protocol",
                            "from_agent": "reviewer",
                            "subject": "Approve plan",
                            "body": "Please confirm the rollout plan.",
                            "transport_kind": "system",
                            "semantic_type": "approval_request",
                            "metadata": {
                                "message_class": "protocol",
                                "protocol_type": "approval_request",
                            },
                        },
                        {
                            "msg_id": "msg-note",
                            "from_agent": "reviewer",
                            "subject": "Worker idle",
                            "body": "Executor is idle.",
                            "metadata": {
                                "message_class": "notification",
                                "notification_kind": "idle",
                                "actionable": False,
                            },
                        },
                    ]
                }
            ),
        )
        task = Task(
            id="execution-work-item",
            title="Execution",
            assigned_to="executor",
            status=TaskStatus.PENDING,
            project_id="proj1",
            metadata={
                "work_item_projection_id": "execution",
                "work_item_role_id": "executor",
                "employee_assignment": {"employee_id": "backend-architect", "role_id": "executor"},
            },
        )
        set_linked_work_item_id(task, "wi-1")

        await runtime.bootstrap([task])
        await runtime.refresh_inbox_state([task])
        session = runtime.session_for_task(task)

        self.assertEqual(session.actionable_inbox_count, 1)
        self.assertEqual(session.protocol_backlog_count, 1)
        self.assertEqual(session.notification_backlog_count, 1)
        self.assertEqual(session.actionable_chat[0]["msg_id"], "msg-chat")
        self.assertEqual(session.protocol_backlog[0]["protocol_type"], "approval_request")
        self.assertEqual(session.latest_notification["notification_kind"], "idle")

        runtime.prepare_task_for_session(session, task)
        self.assertEqual(task.context_snapshot["company_member_inbox"][0]["msg_id"], "msg-chat")
        self.assertEqual(task.context_snapshot["company_member_protocol_backlog"][0]["msg_id"], "msg-protocol")
        self.assertEqual(task.context_snapshot["latest_company_notification"]["msg_id"], "msg-note")
        self.assertEqual(task.context_snapshot["resident_status"], "idle")

    async def test_company_runtime_manager_digest_stays_structured(self) -> None:
        runtime = CompanyRuntime(
            org_engine=DummyOrgEngine(),
            communication=DummyRuntimeCommunication(
                {
                    "executor": [
                        {
                            "msg_id": "msg-block",
                            "from_agent": "reviewer",
                            "subject": "Need approval",
                            "body": "Approve deployment plan.",
                            "reply_needed": True,
                            "metadata": {
                                "message_class": "protocol",
                                "protocol_type": "decision_request",
                            },
                        },
                        {
                            "msg_id": "msg-delivery",
                            "from_agent": "reviewer",
                            "subject": "Delivery candidate",
                            "body": "Patch is ready for release.",
                            "metadata": {
                                "message_class": "notification",
                                "notification_kind": "delivery_candidate",
                                "actionable": False,
                            },
                        },
                    ]
                }
            ),
        )
        task = Task(
            id="execution-work-item",
            title="Execution",
            assigned_to="executor",
            status=TaskStatus.PENDING,
            project_id="proj1",
            metadata={
                "work_item_projection_id": "execution",
                "work_item_role_id": "executor",
                "employee_assignment": {"employee_id": "backend-architect", "role_id": "executor"},
            },
        )
        set_linked_work_item_id(task, "wi-1")

        await runtime.bootstrap([task])
        await runtime.refresh_inbox_state([task])
        session = runtime.session_for_task(task)
        digest = dict(session.manager_digest)

        self.assertEqual(digest["resident_status"], "idle")
        self.assertEqual(digest["current_work_item"]["work_item_id"], "wi-1")
        self.assertEqual(digest["pending_decisions"][0]["protocol_type"], "decision_request")
        self.assertIn("Patch is ready", digest["last_deliverable_summary"])
        self.assertEqual(session.inbox_state["manager_digest"]["pending_decisions"][0]["protocol_type"], "decision_request")

    async def test_company_runtime_manager_digest_includes_manager_board_summary(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="parent-item",
                    run_id="run-1",
                    cell_id="team::ceo",
                    team_id="team::ceo",
                    role_id="ceo",
                    seat_id="seat::team::ceo::ceo",
                    title="CTO delegation",
                    summary="Manage children.",
                    kind="delegate",
                    projection_id="ceo__delegate",
                    phase=Phase.RUNNING,
                    metadata={"dependency_work_item_ids": []},
                )
            )
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="child-item",
                    run_id="run-1",
                    cell_id="team::ceo",
                    team_id="team::ceo",
                    role_id="executor",
                    seat_id="seat::team::ceo::executor",
                    parent_work_item_id="parent-item",
                    title="Execute child",
                    summary="Implement the approved work.",
                    kind="execute",
                    projection_id="executor__execute",
                    phase=Phase.QUEUED,
                    manager_role_id="ceo",
                    manager_seat_id="seat::team::ceo::ceo",
                    metadata={
                        "dependency_work_item_ids": [],
                        "release_policy": "manager_ack",
                        "manager_release_state": "queued",
                    },
                )
            )
            runtime = CompanyRuntime(
                org_engine=DummyOrgEngine(),
                communication=DummyRuntimeCommunication(),
                store=store,
            )
            task = Task(
                id="parent-task",
                title="Manager work item",
                assigned_to="reviewer",
                status=TaskStatus.PENDING,
                project_id="proj1",
                metadata={
                    "work_item_projection_id": "execution",
                    "work_item_role_id": "reviewer",
                    "runtime_model": "multi_team_org",
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::ceo::ceo",
                    "manager_seat_id": "seat::team::owner::owner",
                    "managed_team_id": "team::ceo",
                    "direct_report_role_ids": ["executor"],
                    "direct_report_seat_ids": ["seat::team::ceo::executor"],
                    "employee_assignment": {"employee_id": "backend-architect", "role_id": "reviewer"},
                },
            )
            set_linked_work_item_id(task, "parent-item")

            await runtime.bootstrap([task])
            await runtime.refresh_inbox_state([task])
            session = runtime.session_for_task(task)
            board_summary = dict(session.manager_digest.get("manager_board_summary", {}) or {})

            self.assertEqual(board_summary["total_children"], 1)
            self.assertEqual(board_summary["phase_counts"]["queued"], 1)
            self.assertEqual(board_summary["releasable_work_item_ids"], ["child-item"])
            self.assertEqual(session.current_turn_mode, "monitor_children")
            self.assertEqual(session.manager_digest["current_turn_mode"], "monitor_children")

            await store.close()

    async def test_reactivate_for_unread_mail_only_reopens_changed_actionable_sets(self) -> None:
        with _workspace_tempdir() as tmpdir:
            layout = file_comms.resolve_layout(str(tmpdir), "proj1", "root-session")
            file_comms.ensure_layout(layout, ["executor", "reviewer"])
            file_comms.send_message(
                layout,
                from_role="reviewer",
                to_role="executor",
                subject="Need follow-up",
                body="Please revisit the rollback path.",
                blocking=True,
            )
            saved: list[str] = []

            async def _save_task(task: Task) -> None:
                saved.append(task.status.value if hasattr(task.status, "value") else str(task.status))

            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=DummyRuntimeCommunication(),
                approval_engine=AsyncMock(),
                memory=None,
                execute_task=AsyncMock(return_value=TaskResult(status=TaskStatus.DONE, content="ok")),
                save_task=_save_task,
            )
            task = Task(
                id="done-work-item",
                session_id="work-item-session",
                parent_session_id="root-session",
                title="Done Work Item",
                assigned_to="executor",
                status=TaskStatus.DONE,
                project_id="proj1",
                metadata={
                    "work_item_role_id": "executor",
                    "target_output_dir": str(tmpdir),
                },
            )

            reopened = await executor._reactivate_for_unread_mail(task)
            self.assertTrue(reopened)
            self.assertEqual(task.status, TaskStatus.PENDING)
            self.assertIn("pending", saved)

            task.status = TaskStatus.DONE
            reopened_again = await executor._reactivate_for_unread_mail(task)
            self.assertFalse(reopened_again)
            self.assertIn("skipping another auto-reactivation", task.metadata["progress_log"][-1].lower())

    async def test_reactivate_for_unread_mail_ignores_notification_only_unread(self) -> None:
        with _workspace_tempdir() as tmpdir:
            layout = file_comms.resolve_layout(str(tmpdir), "proj1", "root-session")
            file_comms.ensure_layout(layout, ["executor", "reviewer"])
            file_comms.send_message(
                layout,
                from_role="reviewer",
                to_role="executor",
                subject="Worker idle",
                body="Executor is idle.",
                extra_frontmatter={
                    "message_class": "notification",
                    "notification_kind": "idle",
                    "actionable": False,
                },
            )

            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=DummyRuntimeCommunication(),
                approval_engine=AsyncMock(),
                memory=None,
                execute_task=AsyncMock(return_value=TaskResult(status=TaskStatus.DONE, content="ok")),
                save_task=AsyncMock(),
            )
            task = Task(
                id="done-work-item",
                session_id="work-item-session",
                parent_session_id="root-session",
                title="Done Work Item",
                assigned_to="executor",
                status=TaskStatus.DONE,
                project_id="proj1",
                metadata={
                    "work_item_role_id": "executor",
                    "target_output_dir": str(tmpdir),
                },
            )

            reopened = await executor._reactivate_for_unread_mail(task)
            self.assertFalse(reopened)
            self.assertEqual(task.status, TaskStatus.DONE)

    async def test_reactivate_for_unread_mail_hard_stops_at_depth_limit(self) -> None:
        with _workspace_tempdir() as tmpdir:
            layout = file_comms.resolve_layout(str(tmpdir), "proj1", "root-session")
            file_comms.ensure_layout(layout, ["executor", "reviewer"])
            file_comms.send_message(
                layout,
                from_role="reviewer",
                to_role="executor",
                subject="Need follow-up",
                body="Please revisit the rollback path.",
                blocking=True,
            )
            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=DummyRuntimeCommunication(),
                approval_engine=AsyncMock(),
                memory=None,
                execute_task=AsyncMock(return_value=TaskResult(status=TaskStatus.DONE, content="ok")),
                save_task=AsyncMock(),
            )
            task = Task(
                id="done-work-item",
                session_id="work-item-session",
                parent_session_id="root-session",
                title="Done Work Item",
                assigned_to="executor",
                status=TaskStatus.DONE,
                project_id="proj1",
                metadata={
                    "work_item_role_id": "executor",
                    "target_output_dir": str(tmpdir),
                    "comms_reactivation_depth": executor.COMMS_REACTIVATION_DEPTH_LIMIT,
                },
            )
            reopened = await executor._reactivate_for_unread_mail(task)
            self.assertFalse(reopened)
            self.assertEqual(task.status, TaskStatus.DONE)
            self.assertIn(
                "hard limit",
                task.metadata["progress_log"][-1].lower(),
            )

    async def test_reactivate_for_unread_mail_detects_cross_role_ping_pong(self) -> None:
        with _workspace_tempdir() as tmpdir:
            layout = file_comms.resolve_layout(str(tmpdir), "proj1", "root-session")
            file_comms.ensure_layout(layout, ["executor", "reviewer"])
            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=DummyRuntimeCommunication(),
                approval_engine=AsyncMock(),
                memory=None,
                execute_task=AsyncMock(return_value=TaskResult(status=TaskStatus.DONE, content="ok")),
                save_task=AsyncMock(),
            )
            task = Task(
                id="done-work-item",
                session_id="work-item-session",
                parent_session_id="root-session",
                title="Done Work Item",
                assigned_to="executor",
                status=TaskStatus.DONE,
                project_id="proj1",
                metadata={
                    "work_item_role_id": "executor",
                    "target_output_dir": str(tmpdir),
                },
            )
            # Pre-seed the cross-role history with COMMS_CROSS_ROLE_REPEAT_THRESHOLD
            # entries from `reviewer` on the same subject hash so the next
            # incoming message from `reviewer` (same subject) is rejected
            # as a ping-pong loop.
            subject = "Need follow-up"
            subject_hash = executor._subject_hash(subject)
            task.metadata["comms_cross_role_history"] = [
                {"from_role": "reviewer", "subject_hash": subject_hash, "msg_id": f"prev-{i}", "depth": i + 1}
                for i in range(executor.COMMS_CROSS_ROLE_REPEAT_THRESHOLD)
            ]
            file_comms.send_message(
                layout,
                from_role="reviewer",
                to_role="executor",
                subject=subject,
                body="Same ask once more.",
                blocking=True,
            )
            reopened = await executor._reactivate_for_unread_mail(task)
            self.assertFalse(reopened)
            self.assertEqual(task.status, TaskStatus.DONE)
            self.assertIn(
                "ping-pong",
                task.metadata["progress_log"][-1].lower(),
            )

    async def test_comms_reactivation_sweeper_reopens_done_task_with_actionable_mail(self) -> None:
        from opc.layer2_organization.reactivation_sweeper import CommsReactivationSweeper

        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            layout = file_comms.resolve_layout(str(tmpdir), "proj1", "root-session")
            file_comms.ensure_layout(layout, ["executor", "reviewer"])
            file_comms.send_message(
                layout,
                from_role="reviewer",
                to_role="executor",
                subject="Blocking ask",
                body="Need a decision on X.",
                blocking=True,
            )
            task = Task(
                id="done-work-item",
                session_id="work-item-session",
                parent_session_id="root-session",
                title="Done Work Item",
                assigned_to="executor",
                status=TaskStatus.DONE,
                project_id="proj1",
                metadata={
                    "work_item_role_id": "executor",
                    "target_output_dir": str(tmpdir),
                },
            )
            await store.save_task(task)
            executor = CompanyWorkItemExecutor(
                org_engine=DummyOrgEngine(),
                communication=DummyRuntimeCommunication(),
                approval_engine=AsyncMock(),
                memory=None,
                execute_task=AsyncMock(return_value=TaskResult(status=TaskStatus.DONE, content="ok")),
                save_task=store.save_task,
                store=store,
            )
            sweeper = CommsReactivationSweeper(
                store=store,
                project_id_getter=lambda: "proj1",
                reactivate_fn=executor._reactivate_for_unread_mail,
                interval_sec=10.0,
            )
            # Exercise a single tick directly (no asyncio sleep).
            await sweeper._tick()
            refreshed = await store.get_task(task.id)
            assert refreshed is not None
            self.assertEqual(refreshed.status, TaskStatus.PENDING)
            self.assertGreaterEqual(
                int(refreshed.metadata.get("comms_reactivation_depth", 0) or 0),
                1,
            )
            await store.close()

    async def test_send_manager_notification_routes_to_direct_manager(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus(), org_engine=DummyOrgEngine())
            task = Task(
                id="work-item-result-task",
                session_id="sess-work-item-result",
                parent_session_id="root-session",
                title="Execution",
                project_id="proj1",
                assigned_to="executor",
                status=TaskStatus.RUNNING,
                metadata={
                    "work_item_projection_id": "execution",
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                },
            )
            await store.save_task(task)

            message = await communication.send_manager_notification(
                from_agent="executor",
                task=task,
                semantic_type=CommsSemanticType.WORK_ITEM_RESULT,
                subject="Execution finished",
                body="The implementation finished cleanly.",
            )

            assert message is not None
            self.assertEqual(message.to_agents, ["reviewer"])
            self.assertEqual(message.semantic_type, CommsSemanticType.WORK_ITEM_RESULT)
            header = file_comms.read_header(Path(message.metadata["comms_path"]))
            assert header is not None
            self.assertEqual(header.raw_frontmatter["semantic_type"], CommsSemanticType.WORK_ITEM_RESULT.value)
            await store.close()

    async def test_company_runtime_complete_claim_reports_blocked_status_and_clears_assignment(self) -> None:
        with _workspace_tempdir() as tmpdir:
            communication = DummyManagerNotificationCommunication()
            runtime = CompanyRuntime(
                org_engine=DummyOrgEngine(),
                communication=communication,
            )
            task = Task(
                id="blocked-work-item",
                title="Blocked Work Item",
                assigned_to="executor",
                status=TaskStatus.PENDING,
                project_id="proj1",
                metadata={
                    "work_item_projection_id": "execution",
                    "work_item_turn_type": "execute",
                    "work_item_role_id": "executor",
                    "workspace_root": str(tmpdir),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "employee_assignment": {"employee_id": "backend-architect", "role_id": "executor"},
                },
            )

            await runtime.bootstrap([task])
            session = runtime.session_for_task(task)
            runtime.prepare_task_for_session(session, task)
            task.status = TaskStatus.AWAITING_PEER
            task.metadata["peer_wait"] = {"waiting_on_agents": ["reviewer"]}

            await runtime.complete_claim(
                session,
                task,
                result=TaskResult(status=TaskStatus.AWAITING_PEER, content="Waiting for reviewer."),
            )

            self.assertEqual(session.status, "blocked")
            self.assertEqual(session.current_assignment, {})
            self.assertEqual(communication.calls[-1]["semantic_type"], CommsSemanticType.BLOCKED_ON_DECISION)
            self.assertTrue(communication.calls[-1]["reply_needed"])

    async def test_send_dm_failure_records_session_notice_and_reopens_done_task(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            task = Task(
                id="content-task",
                session_id="sess-content",
                parent_session_id="root-session",
                title="Content Execution",
                project_id="proj1",
                assigned_to="content_specialist",
                status=TaskStatus.DONE,
                metadata={
                    "work_item_projection_id": "content_execution",
                    "workspace_root": str(tmpdir),
                    "output_root": str(Path(tmpdir) / "deliverables"),
                    "target_output_dir": str(Path(tmpdir) / "deliverables"),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                },
            )
            await store.save_task(task)

            with patch("opc.layer2_organization.comms.send_message", side_effect=OSError("disk full")):
                with self.assertRaises(Exception):
                    await communication.send_dm(
                        AgentMessage(
                            from_agent="content_specialist",
                            to_agents=["acquisition_specialist"],
                            subject="Need clip clarification",
                            body="Please confirm Act 1 mapping.",
                            task_id=task.id,
                            context_ref=task.id,
                        ),
                        task=task,
                    )

            refreshed = await store.get_task(task.id)
            assert refreshed is not None
            self.assertEqual(refreshed.status, TaskStatus.PENDING)
            latest_failure = dict(refreshed.context_snapshot.get("latest_comms_failure", {}) or {})
            self.assertEqual(latest_failure.get("operation"), "send_dm")
            session_messages = await store.list_session_messages(task.session_id)
            self.assertTrue(session_messages)
            await store.close()

    async def test_dynamic_allowed_collaboration_targets_extend_soft_topology(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            org_engine = OrgEngine(OPCConfig(), Path(tmpdir))
            communication = CommunicationManager(store, EventBus(), org_engine=org_engine)
            task = Task(
                id="eng-task",
                title="Engineering Execution",
                project_id="proj1",
                assigned_to="senior_engineer",
                status=TaskStatus.RUNNING,
                metadata={
                    "execution_mode": "company_mode",
                    "work_item_projection_id": "engineering_execution",
                    "work_item_turn_type": "execute",
                    "ownership_contract": {
                        "allowed_collaboration_targets": ["content_specialist"],
                    },
                },
            )
            await store.save_task(task)

            message = await communication.send_dm(
                AgentMessage(
                    from_agent="senior_engineer",
                    to_agents=["content_specialist"],
                    subject="Cross-functional copy review",
                    body="Please review the API copy before we finalize.",
                    task_id=task.id,
                    context_ref=task.id,
                ),
                task=task,
            )

            inbox = await communication.read_inbox(
                agent_id="content_specialist",
                task_id=task.id,
                unread_only=True,
                limit=10,
                mark_read=False,
            )
            self.assertEqual(message.to_agents, ["content_specialist"])
            self.assertEqual(len(inbox), 1)
            self.assertEqual(inbox[0]["subject"], "Cross-functional copy review")
            await store.close()

    @unittest.skip("Filesystem handoff stack removed; see plans/task-cleanup-dead-comms.md"
    )
    async def test_context_assembler_includes_ownership_contract_and_pending_handoffs(self) -> None:
        class _MemoryStub:
            async def build_focused_memory_context(self, **_kwargs: object) -> str:
                return ""

            async def build_memory_context(self, **_kwargs: object) -> str:
                return ""

        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            task = Task(
                id="execution-work-item",
                session_id="sess-owner",
                project_id="proj1",
                assigned_to="executor",
                metadata={
                    "execution_mode": "company_mode",
                    "work_item_projection_id": "execution",
                    "work_item_projection_title": "Engineering Execution",
                    "work_item_turn_type": "execute",
                    "member_session_id": "member::proj1::executor::eng-1",
                    "ownership_contract": {
                        "summary": "Implement only the assigned API slice.",
                        "write_scope": str((Path(tmpdir) / "workspace").resolve()),
                        "expected_artifacts": ["Updated API implementation", "Verification evidence"],
                        "downstream_consumer": ["reviewer"],
                        "allowed_collaboration_targets": ["reviewer", "cto"],
                    },
                },
            )
            await store.save_task(task)
            await communication.send_handoff(
                task_id=task.id,
                from_agent="planner",
                to_agent="executor",
                subject="Plan handoff",
                body="Use the approved API contract.",
                handoff={
                    "handoff_id": "handoff-ctx",
                    "summary": "Approved API contract",
                    "source_projection_id": "planning",
                    "target_projection_id": "execution",
                },
                requires_ack=True,
            )
            assembler = ContextAssembler(_MemoryStub(), store=store, communication=communication)
            system_context = await assembler.build_system_context(task, role_id="executor")

            self.assertIn("## Topology", system_context)
            self.assertIn("Write scope:", system_context)
            self.assertIn("Pending Handoff Acknowledgements", system_context)
            self.assertIn("handoff-ctx", system_context)
            await store.close()

    async def test_context_assembler_renders_runtime_owned_mailbox_and_manager_board_summary(self) -> None:
        class _MemoryStub:
            async def build_focused_memory_context(self, **_kwargs: object) -> str:
                return ""

            async def build_memory_context(self, **_kwargs: object) -> str:
                return ""

        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            communication = CommunicationManager(store, EventBus())
            task = Task(
                id="manager-work-item",
                session_id="sess-manager",
                parent_session_id="root-session",
                title="CTO Delegation",
                project_id="proj1",
                assigned_to="cto",
                metadata={
                    "execution_mode": "company_mode",
                    "work_item_projection_id": "cto_delegate",
                    "work_item_projection_title": "CTO Delegation",
                    "work_item_turn_type": "plan",
                    "work_item_role_id": "cto",
                    "workspace_root": str(tmpdir),
                    "comms_workspace_root": str(tmpdir),
                    "comms_root": str(Path(tmpdir) / ".opc-comms"),
                    "member_session_state": {
                        "actionable_chat": [
                            {"from_agent": "ceo", "subject": "Delegate to engineering", "body": "Start routing the work."}
                        ],
                        "protocol_backlog": [],
                        "latest_notification": {},
                        "manager_board_summary": {
                            "total_children": 2,
                            "derived_parent_status": "blocked",
                            "releasable_work_item_ids": ["child-2"],
                            "blocked_reasons": ["Waiting for approval reply"],
                        },
                    },
                },
                context_snapshot={
                    "manager_board_summary": {
                        "total_children": 2,
                        "derived_parent_status": "blocked",
                        "releasable_work_item_ids": ["child-2"],
                        "blocked_reasons": ["Waiting for approval reply"],
                    }
                },
            )
            await store.save_task(task)

            assembler = ContextAssembler(_MemoryStub(), store=store, communication=communication)
            system_context = await assembler.build_system_context(task, role_id="cto")

            self.assertIn("runtime-owned", system_context)
            self.assertIn("Manager board summary:", system_context)
            self.assertIn("Releasable child items: child-2", system_context)
            self.assertNotIn("Call the `read_inbox`", system_context)
            await store.close()

    async def test_multi_team_manager_prompt_uses_current_assignment_and_dispatch_turn_mode(self) -> None:
        assembler = ContextAssembler(memory=SimpleNamespace())
        runtime_topology = {
            "teams": [
                {"team_id": "team::ceo", "lead_role_id": "ceo"},
            ],
            "seats": [
                {
                    "seat_id": "seat::team::ceo::ceo",
                    "team_id": "team::ceo",
                    "role_id": "ceo",
                    "managed_team_id": "team::ceo",
                    "metadata": {"role_name": "CEO"},
                },
                {
                    "seat_id": "seat::team::ceo::cto",
                    "team_id": "team::ceo",
                    "role_id": "cto",
                    "manager_role_id": "ceo",
                    "metadata": {"role_name": "CTO"},
                },
            ],
        }
        task = Task(
            id="ceo-intake",
            title="CEO Intake",
            description="Frame the mission and route it to the right executives.",
            project_id="proj1",
            assigned_to="ceo",
            metadata={
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "delegation_seat_id": "seat::team::ceo::ceo",
                "managed_team_id": "team::ceo",
                "runtime_topology": runtime_topology,
                "work_item_runtime_plan": {"turn_type": "execute", "summary": "This should stay hidden for manager turns."},
                "work_item_artifact_index": [{"label": "draft", "value": "docs/draft.md"}],
                "resident_assignment": {
                    "manager_role_id": "",
                    "assignment_id": "assign-root",
                    "dependency_snapshot": [],
                },
                "current_turn_mode": "dispatch_required",
                "member_session_state": {
                    "current_turn_mode": "dispatch_required",
                    "actionable_chat": [],
                    "protocol_backlog": [],
                    "latest_notification": {},
                    "manager_board_summary": {"total_children": 0},
                },
            },
        )

        brief = assembler.build_task_brief(task)
        member_state = assembler.build_member_session_context(task)

        self.assertIn("## Current Assignment", brief)
        self.assertNotIn("## Resident Assignment", brief)
        self.assertNotIn("## Work Item Runtime Plan", brief)
        self.assertNotIn("## Work Item Artifact Index", brief)
        self.assertNotIn("Current turn mode: dispatch_required", member_state)
        self.assertIn("Mailbox is runtime-owned", member_state)

    def test_company_default_employee_prompt_context_uses_role_specific_persona(self) -> None:
        with _workspace_tempdir() as tmpdir:
            org_engine = OrgEngine(OPCConfig(), Path(tmpdir))
            ceo_assignment = org_engine.resolve_employee_for_work_item("ceo", [], project_id="proj1")
            assert ceo_assignment is not None
            self.assertIn("Operate as a manager by default.", ceo_assignment["prompt_context"])
            self.assertNotIn("task mode", ceo_assignment["prompt_context"].lower())

            engineer_assignment = org_engine.resolve_employee_for_work_item("senior_engineer", [], project_id="proj1")
            assert engineer_assignment is not None
            self.assertIn("execution-focused teammate", engineer_assignment["prompt_context"])

    async def test_tool_approval_callback_blocks_out_of_scope_write_before_autonomy(self) -> None:
        engine = OPCEngine()
        engine.org_engine = OrgEngine(OPCConfig())
        engine.approval_engine = SimpleNamespace(authorize_tool_call=AsyncMock())
        task = Task(
            id="execution-work-item",
            project_id="proj1",
            assigned_to="executor",
            metadata={
                "execution_mode": "company_mode",
                "work_item_turn_type": "execute",
                "work_item_projection_id": "execution",
                "ownership_contract": {
                    "write_scope": str((Path.cwd() / ".tmp-test" / "allowed").resolve()),
                    "expected_artifacts": ["Updated service code"],
                    "allowed_collaboration_targets": ["reviewer"],
                },
                "target_output_dir": str((Path.cwd() / ".tmp-test" / "allowed").resolve()),
            },
        )
        tool = ToolDefinition(
            name="file_write",
            description="Write a file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            func=AsyncMock(),
        )

        allowed, decision = await engine._tool_approval_callback(
            tool,
            {"path": str((Path.cwd() / ".tmp-test" / "outside" / "bad.txt").resolve()), "content": "x"},
            task,
        )

        self.assertFalse(allowed)
        self.assertEqual(decision.policy_source, "ownership_contract")
        self.assertIn("assigned workspace", decision.rationale)
        engine.approval_engine.authorize_tool_call.assert_not_called()

    def test_low_risk_staffing_plan_can_auto_confirm(self) -> None:
        engine = OPCEngine()
        engine.org_engine = OrgEngine(OPCConfig())
        engine.secretary_policies = None
        low_risk = RecruitmentPlan(
            company_profile="corporate",
            proposals=[
                RecruitmentProposal(
                    role_id="senior_engineer",
                    status="proposed_hire",
                    candidate=RecruitmentCandidateRecommendation(
                        template_id="tmpl-1",
                        template_name="General Backend Engineer",
                        category="software-engineering",
                        domains=["coding"],
                    ),
                )
            ],
        )
        high_risk = RecruitmentPlan(
            company_profile="corporate",
            proposals=[
                RecruitmentProposal(
                    role_id="senior_engineer",
                    status="proposed_hire",
                    candidate=RecruitmentCandidateRecommendation(
                        template_id="tmpl-2",
                        template_name="Cross-domain Quant Strategist",
                        category="quant-research",
                        domains=["coding", "finance"],
                    ),
                )
            ],
        )

        self.assertTrue(engine._should_auto_confirm_recruitment_plan(low_risk))
        self.assertFalse(engine._should_auto_confirm_recruitment_plan(high_risk))


class _FailingSimpleChatLLM:
    async def simple_chat(self, *args: Any, **kwargs: Any) -> str:
        raise AssertionError("native llm fallback should not be used in this test")


class CompanyExecutorRolePromptRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_gate_harness_uses_role_prompt_runner(self) -> None:
        calls: list[dict[str, Any]] = []

        async def runner(
            source_task: Task,
            system_prompt: str,
            payload: dict[str, Any],
            prompt_kind: str,
            force_new_session: bool,
        ) -> str:
            calls.append(
                {
                    "task_id": source_task.id,
                    "system_prompt": system_prompt,
                    "payload": dict(payload),
                    "prompt_kind": prompt_kind,
                    "force_new_session": force_new_session,
                }
            )
            return json.dumps({"action": "pass", "reason": "Role runner approved the work item."})

        executor = CompanyWorkItemExecutor(
            org_engine=DummyOrgEngine(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            llm=_FailingSimpleChatLLM(),
            role_prompt_runner=runner,
        )
        task = Task(
            id="gate-task",
            title="Execution",
            description="Produce the requested deliverable.",
            assigned_to="executor",
            assigned_external_agent="codex",
            status=TaskStatus.DONE,
            result={"content": "deliverable ready"},
            metadata={
                "work_item_projection_id": "execution_work_item",
                "runtime_policy": {"gate_harness": {"enabled": True}},
            },
        )

        harness = executor._gate_harness_for_task(task)
        _, decision = await harness.evaluate(task, {"execution_work_item": task})

        self.assertEqual(decision.action, "pass")
        self.assertEqual(decision.summary, "Role runner approved the work item.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["task_id"], task.id)
        self.assertEqual(calls[0]["prompt_kind"], "gate_harness_judge")
        self.assertTrue(calls[0]["force_new_session"])

    async def test_ceo_pre_delivery_assessment_uses_role_prompt_runner(self) -> None:
        calls: list[dict[str, Any]] = []

        class _Org:
            @staticmethod
            def get_agent(role_id: str) -> Any:
                return SimpleNamespace(
                    role_id=role_id,
                    name=role_id.upper(),
                    responsibility="Executive oversight.",
                    preferred_external_agent="codex" if role_id == "ceo" else None,
                )

        async def runner(
            source_task: Task,
            system_prompt: str,
            payload: dict[str, Any],
            prompt_kind: str,
            force_new_session: bool,
        ) -> str:
            calls.append(
                {
                    "task_id": source_task.id,
                    "system_prompt": system_prompt,
                    "payload": dict(payload),
                    "prompt_kind": prompt_kind,
                    "force_new_session": force_new_session,
                }
            )
            return json.dumps(
                {
                    "deliverable": False,
                    "summary": "The package is not ready for final delivery.",
                    "rework_targets": [{"target_projection_id": "engineering", "role_id": "executor", "feedback": "Fix blockers."}],
                }
            )

        executor = CompanyWorkItemExecutor(
            org_engine=_Org(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            llm=_FailingSimpleChatLLM(),
            role_prompt_runner=runner,
        )
        plan = CompanyWorkItemRuntimePlan(
            profile="corporate",
            projections=[],
            metadata={"final_decider_role_id": "ceo"},
        )
        delivery_task = Task(
            id="delivery-task",
            title="CEO Delivery",
            description="Review the package before final delivery.",
            assigned_to="ceo",
            assigned_external_agent="codex",
            status=TaskStatus.DONE,
            project_id="proj1",
            metadata={"work_item_projection_id": "delivery_work_item", "company_profile": "corporate"},
        )
        work_item_tasks = [
            delivery_task,
            Task(
                id="engineering-task",
                title="Engineering",
                description="Implement the product.",
                assigned_to="executor",
                status=TaskStatus.DONE,
                project_id="proj1",
                metadata={"work_item_projection_id": "engineering"},
            ),
        ]

        assessment = await executor._ceo_pre_delivery_assessment(
            delivery_task,
            plan,
            work_item_tasks,
            {"executive_summary": "Package candidate"},
        )

        self.assertFalse(assessment["deliverable"])
        self.assertEqual(assessment["summary"], "The package is not ready for final delivery.")
        self.assertEqual(len(assessment["rework_targets"]), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["task_id"], delivery_task.id)
        self.assertEqual(calls[0]["prompt_kind"], "ceo_pre_delivery_assessment")
        self.assertTrue(calls[0]["force_new_session"])

    async def test_ceo_pre_delivery_assessment_runner_failure_awaits_human_for_blockers(self) -> None:
        class _Org:
            @staticmethod
            def get_agent(role_id: str) -> Any:
                return SimpleNamespace(
                    role_id=role_id,
                    name=role_id.upper(),
                    responsibility="Executive oversight.",
                    preferred_external_agent="codex" if role_id == "ceo" else None,
                )

        async def runner(
            source_task: Task,
            system_prompt: str,
            payload: dict[str, Any],
            prompt_kind: str,
            force_new_session: bool,
        ) -> str | None:
            return None

        executor = CompanyWorkItemExecutor(
            org_engine=_Org(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            llm=_FailingSimpleChatLLM(),
            role_prompt_runner=runner,
        )
        plan = CompanyWorkItemRuntimePlan(
            profile="corporate",
            projections=[],
            metadata={"final_decider_role_id": "ceo"},
        )
        delivery_task = Task(
            id="delivery-task",
            title="CEO Delivery",
            description="Review the package before final delivery.",
            assigned_to="ceo",
            assigned_external_agent="codex",
            status=TaskStatus.DONE,
            project_id="proj1",
            metadata={"work_item_projection_id": "delivery_work_item", "company_profile": "corporate"},
        )
        blocked_task = Task(
            id="engineering-task",
            title="Engineering",
            description="Implement the product.",
            assigned_to="executor",
            status=TaskStatus.BLOCKED,
            project_id="proj1",
            metadata={"work_item_projection_id": "engineering"},
        )

        assessment = await executor._ceo_pre_delivery_assessment(
            delivery_task,
            plan,
            [delivery_task, blocked_task],
            {"executive_summary": "Package candidate", "open_issues": ["engineering blocked"]},
        )

        self.assertFalse(assessment["deliverable"])
        self.assertTrue(assessment["awaiting_human"])
        self.assertTrue(assessment["assessment_infrastructure_failure"])
        self.assertEqual(assessment["rework_targets"], [])

    async def test_multi_team_org_followup_routes_to_final_decider_task(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            plan = CompanyWorkItemRuntimePlan(
                profile="corporate",
                projections=[],
                metadata={
                    "execution_model": "multi_team_org",
                    "runtime_model": "multi_team_org",
                    "final_decider_role_id": "ceo",
                },
            )
            ceo_task = Task(
                id="ceo-task",
                title="CEO Intake",
                session_id="sess-child",
                parent_session_id="sess-parent",
                assigned_to="ceo",
                status=TaskStatus.DONE,
                project_id="proj1",
                metadata={
                    "work_item_projection_id": "ceo_intake",
                    "company_work_item_plan": serialize_company_work_item_plan(plan),
                    "execution_model": "multi_team_org",
                    "runtime_model": "multi_team_org",
                    "work_item_runtime": True,
                    "progress_log": [],
                },
            )
            await store.save_task(ceo_task)
            captured: list[Task] = []

            class DummyExecutor:
                async def execute(self, runtime_plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                    _ = runtime_plan
                    captured.extend(tasks)
                    return "runtime resumed"

            engine = OPCEngine()
            engine.project_id = "proj1"
            engine.store = store
            engine.company_executor = DummyExecutor()

            response = await engine._maybe_resume_existing_company_runtime("请把方向调整到移动端优先。", "sess-parent")
            refreshed = await store.get_task(ceo_task.id)

            self.assertIsNotNone(response)
            assert response is not None
            self.assertEqual(response, "runtime resumed")
            assert refreshed is not None
            self.assertEqual(refreshed.status, TaskStatus.PENDING)
            self.assertEqual(refreshed.context_snapshot.get("user_supplied_input"), "请把方向调整到移动端优先。")
            self.assertEqual(captured[0].id, ceo_task.id)
            await store.close()

    async def test_multi_team_org_followup_accepts_work_item_plan_without_execution_model_metadata(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            plan = CompanyWorkItemRuntimePlan(
                profile="custom",
                final_decider_role_id="ceo",
                top_level_role_ids=["ceo"],
                root_projection_id="ceo_intake",
                projections=[
                    WorkItemProjectionSpec(
                        projection_id="ceo_intake",
                        turn_type="intake",
                        title="CEO Intake",
                        summary="Own and route follow-up directives.",
                        role_id="ceo",
                    )
                ],
                metadata={},
            )
            ceo_task = Task(
                id="ceo-task-no-exec-model",
                title="CEO Intake",
                session_id="sess-child-no-exec-model",
                parent_session_id="sess-parent-no-exec-model",
                assigned_to="ceo",
                status=TaskStatus.DONE,
                project_id="proj1",
                metadata={
                    "work_item_projection_id": "ceo_intake",
                    "company_work_item_plan": serialize_company_work_item_plan(plan),
                    "runtime_model": "multi_team_org",
                    "work_item_runtime": True,
                    "progress_log": [],
                },
            )
            await store.save_task(ceo_task)
            captured: list[Task] = []

            class DummyExecutor:
                async def execute(self, runtime_plan: CompanyWorkItemRuntimePlan, tasks: list[Task]) -> str:
                    _ = runtime_plan
                    captured.extend(tasks)
                    return "runtime resumed"

            engine = OPCEngine()
            engine.project_id = "proj1"
            engine.store = store
            engine.company_executor = DummyExecutor()

            response = await engine._maybe_resume_existing_company_runtime(
                "继续优化，并保留已有工单历史。",
                "sess-parent-no-exec-model",
            )
            refreshed = await store.get_task(ceo_task.id)

            self.assertEqual(response, "runtime resumed")
            assert refreshed is not None
            self.assertEqual(refreshed.status, TaskStatus.PENDING)
            self.assertEqual(refreshed.context_snapshot.get("user_supplied_input"), "继续优化，并保留已有工单历史。")
            self.assertEqual(captured[0].id, ceo_task.id)
            await store.close()


if __name__ == "__main__":
    unittest.main()
