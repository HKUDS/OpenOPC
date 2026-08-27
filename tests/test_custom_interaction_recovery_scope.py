from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from opc.core.config import OPCConfig, RoleConfig
from opc.core.events import EventBus
from opc.core.models import (
    ExecutionCheckpoint,
    ExecutionMode,
    RouterDecision,
    Task,
)
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer0_interaction.coordinator import InteractionCoordinator
from opc.layer2_organization.company_mode import CompanyRuntimeSpecBuilder
from opc.layer2_organization.custom_runtime import CustomRuntimeRunner
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.talent_market import TalentMarket


def _fingerprint(
    call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    runtime_session_id: str,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "tool_call_id": call_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "runtime_session_id": runtime_session_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _recovery_engine(store: OPCStore, opc_home: Path) -> OPCEngine:
    engine = OPCEngine.__new__(OPCEngine)
    engine.store = store
    engine.project_id = "project-a"
    engine.opc_home = opc_home
    # Deliberately disagree with the durable scope. Recovery must not inherit
    # whichever organization happens to be selected by the root process.
    engine.config = SimpleNamespace(
        org=SimpleNamespace(
            company_profile="corporate",
            organization_id="wrong-root-org",
        )
    )
    engine.event_bus = EventBus()
    engine.interaction_coordinator = InteractionCoordinator(
        store=store,
        project_id="project-a",
    )
    engine._interaction_consumer_tasks = set()
    engine._initialized = True
    engine._shutting_down = False
    return engine


async def _wait_for_status(
    store: OPCStore,
    checkpoint: ExecutionCheckpoint,
    status: str,
) -> ExecutionCheckpoint:
    for _ in range(200):
        persisted = await store.get_execution_checkpoint(
            checkpoint.checkpoint_id,
            project_id=checkpoint.project_id,
            checkpoint_type=checkpoint.checkpoint_type,
        )
        if persisted is not None and persisted.status == status:
            return persisted
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"checkpoint {checkpoint.checkpoint_id} did not reach {status}"
    )


