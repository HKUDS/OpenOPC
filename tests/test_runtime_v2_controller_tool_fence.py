from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from opc.core.company_controller import CompanyRunControllerLeaseLost
from opc.core.config import AutonomyConfig, OPCConfig
from opc.core.models import PermissionResolution, Task
from opc.layer2_organization.approval import ApprovalEngine
from opc.layer3_agent.runtime_v2.permissions import RuntimePermissionAdapter
from opc.layer3_agent.runtime_v2.runtime import NativeRuntimeV2
from opc.layer3_agent.runtime_v2.streaming_tool_executor import StreamingToolExecutor
from opc.layer3_agent.runtime_v2.tool_hooks import (
    RuntimeCompanyControllerToolFence,
    RuntimeToolHookBus,
)
from opc.layer3_agent.runtime_v2.tool_planner import ToolPlanner
from opc.layer4_tools.registry import (
    COMPANY_EFFECT_NO_LOCAL_FS,
    COMPANY_EFFECT_RUNTIME_INTERNAL,
    ToolDefinition,
    ToolRegistry,
)


class _ApprovalPreferences:
    def get_autonomy_preferences(self, project_id: str | None = None) -> dict[str, Any]:
        _ = project_id
        return {"learned_actions": {}}

    def record_autonomy_feedback(self, **kwargs: Any) -> None:
        _ = kwargs


class _ApprovalStore:
    async def record_approval(self, **kwargs: Any) -> None:
        _ = kwargs


class _ApprovalMemory:
    def append_autonomy_event(self, event: Any, project: bool = False) -> None:
        _ = (event, project)


def _company_permission_adapter() -> RuntimePermissionAdapter:
    return RuntimePermissionAdapter(
        ApprovalEngine(
            llm=object(),
            store=_ApprovalStore(),
            preferences=_ApprovalPreferences(),
            memory=_ApprovalMemory(),
            config=AutonomyConfig(),
        )
    )


class _LeaseStore:
    def __init__(self, decisions: list[bool] | None = None) -> None:
        self.decisions = list(decisions or [])
        self.calls: list[dict[str, Any]] = []

    async def delegation_run_controller_lease_is_current(
        self,
        run_id: str,
        *,
        project_id: str,
        owner_token: str,
        generation: int,
    ) -> bool:
        self.calls.append(
            {
                "run_id": run_id,
                "project_id": project_id,
                "owner_token": owner_token,
                "generation": generation,
            }
        )
        if self.decisions:
            return self.decisions.pop(0)
        return True


def _company_task() -> Task:
    return Task(
        id="company-task",
        session_id="role-session",
        project_id="project-one",
        metadata={
            "execution_mode": "company_mode",
            "delegation_run_id": "run-one",
            "company_run_controller_owner_token": "owner-one",
            "company_run_controller_lease_generation": 7,
            "claimed_work_item_attempt_seq": 1,
        },
    )


def _executor(
    registry: ToolRegistry,
    *,
    store: Any = None,
    runtime_tool_handler: Any = None,
    hook_bus: RuntimeToolHookBus | None = None,
    permission_resolver: RuntimePermissionAdapter | None = None,
) -> StreamingToolExecutor:
    return StreamingToolExecutor(
        registry=registry,
        planner=ToolPlanner(registry),
        permission_resolver=permission_resolver or RuntimePermissionAdapter(),
        controller_tool_fence=RuntimeCompanyControllerToolFence(store=store),
        runtime_tool_handler=runtime_tool_handler,
        hook_bus=hook_bus,
    )


