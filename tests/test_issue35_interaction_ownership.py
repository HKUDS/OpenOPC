from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opc.core.config import AutonomyConfig, OPCConfig
from opc.core.models import (
    ApprovalAction,
    ApprovalDecision,
    DelegationRoleSession,
    DelegationRun,
    DelegationWorkItem,
    ExecutionCheckpoint,
    RiskLevel,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer0_interaction.coordinator import InteractionCoordinator
from opc.layer2_organization.approval import ApprovalEngine
from opc.layer2_organization.company_runtime_identity import (
    load_company_runtime_identity_index,
)
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer3_agent.runtime_v2.runtime import NativeRuntimeV2
from opc.layer4_tools.registry import (
    COMPANY_EFFECT_NO_LOCAL_FS,
    ToolDefinition,
    ToolRegistry,
)


class _Preferences:
    opc_home = None

    def get_autonomy_preferences(self, project_id=None):
        _ = project_id
        return {"learned_actions": {}}

    def record_autonomy_feedback(self, **kwargs) -> None:
        _ = kwargs


class _ApprovalMemory:
    def append_autonomy_event(self, event, project=False) -> None:
        _ = (event, project)


class _RecordingEventBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)


class _RuntimeMemory:
    def __init__(self, store: OPCStore) -> None:
        self.store = store
        self._message_index = 0

    async def build_session_memory_context(self, session_id: str) -> str:
        _ = session_id
        return ""

    async def record_user_turn(self, *args, **kwargs):
        _ = (args, kwargs)
        self._message_index += 1
        return SimpleNamespace(message_id=f"message-{self._message_index}")

    async def append_session_message(self, *args, **kwargs):
        _ = (args, kwargs)
        self._message_index += 1
        return SimpleNamespace(message_id=f"message-{self._message_index}")

    async def append_session_part(self, *args, **kwargs) -> None:
        _ = (args, kwargs)

    async def update_runtime_session_memory(self, **kwargs):
        _ = kwargs
        return {}


class _ToolThenFinishLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.config = SimpleNamespace(max_tokens=2048)

    def prepare_user_message_content(self, content: str, attachment_refs=None):
        _ = attachment_refs
        return content

    def get_tool_definitions(self, tools):
        return tools

    def is_context_overflow_error(self, error: Exception) -> bool:
        _ = error
        return False

    async def chat_stream(self, messages, tools=None):
        _ = (messages, tools)
        self.calls += 1
        yield SimpleNamespace(
            event_type="message_start",
            payload={},
            model="stub",
        )
        if self.calls == 1:
            yield SimpleNamespace(
                event_type="tool_call_delta",
                payload={
                    "index": 0,
                    "id": "child-call-exact",
                    "name": "dangerous_tool",
                    "arguments": json.dumps({"value": "child"}),
                },
                model="stub",
            )
        else:
            yield SimpleNamespace(
                event_type="assistant_delta",
                payload={"text": "finished"},
                model="stub",
            )
        yield SimpleNamespace(
            event_type="message_stop",
            payload={"finish_reason": "stop"},
            model="stub",
        )


def _engine(store: OPCStore, event_bus: _RecordingEventBus | None = None) -> OPCEngine:
    engine = OPCEngine.__new__(OPCEngine)
    engine.store = store
    engine.project_id = "project-a"
    engine.event_bus = event_bus or _RecordingEventBus()
    engine._interaction_consumer_tasks = set()
    engine._initialized = True
    engine._shutting_down = False
    engine.interaction_coordinator = InteractionCoordinator(
        store=store,
        project_id="project-a",
        checkpoint_changed_callback=engine._interaction_checkpoint_changed,
    )
    return engine


def _approval(
    store: OPCStore,
    coordinator: InteractionCoordinator,
) -> ApprovalEngine:
    return ApprovalEngine(
        llm=object(),
        store=store,
        preferences=_Preferences(),
        memory=_ApprovalMemory(),
        config=AutonomyConfig(),
        interaction_coordinator=coordinator,
    )