@pytest.mark.parametrize(
    ("checkpoint_type", "decision"),
    [
        ("tool_permission", {"option_id": "approve_once"}),
        (
            "company_delivery_feedback",
            {"checkpoint_reply_kind": "ignore", "option_id": "ignore"},
        ),
    ],
)
def test_root_restart_routes_custom_interactions_by_durable_scope_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint_type: str,
    decision: dict[str, Any],
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / f"{checkpoint_type}.db"
        original_store = OPCStore(db_path)
        await original_store.initialize()
        task = Task(
            id="custom-worker",
            project_id="project-a",
            session_id="custom-worker-session",
            metadata={
                "exec_mode": "org",
                "company_profile": "custom",
                "org_id": "saved-custom-org",
                "work_item_role_id": "investment-researcher",
            },
        )
        await original_store.save_task(task)
        interaction = {
            "kind": checkpoint_type,
            "domain_key": f"custom-recovery:{checkpoint_type}",
            "execution_scope": {
                "company_profile": "custom",
                "org_id": "saved-custom-org",
            },
            "ownership": {
                "waiting_task_id": task.id,
                "waiting_session_id": task.session_id,
                "ui_anchor_session_id": "root-session",
            },
        }
        payload: dict[str, Any] = {"interaction": interaction}
        if checkpoint_type == "tool_permission":
            call_id = "exact-call-id"
            tool_name = "write_investment_report"
            arguments = {
                "ticker": "0700.HK",
                "assumptions": {"discount_rate": 0.09},
            }
            runtime_session_id = "custom-runtime-session"
            payload.update(
                {
                    "tool_call": {
                        "id": call_id,
                        "name": tool_name,
                        "arguments": arguments,
                        "runtime_session_id": runtime_session_id,
                        "fingerprint": _fingerprint(
                            call_id,
                            tool_name,
                            arguments,
                            runtime_session_id,
                        ),
                    },
                    "approval": {
                        "action_kind": "tool",
                        "action_name": tool_name,
                    },
                }
            )
            interaction["ownership"]["tool_runtime_session_id"] = runtime_session_id
            await original_store.save_runtime_tool_call(
                runtime_session_id=runtime_session_id,
                task_id=task.id,
                session_id=task.session_id,
                message_id="assistant-message",
                tool_call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
            )
        else:
            payload.update(
                {
                    "delivery_id": "delivery-1",
                    "work_item_id": "investment-analysis",
                }
            )
        checkpoint = ExecutionCheckpoint(
            checkpoint_id=f"custom-{checkpoint_type}",
            project_id="project-a",
            session_id=task.session_id,
            checkpoint_type=checkpoint_type,
            task_id=task.id,
            payload=payload,
        )
        checkpoint, _created = (
            await original_store.publish_owner_interaction_checkpoint(
                checkpoint,
                interaction_key=f"custom-recovery:{checkpoint_type}",
                supersede_pending_scope=False,
            )
        )
        await original_store.accept_execution_checkpoint_decision(
            checkpoint.checkpoint_id,
            project_id=checkpoint.project_id,
            checkpoint_type=checkpoint.checkpoint_type,
            request_id=f"answer-{checkpoint_type}",
            decision_hash=f"hash-{checkpoint_type}",
            decision=decision,
        )
        await original_store.close()

        recovered_store = OPCStore(db_path)
        await recovered_store.initialize()
        dispatches: list[dict[str, Any]] = []

        async def record_custom_dispatch(
            runner: CustomRuntimeRunner,
            lease: Any,
            *,
            org_id: str,
        ) -> str:
            saved_task = await runner.parent.store.get_task("custom-worker")
            dispatches.append(
                {
                    "org_id": org_id,
                    "checkpoint_type": lease.checkpoint.checkpoint_type,
                    "decision": dict(lease.decision),
                    "tool_call": dict(lease.checkpoint.payload.get("tool_call", {})),
                    "task_org_id": saved_task.metadata.get("org_id"),
                    "role_id": saved_task.metadata.get("work_item_role_id"),
                }
            )
            return "custom consumer completed"

        monkeypatch.setattr(
            CustomRuntimeRunner,
            "dispatch_interaction_decision",
            record_custom_dispatch,
        )
        engine = _recovery_engine(recovered_store, tmp_path)
        try:
            await engine._recover_interaction_consumers()
            await _wait_for_status(recovered_store, checkpoint, "resolved")
            # A second root startup scan sees a terminal row and cannot replay
            # the durable effect.
            await engine._recover_interaction_consumers()
            await asyncio.sleep(0.05)

            assert len(dispatches) == 1
            routed = dispatches[0]
            assert routed["org_id"] == "saved-custom-org"
            assert routed["task_org_id"] == "saved-custom-org"
            assert routed["role_id"] == "investment-researcher"
            assert routed["checkpoint_type"] == checkpoint_type
            assert routed["decision"] == decision
            if checkpoint_type == "tool_permission":
                assert routed["tool_call"] == payload["tool_call"]
        finally:
            pending = list(engine._interaction_consumer_tasks)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await recovered_store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("task_org_id", "checkpoint_org_id", "error_fragment"),
    [
        ("", "", "missing its durable org_id"),
        (
            "task-custom-org",
            "different-checkpoint-org",
            "org_id conflicts with its durable task",
        ),
    ],
)
def test_root_recovery_fails_closed_for_missing_or_conflicting_custom_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    task_org_id: str,
    checkpoint_org_id: str,
    error_fragment: str,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / f"invalid-{task_org_id or 'missing'}.db")
        await store.initialize()
        task = Task(
            id="custom-worker",
            project_id="project-a",
            session_id="custom-session",
            metadata={
                "exec_mode": "org",
                "company_profile": "custom",
                **({"org_id": task_org_id} if task_org_id else {}),
            },
        )
        await store.save_task(task)
        checkpoint = ExecutionCheckpoint(
            checkpoint_id=f"invalid-scope-{task_org_id or 'missing'}",
            project_id="project-a",
            session_id=task.session_id,
            checkpoint_type="company_delivery_feedback",
            task_id=task.id,
            payload={
                "interaction": {
                    "kind": "company_delivery_feedback",
                    "domain_key": (
                        f"custom-invalid-scope:{task_org_id or 'missing'}"
                    ),
                    "execution_scope": {
                        "company_profile": "custom",
                        **(
                            {"org_id": checkpoint_org_id}
                            if checkpoint_org_id
                            else {}
                        ),
                    },
                    "ownership": {
                        "waiting_task_id": task.id,
                        "waiting_session_id": task.session_id,
                    },
                }
            },
        )
        checkpoint, _created = await store.publish_owner_interaction_checkpoint(
            checkpoint,
            interaction_key=(
                f"custom-invalid-scope:{task_org_id or 'missing'}"
            ),
            supersede_pending_scope=False,
        )
        await store.accept_execution_checkpoint_decision(
            checkpoint.checkpoint_id,
            project_id=checkpoint.project_id,
            checkpoint_type=checkpoint.checkpoint_type,
            request_id=f"answer-{checkpoint.checkpoint_id}",
            decision_hash=f"hash-{checkpoint.checkpoint_id}",
            decision={"checkpoint_reply_kind": "ignore"},
        )

        async def unexpected_dispatch(*_args: Any, **_kwargs: Any) -> str:
            raise AssertionError("invalid durable scope reached the custom runtime")

        monkeypatch.setattr(
            CustomRuntimeRunner,
            "dispatch_interaction_decision",
            unexpected_dispatch,
        )
        engine = _recovery_engine(store, tmp_path)
        try:
            await engine._recover_interaction_consumers()
            persisted = await _wait_for_status(store, checkpoint, "invalid")
            interaction_error = dict(
                persisted.payload.get("interaction_error", {}) or {}
            )
            assert error_fragment in str(interaction_error.get("message", ""))
            assert interaction_error.get("retryable") is False
        finally:
            pending = list(engine._interaction_consumer_tasks)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await store.close()

    asyncio.run(scenario())