class RuntimeV2ControllerToolFenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_auto_allowed_company_tool_never_calls_registry_handler(self) -> None:
        registry = ToolRegistry()
        effects: list[str] = []

        async def send_dm(recipient_id: str, message: str) -> dict[str, str]:
            effects.append(f"{recipient_id}:{message}")
            return {"delivered": recipient_id}

        registry.register(
            ToolDefinition(
                name="send_dm",
                description="company collaboration message",
                parameters={"type": "object", "properties": {}},
                func=send_dm,
                concurrency_safe=False,
                read_only=False,
                company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL,
            )
        )
        permission_resolver = _company_permission_adapter()
        predicted = permission_resolver.predicted_decision(
            registry.get("send_dm"),
            {"recipient_id": "risk", "message": "status"},
            task=_company_task(),
        )
        self.assertEqual(predicted.resolution, PermissionResolution.ALLOW)
        self.assertEqual(predicted.source, "company_tool_policy")
        lease_store = _LeaseStore([False])

        with self.assertRaises(CompanyRunControllerLeaseLost):
            await _executor(
                registry,
                store=lease_store,
                permission_resolver=permission_resolver,
            ).execute(
                [
                    {
                        "id": "call-stale",
                        "function": "send_dm",
                        "arguments": {"recipient_id": "risk", "message": "status"},
                    }
                ],
                task=_company_task(),
            )

        self.assertEqual(effects, [])
        self.assertEqual(len(lease_store.calls), 1)

    async def test_valid_runtime_managed_tool_calls_handler_once(self) -> None:
        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="agent_send",
                description="runtime-managed message",
                parameters={"type": "object", "properties": {}},
                func=lambda **_: None,  # type: ignore[arg-type]
                runtime_managed=True,
                concurrency_safe=False,
                read_only=False,
                company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL,
            )
        )
        effects: list[tuple[str, dict[str, Any]]] = []

        async def runtime_handler(
            tool_name: str,
            arguments: dict[str, Any],
        ) -> dict[str, Any]:
            effects.append((tool_name, dict(arguments)))
            return {"success": True, "result": {"sent": True}}

        lease_store = _LeaseStore([True, True])
        results = await _executor(
            registry,
            store=lease_store,
            runtime_tool_handler=runtime_handler,
        ).execute(
            [
                {
                    "id": "call-runtime",
                    "function": "agent_send",
                    "arguments": {"agent_id": "worker", "message": "continue"},
                }
            ],
            task=_company_task(),
        )

        self.assertTrue(results[0]["result"]["success"])
        self.assertEqual(
            effects,
            [("agent_send", {"agent_id": "worker", "message": "continue"})],
        )
        self.assertEqual(len(lease_store.calls), 2)

    async def test_lease_change_during_handler_discards_result_before_post_hooks(self) -> None:
        registry = ToolRegistry()
        effects: list[str] = []
        post_hooks: list[str] = []

        async def send_dm(recipient_id: str, message: str) -> dict[str, str]:
            effects.append(f"{recipient_id}:{message}")
            return {"delivered": recipient_id}

        registry.register(
            ToolDefinition(
                name="send_dm",
                description="company collaboration message",
                parameters={"type": "object", "properties": {}},
                func=send_dm,
                concurrency_safe=False,
                read_only=False,
                company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL,
            )
        )
        hook_bus = RuntimeToolHookBus()

        async def post_hook(context: Any) -> dict[str, Any]:
            post_hooks.append(context.tool_name)
            return {}

        hook_bus.register_post_hook("must-not-run", post_hook)
        lease_store = _LeaseStore([True, False])

        with self.assertRaises(CompanyRunControllerLeaseLost):
            await _executor(
                registry,
                store=lease_store,
                hook_bus=hook_bus,
                permission_resolver=_company_permission_adapter(),
            ).execute(
                [
                    {
                        "id": "call-takeover",
                        "function": "send_dm",
                        "arguments": {"recipient_id": "risk", "message": "status"},
                    }
                ],
                task=_company_task(),
            )

        self.assertEqual(effects, ["risk:status"])
        self.assertEqual(post_hooks, [])
        self.assertEqual(len(lease_store.calls), 2)

    async def test_parallel_company_calls_each_cross_the_fence(self) -> None:
        registry = ToolRegistry()
        effects: list[str] = []

        async def manager_board_read(section: str) -> dict[str, str]:
            effects.append(section)
            return {"section": section}

        registry.register(
            ToolDefinition(
                name="manager_board_read",
                description="read company board",
                parameters={"type": "object", "properties": {}},
                func=manager_board_read,
                concurrency_safe=True,
                read_only=True,
                company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL,
            )
        )
        lease_store = _LeaseStore([True, True, True, True])
        results = await _executor(
            registry,
            store=lease_store,
            permission_resolver=_company_permission_adapter(),
        ).execute(
            [
                {
                    "id": "call-a",
                    "function": "manager_board_read",
                    "arguments": {"section": "ready"},
                },
                {
                    "id": "call-b",
                    "function": "manager_board_read",
                    "arguments": {"section": "review"},
                },
            ],
            task=_company_task(),
        )

        self.assertEqual(len(results), 2)
        self.assertCountEqual(effects, ["ready", "review"])
        self.assertEqual(len(lease_store.calls), 4)

    async def test_credential_with_unavailable_store_fails_closed(self) -> None:
        registry = ToolRegistry()
        effects: list[str] = []

        async def file_read(path: str) -> dict[str, str]:
            effects.append(path)
            return {"content": path}

        registry.register(
            ToolDefinition(
                name="file_read",
                description="read file",
                parameters={"type": "object", "properties": {}},
                func=file_read,
                concurrency_safe=True,
                read_only=True,
                company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            )
        )

        with self.assertRaises(CompanyRunControllerLeaseLost):
            await _executor(registry).execute(
                [
                    {
                        "id": "call-no-store",
                        "function": "file_read",
                        "arguments": {"path": "evidence.txt"},
                    }
                ],
                task=_company_task(),
            )

        self.assertEqual(effects, [])

    async def test_ordinary_non_company_tool_is_unchanged(self) -> None:
        registry = ToolRegistry()
        effects: list[str] = []

        async def file_read(path: str) -> dict[str, str]:
            effects.append(path)
            return {"content": path}

        registry.register(
            ToolDefinition(
                name="file_read",
                description="read file",
                parameters={"type": "object", "properties": {}},
                func=file_read,
                concurrency_safe=True,
                read_only=True,
                company_effect_kind=COMPANY_EFFECT_NO_LOCAL_FS,
            )
        )
        task = Task(
            id="ordinary-task",
            session_id="ordinary-session",
            project_id="project-one",
        )
        results = await _executor(registry).execute(
            [
                {
                    "id": "call-ordinary",
                    "function": "file_read",
                    "arguments": {"path": "README.md"},
                }
            ],
            task=task,
        )

        self.assertTrue(results[0]["result"]["success"])
        self.assertEqual(effects, ["README.md"])

    def test_runtime_resolves_interaction_coordinator_store_fallback(self) -> None:
        registry = ToolRegistry()
        lease_store = _LeaseStore()
        runtime = NativeRuntimeV2(
            llm=SimpleNamespace(),
            tool_registry=registry,
            memory_manager=SimpleNamespace(store=object()),
            interaction_coordinator=SimpleNamespace(store=lease_store),
            config=OPCConfig(),
        )

        self.assertIs(runtime._controller_tool_fence_store(), lease_store)