async def _pending_checkpoint(store: OPCStore, checkpoint_type: str):
    for _ in range(200):
        rows = await store.get_execution_checkpoints(
            project_id="project-a",
            checkpoint_types=[checkpoint_type],
            statuses=["pending"],
        )
        if rows:
            return rows[0]
        await asyncio.sleep(0.01)
    raise AssertionError(f"no pending {checkpoint_type} checkpoint")


def test_custom_child_without_pure_anchor_uses_session_owner_and_exact_tool(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        engine = _engine(store)
        try:
            workspace = tmp_path / "workspace"
            workspace.mkdir()
            await store.save_delegation_run(DelegationRun(
                run_id="run-custom",
                project_id="project-a",
                session_id="root-session",
                status="running",
                lifecycle_status="active",
                metadata={"comms_workspace_root": str(workspace)},
            ))
            lease = await store.acquire_delegation_run_controller_lease(
                "run-custom",
                project_id="project-a",
                root_session_id="root-session",
                owner_token="controller-custom",
                lease_seconds=60,
            )
            assert lease.acquired
            await store.save_delegation_role_session(
                DelegationRoleSession(
                    role_session_id="role-session-analyst",
                    run_id="run-custom",
                    project_id="project-a",
                    role_id="analyst",
                    seat_id="seat-analyst",
                    seat_ids=["seat-analyst"],
                    status="running",
                ),
                controller_owner_token="controller-custom",
                controller_lease_generation=lease.generation,
            )
            await store.save_delegation_work_item(DelegationWorkItem(
                work_item_id="work-item-custom",
                run_id="run-custom",
                role_id="analyst",
                seat_id="seat-analyst",
                role_runtime_session_id="role-session-analyst",
                title="Custom analyst turn",
                projection_id="custom-analyst",
            ))
            root = Task(
                id="shared-root-work-item",
                project_id="project-a",
                session_id="root-session",
                parent_session_id="root-session",
                status=TaskStatus.PENDING,
                org_id="studio",
                metadata={
                    "mode": "company",
                    "exec_mode": "custom",
                    "execution_mode": "company_mode",
                    "company_profile": "custom",
                    "org_id": "studio",
                    "company_runtime_root_session_id": "root-session",
                    "shared_role_session": True,
                    "delegation_run_id": "run-custom",
                },
            )
            child = Task(
                id="custom-child",
                project_id="project-a",
                session_id="root-session:role:analyst",
                parent_session_id="root-session",
                status=TaskStatus.RUNNING,
                assigned_to="analyst",
                org_id="studio",
                context_snapshot={
                    "runtime_resume": {"runtime_session_id": "rt-child-exact"}
                },
                metadata={
                    "mode": "company",
                    "exec_mode": "custom",
                    "execution_mode": "company_mode",
                    "company_profile": "custom",
                    "org_id": "studio",
                    "company_runtime_root_session_id": "root-session",
                    "work_item_runtime": True,
                    "work_item_projection_id": "custom-analyst",
                    "work_item_role_id": "analyst",
                    "delegation_run_id": "run-custom",
                    "delegation_role_session_id": "role-session-analyst",
                    "delegation_seat_id": "seat-analyst",
                    "workspace_root": str(workspace),
                    "target_output_dir": str(workspace),
                    "skip_verification": True,
                    "conversation_turn_id": "turn-live-child",
                },
            )
            await store.save_task(root)
            await store.save_task(child)
            assert await store.link_work_item_runtime_task(
                "work-item-custom",
                child.id,
            )
            set_linked_work_item_id(child, "work-item-custom")
            claimed = await store.claim_delegation_work_item_if_dispatchable(
                "work-item-custom",
                expected_phase="ready",
                role_runtime_session_id="role-session-analyst",
                seat_id="seat-analyst",
                task_id=child.id,
                controller_owner_token="controller-custom",
                controller_lease_generation=lease.generation,
            )
            assert claimed is not None
            child.metadata.update({
                "company_run_controller_owner_token": "controller-custom",
                "company_run_controller_lease_generation": lease.generation,
                "claimed_work_item_attempt_seq": int(
                    claimed.metadata.get("attempt_seq", 0) or 0
                ),
            })
            await store.save_task(child)
            identity = (
                await load_company_runtime_identity_index(store, "project-a")
            ).resolve(task_id=child.id)
            assert identity is not None
            assert identity.runtime_session_id == "root-session"
            assert identity.ui_anchor_task_id == ""

            approval = _approval(store, engine.interaction_coordinator)

            async def approval_callback(
                tool,
                arguments,
                task,
                on_progress,
                call_context=None,
            ):
                _ = (tool, on_progress)
                return await approval._ask_user(
                    task=task,
                    action_kind="tool",
                    action_name="dangerous_tool",
                    decision=ApprovalDecision(
                        action=ApprovalAction.ESCALATE,
                        risk_level=RiskLevel.HIGH,
                        rationale="requires owner",
                        confidence=1.0,
                        policy_source="test",
                    ),
                    metadata={
                        "arguments": dict(arguments),
                        "tool_call": dict(call_context or {}),
                    },
                )

            registry = ToolRegistry()
            executed: list[str] = []

            async def dangerous_tool(value: str) -> dict[str, str]:
                executed.append(value)
                return {"value": value}

            registry.register(ToolDefinition(
                name="dangerous_tool",
                description="owner-confirmed exact call",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                },
                func=dangerous_tool,
                requires_confirmation=True,
                concurrency_safe=False,
                read_only=True,
                company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            ))
            runtime = NativeRuntimeV2(
                llm=_ToolThenFinishLLM(),
                tool_registry=registry,
                memory_manager=_RuntimeMemory(store),
                config=OPCConfig(),
                max_iterations=2,
                approval_callback=approval_callback,
                interaction_coordinator=engine.interaction_coordinator,
            )
            running = asyncio.create_task(runtime.run(
                system_prompt="system",
                user_message="run the exact call",
                task=child,
            ))
            checkpoint = await _pending_checkpoint(store, "tool_permission")
            ownership = checkpoint.payload["interaction"]["ownership"]
            assert checkpoint.task_id == child.id
            assert ownership["waiting_task_id"] == child.id
            assert ownership["ui_anchor_task_id"] == ""
            assert ownership["ui_anchor_session_id"] == "root-session"
            assert ownership["company_runtime_session_id"] == "root-session"
            assert checkpoint.payload["interaction"]["execution_scope"] == {
                "company_profile": "custom",
                "org_id": "studio",
            }
            assert checkpoint.payload["tool_call"]["id"] == "child-call-exact"
            assert (
                checkpoint.payload["tool_call"]["runtime_session_id"]
                == "rt-child-exact"
            )
            assert not await engine.can_answer_checkpoint(
                checkpoint,
                requester_task_id=child.id,
                requester_session_id=child.session_id,
            )
            accepted = await engine.submit_checkpoint_decision(
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_type=checkpoint.checkpoint_type,
                decision={"option_id": "approve_once"},
                client_request_id="root-session-approval",
                requester_task_id="",
                requester_session_id="root-session",
            )
            assert accepted["accepted"] is True
            result = await asyncio.wait_for(running, timeout=5)
            assert result.status == TaskStatus.DONE
            assert executed == ["child"]
            persisted = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="project-a",
                checkpoint_type="tool_permission",
            )
            assert persisted is not None and persisted.status == "resolved"
            assert len(await store.list_runtime_tool_results("rt-child-exact")) == 1

            changed_events = [
                event.payload
                for event in engine.event_bus.events
                if getattr(event, "event_type", "") == "runtime_event"
                and event.payload.get("type") == "interaction_checkpoint_changed"
            ]
            assert changed_events
            assert all(event["ui_anchor_task_id"] == "" for event in changed_events)
            assert all(
                event["ui_anchor_session_id"] == "root-session"
                for event in changed_events
            )
        finally:
            await engine.interaction_coordinator.shutdown()
            await store.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "checkpoint_type",
    ["task_user_input", "company_work_item_gate", "company_delivery_feedback"],
)
def test_owner_publication_reconnect_uses_canonical_pure_host(
    tmp_path: Path,
    checkpoint_type: str,
) -> None:
    async def scenario() -> None:
        db_path = tmp_path / f"{checkpoint_type}.db"
        first_store = OPCStore(db_path)
        await first_store.initialize()
        first_engine = _engine(first_store)
        second_store: OPCStore | None = None
        try:
            anchor = Task(
                id="ui-anchor",
                project_id="project-a",
                session_id="root-session",
                metadata={
                    "mode": "company",
                    "execution_mode": "company_mode",
                },
            )
            child = Task(
                id="waiting-child",
                project_id="project-a",
                session_id="root-session:role:worker",
                parent_session_id="root-session",
                metadata={
                    "mode": "company",
                    "execution_mode": "company_mode",
                    "company_runtime_root_session_id": "root-session",
                },
            )
            await first_store.save_task(anchor)
            await first_store.save_task(child)
            await first_engine._save_execution_checkpoint({
                "project_id": "project-a",
                "session_id": child.session_id,
                "checkpoint_type": checkpoint_type,
                "task_id": child.id,
                "payload": {
                    "task_id": child.id,
                    "waiting_task_id": child.id,
                    "session_id": child.session_id,
                    "parent_session_id": "root-session",
                    "prompt": f"Owner decision for {checkpoint_type}",
                    "review_level": "human",
                    "source_event_id": f"event:{checkpoint_type}",
                },
            })
            checkpoint = await _pending_checkpoint(first_store, checkpoint_type)
            ownership = checkpoint.payload["interaction"]["ownership"]
            assert ownership["waiting_task_id"] == child.id
            assert ownership["waiting_session_id"] == child.session_id
            assert ownership["ui_anchor_task_id"] == anchor.id
            assert ownership["ui_anchor_session_id"] == "root-session"
            assert not await first_engine.can_answer_checkpoint(
                checkpoint,
                requester_task_id=child.id,
                requester_session_id=child.session_id,
            )

            # Reconnect through an independently initialized controller/store;
            # visibility is derived only from the persisted canonical actor.
            second_store = OPCStore(db_path)
            await second_store.initialize()
            second_engine = _engine(second_store)
            assert await second_engine.can_answer_checkpoint(
                checkpoint,
                requester_task_id=anchor.id,
                requester_session_id="root-session",
            )
            await second_engine.interaction_coordinator.shutdown()
        finally:
            await first_engine.interaction_coordinator.shutdown()
            if second_store is not None:
                await second_store.close()
            await first_store.close()

    asyncio.run(scenario())