def test_taskless_custom_staffing_publish_and_root_submit_restore_saved_org(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "custom-staffing.db")
        await store.initialize()
        config = OPCConfig()
        config.org.company_profile = "custom"
        config.org.organization_id = "saved-staffing-org"
        config.org.roles = [
            RoleConfig(
                id="investment_lead",
                name="Investment Lead",
                responsibility="Own the investment decision.",
                reports_to="owner",
                can_spawn=["investment_analyst"],
            ),
            RoleConfig(
                id="investment_analyst",
                name="Investment Analyst",
                responsibility="Research the investment target.",
                reports_to="investment_lead",
            ),
        ]
        producer = OPCEngine(
            config=config,
            opc_home=tmp_path,
            project_id="project-a",
        )
        producer.store = store
        producer.interaction_coordinator = InteractionCoordinator(
            store=store,
            project_id="project-a",
        )
        producer.org_engine = OrgEngine(config, tmp_path)
        producer.talent_market = TalentMarket(tmp_path, config)
        decision = RouterDecision(
            mode=ExecutionMode.COMPANY_MODE,
            company_profile="custom",
            org_id="saved-staffing-org",
            preferred_agent="native",
        )
        runtime_spec = CompanyRuntimeSpecBuilder(
            producer.org_engine
        ).build_spec(
            decision,
            original_message="Analyze an investment target",
        )
        payload = producer._build_manual_staffing_checkpoint_payload(
            decision,
            "Analyze an investment target",
            runtime_spec,
            session_id="root-staffing-session",
            origin_channel="office",
            origin_chat_id="",
            origin_thread_id="",
            conversation_turn_id="turn-staffing-1",
            conversation_turn_sequence=1,
        )
        assert payload is not None
        await producer._save_execution_checkpoint(
            {
                "project_id": "project-a",
                "session_id": "root-staffing-session",
                "checkpoint_type": "company_staffing_selection",
                "payload": payload,
            }
        )
        checkpoint = await store.get_latest_pending_checkpoint(
            "project-a",
            "root-staffing-session",
        )
        assert checkpoint is not None
        assert checkpoint.task_id is None
        assert checkpoint.payload["org_id"] == "saved-staffing-org"
        assert checkpoint.payload["interaction"]["execution_scope"] == {
            "company_profile": "custom",
            "org_id": "saved-staffing-org",
        }

        dispatches: list[dict[str, Any]] = []

        async def record_custom_dispatch(
            runner: CustomRuntimeRunner,
            lease: Any,
            *,
            org_id: str,
        ) -> str:
            dispatches.append(
                {
                    "org_id": org_id,
                    "decision": dict(lease.decision or {}),
                    "checkpoint_type": lease.checkpoint.checkpoint_type,
                }
            )
            return "custom staffing resumed"

        monkeypatch.setattr(
            CustomRuntimeRunner,
            "dispatch_interaction_decision",
            record_custom_dispatch,
        )
        root = _recovery_engine(store, tmp_path)
        staffing_selections = {
            role.id: {"kind": "fallback", "id": ""}
            for role in config.org.roles
        }
        role_agents = {role.id: "native" for role in config.org.roles}
        try:
            receipt = await root.submit_checkpoint_decision(
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_type=checkpoint.checkpoint_type,
                decision={
                    "staffing_action": "manual_approve",
                    "staffing_selections": staffing_selections,
                    "recruitment_role_agents": role_agents,
                    "recruitment_agent": "native",
                    "text": "approve",
                },
                client_request_id="custom-staffing-submit-1",
                requester_session_id="root-staffing-session",
            )
            assert receipt["accepted"] is True
            await _wait_for_status(store, checkpoint, "resolved")
            assert dispatches == [
                {
                    "org_id": "saved-staffing-org",
                    "checkpoint_type": "company_staffing_selection",
                    "decision": {
                        "staffing_action": "manual_approve",
                        "staffing_selections": staffing_selections,
                        "recruitment_role_agents": role_agents,
                        "recruitment_agent": "native",
                        "text": "approve",
                    },
                }
            ]
        finally:
            pending = list(root._interaction_consumer_tasks)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await root.interaction_coordinator.shutdown()
            await producer.interaction_coordinator.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_custom_staffing_producer_rejects_missing_or_conflicting_org_identity(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "invalid-custom-staffing.db")
        await store.initialize()
        engine = _recovery_engine(store, tmp_path)
        try:
            with pytest.raises(
                ValueError,
                match="custom staffing checkpoint requires a durable org_id",
            ):
                await engine._save_execution_checkpoint(
                    {
                        "project_id": "project-a",
                        "session_id": "missing-org-session",
                        "checkpoint_type": "company_staffing_selection",
                        "payload": {
                            "company_profile": "custom",
                            "original_message": "Analyze a company",
                        },
                    }
                )
            checkpoints = await store.get_execution_checkpoints(
                project_id="project-a",
            )
            assert checkpoints == []

            decision = RouterDecision(
                mode=ExecutionMode.COMPANY_MODE,
                company_profile="custom",
                org_id="decision-org",
            )
            runtime_spec = SimpleNamespace(
                profile="custom",
                metadata={
                    "company_profile": "custom",
                    "org_id": "different-runtime-org",
                },
            )
            with pytest.raises(
                ValueError,
                match="staffing checkpoint organization identity is inconsistent",
            ):
                engine._staffing_checkpoint_execution_scope(
                    decision,
                    runtime_spec,
                )
        finally:
            await engine.interaction_coordinator.shutdown()
            await store.close()

    asyncio.run(scenario())
