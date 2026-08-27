from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opc.core.config import OPCConfig, RoleConfig, SeatConfig, TeamConfig
from opc.core.events import EventBus
from opc.core.models import CompanyMemberSession, DelegationWorkItem, Phase, SeatState, Task, TaskResult, TaskStatus
from opc.database.store import OPCStore
from opc.layer2_organization.communication import CommunicationManager
from opc.layer2_organization.phase import DONE_PHASES
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
from opc.layer2_organization.company_runtime import CompanyRuntime
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.org_work_item_planner import WorkItemGatePolicy
from opc.layer2_organization.work_item_runtime import (
    WORK_ITEM_RUNTIME_KEY,
    WORK_ITEM_RUNTIME_VERSION_KEY,
    is_work_item_runtime_metadata,
    mark_work_item_runtime,
    migrate_work_item_runtime_metadata,
    work_item_runtime_version,
)
from opc.layer2_organization.work_item_identity import (
    GATE_REWORK_PROJECTION_ID_KEY,
    GATE_TARGET_PROJECTION_ID_KEY,
    WORK_ITEM_PROJECTION_ID_KEY,
    WORK_ITEM_TURN_TYPE_KEY,
    canonical_work_item_turn_type_for_kind,
    company_work_item_gate_basis_hash,
    gate_rework_payload,
    mark_gate_rework_projection,
    mark_projected_work_item_task,
    mark_work_item_projection,
    migrate_work_item_projection_metadata,
    projection_id_for_work_item,
    rework_projection_id_for_gate,
    result_delivery_identity_payload_for_task,
    target_projection_id_for_decision,
    target_projection_ids_for_decision,
    work_item_identity_payload,
    work_item_identity_payload_for_task,
    work_item_identity_payload_from_metadata,
    work_item_projection_id_from_metadata,
    work_item_turn_type_from_metadata,
)
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task, set_linked_work_item_id


class ActorRuntimeOrgEngineTests(unittest.TestCase):
    def test_configured_teams_preserve_multiple_seats_for_middle_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = OPCConfig()
            config.org.company_profile = "custom"
            config.org.final_decider_role_id = "ceo"
            config.org.roles = [
                RoleConfig(id="ceo", name="CEO", responsibility="Set direction.", reports_to="owner"),
                RoleConfig(id="cto", name="CTO", responsibility="Lead engineering.", reports_to="ceo"),
                RoleConfig(id="engineer", name="Engineer", responsibility="Implement the work.", reports_to="cto"),
            ]
            config.org.teams = [
                TeamConfig(
                    team_id="team::ceo",
                    seats=[
                        SeatConfig(seat_id="seat::team::ceo::ceo", role_id="ceo", seat_kind="lead"),
                        SeatConfig(seat_id="seat::team::ceo::cto", role_id="cto"),
                    ],
                ),
                TeamConfig(
                    team_id="team::cto",
                    metadata={"parent_team_id": "team::ceo"},
                    seats=[
                        SeatConfig(
                            seat_id="seat::team::cto::cto",
                            role_id="cto",
                            seat_kind="lead",
                            manager_role_id="ceo",
                            manager_seat_id="seat::team::ceo::cto",
                        ),
                        SeatConfig(seat_id="seat::team::cto::engineer", role_id="engineer"),
                    ],
                ),
            ]

            engine = OrgEngine(config, Path(tmpdir))
            topology = engine.build_runtime_delegation_topology()
            seats_by_id = {seat["seat_id"]: seat for seat in topology["seats"]}

            self.assertEqual(engine.get_execution_model(), "actor_runtime")
            self.assertIn("seat::team::ceo::cto", seats_by_id)
            self.assertIn("seat::team::cto::cto", seats_by_id)
            self.assertEqual(seats_by_id["seat::team::ceo::cto"]["manager_role_id"], "ceo")
            self.assertEqual(seats_by_id["seat::team::cto::cto"]["manager_seat_id"], "seat::team::ceo::cto")
            self.assertEqual(seats_by_id["seat::team::ceo::cto"]["managed_team_id"], "team::cto")
            self.assertTrue(seats_by_id["seat::team::ceo::cto"]["metadata"]["configured_seat"])

    def test_execution_model_comes_from_active_organization_config(self) -> None:
        config = OPCConfig()
        config.org.execution_model = "configured_actor_runtime"

        engine = OrgEngine(config)

        self.assertEqual(engine.get_execution_model(), "configured_actor_runtime")


class WorkItemRuntimeMetadataTests(unittest.TestCase):
    def test_work_item_runtime_marker_reads_only_new_field(self) -> None:
        self.assertTrue(is_work_item_runtime_metadata({WORK_ITEM_RUNTIME_KEY: True}))
        self.assertFalse(is_work_item_runtime_metadata({}))

    def test_mark_work_item_runtime_writes_new_fields_only_by_default(self) -> None:
        metadata = mark_work_item_runtime({"kept": "value"}, version=3)
        self.assertTrue(metadata[WORK_ITEM_RUNTIME_KEY])
        self.assertEqual(metadata[WORK_ITEM_RUNTIME_VERSION_KEY], 3)
        self.assertEqual(metadata["kept"], "value")
        self.assertEqual(work_item_runtime_version(metadata), 3)

    def test_migrate_work_item_runtime_metadata_normalizes_new_marker_version(self) -> None:
        migrated, changed = migrate_work_item_runtime_metadata(
            {
                WORK_ITEM_RUNTIME_KEY: True,
                "kept": "value",
            },
            default_version=4,
        )

        self.assertTrue(changed)
        self.assertTrue(migrated[WORK_ITEM_RUNTIME_KEY])
        self.assertEqual(migrated[WORK_ITEM_RUNTIME_VERSION_KEY], 4)
        self.assertEqual(migrated["kept"], "value")