def test_company_tool_without_durable_runtime_identity_fails_closed(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        store = OPCStore(tmp_path / "tasks.db")
        await store.initialize()
        coordinator = InteractionCoordinator(store=store, project_id="project-a")
        try:
            approval = _approval(store, coordinator)
            process_local_company_task = Task(
                id="unmapped-child",
                project_id="project-a",
                session_id="unmapped-role-session",
                metadata={
                    "mode": "company",
                    "execution_mode": "company_mode",
                    "work_item_runtime": True,
                },
            )
            with pytest.raises(RuntimeError, match="canonical runtime identity"):
                await approval._approval_interaction_checkpoint(
                    task=process_local_company_task,
                    action_kind="tool",
                    action_name="dangerous_tool",
                    decision=ApprovalDecision(
                        action=ApprovalAction.ESCALATE,
                        risk_level=RiskLevel.HIGH,
                        rationale="requires owner",
                    ),
                    metadata={
                        "arguments": {"value": "child"},
                        "tool_call": {
                            "id": "unmapped-call",
                            "runtime_session_id": "rt-unmapped",
                        },
                    },
                    question="Approve?",
                    options=[{"id": "approve_once", "label": "Approve once"}],
                    approval_context={},
                )
            assert await store.get_execution_checkpoints(
                project_id="project-a",
                checkpoint_types=["tool_permission"],
            ) == []
        finally:
            await coordinator.shutdown()
            await store.close()

    asyncio.run(scenario())


def test_startup_backfills_only_missing_legacy_owner_actor(tmp_path: Path) -> None:
    async def scenario() -> None:
        db_path = tmp_path / "tasks.db"
        first_store = OPCStore(db_path)
        await first_store.initialize()
        try:
            anchor = Task(
                id="legacy-ui-anchor",
                project_id="project-a",
                session_id="root-session",
                metadata={
                    "mode": "company",
                    "execution_mode": "company_mode",
                },
            )
            child = Task(
                id="legacy-waiting-child",
                project_id="project-a",
                session_id="root-session:role:worker",
                parent_session_id="root-session",
                metadata={
                    "mode": "company",
                    "execution_mode": "company_mode",
                    "company_runtime_root_session_id": "root-session",
                },
            )
            await first_store.save_task(anchor)
            await first_store.save_task(child)
            missing = ExecutionCheckpoint(
                checkpoint_id="legacy-missing-owner",
                project_id="project-a",
                session_id=child.session_id,
                checkpoint_type="task_user_input",
                task_id=child.id,
                payload={
                    "task_id": child.id,
                    "waiting_task_id": child.id,
                    "session_id": child.session_id,
                    "parent_session_id": "root-session",
                    "prompt": "Legacy question",
                    "review_level": "human",
                    "interaction": {
                        "kind": "task_user_input",
                        "domain_key": "legacy:missing-owner",
                    },
                },
            )
            wrong = ExecutionCheckpoint(
                checkpoint_id="legacy-wrong-owner",
                project_id="project-a",
                session_id=child.session_id,
                checkpoint_type="company_work_item_gate",
                task_id=child.id,
                payload={
                    "task_id": child.id,
                    "waiting_task_id": child.id,
                    "session_id": child.session_id,
                    "parent_session_id": "root-session",
                    "prompt": "Polluted legacy gate",
                    "review_level": "human",
                    "interaction": {
                        "kind": "company_work_item_gate",
                        "domain_key": "legacy:wrong-owner",
                        "ownership": {
                            "waiting_task_id": child.id,
                            "waiting_session_id": child.session_id,
                            "ui_anchor_task_id": child.id,
                            "ui_anchor_session_id": "root-session",
                        },
                    },
                },
            )
            await first_store.create_owner_interaction_checkpoint(
                missing,
                interaction_key="legacy:missing-owner",
            )
            await first_store.create_owner_interaction_checkpoint(
                wrong,
                interaction_key="legacy:wrong-owner",
            )
        finally:
            await first_store.close()

        reopened = OPCStore(db_path)
        await reopened.initialize()
        engine = _engine(reopened)
        try:
            await engine._reconcile_active_owner_interaction_ownership()
            repaired = await reopened.get_execution_checkpoint(
                missing.checkpoint_id,
                project_id="project-a",
                checkpoint_type=missing.checkpoint_type,
            )
            polluted = await reopened.get_execution_checkpoint(
                wrong.checkpoint_id,
                project_id="project-a",
                checkpoint_type=wrong.checkpoint_type,
            )
            assert repaired is not None
            repaired_actor = repaired.payload["interaction"]["ownership"]
            assert repaired_actor["waiting_task_id"] == child.id
            assert repaired_actor["ui_anchor_task_id"] == anchor.id
            assert repaired_actor["ui_anchor_session_id"] == "root-session"
            assert await engine.can_answer_checkpoint(
                repaired,
                requester_task_id=anchor.id,
                requester_session_id="root-session",
            )

            assert polluted is not None
            assert (
                polluted.payload["interaction"]["ownership"]["ui_anchor_task_id"]
                == child.id
            )
            assert not await engine.can_answer_checkpoint(
                polluted,
                requester_task_id=anchor.id,
                requester_session_id="root-session",
            )
        finally:
            await engine.interaction_coordinator.shutdown()
            await reopened.close()

    asyncio.run(scenario())
