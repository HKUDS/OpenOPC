from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from opc.core.models import (
    DelegationRun,
    DelegationWorkItem,
    ExecutionCheckpoint,
    Phase,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.core.interaction_protocol import PreparedOwnerInteractionPublication
from opc.database.store import OPCStore
from opc.database.store import (
    CompanyControllerWorkItemMutation,
    company_controller_task_preimage_hash,
)
from opc.layer1_perception.context_assembler import ContextAssembler
from opc.core.company_controller import (
    CompanyControllerAttemptContext,
    CompanyRunControllerLeaseLost,
)
from opc.engine import OPCEngine
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
from opc.layer2_organization.custom_runtime import CustomRuntimeRunner
from opc.layer2_organization.gate_harness import GateHarnessDecision
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemProjectionSpec,
)
from opc.layer2_organization.pre_delivery_validation import (
    delivery_package_sha256,
)
from opc.layer2_organization.work_item_identity import mark_work_item_projection
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer3_agent.native_agent import NativeAgent


class PreDeliveryValidatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = OPCStore(self.root / "tasks.db")
        await self.store.initialize()
        await self.store.save_delegation_run(
            DelegationRun(
                run_id="run::pre-delivery",
                project_id="project::pre-delivery",
                session_id="session::pre-delivery",
                execution_model="multi_team_org",
                status="running",
                lifecycle_status="active",
            )
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    @staticmethod
    def _work_item(
        projection_id: str,
        *,
        phase: Phase,
        kind: str = "execute",
        role_id: str = "analyst",
        dependencies: list[str] | None = None,
    ) -> DelegationWorkItem:
        return DelegationWorkItem(
            work_item_id=f"wid::{projection_id}",
            run_id="run::pre-delivery",
            cell_id="cell::investment",
            team_id="team::investment",
            role_id=role_id,
            seat_id=f"seat::{role_id}",
            title=projection_id,
            kind=kind,
            projection_id=projection_id,
            phase=phase,
            deliverable_summary="old deliverable",
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_runtime": True,
                "work_kind": kind,
                "dependency_work_item_ids": list(dependencies or []),
                "work_item_summary": "old summary",
                "work_item_artifact_index": [{"path": "old.json"}],
                "attempt_seq": 1,
                "attempt_settled": True,
                "attempt_outcome": "approved",
            },
        )

    @staticmethod
    def _task(
        projection_id: str,
        *,
        status: TaskStatus,
        kind: str = "execute",
        role_id: str = "analyst",
        delivery: bool = False,
    ) -> Task:
        metadata = mark_work_item_projection(
            {
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "work_item_runtime": True,
                "work_kind": kind,
                "progress_log": [],
                **(
                    {
                        "authoritative_output": True,
                        "user_visible": True,
                        "feedback_scope": "final",
                        "review_owner_kind": "human",
                        "requires_user_feedback": True,
                    }
                    if delivery
                    else {}
                ),
            },
            projection_id=projection_id,
            turn_type=kind,
        )
        task = Task(
            id=f"task::{projection_id}",
            project_id="project::pre-delivery",
            session_id="session::pre-delivery",
            title=projection_id,
            assigned_to=role_id,
            status=status,
            result={"content": f"completed {projection_id}"},
            context_snapshot={
                "work_item_owned_outputs": {
                    "work_item_summary": f"completed {projection_id}",
                    "work_item_artifact_index": [{"path": f"{projection_id}.json"}],
                }
            },
            metadata=metadata,
        )
        set_linked_work_item_id(task, f"wid::{projection_id}")
        return task

    async def _seed_delivery_tree(
        self,
    ) -> tuple[CompanyWorkItemRuntimePlan, list[Task]]:
        child_a = self._task(
            "company_analysis",
            status=TaskStatus.DONE,
            role_id="investment_analyst",
        )
        child_b = self._task(
            "risk_analysis",
            status=TaskStatus.DONE,
            role_id="risk_analyst",
        )
        delivery = self._task(
            "investment_delivery",
            status=TaskStatus.AWAITING_HUMAN,
            kind="deliver",
            role_id="investment_lead",
            delivery=True,
        )
        items = [
            self._work_item(
                "company_analysis",
                phase=Phase.APPROVED,
                role_id="investment_analyst",
            ),
            self._work_item(
                "risk_analysis",
                phase=Phase.APPROVED,
                role_id="risk_analyst",
            ),
            self._work_item(
                "investment_delivery",
                phase=Phase.AWAITING_HUMAN,
                kind="deliver",
                role_id="investment_lead",
                dependencies=["wid::company_analysis", "wid::risk_analysis"],
            ),
        ]
        tasks = [child_a, child_b, delivery]
        for item, task in zip(items, tasks, strict=True):
            await self.store.save_delegation_work_item(item)
            await self.store.save_task(task)
            await self.store.link_work_item_runtime_task(item.work_item_id, task.id)
        plan = CompanyWorkItemRuntimePlan(
            profile="custom",
            final_decider_role_id="investment_lead",
            projections=[
                WorkItemProjectionSpec(
                    projection_id="company_analysis",
                    turn_type="execute",
                    role_id="investment_analyst",
                    title="Company analysis",
                ),
                WorkItemProjectionSpec(
                    projection_id="risk_analysis",
                    turn_type="execute",
                    role_id="risk_analyst",
                    title="Risk analysis",
                ),
                WorkItemProjectionSpec(
                    projection_id="investment_delivery",
                    turn_type="deliver",
                    role_id="investment_lead",
                    title="Investment delivery",
                    dependency_projection_ids=[
                        "company_analysis",
                        "risk_analysis",
                    ],
                ),
            ],
        )
        return plan, tasks

    def _executor(
        self,
        *,
        validator=None,
        checkpoint_callback=None,
        checkpoint_prepare_callback=None,
        checkpoint_notify_callback=None,
    ) -> CompanyWorkItemExecutor:
        async def execute_task(task: Task) -> TaskResult:
            return TaskResult(status=task.status, content="", artifacts={})

        return CompanyWorkItemExecutor(
            org_engine=SimpleNamespace(get_agent=lambda _role_id: None),
            communication=SimpleNamespace(
                on_kanban_changed=None,
                on_work_items_created=None,
            ),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=execute_task,
            save_task=self.store.save_task,
            store=self.store,
            checkpoint_callback=checkpoint_callback,
            checkpoint_prepare_callback=checkpoint_prepare_callback,
            checkpoint_notify_callback=checkpoint_notify_callback,
            pre_delivery_validator=validator,
        )

    async def _run_delivery(
        self,
        executor: CompanyWorkItemExecutor,
        plan: CompanyWorkItemRuntimePlan,
        tasks: list[Task],
    ) -> None:
        executor._active_plan = plan
        executor._active_tasks = tasks
        await executor._finalize_completed_work_item(tasks[-1])

    async def test_default_none_preserves_final_delivery_behavior(self) -> None:
        plan, tasks = await self._seed_delivery_tree()
        checkpoints: list[dict] = []

        async def save_checkpoint(payload: dict) -> None:
            checkpoints.append(payload)

        executor = self._executor(checkpoint_callback=save_checkpoint)
        executor._ceo_pre_delivery_assessment = AsyncMock(
            return_value={"deliverable": True, "summary": "ready", "rework_targets": []}
        )

        await self._run_delivery(executor, plan, tasks)

        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0]["checkpoint_type"], "company_delivery_feedback")
        self.assertNotIn("pre_delivery_validation", tasks[-1].metadata)

    async def test_clean_assessment_backend_failure_is_durable_and_awaits_human(
        self,
    ) -> None:
        plan, tasks = await self._seed_delivery_tree()

        async def unavailable(*_args, **_kwargs):
            return None

        checkpoint = AsyncMock()
        executor = self._executor(checkpoint_callback=checkpoint)
        executor.role_prompt_runner = unavailable

        await self._run_delivery(executor, plan, tasks)

        durable = await self.store.get_task(tasks[-1].id)
        assert durable is not None
        assessment = durable.metadata["ceo_pre_delivery_assessment"]
        self.assertTrue(assessment["awaiting_human"])
        self.assertTrue(assessment["deliverable"])
        self.assertEqual(assessment["assessment_status"], "unavailable")
        self.assertEqual(
            assessment["assessment_failure_kind"],
            "role_prompt_empty_result",
        )
        self.assertEqual(
            durable.metadata["pre_delivery_assessment_status"],
            "unavailable",
        )
        self.assertEqual(
            durable.metadata["pre_delivery_assessment_failure_kind"],
            "role_prompt_empty_result",
        )
        checkpoint.assert_awaited_once()

    async def test_successful_assessment_retry_clears_stale_failure_metadata(
        self,
    ) -> None:
        plan, tasks = await self._seed_delivery_tree()
        delivery = tasks[-1]
        delivery.metadata.update(
            {
                "pre_delivery_assessment_status": "unavailable",
                "pre_delivery_assessment_failure_kind": "old_failure",
            }
        )
        await self.store.save_task(delivery)

        async def successful(*_args, **_kwargs):
            return json.dumps(
                {
                    "deliverable": True,
                    "summary": "ready",
                    "rework_targets": [],
                }
            )

        executor = self._executor(checkpoint_callback=AsyncMock())
        executor.role_prompt_runner = successful
        await self._run_delivery(executor, plan, tasks)

        durable = await self.store.get_task(delivery.id)
        assert durable is not None
        self.assertEqual(
            durable.metadata["pre_delivery_assessment_status"],
            "completed",
        )
        self.assertEqual(
            durable.metadata["pre_delivery_assessment_failure_kind"],
            "",
        )
        self.assertEqual(
            durable.metadata["ceo_pre_delivery_assessment"][
                "assessment_status"
            ],
            "completed",
        )

    async def test_valid_result_is_durable_before_ceo_and_can_read_artifact_ledger(
        self,
    ) -> None:
        plan, tasks = await self._seed_delivery_tree()
        artifact = self.root / "investment_case" / "report.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("verified report", encoding="utf-8")
        await self.store.save_runtime_session(
            runtime_session_id="runtime::analyst",
            project_id="project::pre-delivery",
            task_id=tasks[0].id,
            status="completed",
        )
        await self.store.save_runtime_tool_call(
            runtime_session_id="runtime::analyst",
            task_id=tasks[0].id,
            tool_call_id="call::web",
            tool_name="web_search",
            arguments={"query": "NVDA 2026 official results"},
        )
        await self.store.save_runtime_tool_result(
            runtime_session_id="runtime::analyst",
            task_id=tasks[0].id,
            tool_call_id="call::web",
            tool_name="web_search",
            payload={"success": True, "result": {"results": [{"url": "https://nvidia.com"}]}},
        )

        async def validate(
            delivery_task: Task,
            callback_plan: CompanyWorkItemRuntimePlan,
            callback_tasks: list[Task],
            package: dict,
        ) -> dict:
            self.assertEqual(delivery_task.id, tasks[-1].id)
            self.assertIs(callback_plan, plan)
            self.assertEqual([item.id for item in callback_tasks], [item.id for item in tasks])
            self.assertTrue(artifact.is_file())
            calls = await self.store.list_runtime_tool_calls("runtime::analyst")
            results = await self.store.list_runtime_tool_results("runtime::analyst")
            self.assertEqual(len(calls), 1)
            self.assertEqual(len(results), 1)
            self.assertIn("delivered_items", package)
            return {
                "valid": True,
                "evidence": {"artifact": str(artifact), "tool_call_id": "call::web"},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        checkpoints: list[dict] = []
        executor = self._executor(
            validator=validate,
            checkpoint_callback=lambda payload: self._append_async(checkpoints, payload),
        )

        async def assessment(*_args) -> dict:
            durable = await self.store.get_task(tasks[-1].id)
            delivery_item = await self.store.get_delegation_work_item(
                "wid::investment_delivery"
            )
            assert durable is not None and delivery_item is not None
            self.assertEqual(
                durable.metadata["pre_delivery_validation_evidence"]["tool_call_id"],
                "call::web",
            )
            self.assertEqual(
                delivery_item.metadata["pre_delivery_validation_evidence"][
                    "tool_call_id"
                ],
                "call::web",
            )
            return {"deliverable": True, "summary": "ready", "rework_targets": []}

        executor._ceo_pre_delivery_assessment = assessment
        await self._run_delivery(executor, plan, tasks)

        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(
            tasks[-1].metadata["pre_delivery_validation"]["status"],
            "passed",
        )

    async def test_live_controller_persists_validation_to_task_and_work_item_atomically(
        self,
    ) -> None:
        plan, tasks = await self._seed_delivery_tree()
        lease = await self.store.acquire_delegation_run_controller_lease(
            "run::pre-delivery",
            project_id="project::pre-delivery",
            root_session_id="session::pre-delivery",
            owner_token="controller::validator",
            lease_seconds=60,
        )
        self.assertTrue(lease.acquired)
        delivery = tasks[-1]
        delivery.metadata.update(
            {
                "delegation_run_id": "run::pre-delivery",
                "company_run_controller_owner_token": "controller::validator",
                "company_run_controller_lease_generation": lease.generation,
                "claimed_work_item_attempt_seq": 1,
            }
        )
        await self.store.save_task(delivery)
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert delivery_item is not None
        delivery_item.metadata.update(
            {
                "company_run_controller_owner_token": (
                    "controller::validator"
                ),
                "company_run_controller_lease_generation": lease.generation,
            }
        )
        await self.store.save_delegation_work_item(delivery_item)

        async def validate(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {"quality_gate": "run22-final", "checks": 141},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        executor = self._executor(validator=validate)
        allowed = await executor._apply_pre_delivery_validator(
            delivery,
            plan,
            tasks,
            {"delivered_items": []},
        )

        self.assertTrue(allowed)
        durable_task = await self.store.get_task(delivery.id)
        durable_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert durable_task is not None and durable_item is not None
        self.assertEqual(
            durable_task.metadata["pre_delivery_validation_evidence"],
            durable_item.metadata["pre_delivery_validation_evidence"],
        )
        self.assertEqual(
            durable_item.metadata["pre_delivery_validation"]["status"],
            "passed",
        )

    async def test_pass_atomically_clears_exact_lineage_and_preserves_generic_gate(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {"quality_gate": "fixed"},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, executor, generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        delivery_projection = "investment_delivery"
        exact_request = {
            "source_projection_id": delivery_projection,
            "blocker_types": ["pre_delivery_validation"],
        }
        generic_request = {
            "source_projection_id": "manager_gate",
            "blocker_types": ["generic_gate"],
        }
        company, risk, delivery = tasks
        for target in (company, delivery):
            target.metadata.update(
                {
                    "gate_harness_rework_feedback": "old deterministic blocker",
                    "gate_harness_rework_count": 1,
                    "gate_harness_rework_request": copy.deepcopy(exact_request),
                    "gate_harness_rework_requests": [copy.deepcopy(exact_request)],
                }
            )
            target.context_snapshot["latest_gate_harness_rework"] = copy.deepcopy(
                exact_request
            )
        risk.metadata.update(
            {
                "gate_harness_rework_feedback": "unrelated manager blocker",
                "gate_harness_rework_count": 2,
                "gate_harness_rework_request": copy.deepcopy(generic_request),
                "gate_harness_rework_requests": [copy.deepcopy(generic_request)],
                "upstream_gate_harness_rework_source_projection_id": (
                    "company_analysis"
                ),
            }
        )
        risk.context_snapshot.update(
            {
                "latest_gate_harness_rework": copy.deepcopy(generic_request),
                "upstream_gate_harness_rework_source_projection_id": (
                    "company_analysis"
                ),
            }
        )
        for target in tasks:
            projection_id = self._projection_id_for_test_task(target)
            await self.store.save_task(target)
            item = await self.store.get_delegation_work_item(
                f"wid::{projection_id}"
            )
            assert item is not None
            if target is risk:
                item.metadata.update(
                    {
                        "gate_harness_rework_feedback": (
                            "unrelated manager blocker"
                        ),
                        "gate_harness_rework_count": 2,
                        "gate_harness_rework_request": copy.deepcopy(
                            generic_request
                        ),
                        "upstream_gate_harness_rework_source_projection_id": (
                            "company_analysis"
                        ),
                    }
                )
            else:
                item.metadata.update(
                    {
                        "gate_harness_rework_feedback": (
                            "old deterministic blocker"
                        ),
                        "gate_harness_rework_count": 1,
                        "gate_harness_rework_request": copy.deepcopy(
                            exact_request
                        ),
                    }
                )
            await self.store.save_delegation_work_item(item)

        # A graceful controller restart releases every old Task attempt, then
        # restores only the active delivery source under the new generation.
        # Settled children retain their prior WorkItem ledger while their Task
        # envelopes remain released for atomic pass-cleanup adoption.
        generation = await self._restart_controller_with_active_source_attempt(
            source_task=delivery,
            released_tasks=(company, risk),
            owner_token="controller::invalid",
            generation=generation,
        )

        candidate_tasks = executor._pre_delivery_candidate_tasks(
            tasks,
            delivery,
        )
        candidate_delivery = candidate_tasks[-1]
        package = executor._build_authoritative_delivery_package(
            plan,
            candidate_tasks,
            candidate_delivery,
        )
        delivery.context_snapshot["delivery_package"] = copy.deepcopy(package)
        delivery.context_snapshot["work_item_owned_outputs"][
            "delivery_package"
        ] = copy.deepcopy(package)
        allowed = await executor._apply_pre_delivery_validator(
            delivery,
            plan,
            tasks,
            package,
        )

        self.assertTrue(allowed)
        for target in (company, delivery):
            durable = await self.store.get_task(target.id)
            item = await self.store.get_delegation_work_item(
                f"wid::{self._projection_id_for_test_task(target)}"
            )
            assert durable is not None and item is not None
            self.assertEqual(durable.metadata["gate_harness_rework_feedback"], "")
            self.assertEqual(durable.metadata["gate_harness_rework_count"], 0)
            self.assertEqual(durable.metadata["gate_harness_rework_request"], {})
            self.assertEqual(
                durable.metadata["gate_harness_rework_requests"],
                [exact_request],
            )
            self.assertNotIn("gate_harness_rework_feedback", item.metadata)
        durable_risk = await self.store.get_task(risk.id)
        risk_item = await self.store.get_delegation_work_item(
            "wid::risk_analysis"
        )
        assert durable_risk is not None and risk_item is not None
        self.assertEqual(
            durable_risk.metadata["gate_harness_rework_request"],
            generic_request,
        )
        self.assertEqual(
            durable_risk.context_snapshot["latest_gate_harness_rework"],
            generic_request,
        )
        self.assertNotIn(
            "upstream_gate_harness_rework_source_projection_id",
            durable_risk.metadata,
        )
        self.assertEqual(risk_item.metadata["gate_harness_rework_count"], 2)
        self.assertEqual(
            durable_risk.metadata["company_run_controller_lease_generation"],
            generation,
        )
        durable_delivery = await self.store.get_task(delivery.id)
        assert durable_delivery is not None
        self.assertEqual(
            durable_delivery.metadata["pre_delivery_validation"][
                "delivery_package_sha256"
            ],
            delivery_package_sha256(package),
        )
        risk_ref = next(
            ref
            for ref in package["source_projection_refs"]
            if ref["projection_id"] == "risk_analysis"
        )
        self.assertTrue(
            any(
                issue.startswith("gate harness rework")
                for issue in risk_ref["open_issues"]
            )
        )

    async def test_pass_discovers_work_item_only_lineage_and_task_tombstone_wins(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {"quality_gate": "work-item-fallback"},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        company, risk, delivery = tasks
        exact_request = {
            "source_projection_id": "investment_delivery",
            "blocker_types": ["pre_delivery_validation"],
        }
        stale_exact_request = copy.deepcopy(exact_request)
        company_item = await self.store.get_delegation_work_item(
            "wid::company_analysis"
        )
        risk_item = await self.store.get_delegation_work_item(
            "wid::risk_analysis"
        )
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert company_item is not None and risk_item is not None
        assert delivery_item is not None
        # No Task active fields: the WorkItem compatibility group is the
        # exact deterministic lineage authority.
        company_item.metadata.update(
            {
                "gate_harness_rework_feedback": "WI-only blocker",
                "gate_harness_rework_count": 1,
                "gate_harness_rework_request": exact_request,
            }
        )
        delivery_item.metadata.update(
            {
                "gate_harness_rework_feedback": "WI-only delivery blocker",
                "gate_harness_rework_count": 1,
                "gate_harness_rework_request": exact_request,
            }
        )
        # Explicit Task tombstones make the Task group authoritative, so this
        # stale WorkItem request must not re-enter the lineage.
        risk.metadata.update(
            {
                "gate_harness_rework_feedback": "",
                "gate_harness_rework_count": 0,
                "gate_harness_rework_request": {},
            }
        )
        risk_item.metadata.update(
            {
                "gate_harness_rework_feedback": "stale WI blocker",
                "gate_harness_rework_count": 1,
                "gate_harness_rework_request": stale_exact_request,
            }
        )
        await self.store.save_task(risk)
        for item in (company_item, risk_item, delivery_item):
            await self.store.save_delegation_work_item(item)

        metadata_by_id = {
            item.work_item_id: item.metadata
            for item in (company_item, risk_item, delivery_item)
        }
        candidate_tasks = executor._pre_delivery_candidate_tasks(
            tasks,
            delivery,
            work_item_metadata_by_id=metadata_by_id,
        )
        candidate_delivery = candidate_tasks[-1]
        package = executor._build_authoritative_delivery_package(
            plan,
            candidate_tasks,
            candidate_delivery,
        )
        delivery.context_snapshot["delivery_package"] = copy.deepcopy(package)
        delivery.context_snapshot["work_item_owned_outputs"][
            "delivery_package"
        ] = copy.deepcopy(package)
        allowed = await executor._apply_pre_delivery_validator(
            delivery,
            plan,
            tasks,
            package,
        )

        self.assertTrue(allowed)
        durable_company = await self.store.get_task(company.id)
        durable_delivery = await self.store.get_task(delivery.id)
        durable_risk = await self.store.get_task(risk.id)
        company_item = await self.store.get_delegation_work_item(
            "wid::company_analysis"
        )
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        risk_item = await self.store.get_delegation_work_item(
            "wid::risk_analysis"
        )
        assert durable_company is not None and durable_delivery is not None
        assert durable_risk is not None and company_item is not None
        assert delivery_item is not None and risk_item is not None
        for durable, item in (
            (durable_company, company_item),
            (durable_delivery, delivery_item),
        ):
            self.assertEqual(durable.metadata["gate_harness_rework_feedback"], "")
            self.assertEqual(durable.metadata["gate_harness_rework_count"], 0)
            self.assertEqual(durable.metadata["gate_harness_rework_request"], {})
            self.assertNotIn("gate_harness_rework_request", item.metadata)
        self.assertEqual(durable_risk.metadata["gate_harness_rework_request"], {})
        self.assertEqual(
            risk_item.metadata["gate_harness_rework_request"],
            stale_exact_request,
        )

    async def test_pass_child_cas_conflict_writes_no_cleanup_or_failure(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {"quality_gate": "fixed"},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        company, _risk, delivery = tasks
        exact_request = {
            "source_projection_id": "investment_delivery",
            "blocker_types": ["pre_delivery_validation"],
        }
        for target in (company, delivery):
            target.metadata.update(
                {
                    "gate_harness_rework_feedback": "still active",
                    "gate_harness_rework_count": 1,
                    "gate_harness_rework_request": copy.deepcopy(exact_request),
                }
            )
            await self.store.save_task(target)
            item = await self.store.get_delegation_work_item(
                f"wid::{self._projection_id_for_test_task(target)}"
            )
            assert item is not None
            item.metadata.update(
                {
                    "gate_harness_rework_feedback": "still active",
                    "gate_harness_rework_count": 1,
                    "gate_harness_rework_request": copy.deepcopy(exact_request),
                }
            )
            await self.store.save_delegation_work_item(item)
        candidate_tasks = executor._pre_delivery_candidate_tasks(tasks, delivery)
        package = executor._build_authoritative_delivery_package(
            plan,
            candidate_tasks,
            candidate_tasks[-1],
        )
        delivery.context_snapshot["delivery_package"] = copy.deepcopy(package)
        delivery.context_snapshot["work_item_owned_outputs"][
            "delivery_package"
        ] = copy.deepcopy(package)
        original_execute = executor._execute_authoritative_command

        async def race_child(*args, **kwargs):
            company_item = await self.store.get_delegation_work_item(
                "wid::company_analysis"
            )
            assert company_item is not None
            await self.store.update_delegation_work_item(
                company_item.work_item_id,
                metadata_updates={"concurrent_marker": "won"},
            )
            return await original_execute(*args, **kwargs)

        executor._execute_authoritative_command = race_child
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await executor._apply_pre_delivery_validator(
                delivery,
                plan,
                tasks,
                package,
            )

        durable_company = await self.store.get_task(company.id)
        durable_delivery = await self.store.get_task(delivery.id)
        company_item = await self.store.get_delegation_work_item(
            "wid::company_analysis"
        )
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        run = await self.store.get_delegation_run("run::pre-delivery")
        assert durable_company is not None and durable_delivery is not None
        assert company_item is not None and delivery_item is not None
        assert run is not None
        self.assertEqual(
            durable_company.metadata["gate_harness_rework_request"],
            exact_request,
        )
        self.assertNotIn("pre_delivery_validation", durable_delivery.metadata)
        self.assertNotIn("pre_delivery_validation", delivery_item.metadata)
        self.assertEqual(company_item.metadata["concurrent_marker"], "won")
        self.assertEqual(delivery_item.phase, Phase.AWAITING_HUMAN)
        self.assertEqual(run.status, "running")
        self.assertEqual(run.lifecycle_status, "active")

    async def test_store_rejects_forged_pass_snapshot_without_partial_write(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {"quality_gate": "fixed"},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        company, _risk, delivery = tasks
        exact_request = {
            "source_projection_id": "investment_delivery",
            "blocker_types": ["pre_delivery_validation"],
        }
        for target in (company, delivery):
            target.metadata.update(
                {
                    "gate_harness_rework_feedback": "active",
                    "gate_harness_rework_count": 1,
                    "gate_harness_rework_request": copy.deepcopy(exact_request),
                }
            )
            await self.store.save_task(target)
            item = await self.store.get_delegation_work_item(
                f"wid::{self._projection_id_for_test_task(target)}"
            )
            assert item is not None
            item.metadata.update(
                {
                    "gate_harness_rework_feedback": "active",
                    "gate_harness_rework_count": 1,
                    "gate_harness_rework_request": copy.deepcopy(exact_request),
                }
            )
            await self.store.save_delegation_work_item(item)
        candidate_tasks = executor._pre_delivery_candidate_tasks(tasks, delivery)
        package = executor._build_authoritative_delivery_package(
            plan,
            candidate_tasks,
            candidate_tasks[-1],
        )
        delivery.context_snapshot["delivery_package"] = copy.deepcopy(package)
        delivery.context_snapshot["work_item_owned_outputs"][
            "delivery_package"
        ] = copy.deepcopy(package)
        original_execute = executor._execute_authoritative_command

        async def forge_snapshot(*args, **kwargs):
            forged = copy.deepcopy(kwargs["task_snapshot"])
            forged.metadata["pre_delivery_validation_failure_kind"] = (
                "forged-stale-failure"
            )
            forged.context_snapshot["latest_gate_harness_rework"] = {
                "forged": True
            }
            kwargs["task_snapshot"] = forged
            return await original_execute(*args, **kwargs)

        executor._execute_authoritative_command = forge_snapshot
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await executor._apply_pre_delivery_validator(
                delivery,
                plan,
                tasks,
                package,
            )

        durable_company = await self.store.get_task(company.id)
        durable_delivery = await self.store.get_task(delivery.id)
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert durable_company is not None and durable_delivery is not None
        assert delivery_item is not None
        self.assertEqual(
            durable_company.metadata["gate_harness_rework_request"],
            exact_request,
        )
        self.assertNotIn("pre_delivery_validation", durable_delivery.metadata)
        self.assertNotIn(
            "pre_delivery_validation_failure_kind",
            durable_delivery.metadata,
        )
        self.assertNotIn(
            "latest_gate_harness_rework",
            durable_delivery.context_snapshot,
        )
        self.assertEqual(delivery_item.phase, Phase.AWAITING_HUMAN)

    async def test_store_rejects_zero_attempt_released_child_adoption(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, executor, generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        company, _risk, delivery = tasks
        exact_request = {
            "source_projection_id": "investment_delivery",
            "blocker_types": ["pre_delivery_validation"],
        }
        for target in (company, delivery):
            target.metadata.update(
                {
                    "gate_harness_rework_feedback": "active",
                    "gate_harness_rework_count": 1,
                    "gate_harness_rework_request": copy.deepcopy(exact_request),
                }
            )
            await self.store.save_task(target)
            item = await self.store.get_delegation_work_item(
                f"wid::{self._projection_id_for_test_task(target)}"
            )
            assert item is not None
            item.metadata.update(
                {
                    "gate_harness_rework_feedback": "active",
                    "gate_harness_rework_count": 1,
                    "gate_harness_rework_request": copy.deepcopy(exact_request),
                }
            )
            await self.store.save_delegation_work_item(item)
        await self._restart_controller_with_active_source_attempt(
            source_task=delivery,
            released_tasks=(company,),
            owner_token="controller::invalid",
            generation=generation,
        )
        company_item = await self.store.get_delegation_work_item(
            "wid::company_analysis"
        )
        assert company_item is not None
        company_item.metadata["attempt_seq"] = 0
        company_item.metadata["attempt_settled"] = True
        await self.store.save_delegation_work_item(company_item)
        candidate_tasks = executor._pre_delivery_candidate_tasks(tasks, delivery)
        package = executor._build_authoritative_delivery_package(
            plan,
            candidate_tasks,
            candidate_tasks[-1],
        )
        delivery.context_snapshot["delivery_package"] = copy.deepcopy(package)
        delivery.context_snapshot["work_item_owned_outputs"][
            "delivery_package"
        ] = copy.deepcopy(package)

        with self.assertRaises(CompanyRunControllerLeaseLost):
            await executor._apply_pre_delivery_validator(
                delivery,
                plan,
                tasks,
                package,
            )

        durable_delivery = await self.store.get_task(delivery.id)
        durable_company = await self.store.get_task(company.id)
        assert durable_delivery is not None and durable_company is not None
        self.assertNotIn("pre_delivery_validation", durable_delivery.metadata)
        self.assertEqual(
            durable_company.metadata["gate_harness_rework_request"],
            exact_request,
        )
        self.assertNotIn(
            "company_run_controller_owner_token",
            durable_company.metadata,
        )

    async def test_store_rejects_pass_from_non_prepublication_source_state(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        company, _risk, delivery = tasks
        exact_request = {
            "source_projection_id": "investment_delivery",
            "blocker_types": ["pre_delivery_validation"],
        }
        for target in (company, delivery):
            target.metadata.update(
                {
                    "gate_harness_rework_feedback": "active",
                    "gate_harness_rework_count": 1,
                    "gate_harness_rework_request": copy.deepcopy(exact_request),
                }
            )
            await self.store.save_task(target)
            item = await self.store.get_delegation_work_item(
                f"wid::{self._projection_id_for_test_task(target)}"
            )
            assert item is not None
            item.metadata.update(
                {
                    "gate_harness_rework_feedback": "active",
                    "gate_harness_rework_count": 1,
                    "gate_harness_rework_request": copy.deepcopy(exact_request),
                }
            )
            await self.store.save_delegation_work_item(item)
        delivery.status = TaskStatus.DONE
        await self.store.save_task(delivery)
        candidate_tasks = executor._pre_delivery_candidate_tasks(tasks, delivery)
        package = executor._build_authoritative_delivery_package(
            plan,
            candidate_tasks,
            candidate_tasks[-1],
        )
        delivery.context_snapshot["delivery_package"] = copy.deepcopy(package)
        delivery.context_snapshot["work_item_owned_outputs"][
            "delivery_package"
        ] = copy.deepcopy(package)

        with self.assertRaises(CompanyRunControllerLeaseLost):
            await executor._apply_pre_delivery_validator(
                delivery,
                plan,
                tasks,
                package,
            )

        durable_delivery = await self.store.get_task(delivery.id)
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert durable_delivery is not None and delivery_item is not None
        self.assertEqual(durable_delivery.status, TaskStatus.DONE)
        self.assertNotIn("pre_delivery_validation", durable_delivery.metadata)
        self.assertEqual(delivery_item.phase, Phase.AWAITING_HUMAN)

    async def _prepare_live_invalid_tree(
        self,
        *,
        validator,
        owner_token: str = "controller::invalid",
    ) -> tuple[
        CompanyWorkItemRuntimePlan,
        list[Task],
        CompanyWorkItemExecutor,
        int,
    ]:
        plan, tasks = await self._seed_delivery_tree()
        lease = await self.store.acquire_delegation_run_controller_lease(
            "run::pre-delivery",
            project_id="project::pre-delivery",
            root_session_id="session::pre-delivery",
            owner_token=owner_token,
            lease_seconds=60,
        )
        self.assertTrue(lease.acquired)
        for task in tasks:
            task.metadata.update(
                {
                    "delegation_run_id": "run::pre-delivery",
                    "company_run_controller_owner_token": owner_token,
                    "company_run_controller_lease_generation": lease.generation,
                    "claimed_work_item_attempt_seq": 1,
                }
            )
            await self.store.save_task(task)
            work_item = await self.store.get_delegation_work_item(
                f"wid::{self._projection_id_for_test_task(task)}"
            )
            assert work_item is not None
            work_item.metadata.update(
                {
                    "company_run_controller_owner_token": owner_token,
                    "company_run_controller_lease_generation": (
                        lease.generation
                    ),
                }
            )
            await self.store.save_delegation_work_item(work_item)
        executor = self._executor(validator=validator)
        executor._active_plan = plan
        executor._active_tasks = tasks
        return plan, tasks, executor, lease.generation

    @staticmethod
    def _projection_id_for_test_task(task: Task) -> str:
        return str(
            (task.metadata or {}).get("work_item_projection_id", "") or ""
        ).strip()

    async def _restart_controller_with_active_source_attempt(
        self,
        *,
        source_task: Task,
        released_tasks: tuple[Task, ...],
        owner_token: str,
        generation: int,
    ) -> int:
        """Use production fencing to restart and reclaim only the source."""

        source_work_item_id = str(
            getattr(source_task, "linked_work_item_id", "") or ""
        ).strip()
        self.assertTrue(source_work_item_id)
        await self.store.transition_claimed_work_item_and_task_for_controller(
            source_work_item_id,
            task=source_task,
            target_phase=Phase.READY_FOR_REWORK,
            reason="test controller restart preparation",
            release_claim=True,
            attempt_outcome="rework",
        )
        released = await self.store.release_delegation_run_controller_lease(
            "run::pre-delivery",
            project_id="project::pre-delivery",
            root_session_id="session::pre-delivery",
            owner_token=owner_token,
            generation=generation,
        )
        self.assertTrue(released)
        takeover = await self.store.acquire_delegation_run_controller_lease(
            "run::pre-delivery",
            project_id="project::pre-delivery",
            root_session_id="session::pre-delivery",
            owner_token=owner_token,
            lease_seconds=60,
        )
        self.assertTrue(takeover.acquired)
        self.assertGreater(takeover.generation, generation)

        role_session_id = "role-session::pre-delivery-restart"
        claimed = await self.store.claim_delegation_work_item_if_dispatchable(
            source_work_item_id,
            expected_phase=Phase.READY_FOR_REWORK,
            role_runtime_session_id=role_session_id,
            seat_id="seat::investment_lead",
            task_id=source_task.id,
            controller_owner_token=owner_token,
            controller_lease_generation=takeover.generation,
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        claimed_attempt = int(
            (claimed.metadata or {}).get("attempt_seq", 0) or 0
        )
        self.assertGreater(claimed_attempt, 0)
        source_task.metadata.update(
            {
                "delegation_role_session_id": role_session_id,
                "started_work_item_revision": 0,
                "claimed_work_item_revision": 0,
                "claimed_work_item_attempt_seq": claimed_attempt,
                "company_run_controller_owner_token": owner_token,
                "company_run_controller_lease_generation": (
                    takeover.generation
                ),
            }
        )
        await self.store.save_task(source_task)
        await self.store.transition_claimed_work_item_and_task_for_controller(
            source_work_item_id,
            task=source_task,
            target_phase=Phase.AWAITING_HUMAN,
            reason="test controller restart source settlement",
            release_claim=True,
            attempt_outcome="approved",
        )

        credential_keys = (
            "company_run_controller_owner_token",
            "company_run_controller_lease_generation",
            "claimed_work_item_attempt_seq",
        )
        for task in released_tasks:
            durable = await self.store.get_task(task.id)
            work_item_id = str(
                getattr(task, "linked_work_item_id", "") or ""
            ).strip()
            durable_item = await self.store.get_delegation_work_item(
                work_item_id
            )
            assert durable is not None and durable_item is not None
            for key in credential_keys:
                self.assertNotIn(key, durable.metadata)
            item_metadata = dict(durable_item.metadata or {})
            self.assertGreater(int(item_metadata.get("attempt_seq", 0)), 0)
            self.assertIs(item_metadata.get("attempt_settled"), True)
            self.assertEqual(
                item_metadata["company_run_controller_owner_token"],
                owner_token,
            )
            self.assertEqual(
                item_metadata["company_run_controller_lease_generation"],
                generation,
            )
            self.assertFalse(durable_item.claimed_by_role_runtime_session_id)
            self.assertFalse(durable_item.claimed_by_seat_id)
            self.assertFalse(item_metadata.get("claimed_by_role_session_id"))
            self.assertFalse(item_metadata.get("claimed_task_id"))
            self.assertFalse(item_metadata.get("dispatch_hold"))
            task.__dict__.update(copy.deepcopy(durable.__dict__))
        durable_source = await self.store.get_task(source_task.id)
        assert durable_source is not None
        self.assertEqual(
            durable_source.metadata["company_run_controller_owner_token"],
            owner_token,
        )
        self.assertEqual(
            durable_source.metadata[
                "company_run_controller_lease_generation"
            ],
            takeover.generation,
        )
        self.assertEqual(
            durable_source.metadata["claimed_work_item_attempt_seq"],
            claimed_attempt,
        )
        return takeover.generation

    async def _take_over_retained_claims(
        self,
        tasks: list[Task],
        *,
        old_owner: str,
        old_generation: int,
        new_owner: str,
    ) -> int:
        for task in tasks:
            projection_id = self._projection_id_for_test_task(task)
            item = await self.store.get_delegation_work_item(
                f"wid::{projection_id}"
            )
            assert item is not None
            item.claimed_by_role_runtime_session_id = (
                f"role-session::{projection_id}"
            )
            item.claimed_by_seat_id = f"seat::{projection_id}"
            item.metadata["claimed_by_role_session_id"] = (
                item.claimed_by_role_runtime_session_id
            )
            item.metadata["claimed_task_id"] = task.id
            await self.store.save_delegation_work_item(item)
        self.assertTrue(
            await self.store.renew_delegation_run_controller_lease(
                "run::pre-delivery",
                project_id="project::pre-delivery",
                root_session_id="session::pre-delivery",
                owner_token=old_owner,
                generation=old_generation,
                lease_seconds=1,
                heartbeat_at=datetime.now() - timedelta(seconds=2),
            )
        )
        takeover = await self.store.acquire_delegation_run_controller_lease(
            "run::pre-delivery",
            project_id="project::pre-delivery",
            root_session_id="session::pre-delivery",
            owner_token=new_owner,
            lease_seconds=60,
        )
        self.assertTrue(takeover.acquired)
        self.assertEqual(
            await self.store.settle_stale_delegation_run_claims_for_controller(
                "run::pre-delivery",
                project_id="project::pre-delivery",
                root_session_id="session::pre-delivery",
                owner_token=new_owner,
                generation=takeover.generation,
            ),
            len(tasks),
        )
        for task in tasks:
            projection_id = self._projection_id_for_test_task(task)
            durable_task = await self.store.get_task(task.id)
            item = await self.store.get_delegation_work_item(
                f"wid::{projection_id}"
            )
            assert durable_task is not None and item is not None
            self.assertEqual(
                durable_task.metadata[
                    "company_run_controller_lease_generation"
                ],
                takeover.generation,
            )
            self.assertEqual(
                item.metadata["company_run_controller_lease_generation"],
                takeover.generation,
            )
            self.assertEqual(
                durable_task.metadata[
                    "company_run_controller_owner_token"
                ],
                new_owner,
            )
            self.assertEqual(
                item.metadata["company_run_controller_owner_token"],
                new_owner,
            )
        return takeover.generation

    async def test_live_invalid_reopens_all_targets_in_one_authoritative_command(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": False,
                "evidence": {"quality_gate": "failed"},
                "issues": [
                    "company artifact has an invalid claim",
                    "risk artifact has an invalid claim",
                ],
                "rework_target_projection_ids": [
                    "company_analysis",
                    "risk_analysis",
                ],
                "rework_issues_by_projection_id": {
                    "company_analysis": ["company artifact has an invalid claim"],
                    "risk_analysis": ["risk artifact has an invalid claim"],
                },
            }

        plan, tasks, executor, _generation = await self._prepare_live_invalid_tree(
            validator=validate
        )
        allowed = await executor._apply_pre_delivery_validator(
            tasks[-1],
            plan,
            tasks,
            {"delivered_items": []},
        )

        self.assertFalse(allowed)
        for projection_id, task in zip(
            ("company_analysis", "risk_analysis", "investment_delivery"),
            tasks,
            strict=True,
        ):
            item = await self.store.get_delegation_work_item(
                f"wid::{projection_id}"
            )
            durable_task = await self.store.get_task(task.id)
            assert item is not None and durable_task is not None
            self.assertEqual(item.phase, Phase.READY_FOR_REWORK)
            self.assertEqual(durable_task.status, TaskStatus.PENDING)
            self.assertIsNone(durable_task.result)
            request = dict(
                durable_task.metadata.get("gate_harness_rework_request", {})
                or {}
            )
            if projection_id == "company_analysis":
                self.assertEqual(
                    request.get("blockers"),
                    ["company artifact has an invalid claim"],
                )
                self.assertNotIn(
                    "risk artifact", str(request.get("feedback", ""))
                )
            elif projection_id == "risk_analysis":
                self.assertEqual(
                    request.get("blockers"),
                    ["risk artifact has an invalid claim"],
                )
                self.assertNotIn(
                    "company artifact", str(request.get("feedback", ""))
                )
            else:
                self.assertEqual(request.get("blockers"), [])
                self.assertIn(
                    "Upstream deterministic quality rework invalidated",
                    str(request.get("feedback", "")),
                )
                self.assertIn(
                    "re-read the latest corrected dependency outputs",
                    str(request.get("feedback", "")),
                )
                self.assertIn(
                    "rebuild and re-verify only the delivery-owned output",
                    str(request.get("feedback", "")),
                )
                self.assertEqual(
                    request.get("invalidated_by_projection_ids"),
                    ["company_analysis", "risk_analysis"],
                )
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert delivery_item is not None
        self.assertEqual(
            delivery_item.metadata["pre_delivery_validation"]["status"],
            "quality_failed",
        )

    async def test_live_mapped_delivery_rework_keeps_own_blocker_and_seeds_upstream_invalidation(
        self,
    ) -> None:
        company_issue = "company_analysis.json has an invalid claim"
        report_issue = "report.md has an invalid verified-facts heading"

        async def validate(*_args) -> dict:
            return {
                "valid": False,
                "evidence": {"quality_gate": "failed"},
                "issues": [company_issue, report_issue],
                "rework_target_projection_ids": [
                    "company_analysis",
                    "investment_delivery",
                ],
                "rework_issues_by_projection_id": {
                    "company_analysis": [company_issue],
                    "investment_delivery": [report_issue],
                },
            }

        plan, tasks, executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        allowed = await executor._apply_pre_delivery_validator(
            tasks[-1],
            plan,
            tasks,
            {"delivered_items": []},
        )

        self.assertFalse(allowed)
        durable_company = await self.store.get_task(tasks[0].id)
        durable_delivery = await self.store.get_task(tasks[-1].id)
        assert durable_company is not None and durable_delivery is not None
        company_request = dict(
            durable_company.metadata["gate_harness_rework_request"]
        )
        delivery_request = dict(
            durable_delivery.metadata["gate_harness_rework_request"]
        )
        self.assertEqual(company_request["blockers"], [company_issue])
        self.assertNotIn(report_issue, company_request["feedback"])
        self.assertNotIn(
            "Upstream deterministic quality rework invalidated",
            company_request["feedback"],
        )
        self.assertEqual(delivery_request["blockers"], [report_issue])
        self.assertNotIn(company_issue, delivery_request["feedback"])
        self.assertIn(
            "Upstream deterministic quality rework invalidated",
            delivery_request["feedback"],
        )
        self.assertIn(
            "re-read the latest corrected dependency outputs",
            delivery_request["feedback"],
        )
        self.assertIn(
            "rebuild and re-verify only the delivery-owned output",
            delivery_request["feedback"],
        )
        self.assertEqual(
            delivery_request["invalidated_by_projection_ids"],
            ["company_analysis"],
        )

        restarted_store = OPCStore(self.root / "tasks.db")
        await restarted_store.initialize(run_startup_maintenance=False)
        try:
            restarted_delivery = await restarted_store.get_task(
                tasks[-1].id
            )
            assert restarted_delivery is not None
            restarted_request = dict(
                restarted_delivery.metadata["gate_harness_rework_request"]
            )
            self.assertEqual(
                restarted_request["invalidated_by_projection_ids"],
                ["company_analysis"],
            )
            assembler = ContextAssembler(
                memory=AsyncMock(),
                store=restarted_store,
            )
            rendered = await assembler.build_rework_feedback_context(
                restarted_delivery
            )
            self.assertIn(report_issue, rendered)
            self.assertIn(
                "re-read the latest corrected dependency outputs",
                rendered,
            )
            self.assertNotIn(company_issue, rendered)

            seeded_message = await NativeAgent._build_user_message(
                SimpleNamespace(context_assembler=assembler),
                restarted_delivery,
            )
            self.assertIn("Current WorkItem Attempt", seeded_message)
            self.assertIn(report_issue, seeded_message)
            self.assertIn(
                "rebuild and re-verify only the delivery-owned output",
                seeded_message,
            )
            self.assertTrue(
                restarted_delivery.metadata[
                    "_runtime_v2_attempt_user_seed_required"
                ]
            )
            self.assertEqual(
                len(
                    restarted_delivery.metadata[
                        "_runtime_v2_attempt_user_seed_revision"
                    ]
                ),
                64,
            )
        finally:
            await restarted_store.close()

    async def test_controller_rework_matches_cloned_source_by_stable_task_id(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {"valid": True}

        _plan, tasks, executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        source_clone = copy.deepcopy(tasks[-1])
        self.assertIsNot(source_clone, tasks[-1])
        validation_record = {
            "status": "quality_failed",
            "valid": False,
            "evidence": {"quality_gate": "clone-source-regression"},
            "issues": ["company and risk artifacts are inconsistent"],
            "rework_target_projection_ids": [
                "company_analysis",
                "risk_analysis",
            ],
        }
        source_clone.metadata["pre_delivery_validation"] = validation_record
        source_clone.metadata["pre_delivery_validation_evidence"] = dict(
            validation_record["evidence"]
        )
        source_clone.metadata["pre_delivery_rework_count"] = 1
        task_by_projection_id = {
            self._projection_id_for_test_task(task): task for task in tasks
        }
        self.assertIsNot(
            source_clone,
            task_by_projection_id["investment_delivery"],
        )
        decision = GateHarnessDecision(
            action="rework_same_work_item",
            summary="Rework both deterministic analysis artifacts.",
            target_projection_ids=["company_analysis", "risk_analysis"],
            blockers=list(validation_record["issues"]),
            blocker_types=["pre_delivery_validation"],
            source="pre_delivery_validator",
        )
        observed_command: dict = {}
        original_execute = executor._execute_authoritative_command

        async def capture_command(*args, **kwargs):
            observed_command.update(kwargs)
            return await original_execute(*args, **kwargs)

        executor._execute_authoritative_command = capture_command
        target = await executor._gate_harness_initiate_rework_for_controller(
            source_clone,
            decision,
            task_by_projection_id,
            expected_pre_delivery_rework_count=0,
            max_pre_delivery_reworks=3,
        )

        self.assertIs(target, tasks[0])
        source_snapshot = observed_command["task_snapshot"]
        self.assertEqual(source_snapshot.id, source_clone.id)
        self.assertNotIn(
            source_clone.id,
            {snapshot.id for snapshot in observed_command["task_snapshots"]},
        )
        source_mutation = next(
            mutation
            for mutation in observed_command["mutations"]
            if mutation.work_item_id == "wid::investment_delivery"
        )
        self.assertEqual(
            source_mutation.required_pre_delivery_rework_count,
            0,
        )
        self.assertEqual(source_mutation.pre_delivery_rework_count_limit, 3)
        for projection_id in (
            "company_analysis",
            "risk_analysis",
            "investment_delivery",
        ):
            item = await self.store.get_delegation_work_item(
                f"wid::{projection_id}"
            )
            durable_task = await self.store.get_task(f"task::{projection_id}")
            assert item is not None and durable_task is not None
            self.assertEqual(item.phase, Phase.READY_FOR_REWORK)
            self.assertEqual(durable_task.status, TaskStatus.PENDING)
        durable_delivery = await self.store.get_task(source_clone.id)
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert durable_delivery is not None and delivery_item is not None
        self.assertEqual(
            durable_delivery.metadata["pre_delivery_rework_count"], 1
        )
        self.assertEqual(
            durable_delivery.metadata["pre_delivery_validation"],
            validation_record,
        )
        self.assertEqual(
            durable_delivery.metadata["pre_delivery_validation_history"][-1],
            validation_record,
        )
        self.assertEqual(
            delivery_item.metadata["pre_delivery_validation"],
            validation_record,
        )

    async def test_takeover_retained_claims_keep_exact_envelope_for_rework(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": False,
                "evidence": {"quality_gate": "failed"},
                "issues": ["both child artifacts require correction"],
                "rework_target_projection_ids": [
                    "company_analysis",
                    "risk_analysis",
                ],
                "rework_issues_by_projection_id": {
                    "company_analysis": [
                        "both child artifacts require correction"
                    ],
                    "risk_analysis": [
                        "both child artifacts require correction"
                    ],
                },
            }

        plan, tasks, _executor, old_generation = (
            await self._prepare_live_invalid_tree(
                validator=validate,
                owner_token="controller::retained-old",
            )
        )
        new_generation = await self._take_over_retained_claims(
            tasks,
            old_owner="controller::retained-old",
            old_generation=old_generation,
            new_owner="controller::retained-new",
        )
        refreshed_tasks: list[Task] = []
        for old_task in tasks:
            refreshed = await self.store.get_task(old_task.id)
            assert refreshed is not None
            refreshed_tasks.append(refreshed)
        executor = self._executor(validator=validate)
        executor._active_plan = plan
        executor._active_tasks = refreshed_tasks
        allowed = await executor._apply_pre_delivery_validator(
            refreshed_tasks[-1],
            plan,
            refreshed_tasks,
            {"delivered_items": []},
        )
        self.assertFalse(allowed)
        for projection_id in (
            "company_analysis",
            "risk_analysis",
            "investment_delivery",
        ):
            item = await self.store.get_delegation_work_item(
                f"wid::{projection_id}"
            )
            task = await self.store.get_task(f"task::{projection_id}")
            assert item is not None and task is not None
            self.assertEqual(item.phase, Phase.READY_FOR_REWORK)
            self.assertEqual(task.status, TaskStatus.PENDING)
            self.assertEqual(
                item.metadata[
                    "company_run_controller_lease_generation"
                ],
                new_generation,
            )
            self.assertEqual(
                task.metadata[
                    "company_run_controller_lease_generation"
                ],
                new_generation,
            )

    async def test_live_invalid_postcommit_progress_failure_is_advisory(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": False,
                "evidence": {"quality_gate": "failed"},
                "issues": ["company artifact is inconsistent"],
                "rework_target_projection_ids": ["company_analysis"],
            }

        plan, tasks, executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        executor._emit_progress = AsyncMock(
            side_effect=RuntimeError("progress backend unavailable")
        )
        allowed = await executor._apply_pre_delivery_validator(
            tasks[-1],
            plan,
            tasks,
            {"delivered_items": []},
        )
        self.assertFalse(allowed)
        company = await self.store.get_delegation_work_item(
            "wid::company_analysis"
        )
        delivery = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        durable_delivery = await self.store.get_task(tasks[-1].id)
        run = await self.store.get_delegation_run("run::pre-delivery")
        assert (
            company is not None
            and delivery is not None
            and durable_delivery is not None
            and run is not None
        )
        self.assertEqual(company.phase, Phase.READY_FOR_REWORK)
        self.assertEqual(delivery.phase, Phase.READY_FOR_REWORK)
        self.assertEqual(durable_delivery.status, TaskStatus.PENDING)
        self.assertEqual(run.lifecycle_status, "active")
        self.assertEqual(
            durable_delivery.metadata["pre_delivery_validation"]["status"],
            "quality_failed",
        )

    async def test_live_invalid_second_target_cas_mismatch_rolls_back_everything(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": False,
                "evidence": {"quality_gate": "failed"},
                "issues": ["two artifacts failed"],
                "rework_target_projection_ids": [
                    "company_analysis",
                    "risk_analysis",
                ],
                "rework_issues_by_projection_id": {
                    "company_analysis": ["two artifacts failed"],
                    "risk_analysis": ["two artifacts failed"],
                },
            }

        plan, tasks, executor, _generation = await self._prepare_live_invalid_tree(
            validator=validate
        )
        original_execute = executor._execute_authoritative_command

        async def race_second_target(*args, **kwargs):
            risk = await self.store.get_delegation_work_item(
                "wid::risk_analysis"
            )
            assert risk is not None
            await self.store.update_delegation_work_item(
                risk.work_item_id,
                metadata_updates={"concurrent_marker": "won"},
            )
            return await original_execute(*args, **kwargs)

        executor._execute_authoritative_command = race_second_target
        with self.assertRaises(BaseException):
            await executor._apply_pre_delivery_validator(
                tasks[-1],
                plan,
                tasks,
                {"delivered_items": []},
            )

        company = await self.store.get_delegation_work_item(
            "wid::company_analysis"
        )
        risk = await self.store.get_delegation_work_item("wid::risk_analysis")
        delivery = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert company is not None and risk is not None and delivery is not None
        self.assertEqual(company.phase, Phase.APPROVED)
        self.assertEqual(risk.phase, Phase.APPROVED)
        self.assertEqual(delivery.phase, Phase.AWAITING_HUMAN)
        self.assertNotIn("pre_delivery_validation", delivery.metadata)

    async def test_live_invalid_stale_generation_writes_nothing(self) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": False,
                "evidence": {"quality_gate": "failed"},
                "issues": ["quality failed"],
                "rework_target_projection_ids": ["company_analysis"],
            }

        plan, tasks, executor, generation = await self._prepare_live_invalid_tree(
            validator=validate,
            owner_token="controller::stale",
        )
        from datetime import datetime, timedelta

        expired_at = datetime.now() - timedelta(seconds=2)
        self.assertTrue(
            await self.store.renew_delegation_run_controller_lease(
                "run::pre-delivery",
                project_id="project::pre-delivery",
                root_session_id="session::pre-delivery",
                owner_token="controller::stale",
                generation=generation,
                lease_seconds=1,
                heartbeat_at=expired_at,
            )
        )
        takeover_store = OPCStore(self.root / "tasks.db")
        await takeover_store.initialize(run_startup_maintenance=False)
        try:
            takeover = await takeover_store.acquire_delegation_run_controller_lease(
                "run::pre-delivery",
                project_id="project::pre-delivery",
                root_session_id="session::pre-delivery",
                owner_token="controller::fresh",
                lease_seconds=60,
            )
            self.assertTrue(takeover.acquired)
            with self.assertRaises(BaseException):
                await executor._apply_pre_delivery_validator(
                    tasks[-1],
                    plan,
                    tasks,
                    {"delivered_items": []},
                )
        finally:
            await takeover_store.close()

        company = await self.store.get_delegation_work_item(
            "wid::company_analysis"
        )
        delivery = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert company is not None and delivery is not None
        self.assertEqual(company.phase, Phase.APPROVED)
        self.assertEqual(delivery.phase, Phase.AWAITING_HUMAN)
        self.assertNotIn("pre_delivery_validation", delivery.metadata)

    async def test_live_validator_exception_atomically_closes_delivery_and_run(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            raise RuntimeError("validator ledger unavailable")

        plan, tasks, executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        checkpoint = AsyncMock()
        executor.checkpoint_callback = checkpoint
        allowed = await executor._apply_pre_delivery_validator(
            tasks[-1],
            plan,
            tasks,
            {"delivered_items": []},
        )
        self.assertFalse(allowed)
        company = await self.store.get_delegation_work_item(
            "wid::company_analysis"
        )
        risk = await self.store.get_delegation_work_item(
            "wid::risk_analysis"
        )
        delivery = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        durable_delivery = await self.store.get_task(tasks[-1].id)
        run = await self.store.get_delegation_run("run::pre-delivery")
        assert (
            company is not None
            and risk is not None
            and delivery is not None
            and durable_delivery is not None
            and run is not None
        )
        self.assertEqual(company.phase, Phase.APPROVED)
        self.assertEqual(risk.phase, Phase.APPROVED)
        self.assertEqual(delivery.phase, Phase.FAILED)
        self.assertEqual(durable_delivery.status, TaskStatus.FAILED)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.lifecycle_status, "closed_failed")
        self.assertEqual(
            durable_delivery.metadata["pre_delivery_validation"]["status"],
            "infrastructure_failure",
        )
        checkpoint.assert_not_awaited()

    async def test_deterministic_invalid_rework_is_bounded_and_never_finalizes(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": False,
                "evidence": {"quality_gate": "still failed"},
                "issues": ["financial claims remain unverified"],
                "rework_target_projection_ids": ["company_analysis"],
            }

        plan, tasks = await self._seed_delivery_tree()
        tasks[-1].metadata["max_pre_delivery_reworks"] = 1
        tasks[-1].metadata["delegation_run_id"] = "run::pre-delivery"
        await self.store.save_task(tasks[-1])
        checkpoint = AsyncMock()
        executor = self._executor(
            validator=validate,
            checkpoint_callback=checkpoint,
        )
        executor._ceo_pre_delivery_assessment = AsyncMock()

        first = await executor._apply_pre_delivery_validator(
            tasks[-1],
            plan,
            tasks,
            {"delivered_items": [{"attempt": 1}]},
        )
        self.assertFalse(first)
        self.assertEqual(tasks[-1].metadata["pre_delivery_rework_count"], 1)

        # Model the next successful worker attempts returning both cards to
        # their post-completion states before deterministic validation runs.
        for projection_id, phase, status in (
            ("company_analysis", Phase.APPROVED, TaskStatus.DONE),
            (
                "investment_delivery",
                Phase.AWAITING_HUMAN,
                TaskStatus.AWAITING_HUMAN,
            ),
        ):
            item = await self.store.get_delegation_work_item(
                f"wid::{projection_id}"
            )
            assert item is not None
            item.phase = Phase.RUNNING
            await self.store.save_delegation_work_item(item)
            item.phase = phase
            item.metadata["attempt_seq"] = 2
            item.metadata["attempt_settled"] = True
            item.metadata["attempt_outcome"] = phase.value
            await self.store.save_delegation_work_item(item)
            task = next(
                candidate
                for candidate in tasks
                if candidate.id == f"task::{projection_id}"
            )
            task.status = status
            task.result = {"content": f"retry {projection_id}"}
            await self.store.save_task(task)

        second = await executor._apply_pre_delivery_validator(
            tasks[-1],
            plan,
            tasks,
            {"delivered_items": [{"attempt": 2}]},
        )
        self.assertFalse(second)
        published_checkpoints = [
            dict(call.args[0])
            for call in checkpoint.await_args_list
            if call.args and isinstance(call.args[0], dict)
        ]
        self.assertFalse(
            any(
                payload.get("checkpoint_type")
                == "company_delivery_feedback"
                for payload in published_checkpoints
            )
        )
        self.assertEqual(
            sum(
                payload.get("checkpoint_type")
                == "company_run_failure_review"
                for payload in published_checkpoints
            ),
            1,
        )
        executor._ceo_pre_delivery_assessment.assert_not_awaited()
        delivery = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        run = await self.store.get_delegation_run("run::pre-delivery")
        assert delivery is not None and run is not None
        self.assertEqual(delivery.phase, Phase.FAILED)
        self.assertEqual(run.lifecycle_status, "closed_failed")
        self.assertEqual(
            delivery.metadata["pre_delivery_validation"]["status"],
            "quality_rework_cap_reached",
        )
        self.assertEqual(
            delivery.metadata["pre_delivery_validation_evidence"][
                "quality_gate"
            ],
            "still failed",
        )

    async def test_restart_checkpoint_recovery_requires_current_validation_basis(
        self,
    ) -> None:
        async def validator(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks = await self._seed_delivery_tree()
        engine = OPCEngine(
            opc_home=self.root,
            project_id="project::pre-delivery",
            pre_delivery_validator=validator,
        )
        engine.store = self.store
        engine.company_executor = self._executor(validator=validator)
        engine._save_company_feedback_followup_checkpoint = AsyncMock()

        # No validation record: the transient AWAITING_HUMAN phase cannot
        # manufacture a feedback checkpoint after a crash.
        await engine._ensure_open_final_delivery_review_checkpoints(plan, tasks)
        engine._save_company_feedback_followup_checkpoint.assert_not_awaited()

        package = {"delivered_items": [{"attempt": 1}]}
        tasks[-1].context_snapshot["delivery_package"] = package
        record = {
            "status": "passed",
            "valid": True,
            "evidence": {},
            "issues": [],
            "rework_target_projection_ids": [],
            "work_item_attempt_seq": 1,
            "delivery_package_sha256": delivery_package_sha256(package),
            "validated_at": "2026-08-13T00:00:00+00:00",
        }
        tasks[-1].metadata["pre_delivery_validation"] = dict(record)
        delivery = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert delivery is not None
        delivery.metadata["pre_delivery_validation"] = dict(record)
        await self.store.save_delegation_work_item(delivery)
        await self.store.save_task(tasks[-1])

        # A validator pass occurs before the CEO gate.  Startup therefore
        # never treats it as permission to synthesize a final card.
        await engine._ensure_open_final_delivery_review_checkpoints(plan, tasks)
        engine._save_company_feedback_followup_checkpoint.assert_not_awaited()

        # Attempt 2 has not run the validator yet.  An old attempt-1 pass may
        # not authorize recovery even if the Task still carries that record.
        delivery = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert delivery is not None
        delivery.metadata["attempt_seq"] = 2
        await self.store.save_delegation_work_item(delivery)
        await engine._ensure_open_final_delivery_review_checkpoints(plan, tasks)
        engine._save_company_feedback_followup_checkpoint.assert_not_awaited()

    async def test_valid_result_cannot_carry_issues_or_rework_targets(self) -> None:
        for contradictory in (
            {
                "valid": True,
                "evidence": {},
                "issues": ["claimed valid but reported a blocker"],
                "rework_target_projection_ids": [],
            },
            {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": ["company_analysis"],
            },
        ):
            with self.subTest(contradictory=contradictory):
                with self.assertRaises(ValueError):
                    CompanyWorkItemExecutor._normalize_pre_delivery_validation_result(
                        contradictory
                    )

    async def test_strict_result_rejects_extra_nan_and_duplicate_targets(self) -> None:
        invalid_results = (
            {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
                "unexpected": True,
            },
            {
                "valid": True,
                "evidence": {"score": float("nan")},
                "issues": [],
                "rework_target_projection_ids": [],
            },
            {
                "valid": False,
                "evidence": {},
                "issues": ["bad"],
                "rework_target_projection_ids": [
                    "company_analysis",
                    "company_analysis",
                ],
            },
            {
                "valid": False,
                "evidence": {},
                "issues": ["company bad", "risk bad"],
                "rework_target_projection_ids": [
                    "company_analysis",
                    "risk_analysis",
                ],
                "rework_issues_by_projection_id": {
                    "company_analysis": ["company bad"],
                    "risk_analysis": ["company bad"],
                },
            },
        )
        for invalid in invalid_results:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    CompanyWorkItemExecutor._normalize_pre_delivery_validation_result(
                        invalid
                    )

    async def test_strict_result_preserves_per_target_issue_mapping(self) -> None:
        normalized = (
            CompanyWorkItemExecutor._normalize_pre_delivery_validation_result(
                {
                    "valid": False,
                    "evidence": {"quality_gate": "failed"},
                    "issues": ["company bad", "risk bad", "shared bad"],
                    "rework_target_projection_ids": [
                        "company_analysis",
                        "risk_analysis",
                    ],
                    "rework_issues_by_projection_id": {
                        "risk_analysis": ["risk bad", "shared bad"],
                        "company_analysis": ["company bad", "shared bad"],
                    },
                }
            )
        )
        self.assertEqual(
            list(normalized["rework_issues_by_projection_id"]),
            ["company_analysis", "risk_analysis"],
        )
        self.assertEqual(
            normalized["rework_issues_by_projection_id"]["company_analysis"],
            ["company bad", "shared bad"],
        )

    async def test_legacy_multi_target_result_keeps_aggregate_feedback(self) -> None:
        normalized = (
            CompanyWorkItemExecutor._normalize_pre_delivery_validation_result(
                {
                    "valid": False,
                    "evidence": {},
                    "issues": ["legacy shared blocker"],
                    "rework_target_projection_ids": [
                        "company_analysis",
                        "risk_analysis",
                    ],
                }
            )
        )
        self.assertEqual(
            normalized["rework_issues_by_projection_id"],
            {
                "company_analysis": ["legacy shared blocker"],
                "risk_analysis": ["legacy shared blocker"],
            },
        )

    async def test_store_rejects_mixed_generation_additional_snapshot_atomically(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": False,
                "evidence": {},
                "issues": ["bad"],
                "rework_target_projection_ids": ["company_analysis"],
            }

        _plan, tasks, _executor, generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        delivery = tasks[-1]
        child = tasks[0]
        stale_child = await self.store.get_task(child.id)
        assert stale_child is not None
        stale_child.metadata["company_run_controller_lease_generation"] = (
            generation - 1
        )
        child_item = await self.store.get_delegation_work_item(
            "wid::company_analysis"
        )
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert child_item is not None and delivery_item is not None
        context = CompanyControllerAttemptContext.from_task(
            delivery,
            work_item_id="wid::investment_delivery",
        )
        result = await self.store.execute_company_controller_authoritative_command(
            context,
            operation="pre_delivery_validation_rework",
            mutations=(
                CompanyControllerWorkItemMutation(
                    work_item_id=child_item.work_item_id,
                    expected_phases=(Phase.APPROVED,),
                    expected_updated_at=child_item.updated_at,
                    phase=Phase.READY_FOR_REWORK,
                    allow_approved_rework=True,
                ),
                CompanyControllerWorkItemMutation(
                    work_item_id=delivery_item.work_item_id,
                    expected_phases=(Phase.AWAITING_HUMAN,),
                    expected_updated_at=delivery_item.updated_at,
                    phase=Phase.READY_FOR_REWORK,
                ),
            ),
            task_snapshot=delivery,
            task_snapshots=(stale_child,),
            task_preimage_hashes={
                delivery.id: company_controller_task_preimage_hash(delivery),
                child.id: company_controller_task_preimage_hash(child),
            },
        )
        self.assertFalse(result.applied)
        current_child_item = await self.store.get_delegation_work_item(
            child_item.work_item_id
        )
        current_delivery_item = await self.store.get_delegation_work_item(
            delivery_item.work_item_id
        )
        assert current_child_item is not None and current_delivery_item is not None
        self.assertEqual(current_child_item.phase, Phase.APPROVED)
        self.assertEqual(current_delivery_item.phase, Phase.AWAITING_HUMAN)

    async def test_store_restricts_approved_rework_bypass_to_named_command(self) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        _plan, tasks, _executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        delivery = tasks[-1]
        child_item = await self.store.get_delegation_work_item(
            "wid::company_analysis"
        )
        assert child_item is not None
        context = CompanyControllerAttemptContext.from_task(
            delivery,
            work_item_id="wid::investment_delivery",
        )
        for operation, target in (
            ("unrelated_operation", Phase.READY_FOR_REWORK),
            ("pre_delivery_validation_rework", Phase.READY),
        ):
            with self.subTest(operation=operation, target=target):
                with self.assertRaises(Exception):
                    await self.store.execute_company_controller_authoritative_command(
                        context,
                        operation=operation,
                        mutations=(
                            CompanyControllerWorkItemMutation(
                                work_item_id=child_item.work_item_id,
                                expected_phases=(Phase.APPROVED,),
                                phase=target,
                                allow_approved_rework=True,
                            ),
                        ),
                    )

    async def test_store_rejects_named_rework_without_exact_task_bijection(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        _plan, tasks, _executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        delivery = tasks[-1]
        child_item = await self.store.get_delegation_work_item(
            "wid::company_analysis"
        )
        assert child_item is not None
        context = CompanyControllerAttemptContext.from_task(
            delivery,
            work_item_id="wid::investment_delivery",
        )
        with self.assertRaises(ValueError):
            await self.store.execute_company_controller_authoritative_command(
                context,
                operation="pre_delivery_validation_rework",
                mutations=(
                    CompanyControllerWorkItemMutation(
                        work_item_id=child_item.work_item_id,
                        expected_phases=(Phase.APPROVED,),
                        phase=Phase.READY_FOR_REWORK,
                        allow_approved_rework=True,
                    ),
                ),
            )
        current = await self.store.get_delegation_work_item(
            child_item.work_item_id
        )
        assert current is not None
        self.assertEqual(current.phase, Phase.APPROVED)

    async def test_store_rejects_named_rework_without_controller_source(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        _plan, tasks, _executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        delivery = tasks[-1]
        child = await self.store.get_task(tasks[0].id)
        child_item = await self.store.get_delegation_work_item(
            "wid::company_analysis"
        )
        assert child is not None and child_item is not None
        durable_child_hash = company_controller_task_preimage_hash(child)
        child.status = TaskStatus.PENDING
        context = CompanyControllerAttemptContext.from_task(
            delivery,
            work_item_id="wid::investment_delivery",
        )
        with self.assertRaises(ValueError):
            await self.store.execute_company_controller_authoritative_command(
                context,
                operation="pre_delivery_validation_rework",
                mutations=(
                    CompanyControllerWorkItemMutation(
                        work_item_id=child_item.work_item_id,
                        expected_phases=(Phase.APPROVED,),
                        phase=Phase.READY_FOR_REWORK,
                        allow_approved_rework=True,
                    ),
                ),
                task_snapshots=(child,),
                task_preimage_hashes={
                    child.id: durable_child_hash
                },
            )
        current = await self.store.get_delegation_work_item(
            child_item.work_item_id
        )
        assert current is not None
        self.assertEqual(current.phase, Phase.APPROVED)

    async def test_store_rejects_named_rework_task_projection_drift(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        _plan, tasks, _executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        child = await self.store.get_task(tasks[0].id)
        delivery = await self.store.get_task(tasks[-1].id)
        child_item = await self.store.get_delegation_work_item(
            "wid::company_analysis"
        )
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert (
            child is not None
            and delivery is not None
            and child_item is not None
            and delivery_item is not None
        )
        preimages = {
            child.id: company_controller_task_preimage_hash(child),
            delivery.id: company_controller_task_preimage_hash(delivery),
        }
        # Source is a coherent reset snapshot.  The child deliberately keeps
        # its terminal projection/result while its WorkItem requests rework.
        delivery.status = TaskStatus.PENDING
        delivery.result = None
        child.status = TaskStatus.DONE
        child.result = {"content": "stale terminal output"}
        context = CompanyControllerAttemptContext.from_task(
            delivery,
            work_item_id=delivery_item.work_item_id,
        )
        result = await self.store.execute_company_controller_authoritative_command(
            context,
            operation="pre_delivery_validation_rework",
            mutations=(
                CompanyControllerWorkItemMutation(
                    work_item_id=child_item.work_item_id,
                    expected_phases=(Phase.APPROVED,),
                    phase=Phase.READY_FOR_REWORK,
                    allow_approved_rework=True,
                ),
                CompanyControllerWorkItemMutation(
                    work_item_id=delivery_item.work_item_id,
                    expected_phases=(Phase.AWAITING_HUMAN,),
                    phase=Phase.READY_FOR_REWORK,
                ),
            ),
            task_snapshot=delivery,
            task_snapshots=(child,),
            task_preimage_hashes=preimages,
        )
        self.assertFalse(result.applied)
        durable_child = await self.store.get_task(child.id)
        current_child_item = await self.store.get_delegation_work_item(
            child_item.work_item_id
        )
        current_delivery_item = await self.store.get_delegation_work_item(
            delivery_item.work_item_id
        )
        assert (
            durable_child is not None
            and current_child_item is not None
            and current_delivery_item is not None
        )
        self.assertEqual(durable_child.status, TaskStatus.DONE)
        self.assertEqual(current_child_item.phase, Phase.APPROVED)
        self.assertEqual(
            current_delivery_item.phase, Phase.AWAITING_HUMAN
        )

    async def test_store_rejects_named_rework_attempt_envelope_override(
        self,
    ) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        _plan, tasks, _executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        child = await self.store.get_task(tasks[0].id)
        delivery = await self.store.get_task(tasks[-1].id)
        child_item = await self.store.get_delegation_work_item(
            "wid::company_analysis"
        )
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert (
            child is not None
            and delivery is not None
            and child_item is not None
            and delivery_item is not None
        )
        preimages = {
            child.id: company_controller_task_preimage_hash(child),
            delivery.id: company_controller_task_preimage_hash(delivery),
        }
        child.status = TaskStatus.PENDING
        child.result = None
        delivery.status = TaskStatus.PENDING
        delivery.result = None
        context = CompanyControllerAttemptContext.from_task(
            delivery,
            work_item_id=delivery_item.work_item_id,
        )
        result = await self.store.execute_company_controller_authoritative_command(
            context,
            operation="pre_delivery_validation_rework",
            mutations=(
                CompanyControllerWorkItemMutation(
                    work_item_id=child_item.work_item_id,
                    expected_phases=(Phase.APPROVED,),
                    phase=Phase.READY_FOR_REWORK,
                    allow_approved_rework=True,
                    metadata_updates={"attempt_seq": 999},
                ),
                CompanyControllerWorkItemMutation(
                    work_item_id=delivery_item.work_item_id,
                    expected_phases=(Phase.AWAITING_HUMAN,),
                    phase=Phase.READY_FOR_REWORK,
                ),
            ),
            task_snapshot=delivery,
            task_snapshots=(child,),
            task_preimage_hashes=preimages,
        )
        self.assertFalse(result.applied)
        durable_child = await self.store.get_task(child.id)
        current_child_item = await self.store.get_delegation_work_item(
            child_item.work_item_id
        )
        current_delivery_item = await self.store.get_delegation_work_item(
            delivery_item.work_item_id
        )
        assert (
            durable_child is not None
            and current_child_item is not None
            and current_delivery_item is not None
        )
        self.assertEqual(
            current_child_item.metadata["attempt_seq"], 1
        )
        self.assertEqual(current_child_item.phase, Phase.APPROVED)
        self.assertEqual(durable_child.status, TaskStatus.DONE)
        self.assertEqual(
            current_delivery_item.phase, Phase.AWAITING_HUMAN
        )

    async def test_store_rejects_named_rework_identity_override(self) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        for forged_updates, forged_task_turn in (
            ({"work_item_projection_id": "forged"}, "execute"),
            ({}, "deliver"),
        ):
            with self.subTest(
                forged_updates=forged_updates,
                forged_task_turn=forged_task_turn,
            ):
                # Each subcase needs a fresh database because the Store command
                # validates an exact transactional preimage.
                if forged_task_turn == "deliver":
                    await self.store.close()
                    self._tmp.cleanup()
                    await self.asyncSetUp()
                _plan, tasks, _executor, _generation = (
                    await self._prepare_live_invalid_tree(
                        validator=validate
                    )
                )
                child = await self.store.get_task(tasks[0].id)
                delivery = await self.store.get_task(tasks[-1].id)
                child_item = await self.store.get_delegation_work_item(
                    "wid::company_analysis"
                )
                delivery_item = await self.store.get_delegation_work_item(
                    "wid::investment_delivery"
                )
                assert (
                    child is not None
                    and delivery is not None
                    and child_item is not None
                    and delivery_item is not None
                )
                preimages = {
                    child.id: company_controller_task_preimage_hash(child),
                    delivery.id: company_controller_task_preimage_hash(
                        delivery
                    ),
                }
                child.status = TaskStatus.PENDING
                child.result = None
                child.metadata["work_item_turn_type"] = forged_task_turn
                delivery.status = TaskStatus.PENDING
                delivery.result = None
                context = CompanyControllerAttemptContext.from_task(
                    delivery,
                    work_item_id=delivery_item.work_item_id,
                )
                result = (
                    await self.store.execute_company_controller_authoritative_command(
                        context,
                        operation="pre_delivery_validation_rework",
                        mutations=(
                            CompanyControllerWorkItemMutation(
                                work_item_id=child_item.work_item_id,
                                expected_phases=(Phase.APPROVED,),
                                phase=Phase.READY_FOR_REWORK,
                                allow_approved_rework=True,
                                metadata_updates=forged_updates,
                            ),
                            CompanyControllerWorkItemMutation(
                                work_item_id=delivery_item.work_item_id,
                                expected_phases=(Phase.AWAITING_HUMAN,),
                                phase=Phase.READY_FOR_REWORK,
                            ),
                        ),
                        task_snapshot=delivery,
                        task_snapshots=(child,),
                        task_preimage_hashes=preimages,
                    )
                )
                self.assertFalse(result.applied)
                current_child_item = (
                    await self.store.get_delegation_work_item(
                        child_item.work_item_id
                    )
                )
                current_delivery_item = (
                    await self.store.get_delegation_work_item(
                        delivery_item.work_item_id
                    )
                )
                assert (
                    current_child_item is not None
                    and current_delivery_item is not None
                )
                self.assertEqual(current_child_item.phase, Phase.APPROVED)
                self.assertEqual(
                    current_delivery_item.phase,
                    Phase.AWAITING_HUMAN,
                )

    async def test_store_rejects_forged_primary_snapshot_envelope(self) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        _plan, tasks, _executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        delivery = tasks[-1]
        forged = await self.store.get_task(delivery.id)
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert forged is not None and delivery_item is not None
        durable_hash = company_controller_task_preimage_hash(forged)
        forged.metadata["company_run_controller_owner_token"] = "forged"
        context = CompanyControllerAttemptContext.from_task(
            delivery,
            work_item_id=delivery_item.work_item_id,
        )
        result = await self.store.execute_company_controller_authoritative_command(
            context,
            operation="record_pre_delivery_validation",
            mutations=(
                CompanyControllerWorkItemMutation(
                    work_item_id=delivery_item.work_item_id,
                    expected_phases=(Phase.AWAITING_HUMAN,),
                    expected_updated_at=delivery_item.updated_at,
                ),
            ),
            task_snapshot=forged,
            task_preimage_hashes={forged.id: durable_hash},
        )
        self.assertFalse(result.applied)
        durable = await self.store.get_task(delivery.id)
        assert durable is not None
        self.assertNotEqual(
            durable.metadata["company_run_controller_owner_token"],
            "forged",
        )

    async def test_live_invalid_preserves_fresh_durable_task_fields(self) -> None:
        async def validate(*_args) -> dict:
            return {
                "valid": False,
                "evidence": {},
                "issues": ["bad"],
                "rework_target_projection_ids": ["company_analysis"],
            }

        plan, tasks, executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        durable_child = await self.store.get_task(tasks[0].id)
        assert durable_child is not None
        durable_child.title = "durable newer title"
        durable_child.comments = ["durable comment"]
        durable_child.retry_count = 7
        durable_child.metadata["durable_marker"] = "preserve"
        await self.store.save_task(durable_child)

        allowed = await executor._apply_pre_delivery_validator(
            tasks[-1],
            plan,
            tasks,
            {"delivered_items": []},
        )
        self.assertFalse(allowed)
        persisted = await self.store.get_task(tasks[0].id)
        assert persisted is not None
        self.assertEqual(persisted.title, "durable newer title")
        self.assertEqual(persisted.comments, ["durable comment"])
        self.assertEqual(persisted.retry_count, 7)
        self.assertEqual(persisted.metadata["durable_marker"], "preserve")
        self.assertEqual(tasks[0].status, TaskStatus.PENDING)
        self.assertIsNone(tasks[0].result)

    async def test_restart_requeues_unpublished_delivery_for_full_prefinal_retry(
        self,
    ) -> None:
        async def validator(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, _executor, generation = (
            await self._prepare_live_invalid_tree(
                validator=validator,
                owner_token="controller::restart",
            )
        )
        engine = OPCEngine(
            opc_home=self.root,
            project_id="project::pre-delivery",
            pre_delivery_validator=validator,
        )
        engine.store = self.store
        engine.company_executor = self._executor(validator=validator)
        await engine._preserve_stable_waiting_task_after_restart(
            tasks[-1],
            reason="restart before final publication",
            plan=plan,
            tasks=tasks,
            controller_credential={
                "run_id": "run::pre-delivery",
                "project_id": "project::pre-delivery",
                "root_session_id": "session::pre-delivery",
                "owner_token": "controller::restart",
                "generation": generation,
            },
        )
        delivery_task = await self.store.get_task(tasks[-1].id)
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert delivery_task is not None and delivery_item is not None
        self.assertEqual(delivery_task.status, TaskStatus.PENDING)
        self.assertEqual(delivery_item.phase, Phase.READY_FOR_REWORK)
        self.assertIsNone(delivery_task.result)

    async def test_restart_requeue_cas_miss_retries_from_fresh_preimage(
        self,
    ) -> None:
        async def validator(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, _executor, generation = (
            await self._prepare_live_invalid_tree(
                validator=validator,
                owner_token="controller::restart-cas",
            )
        )
        engine = OPCEngine(
            opc_home=self.root,
            project_id="project::pre-delivery",
            pre_delivery_validator=validator,
        )
        engine.store = self.store
        engine.company_executor = self._executor(validator=validator)
        original_requeue = (
            self.store.requeue_unpublished_delivery_for_controller
        )
        calls = 0

        async def race_once(**kwargs) -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                durable = await self.store.get_task(tasks[-1].id)
                assert durable is not None
                durable.metadata["concurrent_restart_marker"] = "won"
                await self.store.save_task(durable)
            return await original_requeue(**kwargs)

        self.store.requeue_unpublished_delivery_for_controller = race_once
        recovered = await engine._preserve_stable_waiting_task_after_restart(
            tasks[-1],
            reason="restart CAS race",
            plan=plan,
            tasks=tasks,
            controller_credential={
                "run_id": "run::pre-delivery",
                "project_id": "project::pre-delivery",
                "root_session_id": "session::pre-delivery",
                "owner_token": "controller::restart-cas",
                "generation": generation,
            },
        )
        self.assertTrue(recovered)
        self.assertEqual(calls, 2)
        delivery_task = await self.store.get_task(tasks[-1].id)
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert delivery_task is not None and delivery_item is not None
        self.assertEqual(delivery_task.status, TaskStatus.PENDING)
        self.assertEqual(delivery_item.phase, Phase.READY_FOR_REWORK)
        self.assertEqual(
            delivery_task.metadata["concurrent_restart_marker"], "won"
        )

    async def test_restart_invalidates_legacy_active_card_and_requeues(
        self,
    ) -> None:
        async def validator(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, _executor, generation = (
            await self._prepare_live_invalid_tree(
                validator=validator,
                owner_token="controller::legacy-card",
            )
        )
        legacy = ExecutionCheckpoint(
            checkpoint_id="checkpoint::legacy-final",
            project_id="project::pre-delivery",
            session_id="session::pre-delivery",
            checkpoint_type="company_delivery_feedback",
            task_id=tasks[-1].id,
            payload={
                "waiting_task_id": tasks[-1].id,
                "feedback_scope": "final",
            },
        )
        await self.store.publish_owner_interaction_checkpoint(
            legacy,
            interaction_key="legacy-final-card",
            supersede_pending_scope=False,
        )
        engine = OPCEngine(
            opc_home=self.root,
            project_id="project::pre-delivery",
            pre_delivery_validator=validator,
        )
        engine.store = self.store
        engine.company_executor = self._executor(validator=validator)

        recovered = await engine._preserve_stable_waiting_task_after_restart(
            tasks[-1],
            reason="legacy active card is not an atomic final publication",
            plan=plan,
            tasks=tasks,
            controller_credential={
                "run_id": "run::pre-delivery",
                "project_id": "project::pre-delivery",
                "root_session_id": "session::pre-delivery",
                "owner_token": "controller::legacy-card",
                "generation": generation,
            },
        )
        self.assertTrue(recovered)
        current_card = await self.store.get_execution_checkpoint(
            legacy.checkpoint_id,
            project_id=legacy.project_id,
            checkpoint_type=legacy.checkpoint_type,
        )
        delivery_task = await self.store.get_task(tasks[-1].id)
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert current_card is not None
        assert delivery_task is not None and delivery_item is not None
        self.assertEqual(current_card.status, "invalid")
        self.assertEqual(delivery_task.status, TaskStatus.PENDING)
        self.assertEqual(delivery_item.phase, Phase.READY_FOR_REWORK)

    async def test_full_reconcile_does_not_let_legacy_card_suppress_requeue(
        self,
    ) -> None:
        async def validator(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, _executor, generation = (
            await self._prepare_live_invalid_tree(
                validator=validator,
                owner_token="controller::legacy-full",
            )
        )
        legacy = ExecutionCheckpoint(
            checkpoint_id="checkpoint::legacy-full-final",
            project_id="project::pre-delivery",
            session_id="session::pre-delivery",
            checkpoint_type="company_delivery_feedback",
            task_id=tasks[-1].id,
            payload={"waiting_task_id": tasks[-1].id},
        )
        await self.store.publish_owner_interaction_checkpoint(
            legacy,
            interaction_key="legacy-full-final-card",
            supersede_pending_scope=False,
        )
        engine = OPCEngine(
            opc_home=self.root,
            project_id="project::pre-delivery",
            pre_delivery_validator=validator,
        )
        engine.store = self.store
        engine.company_executor = self._executor(validator=validator)
        updated = await engine._reconcile_company_runtime_state(
            "session::pre-delivery",
            plan,
            tasks,
            controller_credential={
                "run_id": "run::pre-delivery",
                "project_id": "project::pre-delivery",
                "root_session_id": "session::pre-delivery",
                "owner_token": "controller::legacy-full",
                "generation": generation,
            },
        )
        self.assertGreaterEqual(updated, 1)
        current_card = await self.store.get_execution_checkpoint(
            legacy.checkpoint_id,
            project_id=legacy.project_id,
            checkpoint_type=legacy.checkpoint_type,
        )
        delivery_task = await self.store.get_task(tasks[-1].id)
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert current_card is not None
        assert delivery_task is not None and delivery_item is not None
        self.assertEqual(current_card.status, "invalid")
        self.assertEqual(delivery_task.status, TaskStatus.PENDING)
        self.assertEqual(delivery_item.phase, Phase.READY_FOR_REWORK)
        self.assertEqual(
            delivery_item.metadata.get("dispatch_hold"),
            "company_runtime_suspended",
        )

    async def test_restart_invalidates_old_basis_atomic_card_and_requeues(
        self,
    ) -> None:
        async def validator(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, _executor, generation = (
            await self._prepare_live_invalid_tree(
                validator=validator,
                owner_token="controller::old-basis",
            )
        )
        package = {"delivered_items": [{"attempt": 1}]}
        tasks[-1].context_snapshot["delivery_package"] = package
        tasks[-1].context_snapshot["work_item_owned_outputs"][
            "delivery_package"
        ] = package
        executor = self._executor(validator=validator)
        executor._active_plan = plan
        executor._active_tasks = tasks
        self.assertTrue(
            await executor._apply_pre_delivery_validator(
                tasks[-1], plan, tasks, package
            )
        )
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        durable_task = await self.store.get_task(tasks[-1].id)
        assert delivery_item is not None and durable_task is not None
        validation = dict(
            durable_task.metadata["pre_delivery_validation"]
        )
        from opc.layer2_organization.pre_delivery_validation import (
            FINAL_DELIVERY_PUBLICATION_PROVENANCE_KEY,
            final_delivery_publication_provenance,
        )

        payload = {
            "waiting_task_id": durable_task.id,
            "waiting_work_item_id": delivery_item.work_item_id,
            "work_item_attempt_seq": 1,
            "delivery_package_sha256": validation[
                "delivery_package_sha256"
            ],
            FINAL_DELIVERY_PUBLICATION_PROVENANCE_KEY: (
                final_delivery_publication_provenance(
                    run_id="run::pre-delivery",
                    task_id=durable_task.id,
                    work_item_id=delivery_item.work_item_id,
                    attempt_seq=1,
                    package_hash=validation["delivery_package_sha256"],
                )
            ),
        }
        checkpoint = ExecutionCheckpoint(
            checkpoint_id="checkpoint::old-basis-final",
            project_id="project::pre-delivery",
            session_id="session::pre-delivery",
            checkpoint_type="company_delivery_feedback",
            task_id=durable_task.id,
            payload=payload,
        )
        with self.assertRaisesRegex(ValueError, "provenance is reserved"):
            await self.store.publish_owner_interaction_checkpoint(
                checkpoint,
                interaction_key="old-basis-final-card",
                supersede_pending_scope=False,
            )
        # Simulate an old binary's already-durable row.  The public Store API
        # above is now sealed; direct SQL is intentionally limited to this
        # migration/recovery fixture.
        assert self.store._db is not None
        await self.store._db.execute(
            """INSERT INTO execution_checkpoints
               (checkpoint_id, project_id, session_id, checkpoint_type,
                status, task_id, payload, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                checkpoint.checkpoint_id,
                checkpoint.project_id,
                checkpoint.session_id,
                checkpoint.checkpoint_type,
                checkpoint.status,
                checkpoint.task_id,
                json.dumps(checkpoint.payload),
                checkpoint.created_at.isoformat(),
                checkpoint.updated_at.isoformat(),
            ),
        )
        await self.store._db.commit()
        # Drift only the durable package after the old card was published.
        durable_task.context_snapshot["delivery_package"] = {
            "delivered_items": [{"attempt": "drifted"}]
        }
        durable_task.context_snapshot["work_item_owned_outputs"][
            "delivery_package"
        ] = durable_task.context_snapshot["delivery_package"]
        await self.store.save_task(durable_task)
        engine = OPCEngine(
            opc_home=self.root,
            project_id="project::pre-delivery",
            pre_delivery_validator=validator,
        )
        engine.store = self.store
        engine.company_executor = self._executor(validator=validator)
        self.assertTrue(
            await engine._preserve_stable_waiting_task_after_restart(
                durable_task,
                reason="old package basis",
                plan=plan,
                tasks=tasks,
                controller_credential={
                    "run_id": "run::pre-delivery",
                    "project_id": "project::pre-delivery",
                    "root_session_id": "session::pre-delivery",
                    "owner_token": "controller::old-basis",
                    "generation": generation,
                },
            )
        )
        current_card = await self.store.get_execution_checkpoint(
            checkpoint.checkpoint_id,
            project_id=checkpoint.project_id,
            checkpoint_type=checkpoint.checkpoint_type,
        )
        assert current_card is not None
        self.assertEqual(current_card.status, "invalid")

    async def test_restart_requeue_postcommit_takeover_notification_is_advisory(
        self,
    ) -> None:
        async def validator(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, _executor, generation = (
            await self._prepare_live_invalid_tree(
                validator=validator,
                owner_token="controller::restart-notify",
            )
        )
        engine = OPCEngine(
            opc_home=self.root,
            project_id="project::pre-delivery",
            pre_delivery_validator=validator,
        )
        engine.store = self.store
        engine.company_executor = self._executor(validator=validator)
        with patch(
            "opc.database.store.on_phase_transition",
            AsyncMock(
                side_effect=CompanyRunControllerLeaseLost(
                    "takeover after commit"
                )
            ),
        ):
            recovered = await engine._preserve_stable_waiting_task_after_restart(
                tasks[-1],
                reason="restart notification race",
                plan=plan,
                tasks=tasks,
                controller_credential={
                    "run_id": "run::pre-delivery",
                    "project_id": "project::pre-delivery",
                    "root_session_id": "session::pre-delivery",
                    "owner_token": "controller::restart-notify",
                    "generation": generation,
                },
            )
        self.assertTrue(recovered)
        delivery_task = await self.store.get_task(tasks[-1].id)
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert delivery_task is not None and delivery_item is not None
        self.assertEqual(delivery_task.status, TaskStatus.PENDING)
        self.assertEqual(delivery_item.phase, Phase.READY_FOR_REWORK)

    async def test_full_restart_reconcile_atomically_requeues_and_suspends(
        self,
    ) -> None:
        async def validator(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, _executor, generation = (
            await self._prepare_live_invalid_tree(
                validator=validator,
                owner_token="controller::restart-full",
            )
        )
        engine = OPCEngine(
            opc_home=self.root,
            project_id="project::pre-delivery",
            pre_delivery_validator=validator,
        )
        engine.store = self.store
        engine.company_executor = self._executor(validator=validator)
        updated = await engine._reconcile_company_runtime_state(
            "session::pre-delivery",
            plan,
            tasks,
            controller_credential={
                "run_id": "run::pre-delivery",
                "project_id": "project::pre-delivery",
                "root_session_id": "session::pre-delivery",
                "owner_token": "controller::restart-full",
                "generation": generation,
            },
        )
        self.assertGreaterEqual(updated, 1)
        delivery_task = await self.store.get_task(tasks[-1].id)
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert delivery_task is not None and delivery_item is not None
        self.assertEqual(delivery_item.phase, Phase.READY_FOR_REWORK)
        self.assertEqual(
            delivery_item.metadata.get("dispatch_hold"),
            "company_runtime_suspended",
        )
        self.assertEqual(
            delivery_task.metadata.get("dispatch_hold"),
            "company_runtime_suspended",
        )
        checkpoints = await self.store.get_execution_checkpoints(
            project_id="project::pre-delivery",
            session_id="session::pre-delivery",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["pending"],
        )
        self.assertEqual(len(checkpoints), 1)

    async def test_takeover_retained_delivery_claim_full_reconcile_recovers(
        self,
    ) -> None:
        async def validator(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, _executor, old_generation = (
            await self._prepare_live_invalid_tree(
                validator=validator,
                owner_token="controller::restart-old",
            )
        )
        new_generation = await self._take_over_retained_claims(
            tasks,
            old_owner="controller::restart-old",
            old_generation=old_generation,
            new_owner="controller::restart-new",
        )
        engine = OPCEngine(
            opc_home=self.root,
            project_id="project::pre-delivery",
            pre_delivery_validator=validator,
        )
        engine.store = self.store
        engine.company_executor = self._executor(validator=validator)
        updated = await engine._reconcile_company_runtime_state(
            "session::pre-delivery",
            plan,
            tasks,
            controller_credential={
                "run_id": "run::pre-delivery",
                "project_id": "project::pre-delivery",
                "root_session_id": "session::pre-delivery",
                "owner_token": "controller::restart-new",
                "generation": new_generation,
            },
        )
        self.assertGreaterEqual(updated, 1)
        delivery_task = await self.store.get_task(tasks[-1].id)
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert delivery_task is not None and delivery_item is not None
        self.assertEqual(delivery_item.phase, Phase.READY_FOR_REWORK)
        self.assertEqual(
            delivery_item.metadata.get("dispatch_hold"),
            "company_runtime_suspended",
        )
        self.assertEqual(
            delivery_task.metadata.get("dispatch_hold"),
            "company_runtime_suspended",
        )
        self.assertEqual(
            delivery_task.metadata[
                "company_run_controller_lease_generation"
            ],
            new_generation,
        )
        checkpoints = await self.store.get_execution_checkpoints(
            project_id="project::pre-delivery",
            session_id="session::pre-delivery",
            checkpoint_types=["company_runtime_interrupted"],
            statuses=["pending"],
        )
        self.assertEqual(len(checkpoints), 1)

    async def test_live_rework_counter_race_closes_at_cap_without_reopen(
        self,
    ) -> None:
        async def validator(*_args) -> dict:
            return {
                "valid": False,
                "evidence": {"quality_gate": "failed"},
                "issues": ["claim evidence remains invalid"],
                "rework_target_projection_ids": ["company_analysis"],
            }

        plan, tasks, executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validator)
        )
        tasks[-1].metadata["max_pre_delivery_reworks"] = 1
        durable_delivery = await self.store.get_task(tasks[-1].id)
        assert durable_delivery is not None
        durable_delivery.metadata["max_pre_delivery_reworks"] = 1
        await self.store.save_task(durable_delivery)
        original_execute = executor._execute_authoritative_command
        raced = False

        async def race_counter(*args, **kwargs):
            nonlocal raced
            if (
                not raced
                and kwargs.get("operation")
                == "pre_delivery_validation_rework"
            ):
                raced = True
                latest = await self.store.get_task(tasks[-1].id)
                assert latest is not None
                latest.metadata["pre_delivery_rework_count"] = 1
                await self.store.save_task(latest)
            return await original_execute(*args, **kwargs)

        executor._execute_authoritative_command = race_counter
        allowed = await executor._apply_pre_delivery_validator(
            tasks[-1],
            plan,
            tasks,
            {"delivered_items": []},
        )
        self.assertFalse(allowed)
        company = await self.store.get_delegation_work_item(
            "wid::company_analysis"
        )
        delivery = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        durable_delivery = await self.store.get_task(tasks[-1].id)
        run = await self.store.get_delegation_run("run::pre-delivery")
        assert (
            company is not None
            and delivery is not None
            and durable_delivery is not None
            and run is not None
        )
        self.assertEqual(company.phase, Phase.APPROVED)
        self.assertEqual(delivery.phase, Phase.FAILED)
        self.assertEqual(durable_delivery.status, TaskStatus.FAILED)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.lifecycle_status, "closed_failed")
        self.assertEqual(
            durable_delivery.metadata["pre_delivery_rework_count"], 1
        )
        feedback = await self.store.get_execution_checkpoints(
            project_id="project::pre-delivery",
            checkpoint_types=["company_delivery_feedback"],
        )
        self.assertEqual(feedback, [])

    async def test_live_final_publication_rechecks_validation_after_prepare(
        self,
    ) -> None:
        async def validator(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {"quality_gate": "passed"},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, _unused_executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validator)
        )
        prepared_calls = 0

        async def prepare(data: dict) -> PreparedOwnerInteractionPublication:
            nonlocal prepared_calls
            prepared_calls += 1
            # Win the check->publish race after validation was checked but
            # before the final authoritative command starts its transaction.
            await self.store.update_delegation_work_item(
                "wid::investment_delivery",
                metadata_updates={"concurrent_prefinal_marker": "won"},
            )
            interaction_key = "delivery:attempt-1"
            supersession_key = (
                "owner:project::pre-delivery:session::pre-delivery"
            )
            payload = dict(data["payload"])
            payload["interaction"] = {
                "domain_key": interaction_key,
                "supersession_key": supersession_key,
                "supersession_order": [1, 0],
            }
            return PreparedOwnerInteractionPublication(
                checkpoint=ExecutionCheckpoint(
                    checkpoint_id="checkpoint::prefinal-race",
                    project_id=data["project_id"],
                    session_id=data["session_id"],
                    checkpoint_type=data["checkpoint_type"],
                    task_id=data["task_id"],
                    payload=payload,
                ),
                interaction_key=interaction_key,
                supersession_key=supersession_key,
                supersession_order=(1, 0),
            )

        notify = AsyncMock()
        executor = self._executor(
            validator=validator,
            checkpoint_prepare_callback=prepare,
            checkpoint_notify_callback=notify,
        )
        executor._active_plan = plan
        executor._active_tasks = tasks
        package = {"delivered_items": [{"attempt": 1}]}
        tasks[-1].context_snapshot["delivery_package"] = package
        tasks[-1].context_snapshot["work_item_owned_outputs"][
            "delivery_package"
        ] = package
        self.assertTrue(
            await executor._apply_pre_delivery_validator(
                tasks[-1], plan, tasks, package
            )
        )
        tasks[-1].metadata["ceo_pre_delivery_assessment"] = {
            "deliverable": True,
            "summary": "ready",
        }
        with self.assertRaises(CompanyRunControllerLeaseLost) as raised:
            await executor._commit_final_delivery_owner_handoff(
                tasks[-1],
                summary="ready",
                progress_message="ready",
            )
        self.assertEqual(prepared_calls, 1, str(raised.exception))
        notify.assert_not_awaited()
        checkpoints = await self.store.get_execution_checkpoints(
            project_id="project::pre-delivery",
            checkpoint_types=["company_delivery_feedback"],
        )
        self.assertEqual(checkpoints, [])
        run = await self.store.get_delegation_run("run::pre-delivery")
        delivery = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        assert run is not None and delivery is not None
        self.assertEqual(run.lifecycle_status, "active")
        self.assertEqual(delivery.phase, Phase.AWAITING_HUMAN)
        self.assertEqual(
            delivery.metadata["concurrent_prefinal_marker"], "won"
        )

    async def test_live_final_rejects_generic_card_occupying_domain_key(
        self,
    ) -> None:
        async def validator(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {"quality_gate": "passed"},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, _unused_executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validator)
        )
        interaction_key = "delivery:occupied-before-final"
        supersession_key = (
            "owner:project::pre-delivery:session::pre-delivery"
        )

        async def prepare(data: dict) -> PreparedOwnerInteractionPublication:
            ownership = {
                "waiting_task_id": data["task_id"],
                "waiting_session_id": data["session_id"],
                "root_session_id": "session::pre-delivery",
                "ui_anchor_session_id": "session::pre-delivery",
            }
            generic_payload = dict(data["payload"])
            generic_payload["interaction"] = {
                "kind": "company_delivery_feedback",
                "prompt": generic_payload["prompt"],
                "domain_key": interaction_key,
                "supersession_key": supersession_key,
                "supersession_order": [1, 0],
                "ownership": ownership,
            }
            await self.store.publish_owner_interaction_checkpoint(
                ExecutionCheckpoint(
                    checkpoint_id="checkpoint::generic-domain-occupant",
                    project_id=data["project_id"],
                    session_id=data["session_id"],
                    checkpoint_type=data["checkpoint_type"],
                    task_id=data["task_id"],
                    payload=generic_payload,
                ),
                interaction_key=interaction_key,
                supersession_key=supersession_key,
                supersession_order=(1, 0),
            )
            final_payload = dict(data["payload"])
            final_payload["interaction"] = {
                "kind": "company_delivery_feedback",
                "prompt": final_payload["prompt"],
                "domain_key": interaction_key,
                "supersession_key": supersession_key,
                "supersession_order": [1, 0],
                "ownership": ownership,
            }
            return PreparedOwnerInteractionPublication(
                checkpoint=ExecutionCheckpoint(
                    checkpoint_id="checkpoint::authoritative-final",
                    project_id=data["project_id"],
                    session_id=data["session_id"],
                    checkpoint_type=data["checkpoint_type"],
                    task_id=data["task_id"],
                    payload=final_payload,
                ),
                interaction_key=interaction_key,
                supersession_key=supersession_key,
                supersession_order=(1, 0),
            )

        notify = AsyncMock()
        executor = self._executor(
            validator=validator,
            checkpoint_prepare_callback=prepare,
            checkpoint_notify_callback=notify,
        )
        executor._active_plan = plan
        executor._active_tasks = tasks
        package = {"delivered_items": [{"attempt": 1}]}
        tasks[-1].context_snapshot["delivery_package"] = package
        tasks[-1].context_snapshot["work_item_owned_outputs"][
            "delivery_package"
        ] = package
        self.assertTrue(
            await executor._apply_pre_delivery_validator(
                tasks[-1], plan, tasks, package
            )
        )
        tasks[-1].metadata["ceo_pre_delivery_assessment"] = {
            "deliverable": True,
            "summary": "ready",
        }

        with self.assertRaisesRegex(
            RuntimeError,
            "conflicts with the authoritative publication basis",
        ):
            await executor._commit_final_delivery_owner_handoff(
                tasks[-1],
                summary="ready",
                progress_message="ready",
            )

        notify.assert_not_awaited()
        run = await self.store.get_delegation_run("run::pre-delivery")
        delivery = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        checkpoints = await self.store.get_execution_checkpoints(
            project_id="project::pre-delivery",
            checkpoint_types=["company_delivery_feedback"],
        )
        assert run is not None and delivery is not None
        self.assertEqual(run.lifecycle_status, "active")
        self.assertEqual(delivery.phase, Phase.AWAITING_HUMAN)
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(
            checkpoints[0].checkpoint_id,
            "checkpoint::generic-domain-occupant",
        )
        self.assertNotIn(
            "company_final_delivery_publication",
            checkpoints[0].payload,
        )

    async def test_live_final_postcommit_takeover_notification_is_advisory(
        self,
    ) -> None:
        async def validator(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {"quality_gate": "passed"},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, _unused_executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validator)
        )

        async def prepare(data: dict) -> PreparedOwnerInteractionPublication:
            interaction_key = "delivery:postcommit-notify"
            supersession_key = (
                "owner:project::pre-delivery:session::pre-delivery"
            )
            payload = dict(data["payload"])
            payload["interaction"] = {
                "kind": "company_delivery_feedback",
                "prompt": payload["prompt"],
                "domain_key": interaction_key,
                "supersession_key": supersession_key,
                "supersession_order": [1, 0],
                "ownership": {
                    "waiting_task_id": data["task_id"],
                    "waiting_session_id": data["session_id"],
                    "root_session_id": "session::pre-delivery",
                    "ui_anchor_session_id": "session::pre-delivery",
                },
            }
            return PreparedOwnerInteractionPublication(
                checkpoint=ExecutionCheckpoint(
                    checkpoint_id="checkpoint::postcommit-notify",
                    project_id=data["project_id"],
                    session_id=data["session_id"],
                    checkpoint_type=data["checkpoint_type"],
                    task_id=data["task_id"],
                    payload=payload,
                ),
                interaction_key=interaction_key,
                supersession_key=supersession_key,
                supersession_order=(1, 0),
            )

        notify = AsyncMock(
            side_effect=CompanyRunControllerLeaseLost(
                "takeover after final commit"
            )
        )
        executor = self._executor(
            validator=validator,
            checkpoint_prepare_callback=prepare,
            checkpoint_notify_callback=notify,
        )
        executor._active_plan = plan
        executor._active_tasks = tasks
        package = {"delivered_items": [{"attempt": 1}]}
        tasks[-1].context_snapshot["delivery_package"] = package
        tasks[-1].context_snapshot["work_item_owned_outputs"][
            "delivery_package"
        ] = package
        self.assertTrue(
            await executor._apply_pre_delivery_validator(
                tasks[-1], plan, tasks, package
            )
        )
        tasks[-1].metadata["ceo_pre_delivery_assessment"] = {
            "deliverable": True,
            "summary": "ready",
        }
        self.assertTrue(
            await executor._commit_final_delivery_owner_handoff(
                tasks[-1],
                summary="ready",
                progress_message="ready",
            )
        )
        notify.assert_awaited_once()
        checkpoints = await self.store.get_execution_checkpoints(
            project_id="project::pre-delivery",
            checkpoint_types=["company_delivery_feedback"],
            statuses=["pending"],
        )
        run = await self.store.get_delegation_run("run::pre-delivery")
        assert run is not None
        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(run.lifecycle_status, "awaiting_owner")

    async def test_finalize_postcommit_progress_failure_cannot_poison_final(
        self,
    ) -> None:
        async def validator(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {"quality_gate": "passed"},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        plan, tasks, _unused_executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validator)
        )

        async def prepare(data: dict) -> PreparedOwnerInteractionPublication:
            interaction_key = "delivery:finalize-progress"
            supersession_key = (
                "owner:project::pre-delivery:session::pre-delivery"
            )
            payload = dict(data["payload"])
            payload["interaction"] = {
                "kind": "company_delivery_feedback",
                "prompt": payload["prompt"],
                "domain_key": interaction_key,
                "supersession_key": supersession_key,
                "supersession_order": [1, 0],
                "ownership": {
                    "waiting_task_id": data["task_id"],
                    "waiting_session_id": data["session_id"],
                    "root_session_id": "session::pre-delivery",
                    "ui_anchor_session_id": "session::pre-delivery",
                },
            }
            return PreparedOwnerInteractionPublication(
                checkpoint=ExecutionCheckpoint(
                    checkpoint_id="checkpoint::finalize-progress",
                    project_id=data["project_id"],
                    session_id=data["session_id"],
                    checkpoint_type=data["checkpoint_type"],
                    task_id=data["task_id"],
                    payload=payload,
                ),
                interaction_key=interaction_key,
                supersession_key=supersession_key,
                supersession_order=(1, 0),
            )

        executor = self._executor(
            validator=validator,
            checkpoint_prepare_callback=prepare,
            checkpoint_notify_callback=AsyncMock(),
        )
        executor._active_plan = plan
        executor._active_tasks = tasks
        executor._ceo_pre_delivery_assessment = AsyncMock(
            return_value={
                "deliverable": True,
                "summary": "ready",
                "rework_targets": [],
            }
        )
        executor._emit_progress = AsyncMock(
            side_effect=RuntimeError("progress backend unavailable")
        )
        await executor._finalize_completed_work_item(tasks[-1])
        delivery_task = await self.store.get_task(tasks[-1].id)
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        run = await self.store.get_delegation_run("run::pre-delivery")
        checkpoints = await self.store.get_execution_checkpoints(
            project_id="project::pre-delivery",
            checkpoint_types=["company_delivery_feedback"],
            statuses=["pending"],
        )
        assert (
            delivery_task is not None
            and delivery_item is not None
            and run is not None
        )
        self.assertEqual(delivery_task.status, TaskStatus.AWAITING_HUMAN)
        self.assertEqual(delivery_item.phase, Phase.AWAITING_HUMAN)
        self.assertEqual(run.lifecycle_status, "awaiting_owner")
        self.assertEqual(len(checkpoints), 1)
        self.assertNotIn("crash_quarantine", delivery_task.metadata)
        self.assertNotIn("dispatch_hold", delivery_task.metadata)

    @staticmethod
    async def _append_async(target: list, value) -> None:
        target.append(value)

    async def test_finalize_replaces_stale_delivery_instance_for_atomic_risk_rework(
        self,
    ) -> None:
        current_delivery: Task | None = None

        async def validate(
            delivery_task: Task,
            _plan: CompanyWorkItemRuntimePlan,
            callback_tasks: list[Task],
            _package: dict,
        ) -> dict:
            assert current_delivery is not None
            self.assertIs(delivery_task, current_delivery)
            delivery_entries = [
                candidate
                for candidate in callback_tasks
                if self._projection_id_for_test_task(candidate)
                == "investment_delivery"
            ]
            self.assertEqual(delivery_entries, [current_delivery])
            return {
                "valid": False,
                "evidence": {"failed_artifact": "risk_analysis.json"},
                "issues": ["Risk analysis failed deterministic quality checks."],
                "rework_target_projection_ids": ["risk_analysis"],
            }

        plan, tasks, executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validate)
        )
        stale_delivery = tasks[-1]
        current_delivery = copy.deepcopy(stale_delivery)
        self.assertIsNot(current_delivery, stale_delivery)
        executor._active_tasks = [*tasks[:-1], stale_delivery]
        assessment = AsyncMock()
        executor._ceo_pre_delivery_assessment = assessment

        await executor._finalize_completed_work_item(current_delivery)

        assessment.assert_not_awaited()
        self.assertIs(executor._active_tasks[-1], current_delivery)
        company_item = await self.store.get_delegation_work_item(
            "wid::company_analysis"
        )
        risk_item = await self.store.get_delegation_work_item(
            "wid::risk_analysis"
        )
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        durable_risk = await self.store.get_task(tasks[1].id)
        durable_delivery = await self.store.get_task(current_delivery.id)
        run = await self.store.get_delegation_run("run::pre-delivery")
        assert (
            company_item is not None
            and risk_item is not None
            and delivery_item is not None
            and durable_risk is not None
            and durable_delivery is not None
            and run is not None
        )
        self.assertEqual(company_item.phase, Phase.APPROVED)
        self.assertEqual(risk_item.phase, Phase.READY_FOR_REWORK)
        self.assertEqual(delivery_item.phase, Phase.READY_FOR_REWORK)
        self.assertEqual(durable_risk.status, TaskStatus.PENDING)
        self.assertEqual(durable_delivery.status, TaskStatus.PENDING)
        self.assertEqual(run.status, "running")
        self.assertEqual(run.lifecycle_status, "active")
        self.assertEqual(
            durable_delivery.metadata["pre_delivery_validation"]["status"],
            "quality_failed",
        )
        self.assertNotIn(
            "pre_delivery_validation_failure_kind",
            durable_delivery.metadata,
        )
        checkpoints = await self.store.get_execution_checkpoints(
            project_id="project::pre-delivery",
            checkpoint_types=["company_delivery_feedback"],
            statuses=["pending"],
        )
        self.assertEqual(checkpoints, [])

    async def test_finalize_fails_closed_on_conflicting_delivery_task_identity(
        self,
    ) -> None:
        validator = AsyncMock(
            return_value={
                "valid": True,
                "evidence": {"quality_gate": "passed"},
                "issues": [],
                "rework_target_projection_ids": [],
            }
        )
        plan, tasks, executor, _generation = (
            await self._prepare_live_invalid_tree(validator=validator)
        )
        current_delivery = tasks[-1]
        projection_impostor = copy.deepcopy(current_delivery)
        projection_impostor.id = "task::investment_delivery_impostor"
        executor._active_plan = plan
        executor._active_tasks = [*tasks[:-1], projection_impostor]
        assessment = AsyncMock()
        executor._ceo_pre_delivery_assessment = assessment

        await executor._finalize_completed_work_item(current_delivery)

        validator.assert_not_awaited()
        assessment.assert_not_awaited()
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        durable_delivery = await self.store.get_task(current_delivery.id)
        run = await self.store.get_delegation_run("run::pre-delivery")
        assert delivery_item is not None and durable_delivery is not None
        assert run is not None
        self.assertEqual(delivery_item.phase, Phase.FAILED)
        self.assertEqual(durable_delivery.status, TaskStatus.FAILED)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.lifecycle_status, "closed_failed")
        self.assertEqual(
            durable_delivery.metadata[
                "pre_delivery_validation_failure_kind"
            ],
            "conflicting_delivery_projection_identity",
        )

    async def test_non_controller_rework_keeps_each_target_feedback_scoped(
        self,
    ) -> None:
        plan, tasks = await self._seed_delivery_tree()
        company_issue = "company_analysis.json has an invalid claim"
        report_issue = "report.md has an invalid verified-facts heading"

        async def validate(*_args) -> dict:
            return {
                "valid": False,
                "evidence": {"quality_gate": "failed"},
                "issues": [company_issue, report_issue],
                "rework_target_projection_ids": [
                    "company_analysis",
                    "investment_delivery",
                ],
                "rework_issues_by_projection_id": {
                    "company_analysis": [company_issue],
                    "investment_delivery": [report_issue],
                },
            }

        executor = self._executor(
            validator=validate,
            checkpoint_callback=AsyncMock(),
        )
        executor._ceo_pre_delivery_assessment = AsyncMock()

        await self._run_delivery(executor, plan, tasks)

        durable_company = await self.store.get_task(tasks[0].id)
        durable_risk = await self.store.get_task(tasks[1].id)
        durable_delivery = await self.store.get_task(tasks[2].id)
        assert durable_company is not None
        assert durable_risk is not None
        assert durable_delivery is not None
        company_request = dict(
            durable_company.metadata["gate_harness_rework_request"]
        )
        delivery_request = dict(
            durable_delivery.metadata["gate_harness_rework_request"]
        )
        self.assertEqual(company_request["blockers"], [company_issue])
        self.assertNotIn(report_issue, company_request["feedback"])
        self.assertNotIn(
            "Upstream deterministic quality rework invalidated",
            company_request["feedback"],
        )
        self.assertEqual(delivery_request["blockers"], [report_issue])
        self.assertNotIn(company_issue, delivery_request["feedback"])
        self.assertIn(
            "Upstream deterministic quality rework invalidated",
            delivery_request["feedback"],
        )
        self.assertIn(
            "re-read the latest corrected dependency outputs",
            delivery_request["feedback"],
        )
        self.assertIn(
            "rebuild and re-verify only the delivery-owned output",
            delivery_request["feedback"],
        )
        self.assertEqual(
            delivery_request["invalidated_by_projection_ids"],
            ["company_analysis"],
        )
        self.assertEqual(durable_risk.status, TaskStatus.DONE)

    async def test_delivery_only_rework_does_not_invent_upstream_invalidation(
        self,
    ) -> None:
        plan, tasks = await self._seed_delivery_tree()
        report_issue = "report.md has an invalid verified-facts heading"

        async def validate(*_args) -> dict:
            return {
                "valid": False,
                "evidence": {"quality_gate": "failed"},
                "issues": [report_issue],
                "rework_target_projection_ids": ["investment_delivery"],
                "rework_issues_by_projection_id": {
                    "investment_delivery": [report_issue],
                },
            }

        executor = self._executor(
            validator=validate,
            checkpoint_callback=AsyncMock(),
        )
        executor._ceo_pre_delivery_assessment = AsyncMock()

        await self._run_delivery(executor, plan, tasks)

        durable_delivery = await self.store.get_task(tasks[-1].id)
        assert durable_delivery is not None
        request = dict(
            durable_delivery.metadata["gate_harness_rework_request"]
        )
        self.assertEqual(request["blockers"], [report_issue])
        self.assertEqual(
            request["feedback"],
            f"Deterministic pre-delivery validation failed: {report_issue}",
        )
        self.assertNotIn("invalidated_by_projection_ids", request)
        self.assertNotIn(
            "Upstream deterministic quality rework invalidated",
            request["feedback"],
        )

    async def test_invalid_two_targets_reopens_children_and_delivery_once(self) -> None:
        plan, tasks = await self._seed_delivery_tree()

        async def validate(*_args) -> dict:
            return {
                "valid": False,
                "evidence": {"failed_artifacts": ["company_analysis.json", "risk_analysis.json"]},
                "issues": ["Both analyst notes failed deterministic quality checks."],
                "rework_target_projection_ids": ["company_analysis", "risk_analysis"],
                "rework_issues_by_projection_id": {
                    "company_analysis": [
                        "Both analyst notes failed deterministic quality checks."
                    ],
                    "risk_analysis": [
                        "Both analyst notes failed deterministic quality checks."
                    ],
                },
            }

        checkpoint = AsyncMock()
        executor = self._executor(
            validator=validate,
            checkpoint_callback=checkpoint,
        )
        assessment = AsyncMock()
        executor._ceo_pre_delivery_assessment = assessment

        await self._run_delivery(executor, plan, tasks)

        checkpoint.assert_not_awaited()
        assessment.assert_not_awaited()
        for projection_id, task in zip(
            ("company_analysis", "risk_analysis", "investment_delivery"),
            tasks,
            strict=True,
        ):
            item = await self.store.get_delegation_work_item(f"wid::{projection_id}")
            durable_task = await self.store.get_task(task.id)
            assert item is not None and durable_task is not None
            self.assertEqual(item.phase, Phase.READY_FOR_REWORK)
            self.assertEqual(task.status, TaskStatus.PENDING)
            self.assertEqual(durable_task.status, TaskStatus.PENDING)
            self.assertIsNone(task.result)
            self.assertEqual(task.metadata.get("gate_harness_rework_count"), 1)
            self.assertNotIn("work_item_summary", item.metadata)
            self.assertEqual(item.claimed_by_role_runtime_session_id, "")
            self.assertEqual(item.claimed_by_seat_id, "")
            if projection_id == "investment_delivery":
                self.assertEqual(
                    item.metadata["pre_delivery_validation"]["status"],
                    "quality_failed",
                )
                self.assertEqual(
                    durable_task.metadata["pre_delivery_validation_evidence"],
                    item.metadata["pre_delivery_validation_evidence"],
                )

        restarted_store = OPCStore(self.root / "tasks.db")
        await restarted_store.initialize(run_startup_maintenance=False)
        try:
            restarted_delivery = await restarted_store.get_delegation_work_item(
                "wid::investment_delivery"
            )
            restarted_task = await restarted_store.get_task(tasks[-1].id)
            assert restarted_delivery is not None and restarted_task is not None
            self.assertEqual(
                restarted_delivery.metadata["pre_delivery_validation_history"][-1][
                    "status"
                ],
                "quality_failed",
            )
            self.assertEqual(
                restarted_task.metadata["pre_delivery_validation"]["status"],
                "quality_failed",
            )
        finally:
            await restarted_store.close()

        lease = await self.store.acquire_delegation_run_controller_lease(
            "run::pre-delivery",
            project_id="project::pre-delivery",
            root_session_id="session::pre-delivery",
            owner_token="controller::retry",
            lease_seconds=60,
        )
        self.assertTrue(lease.acquired)
        claimed = await self.store.claim_delegation_work_item_if_dispatchable(
            "wid::company_analysis",
            expected_phase=Phase.READY_FOR_REWORK,
            role_runtime_session_id="role-session::retry",
            seat_id="seat::investment_analyst",
            task_id=tasks[0].id,
            controller_owner_token="controller::retry",
            controller_lease_generation=lease.generation,
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.phase, Phase.RUNNING)
        self.assertFalse(claimed.metadata["attempt_settled"])
        self.assertEqual(claimed.metadata["attempt_seq"], 2)

    async def test_validator_exception_fails_delivery_without_child_rework_or_final(self) -> None:
        plan, tasks = await self._seed_delivery_tree()

        async def validate(*_args) -> dict:
            raise RuntimeError("ledger unavailable")

        checkpoint = AsyncMock()
        executor = self._executor(
            validator=validate,
            checkpoint_callback=checkpoint,
        )
        assessment = AsyncMock()
        executor._ceo_pre_delivery_assessment = assessment

        await self._run_delivery(executor, plan, tasks)

        checkpoint.assert_not_awaited()
        assessment.assert_not_awaited()
        for projection_id in ("company_analysis", "risk_analysis"):
            item = await self.store.get_delegation_work_item(f"wid::{projection_id}")
            assert item is not None
            self.assertEqual(item.phase, Phase.APPROVED)
        delivery_item = await self.store.get_delegation_work_item(
            "wid::investment_delivery"
        )
        durable_delivery = await self.store.get_task(tasks[-1].id)
        assert delivery_item is not None and durable_delivery is not None
        self.assertEqual(delivery_item.phase, Phase.FAILED)
        self.assertEqual(durable_delivery.status, TaskStatus.FAILED)
        self.assertEqual(
            durable_delivery.metadata["pre_delivery_validation"]["status"],
            "infrastructure_failure",
        )
        self.assertNotIn("gate_harness_rework_count", tasks[0].metadata)
        self.assertNotIn("gate_harness_rework_count", tasks[1].metadata)

    async def test_engine_default_and_custom_child_inherit_validator(self) -> None:
        async def validator(*_args) -> dict:
            return {
                "valid": True,
                "evidence": {},
                "issues": [],
                "rework_target_projection_ids": [],
            }

        default_engine = OPCEngine(opc_home=self.root)
        self.assertIsNone(default_engine.pre_delivery_validator)
        parent = SimpleNamespace(
            opc_home=self.root,
            config=SimpleNamespace(),
            project_id="project::parent",
            store=self.store,
            _active_task_run_registry=None,
            on_progress=None,
            on_runtime_event=None,
            interaction_coordinator=None,
            on_company_runtime_children=None,
            on_company_kanban_callback_factory=None,
            company_executor=SimpleNamespace(on_kanban_changed=None),
            pre_delivery_validator=validator,
            _register_runtime_child=lambda _runtime: None,
            _unregister_runtime_child=lambda _runtime, _task: None,
        )
        runner = CustomRuntimeRunner(parent)
        runner._build_org_config = lambda _org_id: (
            SimpleNamespace(org=SimpleNamespace(organization_id="org::test")),
            "org::test",
        )

        class FakeEngine:
            def __init__(self, **kwargs) -> None:
                self.pre_delivery_validator = kwargs.get("pre_delivery_validator")
                self.company_executor = None

            async def initialize(self) -> None:
                self.company_executor = SimpleNamespace(
                    pre_delivery_validator=self.pre_delivery_validator,
                    on_kanban_changed=None,
                )

            async def shutdown(self) -> None:
                return None

        with patch("opc.engine.OPCEngine", FakeEngine):
            root_engine = OPCEngine(
                opc_home=self.root,
                project_id="project::root",
                pre_delivery_validator=validator,
            )
            delegate = await root_engine._get_project_delegate(
                "project::delegate"
            )
            runtime, org_id = await runner._create_runtime(
                project_id="project::parent",
                org_id="org::test",
            )

        self.assertEqual(org_id, "org::test")
        self.assertIs(delegate.pre_delivery_validator, validator)
        self.assertIs(runtime.pre_delivery_validator, validator)
        self.assertIs(runtime.company_executor.pre_delivery_validator, validator)


if __name__ == "__main__":
    unittest.main()