class WorkItemProjectionIdentityTests(unittest.TestCase):
    def test_projection_identity_reads_new_fields_only(self) -> None:
        metadata = {
            WORK_ITEM_PROJECTION_ID_KEY: "new-projection",
            WORK_ITEM_TURN_TYPE_KEY: "review",
        }

        self.assertEqual(work_item_projection_id_from_metadata(metadata), "new-projection")
        self.assertEqual(work_item_turn_type_from_metadata(metadata), "review")
        self.assertEqual(work_item_projection_id_from_metadata({}, fallback="fallback-projection"), "fallback-projection")
        self.assertEqual(work_item_turn_type_from_metadata({}, fallback="execute"), "execute")

    def test_projection_turn_type_normalizes_delivery_alias(self) -> None:
        self.assertEqual(canonical_work_item_turn_type_for_kind("delivery"), "deliver")
        self.assertEqual(canonical_work_item_turn_type_for_kind("self-evolution"), "self_evolution")
        self.assertEqual(canonical_work_item_turn_type_for_kind("self evolution"), "self_evolution")
        self.assertEqual(
            work_item_turn_type_from_metadata({"work_kind": "delivery"}),
            "deliver",
        )
        self.assertEqual(
            work_item_turn_type_from_metadata({"work_kind": "self-evolution"}),
            "self_evolution",
        )
        self.assertEqual(
            work_item_turn_type_from_metadata({WORK_ITEM_TURN_TYPE_KEY: "delivery"}),
            "deliver",
        )
        self.assertEqual(
            mark_work_item_projection({}, projection_id="proj-1", turn_type="delivery")[WORK_ITEM_TURN_TYPE_KEY],
            "deliver",
        )

    def test_mark_work_item_projection_writes_new_fields_only(self) -> None:
        metadata = mark_work_item_projection({"kept": "value"}, projection_id="proj-1", turn_type="deliver")

        self.assertEqual(metadata[WORK_ITEM_PROJECTION_ID_KEY], "proj-1")
        self.assertEqual(metadata[WORK_ITEM_TURN_TYPE_KEY], "deliver")
        self.assertEqual(metadata["kept"], "value")

    def test_mark_projected_work_item_task_writes_new_fields_only(self) -> None:
        metadata = mark_projected_work_item_task(
            {
                "kept": "value",
            },
            projection_id="proj-task",
            turn_type="execute",
        )

        self.assertEqual(metadata[WORK_ITEM_PROJECTION_ID_KEY], "proj-task")
        self.assertEqual(metadata[WORK_ITEM_TURN_TYPE_KEY], "execute")
        self.assertEqual(metadata["kept"], "value")

    def test_work_item_identity_payload_outputs_new_fields_only(self) -> None:
        payload = work_item_identity_payload(
            projection_id="proj-payload",
            turn_type="Deliver",
            source={
                WORK_ITEM_PROJECTION_ID_KEY: "source-projection",
                WORK_ITEM_TURN_TYPE_KEY: "review",
            },
        )

        self.assertEqual(payload, {
            WORK_ITEM_PROJECTION_ID_KEY: "proj-payload",
            WORK_ITEM_TURN_TYPE_KEY: "deliver",
        })

    def test_work_item_identity_payload_uses_explicit_fallbacks_without_identity(self) -> None:
        payload = work_item_identity_payload_from_metadata(
            {
                "unrelated": "value",
            },
            projection_id_fallback="fallback-projection",
            turn_type_fallback="execute",
        )

        self.assertEqual(payload[WORK_ITEM_PROJECTION_ID_KEY], "fallback-projection")
        self.assertEqual(payload[WORK_ITEM_TURN_TYPE_KEY], "execute")

    def test_work_item_identity_payload_for_task_reads_projection_helpers(self) -> None:
        task = SimpleNamespace(
            id="task-1",
            metadata={
                WORK_ITEM_PROJECTION_ID_KEY: "task-proj",
                WORK_ITEM_TURN_TYPE_KEY: "report",
            },
        )

        payload = work_item_identity_payload_for_task(task)

        self.assertEqual(payload[WORK_ITEM_PROJECTION_ID_KEY], "task-proj")
        self.assertEqual(payload[WORK_ITEM_TURN_TYPE_KEY], "report")

    def test_result_delivery_identity_uses_canonical_turn_and_retry_attempt(self) -> None:
        task = SimpleNamespace(
            id="task-1",
            retry_count=2,
            metadata={"runtime_v2_current_turn_id": "canonical-turn-1"},
        )

        payload = result_delivery_identity_payload_for_task(task)

        self.assertEqual(
            payload["result_delivery_id"],
            "result:task:task-1:turn:canonical-turn-1:attempt:2",
        )
        self.assertEqual(payload["source_task_id"], "task-1")
        self.assertEqual(payload["canonical_turn_id"], "canonical-turn-1")

    def test_result_delivery_identity_does_not_collide_for_parallel_tasks(self) -> None:
        first = SimpleNamespace(id="task-1", retry_count=0, metadata={})
        second = SimpleNamespace(id="task-2", retry_count=0, metadata={})

        first_payload = result_delivery_identity_payload_for_task(
            first,
            canonical_turn_id="shared-parent-turn",
        )
        second_payload = result_delivery_identity_payload_for_task(
            second,
            canonical_turn_id="shared-parent-turn",
        )

        self.assertNotEqual(
            first_payload["result_delivery_id"],
            second_payload["result_delivery_id"],
        )
        self.assertIn(":task-1:", first_payload["result_delivery_id"])
        self.assertIn(":task-2:", second_payload["result_delivery_id"])

    def test_result_delivery_identity_requires_an_execution_scope(self) -> None:
        task = SimpleNamespace(id="reused-task", retry_count=0, metadata={})

        self.assertEqual(result_delivery_identity_payload_for_task(task), {
            "source_task_id": "reused-task",
        })
        first = result_delivery_identity_payload_for_task(task, execution_id="execution-1")
        second = result_delivery_identity_payload_for_task(task, execution_id="execution-2")
        self.assertNotEqual(first["result_delivery_id"], second["result_delivery_id"])

    def test_migrate_projection_metadata_backfills_from_fallbacks_without_overwriting(self) -> None:
        migrated, changed = migrate_work_item_projection_metadata(
            {},
            projection_id_fallback="fallback-projection",
            turn_type_fallback="execute",
        )

        self.assertTrue(changed)
        self.assertEqual(migrated[WORK_ITEM_PROJECTION_ID_KEY], "fallback-projection")
        self.assertEqual(migrated[WORK_ITEM_TURN_TYPE_KEY], "execute")

        migrated_again, changed_again = migrate_work_item_projection_metadata(
            {
                WORK_ITEM_PROJECTION_ID_KEY: "new-projection",
                WORK_ITEM_TURN_TYPE_KEY: "review",
            }
        )
        self.assertFalse(changed_again)
        self.assertEqual(migrated_again[WORK_ITEM_PROJECTION_ID_KEY], "new-projection")
        self.assertEqual(migrated_again[WORK_ITEM_TURN_TYPE_KEY], "review")

    def test_projection_id_for_work_item_falls_back_to_projection_id(self) -> None:
        item = SimpleNamespace(metadata={}, projection_id="projection-column-value", work_item_id="wi-1", kind="execute")

        self.assertEqual(projection_id_for_work_item(item), "projection-column-value")

    def test_rework_projection_id_for_gate_prefers_new_metadata(self) -> None:
        gate = WorkItemGatePolicy(
            gate_type="review",
            rework_projection_id="field-projection",
            metadata={GATE_REWORK_PROJECTION_ID_KEY: "new-projection"},
        )

        self.assertEqual(rework_projection_id_for_gate(gate), "new-projection")
        self.assertEqual(
            rework_projection_id_for_gate(WorkItemGatePolicy(gate_type="review", rework_projection_id="field-projection")),
            "field-projection",
        )

    def test_mark_gate_rework_projection_syncs_new_metadata_and_field(self) -> None:
        gate = WorkItemGatePolicy(gate_type="review")

        marked = mark_gate_rework_projection(gate, "projection-target")

        self.assertIs(marked, gate)
        self.assertEqual(gate.metadata[GATE_REWORK_PROJECTION_ID_KEY], "projection-target")
        self.assertEqual(gate.rework_projection_id, "projection-target")

    def test_target_projection_helpers_read_projection_fields_only(self) -> None:
        decision = SimpleNamespace(
            target_projection_id="new-target",
            target_projection_ids=["new-target", "second-target"],
        )

        self.assertEqual(target_projection_id_for_decision(decision), "new-target")
        self.assertEqual(target_projection_ids_for_decision(decision), ["new-target", "second-target"])
        self.assertEqual(target_projection_id_for_decision(SimpleNamespace()), "")
        self.assertEqual(target_projection_ids_for_decision(SimpleNamespace()), [])

    def test_gate_rework_payload_defaults_to_projection_fields(self) -> None:
        payload = gate_rework_payload(
            review_projection_id="review-proj",
            target_projection_id="target-proj",
            rework_projection_id="rework-proj",
        )

        self.assertEqual(payload["review_projection_id"], "review-proj")
        self.assertEqual(payload[GATE_TARGET_PROJECTION_ID_KEY], "target-proj")
        self.assertEqual(payload[GATE_REWORK_PROJECTION_ID_KEY], "rework-proj")
        self.assertEqual(
            set(payload),
            {"review_projection_id", GATE_TARGET_PROJECTION_ID_KEY, GATE_REWORK_PROJECTION_ID_KEY},
        )

    def test_company_work_item_gate_basis_binds_policy_and_pending_decision(self) -> None:
        task = Task(
            id="gate-task",
            project_id="project-a",
            metadata=mark_work_item_projection(
                {
                    "claimed_work_item_attempt_seq": 3,
                    "gate_harness_pending_decision": {
                        "action": "await_user_decision",
                        "constraints": ["keep audit evidence"],
                        "blockers": ["owner confirmation required"],
                    },
                },
                projection_id="review-output",
                turn_type="review",
            ),
        )
        gate = {
            "type": "human_confirmation",
            "instructions": "Confirm the reviewed output.",
            "requires_human": True,
            "on_reject": "rework",
            "rework_projection_id": "draft-output",
            "max_retries": 1,
            "metadata": {
                "source": "gate_harness",
                "recommended_action": "await_user_decision",
                "constraints": ["keep audit evidence"],
                "blockers": ["owner confirmation required"],
            },
        }

        baseline = company_work_item_gate_basis_hash(task, gate)
        changed_gate = {
            **gate,
            "rework_projection_id": "different-output",
        }
        self.assertNotEqual(
            baseline,
            company_work_item_gate_basis_hash(task, changed_gate),
        )
        task.metadata["gate_harness_pending_decision"] = {
            **task.metadata["gate_harness_pending_decision"],
            "blockers": ["a different durable blocker"],
        }
        self.assertNotEqual(
            baseline,
            company_work_item_gate_basis_hash(task, gate),
        )

    def test_delegation_work_item_projection_field_is_canonical(self) -> None:
        item = DelegationWorkItem(
            work_item_id="wi-1",
            projection_id="column-projection",
            metadata={WORK_ITEM_PROJECTION_ID_KEY: "metadata-projection"},
        )

        self.assertEqual(item.projection_id, "column-projection")
        self.assertEqual(projection_id_for_work_item(item), "column-projection")

    def test_delegation_work_item_projection_field_no_longer_backfills_metadata(self) -> None:
        item = DelegationWorkItem(work_item_id="wi-1", projection_id="column-projection", metadata={})

        self.assertEqual(item.projection_id, "column-projection")
        item.projection_id = "new-projection"
        self.assertEqual(item.projection_id, "new-projection")
        self.assertNotIn(WORK_ITEM_PROJECTION_ID_KEY, item.metadata)

    def test_company_gate_metadata_prefers_rework_projection_id(self) -> None:
        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)

        gate = executor._gate_from_metadata({
            "type": "review",
            "rework_projection_id": "projection-target",
            "metadata": {},
        })

        self.assertIsNotNone(gate)
        assert gate is not None
        self.assertEqual(rework_projection_id_for_gate(gate), "projection-target")
        self.assertEqual(gate.metadata[GATE_REWORK_PROJECTION_ID_KEY], "projection-target")


class ActorRuntimeCompanyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_work_item_runtime_bootstrap_collapses_role_sessions_across_seats(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = OPCConfig()
            config.org.company_profile = "custom"
            config.org.final_decider_role_id = "ceo"
            config.org.roles = [
                RoleConfig(id="ceo", name="CEO", responsibility="Set direction.", reports_to="owner"),
                RoleConfig(id="cto", name="CTO", responsibility="Lead engineering.", reports_to="ceo"),
                RoleConfig(id="engineer", name="Engineer", responsibility="Implement the work.", reports_to="cto"),
            ]
            config.org.teams = [
                TeamConfig(
                    team_id="team::ceo",
                    seats=[
                        SeatConfig(seat_id="seat::team::ceo::ceo", role_id="ceo", seat_kind="lead"),
                        SeatConfig(seat_id="seat::team::ceo::cto", role_id="cto"),
                    ],
                ),
                TeamConfig(
                    team_id="team::cto",
                    metadata={"parent_team_id": "team::ceo"},
                    seats=[
                        SeatConfig(
                            seat_id="seat::team::cto::cto",
                            role_id="cto",
                            seat_kind="lead",
                            manager_role_id="ceo",
                            manager_seat_id="seat::team::ceo::cto",
                        ),
                        SeatConfig(seat_id="seat::team::cto::engineer", role_id="engineer"),
                    ],
                ),
            ]

            org_engine = OrgEngine(config, Path(tmpdir))
            topology = org_engine.build_runtime_delegation_topology()
            runtime = CompanyRuntime(org_engine=org_engine, communication=None, store=None)
            root_task = Task(
                id="root-task",
                title="Root intake",
                project_id="proj1",
                assigned_to="ceo",
                status=TaskStatus.PENDING,
                metadata={
                    "work_item_runtime": True,
                    "delegation_run_id": "run-1",
                    "runtime_topology": topology,
                    "delegation_seat_id": "seat::team::ceo::ceo",
                },
            )

            await runtime.bootstrap([root_task])

            # Role-instance model: CTO's two seats (subordinate-to-CEO
            # and leader-of-CTO-team) share ONE session. The role_session
            # lists both seats for org lookups.
            cto_sessions = [
                session
                for session in runtime.member_sessions.values()
                if session.role_id == "cto"
            ]
            self.assertEqual(len(cto_sessions), 1)
            self.assertEqual(
                cto_sessions[0].role_session_id,
                "role-runtime::run-1::cto",
            )
            cto_role_session = runtime.role_sessions["role-runtime::run-1::cto"]
            self.assertEqual(
                sorted(cto_role_session.seat_ids),
                [
                    "seat::team::ceo::cto",
                    "seat::team::cto::cto",
                ],
            )

    async def test_work_item_runtime_bootstrap_recovers_direct_reports_from_persisted_seat_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.company_profile = "custom"
            config.org.final_decider_role_id = "ceo"
            config.org.roles = [
                RoleConfig(id="ceo", name="CEO", responsibility="Set direction.", reports_to="owner"),
                RoleConfig(id="cto", name="CTO", responsibility="Lead engineering.", reports_to="ceo"),
            ]

            org_engine = OrgEngine(config, root)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            try:
                runtime = CompanyRuntime(org_engine=org_engine, communication=None, store=store)
                await store.save_delegation_seat_state(
                    SeatState(
                        seat_state_id="seat-state::run-1::seat::team::ceo::ceo",
                        team_instance_id="team-instance::run-1::team::ceo",
                        run_id="run-1",
                        project_id="proj1",
                        team_id="team::ceo",
                        seat_id="seat::team::ceo::ceo",
                        role_id="ceo",
                        metadata={
                            "managed_team_id": "team::ceo",
                            "allowed_delegate_role_ids": ["cto"],
                        },
                    )
                )
                await store.save_delegation_seat_state(
                    SeatState(
                        seat_state_id="seat-state::run-1::seat::team::ceo::cto",
                        team_instance_id="team-instance::run-1::team::ceo",
                        run_id="run-1",
                        project_id="proj1",
                        team_id="team::ceo",
                        seat_id="seat::team::ceo::cto",
                        role_id="cto",
                        manager_role_id="ceo",
                        manager_seat_id="seat::team::ceo::ceo",
                    )
                )
                root_task = Task(
                    id="root-task",
                    title="CEO Intake",
                    project_id="proj1",
                    assigned_to="ceo",
                    status=TaskStatus.PENDING,
                    metadata={
                        "execution_mode": "company_mode",
                        "runtime_model": "multi_team_org",
                        "work_item_runtime": True,
                        "delegation_run_id": "run-1",
                        "delegation_seat_id": "seat::team::ceo::ceo",
                        "runtime_topology": {"seats": []},
                    },
                )
                set_linked_work_item_id(root_task, "ceo-work-item")

                await runtime.bootstrap([root_task])

                # Fix 5 PR4: member_session key is role-scoped —
                # ``(project, scope, role, employee)``. No team_instance
                # slot; same role = one session across every team context.
                session = runtime.member_sessions[
                    "role-session::proj1::ceo::ceo-default-session"
                ]
                self.assertEqual(session.metadata["direct_report_role_ids"], ["cto"])
                self.assertEqual(session.metadata["direct_report_seat_ids"], ["seat::team::ceo::cto"])
                runtime.prepare_task_for_session(session, root_task)
                self.assertEqual(root_task.metadata["current_turn_mode"], "manager_decide")
            finally:
                await store.close()


class ActorRuntimeAttentionWorkItemTests(unittest.IsolatedAsyncioTestCase):
    async def test_inbox_attention_upserts_work_item_instead_of_synthetic_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()

            config = OPCConfig()
            config.org.company_profile = "custom"
            config.org.final_decider_role_id = "ceo"
            config.org.roles = [
                RoleConfig(id="ceo", name="CEO", responsibility="Set direction.", reports_to="owner"),
            ]
            org_engine = OrgEngine(config, root)
            communication = CommunicationManager(store, EventBus(), llm=None, org_engine=org_engine)
            executor = CompanyWorkItemExecutor(
                org_engine=org_engine,
                communication=communication,
                approval_engine=SimpleNamespace(),
                memory=None,
                execute_task=AsyncMock(),
                save_task=store.save_task,
                store=store,
                llm=None,
            )

            topology = org_engine.build_runtime_delegation_topology()
            root_task = Task(
                id="root-task",
                title="CEO Intake",
                project_id="proj1",
                session_id="sess-root",
                parent_session_id="sess-root",
                assigned_to="ceo",
                status=TaskStatus.PENDING,
                metadata={
                    "mode": "company",
                    "execution_mode": "company_mode",
                    "execution_model": "multi_team_org",
                    "runtime_model": "multi_team_org",
                    "work_item_runtime": True,
                    "delegation_run_id": "run-1",
                    "runtime_topology": topology,
                    "delegation_seat_id": "seat::team::ceo::ceo",
                    "delegation_team_id": "team::ceo",
                    "delegation_role_session_id": "role-runtime::run-1::seat::team::ceo::ceo",
                    "original_message": "Build the feature",
                },
            )
            set_linked_work_item_id(root_task, "root-work-item")
            await store.save_task(root_task)
            await store.save_delegation_work_item(
                DelegationWorkItem(
                    work_item_id="root-work-item",
                    run_id="run-1",
                    cell_id="team::ceo",
                    team_instance_id="team-instance::run-1::team::ceo",
                    team_id="team::ceo",
                    role_id="ceo",
                    seat_id="seat::team::ceo::ceo",
                    seat_state_id="seat-state::run-1::seat::team::ceo::ceo",
                    role_runtime_session_id="role-runtime::run-1::seat::team::ceo::ceo",
                    title="CEO Intake",
                    summary="Build the feature",
                    kind="intake",
                    projection_id="root-work-item",
                    phase=Phase.APPROVED,
                    metadata={"work_item_runtime": True, "runtime_model": "multi_team_org"},
                )
            )
            await store.link_work_item_runtime_task("root-work-item", root_task.id)

            session = CompanyMemberSession(
                member_session_id="seat-session::proj1::seat::team::ceo::ceo",
                role_session_id="role-runtime::run-1::seat::team::ceo::ceo",
                team_instance_id="team-instance::run-1::team::ceo",
                team_id="team::ceo",
                role_id="ceo",
                seat_id="seat::team::ceo::ceo",
                seat_state_id="seat-state::run-1::seat::team::ceo::ceo",
                employee_id="ceo-default-session",
                status="idle",
                resident_status="idle",
                current_turn_mode="manager_decide",
                actionable_chat=[
                    {
                        "msg_id": "msg-1",
                        "from_agent": "owner",
                        "subject": "Please continue",
                        "body": "Keep the team moving.",
                        "message_class": "chat",
                        "actionable": True,
                    }
                ],
                inbox_state={"current_turn_mode": "manager_decide"},
                metadata={
                    "team_id": "team::ceo",
                    "seat_id": "seat::team::ceo::ceo",
                    "manager_seat_id": "",
                    "managed_team_id": "",
                    "contact_role_ids": [],
                    "allowed_delegate_role_ids": [],
                },
            )
            executor.runtime.member_sessions[session.member_session_id] = session

            tasks, work_items = await executor._queue_multi_team_response_tasks(
                [root_task],
                await store.list_delegation_work_items("run-1"),
            )

            attention_items = [
                item for item in work_items
                if bool((item.metadata or {}).get("attention_work_item", False))
            ]
            self.assertEqual(len(attention_items), 1)
            self.assertEqual(attention_items[0].kind, "plan")
            self.assertEqual(attention_items[0].phase, Phase.READY)
            self.assertEqual(attention_items[0].seat_id, "seat::team::ceo::ceo")
            self.assertTrue(
                all(not bool((task.metadata or {}).get("synthetic_inbox_turn", False)) for task in tasks)
            )
            projected = next(
                task for task in tasks
                if linked_work_item_id_for_task(task) == attention_items[0].work_item_id
            )
            self.assertEqual(projected.status, TaskStatus.PENDING)

    async def test_delivery_attention_not_created_before_dependencies_are_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()

            config = OPCConfig()
            config.org.company_profile = "custom"
            config.org.final_decider_role_id = "ceo"
            config.org.roles = [
                RoleConfig(id="ceo", name="CEO", responsibility="Set direction.", reports_to="owner"),
            ]
            org_engine = OrgEngine(config, root)
            communication = CommunicationManager(store, EventBus(), llm=None, org_engine=org_engine)
            executor = CompanyWorkItemExecutor(
                org_engine=org_engine,
                communication=communication,
                approval_engine=SimpleNamespace(),
                memory=None,
                execute_task=AsyncMock(),
                save_task=store.save_task,
                store=store,
                llm=None,
            )

            root_task = Task(
                id="root-task",
                title="CEO Intake",
                project_id="proj1",
                session_id="sess-root",
                parent_session_id="sess-root",
                assigned_to="ceo",
                status=TaskStatus.BLOCKED,
                metadata={
                    "mode": "company",
                    "execution_mode": "company_mode",
                    "execution_model": "multi_team_org",
                    "runtime_model": "multi_team_org",
                    "work_item_runtime": True,
                    "delegation_run_id": "run-1",
                    "delegation_seat_id": "seat::team::ceo::ceo",
                    "delegation_team_id": "team::ceo",
                    "delegation_role_session_id": "role-runtime::run-1::seat::team::ceo::ceo",
                },
            )
            set_linked_work_item_id(root_task, "root-work-item")
            await store.save_task(root_task)
            root_item = DelegationWorkItem(
                work_item_id="root-work-item",
                run_id="run-1",
                cell_id="team::ceo",
                team_instance_id="team-instance::run-1::team::ceo",
                team_id="team::ceo",
                role_id="ceo",
                seat_id="seat::team::ceo::ceo",
                seat_state_id="seat-state::run-1::seat::team::ceo::ceo",
                role_runtime_session_id="role-runtime::run-1::seat::team::ceo::ceo",
                title="CEO Intake",
                summary="Waiting for child work.",
                kind="intake",
                projection_id="root-work-item",
                phase=Phase.WAITING_FOR_CHILDREN,
                metadata={
                    "work_item_runtime": True,
                    "runtime_model": "multi_team_org",
                    "dependency_work_item_ids": ["child-work-item"],
                },
            )
            child_item = DelegationWorkItem(
                work_item_id="child-work-item",
                run_id="run-1",
                cell_id="team::cto",
                team_instance_id="team-instance::run-1::team::cto",
                team_id="team::cto",
                role_id="cto",
                seat_id="seat::team::ceo::cto",
                parent_work_item_id="root-work-item",
                title="CTO child",
                summary="Still running.",
                kind="execute",
                projection_id="child-work-item",
                phase=Phase.RUNNING,
                manager_role_id="ceo",
                manager_seat_id="seat::team::ceo::ceo",
                metadata={"work_item_runtime": True, "runtime_model": "multi_team_org"},
            )
            await store.save_delegation_work_item(root_item)
            await store.save_delegation_work_item(child_item)

            session = CompanyMemberSession(
                member_session_id="seat-session::proj1::seat::team::ceo::ceo",
                role_session_id="role-runtime::run-1::seat::team::ceo::ceo",
                team_instance_id="team-instance::run-1::team::ceo",
                team_id="team::ceo",
                role_id="ceo",
                seat_id="seat::team::ceo::ceo",
                seat_state_id="seat-state::run-1::seat::team::ceo::ceo",
                employee_id="ceo-default-session",
                status="blocked",
                resident_status="blocked",
                focused_work_item_id="root-work-item",
                current_turn_mode="deliver_required",
                inbox_state={"current_turn_mode": "deliver_required"},
                current_work_item={"work_item_id": "root-work-item"},
                metadata={
                    "team_id": "team::ceo",
                    "seat_id": "seat::team::ceo::ceo",
                    "manager_seat_id": "",
                    "managed_team_id": "team::ceo",
                    "contact_role_ids": ["cto"],
                    "allowed_delegate_role_ids": ["cto"],
                },
            )

            _tasks, work_items = await executor._upsert_attention_work_item(
                root_task=root_task,
                tasks=[root_task],
                work_items=[root_item, child_item],
                session=session,
                source_message={
                    "msg_id": "msg-blocked",
                    "from_agent": "cto",
                    "subject": "Blocked child",
                    "body": "Child work is still running.",
                },
            )

            self.assertFalse(
                any(bool((item.metadata or {}).get("attention_work_item", False)) for item in work_items)
            )
            persisted = await store.list_delegation_work_items("run-1")
            self.assertFalse(
                any(bool((item.metadata or {}).get("attention_work_item", False)) for item in persisted)
            )
            await store.close()


class ActorRuntimeManagerDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.store = OPCStore(self.root / "tasks.db")
        await self.store.initialize()
        self.config = OPCConfig()
        self.config.org.company_profile = "custom"
        self.config.org.final_decider_role_id = "ceo"
        self.config.org.roles = [
            RoleConfig(id="ceo", name="CEO", responsibility="Set direction.", reports_to="owner"),
            RoleConfig(id="cto", name="CTO", responsibility="Lead engineering.", reports_to="ceo"),
        ]
        self.org_engine = OrgEngine(self.config, self.root)
        self.communication = CommunicationManager(self.store, EventBus(), llm=None, org_engine=self.org_engine)
        self.executor = CompanyWorkItemExecutor(
            org_engine=self.org_engine,
            communication=self.communication,
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=self.store.save_task,
            store=self.store,
            llm=None,
        )
        self.task = Task(
            id="ceo-dispatch-task",
            title="CEO Dispatch",
            project_id="proj1",
            assigned_to="ceo",
            status=TaskStatus.PENDING,
            metadata={
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "delegation_run_id": "run-1",
                "delegation_seat_id": "seat::team::ceo::ceo",
                "current_turn_mode": "manager_decide",
                "direct_report_role_ids": ["cto"],
                "direct_report_seat_ids": ["seat::team::ceo::cto"],
            },
        )
        set_linked_work_item_id(self.task, "ceo-work-item")
        await self.store.save_delegation_work_item(
            DelegationWorkItem(
                work_item_id="ceo-work-item",
                run_id="run-1",
                cell_id="team::ceo",
                team_instance_id="team-instance::run-1::team::ceo",
                team_id="team::ceo",
                role_id="ceo",
                seat_id="seat::team::ceo::ceo",
                seat_state_id="seat-state::run-1::seat::team::ceo::ceo",
                role_runtime_session_id="role-runtime::run-1::seat::team::ceo::ceo",
                title="CEO Dispatch",
                summary="Route the work.",
                kind="dispatch",
                projection_id="ceo-work-item",
                phase=Phase.READY,
                metadata={"work_item_runtime": True, "runtime_model": "multi_team_org"},
            )
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.tmpdir.cleanup()

    async def test_manager_can_complete_directly_without_delegation_protocol(self) -> None:
        task = await self._make_cto_dispatch_task(
            metadata_extra={
                "direct_report_role_ids": ["dev"],
                "direct_report_seat_ids": ["seat::team::cto::dev"],
            },
        )
        task.status = TaskStatus.PENDING
        self.executor.execute_task = AsyncMock(
            return_value=TaskResult(
                status=TaskStatus.DONE,
                content="Completed the leader-owned architecture decision.",
            )
        )

        result = await self.executor._run_work_item(task, {})

        self.assertIsNotNone(result)
        self.assertNotEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(self.executor.execute_task.await_count, 1)
        self.assertEqual(task.metadata["manager_execution_choice"], "direct")
        work_item = await self.store.get_delegation_work_item("cto-dispatch-item")
        self.assertEqual(work_item.phase, Phase.AWAITING_MANAGER_REVIEW)
        dispatch_evidence = dict(
            dict((work_item.metadata or {}).get("review_evidence", {}) or {}).get(
                "manager_dispatch", {}
            )
            or {}
        )
        self.assertEqual(dispatch_evidence.get("outcome"), "self_produced")
        self.assertEqual(dispatch_evidence.get("source"), "manager_decision")
        self.assertNotIn("turn_output_kind", work_item.metadata or {})
        self.assertNotIn("turn_output_source", work_item.metadata or {})
        report_cards = await self._aux_cards_targeting("cto-dispatch-item", "report_target_work_item_id")
        self.assertEqual(len(report_cards), 1)
        self.assertNotIn(report_cards[0].phase, DONE_PHASES)

    async def test_manager_execution_choice_uses_turn_start_mode(self) -> None:
        task = await self._make_cto_dispatch_task()
        task.status = TaskStatus.PENDING

        async def execute_and_advance_runtime_mode(executing_task: Task, **_kwargs) -> TaskResult:
            executing_task.metadata["current_turn_mode"] = "worker_execute"
            return TaskResult(
                status=TaskStatus.DONE,
                content="Completed the leader-owned architecture decision.",
            )

        self.executor.execute_task = AsyncMock(
            side_effect=execute_and_advance_runtime_mode,
        )

        result = await self.executor._run_work_item(task, {})

        self.assertIsNotNone(result)
        self.assertEqual(task.metadata["manager_execution_choice"], "direct")
        work_item = await self.store.get_delegation_work_item("cto-dispatch-item")
        dispatch_evidence = dict(
            dict((work_item.metadata or {}).get("review_evidence", {}) or {}).get(
                "manager_dispatch", {}
            )
            or {}
        )
        self.assertEqual(dispatch_evidence.get("source"), "manager_decision")

    async def _aux_cards_targeting(self, work_item_id: str, key: str) -> list[DelegationWorkItem]:
        run_items = await self.store.list_delegation_work_items("run-1")
        return [
            item for item in run_items
            if str((item.metadata or {}).get(key, "") or "").strip() == work_item_id
        ]

    async def _make_cto_dispatch_task(self, *, metadata_extra: dict | None = None) -> Task:
        task = Task(
            id="cto-dispatch-task",
            title="CTO Dispatch",
            project_id="proj1",
            assigned_to="cto",
            status=TaskStatus.DONE,
            metadata={
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "delegation_run_id": "run-1",
                "delegation_seat_id": "seat::team::ceo::cto",
                "current_turn_mode": "manager_decide",
                "work_kind": "dispatch",
                "manager_role_id": "ceo",
                "manager_seat_id": "seat::team::ceo::ceo",
                **(metadata_extra or {}),
            },
        )
        set_linked_work_item_id(task, "cto-dispatch-item")
        await self.store.save_delegation_work_item(
            DelegationWorkItem(
                work_item_id="cto-dispatch-item",
                run_id="run-1",
                cell_id="team::ceo",
                team_instance_id="team-instance::run-1::team::ceo",
                team_id="team::ceo",
                role_id="cto",
                seat_id="seat::team::ceo::cto",
                seat_state_id="seat-state::run-1::seat::team::ceo::cto",
                role_runtime_session_id="role-runtime::run-1::seat::team::ceo::cto",
                title="CTO Dispatch",
                summary="Route the engineering work.",
                kind="dispatch",
                projection_id="cto-dispatch-item",
                phase=Phase.RUNNING,
                manager_role_id="ceo",
                manager_seat_id="seat::team::ceo::ceo",
                metadata={"work_item_runtime": True, "runtime_model": "multi_team_org"},
            )
        )
        return task

    async def test_self_produced_manager_decision_runs_full_report_review_chain(self) -> None:
        # Real store, no lifecycle mocks: leader-owned output
        # output must route to manager review, actually spawn the report
        # card, and — once the report turn finishes — actually spawn the
        # review card in the manager seat.
        task = await self._make_cto_dispatch_task(
            metadata_extra={"manager_execution_choice": "direct"},
        )

        phase = await self.executor._apply_done_transition(
            task, result=TaskResult(status=TaskStatus.DONE, content="Scoped the work; no delegation needed."),
        )

        self.assertEqual(phase, Phase.AWAITING_MANAGER_REVIEW)
        work_item = await self.store.get_delegation_work_item("cto-dispatch-item")
        self.assertEqual(work_item.phase, Phase.AWAITING_MANAGER_REVIEW)
        dispatch_evidence = dict(
            dict((work_item.metadata or {}).get("review_evidence", {}) or {}).get(
                "manager_dispatch", {}
            )
            or {}
        )
        self.assertEqual(dispatch_evidence.get("outcome"), "self_produced")
        self.assertEqual(dispatch_evidence.get("source"), "manager_decision")
        self.assertNotIn("turn_output_kind", work_item.metadata or {})
        self.assertNotIn("turn_output_source", work_item.metadata or {})
        report_cards = await self._aux_cards_targeting("cto-dispatch-item", "report_target_work_item_id")
        self.assertEqual(len(report_cards), 1)
        report_card = report_cards[0]
        self.assertNotIn(report_card.phase, DONE_PHASES)

        # Drive the report turn to completion — the review card must appear.
        # Production dispatch claims READY → RUNNING before materializing
        # the Task; this direct lifecycle test must model that claim.
        await self.store.update_delegation_work_item(
            report_card.work_item_id,
            phase=Phase.RUNNING,
        )
        report_task = Task(
            id="cto-report-task",
            title=report_card.title,
            project_id="proj1",
            assigned_to="cto",
            status=TaskStatus.DONE,
            metadata={
                **dict(report_card.metadata or {}),
                "execution_mode": "company_mode",
                "delegation_run_id": "run-1",
                "delegation_seat_id": "seat::team::ceo::cto",
                "manager_role_id": "ceo",
                "manager_seat_id": "seat::team::ceo::ceo",
            },
        )
        set_linked_work_item_id(report_task, report_card.work_item_id)
        await self.executor._apply_done_transition(
            report_task,
            result=TaskResult(status=TaskStatus.DONE, content="Structured handoff report."),
        )

        review_cards = await self._aux_cards_targeting("cto-dispatch-item", "review_target_work_item_id")
        self.assertEqual(len(review_cards), 1)
        self.assertNotIn(review_cards[0].phase, DONE_PHASES)
        self.assertEqual(str(review_cards[0].role_id or ""), "ceo")
        review_dispatch_evidence = dict(
            dict((review_cards[0].metadata or {}).get("review_evidence", {}) or {}).get(
                "manager_dispatch", {}
            )
            or {}
        )
        self.assertEqual(review_dispatch_evidence.get("source"), "manager_decision")

    async def test_historical_business_child_does_not_exempt_current_self_output(self) -> None:
        # A child created by a previous attempt is durable board state, not
        # proof that this completion delegated its output. The current direct
        # work product still needs the manager review chain.
        task = await self._make_cto_dispatch_task(
            metadata_extra={
                "manager_execution_choice": "direct"
            }
        )
        await self.store.save_delegation_work_item(
            DelegationWorkItem(
                work_item_id="cto-historical-child",
                run_id="run-1",
                cell_id="team::ceo",
                team_instance_id="team-instance::run-1::team::ceo",
                team_id="team::ceo",
                role_id="cto",
                seat_id="seat::team::ceo::cto",
                seat_state_id="seat-state::run-1::seat::team::ceo::cto",
                role_runtime_session_id="role-runtime::run-1::seat::team::ceo::cto",
                parent_work_item_id="cto-dispatch-item",
                title="Historical delegated child",
                summary="Completed in a previous attempt.",
                kind="execute",
                projection_id="cto-historical-child",
                phase=Phase.APPROVED,
                manager_role_id="cto",
                manager_seat_id="seat::team::ceo::cto",
                metadata={"work_item_runtime": True, "runtime_model": "multi_team_org"},
            )
        )

        phase = await self.executor._apply_done_transition(
            task,
            result=TaskResult(
                status=TaskStatus.DONE,
                content="Made the new architecture decision directly.",
            ),
        )

        self.assertEqual(phase, Phase.AWAITING_MANAGER_REVIEW)
        work_item = await self.store.get_delegation_work_item("cto-dispatch-item")
        report_cards = await self._aux_cards_targeting("cto-dispatch-item", "report_target_work_item_id")
        self.assertEqual(len(report_cards), 1)
        dispatch_evidence = dict(
            dict((work_item.metadata or {}).get("review_evidence", {}) or {}).get(
                "manager_dispatch", {}
            )
            or {}
        )
        self.assertEqual(dispatch_evidence.get("outcome"), "self_produced")

    async def test_attention_aux_does_not_exempt_current_self_output(self) -> None:
        task = await self._make_cto_dispatch_task(
            metadata_extra={
                "manager_execution_choice": "direct"
            }
        )
        await self.store.save_delegation_work_item(
            DelegationWorkItem(
                work_item_id="cto-attention-item",
                run_id="run-1",
                parent_work_item_id="cto-dispatch-item",
                role_id="cto",
                seat_id="seat::team::ceo::cto",
                title="CTO attention",
                summary="Runtime wake-up wrapper, not delegated business work.",
                kind="monitor",
                projection_id="cto-attention-item",
                phase=Phase.APPROVED,
                metadata={
                    "work_item_runtime": True,
                    "runtime_model": "multi_team_org",
                    "attention_work_item": True,
                },
            )
        )

        phase = await self.executor._apply_done_transition(
            task,
            result=TaskResult(
                status=TaskStatus.DONE,
                content="Made the architecture decision directly.",
            ),
        )

        self.assertEqual(phase, Phase.AWAITING_MANAGER_REVIEW)
        work_item = await self.store.get_delegation_work_item("cto-dispatch-item")
        self.assertEqual(work_item.phase, Phase.AWAITING_MANAGER_REVIEW)
        self.assertEqual(
            len(await self._aux_cards_targeting("cto-dispatch-item", "report_target_work_item_id")),
            1,
        )

    async def test_current_turn_business_board_mutation_keeps_dispatch_auto_approve(self) -> None:
        task = await self._make_cto_dispatch_task(
            metadata_extra={"manager_board_mutation_performed": True}
        )

        phase = await self.executor._apply_done_transition(
            task, result=TaskResult(status=TaskStatus.DONE, content="Delegated to the team."),
        )

        self.assertEqual(phase, Phase.APPROVED)
        work_item = await self.store.get_delegation_work_item("cto-dispatch-item")
        self.assertEqual(work_item.phase, Phase.APPROVED)
        self.assertNotIn("turn_output_kind", work_item.metadata or {})
        self.assertNotIn("turn_output_source", work_item.metadata or {})
        report_cards = await self._aux_cards_targeting("cto-dispatch-item", "report_target_work_item_id")
        self.assertEqual(report_cards, [])

    async def test_self_produced_intake_routes_to_manager_review(self) -> None:
        # The delegation-output rule covers every review-exempt delegation
        # kind, not just dispatch — an intake turn that answered the work
        # itself needs review too.
        task = await self._make_cto_dispatch_task(metadata_extra={"work_kind": "intake"})

        phase = await self.executor._apply_done_transition(
            task, result=TaskResult(status=TaskStatus.DONE, content="Handled the intake question directly."),
        )

        self.assertEqual(phase, Phase.AWAITING_MANAGER_REVIEW)
        work_item = await self.store.get_delegation_work_item("cto-dispatch-item")
        self.assertEqual(work_item.phase, Phase.AWAITING_MANAGER_REVIEW)
        self.assertNotIn("turn_output_kind", work_item.metadata or {})
        self.assertNotIn("turn_output_source", work_item.metadata or {})

    async def test_top_seat_self_produced_routes_to_owner_review(self) -> None:
        # The CEO has no organizational manager. A directly produced outcome
        # is therefore the authoritative delivery and must go to the owner;
        # silently auto-approving it would bypass the final quality gate.
        self.task.status = TaskStatus.DONE
        self.task.metadata["work_kind"] = "intake"
        self.task.metadata["manager_execution_choice"] = "direct"
        await self.store.update_delegation_work_item("ceo-work-item", phase=Phase.RUNNING)

        phase = await self.executor._apply_done_transition(
            self.task, result=TaskResult(status=TaskStatus.DONE, content="Handled at the top."),
        )

        self.assertEqual(phase, Phase.AWAITING_HUMAN)
        work_item = await self.store.get_delegation_work_item("ceo-work-item")
        self.assertEqual(work_item.phase, Phase.AWAITING_HUMAN)
        self.assertTrue(self.task.metadata["authoritative_output"])
        self.assertTrue(self.task.metadata["user_visible"])
        self.assertEqual(self.task.metadata["feedback_scope"], "final")
        self.assertEqual(self.task.metadata["review_owner_kind"], "human")
        self.assertTrue(self.task.metadata["requires_user_feedback"])
        self.assertEqual(
            await self._aux_cards_targeting("ceo-work-item", "report_target_work_item_id"),
            [],
        )
        self.assertEqual(
            await self._aux_cards_targeting("ceo-work-item", "review_target_work_item_id"),
            [],
        )

    async def test_reconcile_rebuilds_missing_report_card(self) -> None:
        # Current-state crash shape: phase was durably written but the process
        # stopped before its report card was saved. Phase alone is the
        # authoritative recovery fact; no output-kind marker is required.
        await self._make_cto_dispatch_task()
        await self.store.update_delegation_work_item(
            "cto-dispatch-item",
            phase=Phase.AWAITING_MANAGER_REVIEW,
        )
        run_items = await self.store.list_delegation_work_items("run-1")

        run_items = await self.executor._reconcile_missing_review_chain(run_items)

        report_cards = await self._aux_cards_targeting("cto-dispatch-item", "report_target_work_item_id")
        self.assertEqual(len(report_cards), 1)
        # Idempotent: a second pass with the live report card present must
        # not spawn another attempt.
        run_items = await self.executor._reconcile_missing_review_chain(run_items)
        report_cards = await self._aux_cards_targeting("cto-dispatch-item", "report_target_work_item_id")
        self.assertEqual(len(report_cards), 1)


class CompanyModeParallelIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatcher_waits_for_old_attempt_tail_before_self_rework_claim(self) -> None:
        org_engine = SimpleNamespace(
            get_agent=lambda role_id: SimpleNamespace(
                role_id=role_id,
                reports_to="owner",
            )
        )
        executor = CompanyWorkItemExecutor(
            org_engine=org_engine,
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            store=None,
            llm=None,
        )
        executor.on_kanban_changed = AsyncMock()
        task = Task(
            id="delivery-task",
            title="Delivery",
            session_id="root-a",
            parent_session_id="root-a",
            project_id="proj1",
            assigned_to="lead",
            status=TaskStatus.PENDING,
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_projection_id": "lead::delivery::one",
                "work_item_role_id": "lead",
                "delegation_run_id": "run-1",
                "employee_assignment": {
                    "employee_id": "lead-a",
                    "role_id": "lead",
                },
            },
        )
        set_linked_work_item_id(task, "delivery-wi")
        work_item = DelegationWorkItem(
            work_item_id="delivery-wi",
            run_id="run-1",
            cell_id="team::lead",
            role_id="lead",
            title="Delivery",
            kind="delivery",
            projection_id="lead::delivery::one",
            phase=Phase.READY,
            metadata={"runtime_model": "multi_team_org"},
        )
        member_session, role_session = executor.runtime.ensure_role_instance_session(
            task
        )
        self.assertIsNotNone(role_session)
        executor.runtime.bootstrap = AsyncMock()
        executor.runtime.refresh_inbox_state = AsyncMock()
        executor._load_delegation_work_items = AsyncMock(
            side_effect=lambda _tasks: [work_item]
        )
        executor._refresh_ready_work_items = AsyncMock(
            side_effect=lambda items, tasks=None: items
        )
        executor._materialize_work_item_tasks = AsyncMock(
            side_effect=lambda tasks, _items: tasks
        )
        executor._queue_multi_team_response_tasks = AsyncMock(
            side_effect=lambda tasks, items: (tasks, items)
        )
        executor._reconcile_role_serial_queues = AsyncMock(
            side_effect=lambda items: items
        )
        executor._sync_task_projection_from_work_items = lambda _tasks, _items: None
        executor._diagnose_work_item_runtime_projection_issues = AsyncMock()
        executor._summarize_multi_team_org_results = lambda _tasks: "done"

        old_tail_released_claim = asyncio.Event()
        old_tail_may_finish = asyncio.Event()
        excluded_claim_pass_seen = asyncio.Event()
        second_attempt_started = asyncio.Event()
        attempt_starts = 0
        concurrent_owners = 0
        max_concurrent_owners = 0
        original_claim = executor.runtime.claim_runnable_tasks

        async def observed_claim(tasks, work_items=None, **kwargs):
            result = await original_claim(tasks, work_items=work_items, **kwargs)
            if old_tail_released_claim.is_set() and not old_tail_may_finish.is_set():
                self.assertIn(task.id, kwargs["excluded_task_ids"])
                self.assertIn(work_item.work_item_id, kwargs["excluded_work_item_ids"])
                self.assertIn(
                    member_session.member_session_id,
                    kwargs["excluded_member_session_ids"],
                )
                self.assertIn(
                    member_session.role_session_id,
                    kwargs["excluded_role_session_ids"],
                )
                self.assertEqual(result, [])
                excluded_claim_pass_seen.set()
            return result

        executor.runtime.claim_runnable_tasks = observed_claim

        async def run_claimed(member, claimed_task, _task_by_projection_id):
            nonlocal attempt_starts, concurrent_owners, max_concurrent_owners
            attempt_starts += 1
            concurrent_owners += 1
            max_concurrent_owners = max(max_concurrent_owners, concurrent_owners)
            try:
                executor.runtime._claimed_task_ids.discard(claimed_task.id)
                executor.runtime._claimed_work_item_ids.discard(
                    linked_work_item_id_for_task(claimed_task)
                )
                member.status = member.resident_status = "idle"
                member.current_task_id = ""
                member.focused_work_item_id = ""
                assert role_session is not None
                role_session.status = "idle"
                role_session.focused_work_item_id = ""
                work_item.claimed_by_role_runtime_session_id = ""
                work_item.claimed_by_seat_id = ""
                if attempt_starts == 1:
                    work_item.phase = Phase.READY_FOR_REWORK
                    claimed_task.status = TaskStatus.PENDING
                    old_tail_released_claim.set()
                    executor._signal_dispatcher_wake()
                    await old_tail_may_finish.wait()
                else:
                    work_item.phase = Phase.APPROVED
                    claimed_task.status = TaskStatus.DONE
                    second_attempt_started.set()
                return TaskResult(status=TaskStatus.DONE, content="done")
            finally:
                concurrent_owners -= 1

        executor._run_claimed_work_item = run_claimed
        dispatcher = asyncio.create_task(
            executor._execute_multi_team_org_scoped(
                SimpleNamespace(metadata={}),
                [task],
            )
        )
        try:
            await asyncio.wait_for(old_tail_released_claim.wait(), timeout=2)
            await asyncio.wait_for(excluded_claim_pass_seen.wait(), timeout=2)
            self.assertEqual(attempt_starts, 1)
            self.assertFalse(second_attempt_started.is_set())

            old_tail_may_finish.set()
            await asyncio.wait_for(second_attempt_started.wait(), timeout=2)
            self.assertEqual(await asyncio.wait_for(dispatcher, timeout=2), "done")
        finally:
            if not dispatcher.done():
                dispatcher.cancel()
                await asyncio.gather(dispatcher, return_exceptions=True)

        self.assertEqual(attempt_starts, 2)
        self.assertEqual(max_concurrent_owners, 1)

    async def test_execute_multi_team_org_isolates_claimed_work_item_exception(self) -> None:
        executor = CompanyWorkItemExecutor(
            org_engine=SimpleNamespace(),
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=AsyncMock(),
            store=None,
            llm=None,
        )
        executor.on_kanban_changed = AsyncMock()

        task_a = Task(
            id="task-a",
            title="COO dispatch",
            project_id="proj1",
            assigned_to="coo",
            status=TaskStatus.PENDING,
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_projection_id": "coo::execute::dispatch",
            },
        )
        task_b = Task(
            id="task-b",
            title="CTO dispatch",
            project_id="proj1",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            metadata={
                "runtime_model": "multi_team_org",
                "work_item_projection_id": "cto::execute::dispatch",
            },
        )
        set_linked_work_item_id(task_a, "wi-a")
        set_linked_work_item_id(task_b, "wi-b")
        work_items = [
            DelegationWorkItem(
                work_item_id="wi-a",
                run_id="run-1",
                cell_id="team::ceo",
                team_id="team::ceo",
                role_id="coo",
                seat_id="seat::team::ceo::coo",
                title="COO dispatch",
                kind="dispatch",
                projection_id="coo::execute::dispatch",
                phase=Phase.READY,
                metadata={"runtime_model": "multi_team_org"},
            ),
            DelegationWorkItem(
                work_item_id="wi-b",
                run_id="run-1",
                cell_id="team::ceo",
                team_id="team::ceo",
                role_id="cto",
                seat_id="seat::team::ceo::cto",
                title="CTO dispatch",
                kind="dispatch",
                projection_id="cto::execute::dispatch",
                phase=Phase.READY,
                metadata={"runtime_model": "multi_team_org"},
            ),
        ]
        session_a = CompanyMemberSession(
            member_session_id="session-a",
            role_id="coo",
            seat_id="seat::team::ceo::coo",
            status="idle",
            resident_status="idle",
            metadata={"seat_id": "seat::team::ceo::coo"},
        )
        session_b = CompanyMemberSession(
            member_session_id="session-b",
            role_id="cto",
            seat_id="seat::team::ceo::cto",
            status="idle",
            resident_status="idle",
            metadata={"seat_id": "seat::team::ceo::cto"},
        )
        executor.runtime.member_sessions = {
            session_a.member_session_id: session_a,
            session_b.member_session_id: session_b,
        }
        executor.runtime.bootstrap = AsyncMock()
        executor.runtime.refresh_inbox_state = AsyncMock()
        executor.runtime.enqueue_runnable_work_items = lambda *args, **kwargs: None
        executor.runtime.enqueue_runnable_tasks = lambda *args, **kwargs: None
        executor._load_delegation_work_items = AsyncMock(return_value=work_items)
        executor._refresh_ready_work_items = AsyncMock(side_effect=lambda items, tasks=None: items)
        executor._materialize_work_item_tasks = AsyncMock(side_effect=lambda tasks, work_items: tasks)
        executor._queue_multi_team_response_tasks = AsyncMock(side_effect=lambda tasks, work_items: (tasks, work_items))
        executor._sync_task_projection_from_work_items = lambda tasks, work_items: None
        executor._work_item_is_runnable = lambda item, work_item_by_id, task_by_work_item_id: True
        executor._summarize_multi_team_org_results = lambda tasks: "isolated"
        claimed_once = False

        async def fake_claim_runnable_tasks(tasks, work_items=None, **_kwargs):
            nonlocal claimed_once
            _ = (tasks, work_items)
            if claimed_once:
                return []
            claimed_once = True
            executor.runtime._claimed_task_ids = {task_a.id, task_b.id}
            executor.runtime._claimed_work_item_ids = {"wi-a", "wi-b"}
            session_a.status = session_a.resident_status = "running"
            session_a.current_task_id = task_a.id
            session_a.focused_work_item_id = "wi-a"
            session_b.status = session_b.resident_status = "running"
            session_b.current_task_id = task_b.id
            session_b.focused_work_item_id = "wi-b"
            return [(session_a, task_a), (session_b, task_b)]

        async def fake_complete_claim(session, task, result=None):
            _ = result
            executor.runtime._claimed_task_ids.discard(task.id)
            executor.runtime._claimed_work_item_ids.discard(
                linked_work_item_id_for_task(task)
            )
            session.status = session.resident_status = "idle"
            session.current_task_id = ""
            session.focused_work_item_id = ""

        async def fake_run_claimed_work_item(member_session, task, task_by_projection_id):
            _ = (member_session, task_by_projection_id)
            if task.id == "task-a":
                raise RuntimeError("sync blew up")
            task.status = TaskStatus.DONE
            result = TaskResult(status=TaskStatus.DONE, content="done")
            await fake_complete_claim(session_b, task, result=result)
            return result

        executor.runtime.claim_runnable_tasks = AsyncMock(side_effect=fake_claim_runnable_tasks)
        executor.runtime.complete_claim = AsyncMock(side_effect=fake_complete_claim)
        executor._run_claimed_work_item = AsyncMock(side_effect=fake_run_claimed_work_item)

        result = await executor.execute(SimpleNamespace(metadata={}), [task_a, task_b])

        self.assertEqual(result, "isolated")
        self.assertEqual(task_a.status, TaskStatus.FAILED)
        self.assertIn("RuntimeError", str((task_a.result or {}).get("content", "")))
        self.assertEqual(session_a.status, "idle")
        self.assertEqual(session_a.current_task_id, "")
        self.assertEqual(task_b.status, TaskStatus.DONE)
        self.assertEqual(session_b.status, "idle")
