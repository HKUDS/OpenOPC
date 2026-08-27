from __future__ import annotations

import contextlib
import shutil
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from opc.core.config import AutonomyConfig, OPCConfig
from opc.core.models import ApprovalAction, ApprovalDecision, PermissionResolution, RiskLevel, Task, TaskResult, TaskStatus
from opc.layer2_organization.approval import ApprovalEngine
from opc.layer3_agent.runtime_v2.permissions import RuntimePermissionAdapter
from opc.layer3_agent.runtime_v2.runtime import NativeRuntimeV2
from opc.layer3_agent.runtime_v2.streaming_tool_executor import StreamingToolExecutor
from opc.layer3_agent.runtime_v2.subagents import SubagentManager
from opc.layer3_agent.runtime_v2.tool_planner import ToolPlanner
from opc.layer4_tools.registry import ToolDefinition, ToolRegistry


@contextlib.contextmanager
def _workspace_tempdir() -> Path:
    base = Path.cwd() / ".tmp-test" / f"runtime-hooks-{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


class _StubLLM:
    def __init__(self) -> None:
        self.config = type("Cfg", (), {"max_tokens": 2048})()


class _PrefsStub:
    def get_autonomy_preferences(self, project_id=None):
        _ = project_id
        return {"learned_actions": {}}

    def record_autonomy_feedback(self, **kwargs):
        _ = kwargs


class _StoreStub:
    async def record_approval(self, **kwargs):
        _ = kwargs


class _MemoryStub:
    def append_autonomy_event(self, event, project=False):
        _ = (event, project)


def _policy_adapter() -> RuntimePermissionAdapter:
    return RuntimePermissionAdapter(ApprovalEngine(
        llm=object(),
        store=_StoreStub(),
        preferences=_PrefsStub(),
        memory=_MemoryStub(),
        config=AutonomyConfig(),
    ))


class RuntimeHookBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_hook_permission_gate_blocks_execution(self) -> None:
        registry = ToolRegistry()
        executed: list[str] = []

        async def shell_tool(command: str) -> dict[str, str]:
            executed.append(command)
            return {"stdout": command}

        registry.register(ToolDefinition(
            name="shell_exec",
            description="shell",
            parameters={"type": "object", "properties": {"command": {"type": "string"}}},
            func=shell_tool,
            concurrency_safe=False,
            read_only=False,
            requires_confirmation=True,
        ))

        async def approval_callback(tool, arguments, task, on_progress):
            _ = (tool, arguments, task, on_progress)
            return False, ApprovalDecision(
                action=ApprovalAction.ESCALATE,
                risk_level=RiskLevel.HIGH,
                rationale="Need approval first.",
                confidence=1.0,
                policy_source="test",
            )

        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=registry,
            config=OPCConfig(),
            approval_callback=approval_callback,
        )
        task = Task(id="task-hook", session_id="sess-hook", project_id="proj1")
        hook_bus = runtime._build_tool_hook_bus(
            runtime_session_id="rt_hook",
            task=task,
            permission_resolver=_policy_adapter(),
        )
        executor = StreamingToolExecutor(
            registry=registry,
            planner=ToolPlanner(registry),
            permission_resolver=_policy_adapter(),
            hook_bus=hook_bus,
        )

        results = await executor.execute([
            {"id": "call-1", "function": "shell_exec", "arguments": {"command": "git commit -m test"}},
        ], task=task)

        self.assertEqual(executed, [])
        self.assertFalse(results[0]["result"]["success"])
        self.assertEqual(results[0]["permission_decision"].resolution, PermissionResolution.ASK)
        self.assertEqual(results[0]["result"]["approval"]["policy_source"], "test")
        self.assertEqual(
            results[0]["result"]["error"],
            "Tool execution blocked by autonomy policy: Need approval first.",
        )

    async def test_live_durable_human_denial_uses_canonical_exact_result(self) -> None:
        registry = ToolRegistry()
        executed: list[str] = []

        async def dangerous_tool(value: str) -> dict[str, str]:
            executed.append(value)
            return {"value": value}

        registry.register(ToolDefinition(
            name="dangerous_tool",
            description="mutating tool",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
            func=dangerous_tool,
            concurrency_safe=False,
            read_only=False,
            requires_confirmation=True,
        ))
        permit = {
            "id": "call-live-deny",
            "function": "dangerous_tool",
            "arguments": {"value": "must-not-run"},
            "fingerprint": "fingerprint-live-deny",
            "runtime_session_id": "rt-live-deny",
            "checkpoint_id": "checkpoint-live-deny",
            "checkpoint_type": "tool_permission",
            "checkpoint_project_id": "project-live-deny",
            "task_id": "task-live-deny",
            "claim_id": "claim-live-deny",
            "consumer_id": "consumer-live-deny",
            "decision": "deny",
            "approved": False,
            "state": "ready",
        }
        task = Task(
            id="task-live-deny",
            session_id="session-live-deny",
            project_id="project-live-deny",
            context_snapshot={
                "runtime_resume": {
                    "runtime_session_id": "rt-live-deny",
                    "approved_tool_calls": {permit["fingerprint"]: permit},
                }
            },
        )
        reported_checkpoint_id = {"value": permit["checkpoint_id"]}
        reported_fingerprint = {"value": permit["fingerprint"]}
        self_outer = self

        class PermitStore:
            def __init__(self) -> None:
                self.update_calls = 0

            async def get_execution_checkpoint(
                self,
                checkpoint_id: str,
                *,
                project_id: str,
                checkpoint_type: str,
            ) -> object | None:
                self_outer.assertEqual(project_id, "project-live-deny")
                self_outer.assertEqual(checkpoint_type, "tool_permission")
                if checkpoint_id != permit["checkpoint_id"]:
                    return None
                return SimpleNamespace(
                    task_id=task.id,
                    status="consuming",
                    payload={
                        "tool_call": {
                            "id": permit["id"],
                            "runtime_session_id": permit["runtime_session_id"],
                            "fingerprint": permit["fingerprint"],
                        },
                        "interaction": {
                            "ownership": {"waiting_task_id": task.id},
                            "claim": {
                                "claim_id": permit["claim_id"],
                                "consumer_id": permit["consumer_id"],
                            },
                        },
                    },
                )

            async def update_task_runtime_tool_permit(
                self,
                task_id: str,
                *,
                runtime_session_id: str,
                fingerprint: str,
                permit: dict[str, object] | None,
                **_: object,
            ) -> Task:
                self.update_calls += 1
                self_outer.assertEqual(task_id, task.id)
                self_outer.assertEqual(runtime_session_id, "rt-live-deny")
                runtime_resume = dict(task.context_snapshot["runtime_resume"])
                approved_calls = dict(runtime_resume["approved_tool_calls"])
                if permit is None:
                    approved_calls.pop(fingerprint, None)
                else:
                    approved_calls[fingerprint] = dict(permit)
                runtime_resume["approved_tool_calls"] = approved_calls
                task.context_snapshot["runtime_resume"] = runtime_resume
                return task

        async def approval_callback(tool, arguments, callback_task, on_progress, **kwargs):
            _ = (tool, on_progress)
            self.assertEqual(arguments, permit["arguments"])
            self.assertIs(callback_task, task)
            self.assertEqual(kwargs["call_context"]["id"], permit["id"])
            return False, ApprovalDecision(
                action=ApprovalAction.REJECT,
                risk_level=RiskLevel.HIGH,
                rationale="Owner rejected the late unmodeled command.",
                confidence=0.97,
                policy_source="human_escalation",
                metadata={
                    "human_reply": "deny",
                    "approval_checkpoint_id": reported_checkpoint_id["value"],
                    "approved_tool_call_id": permit["id"],
                    "approved_tool_call_fingerprint": reported_fingerprint["value"],
                    "approval_claim_id": permit["claim_id"],
                    "approval_consumer_id": permit["consumer_id"],
                },
            )

        adapter = _policy_adapter()
        permit_store = PermitStore()
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=registry,
            config=OPCConfig(),
            approval_callback=approval_callback,
            memory_manager=SimpleNamespace(store=permit_store),
        )
        executor = StreamingToolExecutor(
            registry=registry,
            planner=ToolPlanner(registry),
            permission_resolver=adapter,
            hook_bus=runtime._build_tool_hook_bus(
                runtime_session_id="rt-live-deny",
                task=task,
                permission_resolver=adapter,
            ),
        )

        results = await executor.execute(
            [{
                "id": permit["id"],
                "function": permit["function"],
                "arguments": dict(permit["arguments"]),
            }],
            task=task,
        )

        self.assertEqual(executed, [])
        result = results[0]["result"]
        self.assertEqual(result["error"], "The owner denied this exact ToolCall.")
        self.assertFalse(result["success"])
        self.assertEqual(
            result["approval"],
            {
                "action": "reject",
                "risk_level": "high",
                "confidence": 0.97,
                "policy_source": "human_escalation",
                "rationale": "Owner rejected the late unmodeled command.",
                "human_reply": "deny",
                "approval_checkpoint_id": "checkpoint-live-deny",
                "approved_tool_call_id": "call-live-deny",
                "approved_tool_call_fingerprint": "fingerprint-live-deny",
                "approval_claim_id": "claim-live-deny",
                "approval_consumer_id": "consumer-live-deny",
                "approval_checkpoint_type": "tool_permission",
                "approval_checkpoint_project_id": "project-live-deny",
            },
        )
        self.assertEqual(
            results[0]["permission_decision"].resolution,
            PermissionResolution.DENY,
        )
        self.assertEqual(
            task.context_snapshot["runtime_resume"]["approved_tool_calls"][
                permit["fingerprint"]
            ]["state"],
            "denied",
        )
        self.assertEqual(permit_store.update_calls, 1)

        # A callback cannot opt into the canonical owner-exact result by
        # merely reporting durable-looking metadata.  The checkpoint and
        # exact permit must still match the active Task/runtime/call.
        task.context_snapshot["runtime_resume"]["approved_tool_calls"] = {
            permit["fingerprint"]: {**permit, "state": "ready"}
        }
        reported_checkpoint_id["value"] = "forged-checkpoint"
        forged_adapter = _policy_adapter()
        forged_executor = StreamingToolExecutor(
            registry=registry,
            planner=ToolPlanner(registry),
            permission_resolver=forged_adapter,
            hook_bus=runtime._build_tool_hook_bus(
                runtime_session_id="rt-live-deny",
                task=task,
                permission_resolver=forged_adapter,
            ),
        )
        forged = await forged_executor.execute(
            [{
                "id": permit["id"],
                "function": permit["function"],
                "arguments": dict(permit["arguments"]),
            }],
            task=task,
        )
        self.assertEqual(
            forged[0]["result"]["error"],
            (
                "Tool execution blocked by autonomy policy: "
                "Owner rejected the late unmodeled command."
            ),
        )
        self.assertEqual(
            task.context_snapshot["runtime_resume"]["approved_tool_calls"][
                permit["fingerprint"]
            ]["state"],
            "ready",
        )
        self.assertEqual(permit_store.update_calls, 1)

        reported_checkpoint_id["value"] = permit["checkpoint_id"]
        reported_fingerprint["value"] = "forged-fingerprint"
        forged_fingerprint_adapter = _policy_adapter()
        forged_fingerprint_executor = StreamingToolExecutor(
            registry=registry,
            planner=ToolPlanner(registry),
            permission_resolver=forged_fingerprint_adapter,
            hook_bus=runtime._build_tool_hook_bus(
                runtime_session_id="rt-live-deny",
                task=task,
                permission_resolver=forged_fingerprint_adapter,
            ),
        )
        forged_fingerprint = await forged_fingerprint_executor.execute(
            [{
                "id": permit["id"],
                "function": permit["function"],
                "arguments": dict(permit["arguments"]),
            }],
            task=task,
        )
        self.assertIn(
            "blocked by autonomy policy",
            forged_fingerprint[0]["result"]["error"],
        )
        self.assertEqual(
            task.context_snapshot["runtime_resume"]["approved_tool_calls"][
                permit["fingerprint"]
            ]["state"],
            "ready",
        )
        self.assertEqual(permit_store.update_calls, 1)

        reported_fingerprint["value"] = permit["fingerprint"]
        no_store_adapter = _policy_adapter()
        no_store_runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=registry,
            config=OPCConfig(),
            approval_callback=approval_callback,
        )
        no_store_executor = StreamingToolExecutor(
            registry=registry,
            planner=ToolPlanner(registry),
            permission_resolver=no_store_adapter,
            hook_bus=no_store_runtime._build_tool_hook_bus(
                runtime_session_id="rt-live-deny",
                task=task,
                permission_resolver=no_store_adapter,
            ),
        )
        no_store = await no_store_executor.execute(
            [{
                "id": permit["id"],
                "function": permit["function"],
                "arguments": dict(permit["arguments"]),
            }],
            task=task,
        )
        self.assertIn(
            "blocked by autonomy policy",
            no_store[0]["result"]["error"],
        )
        self.assertEqual(
            task.context_snapshot["runtime_resume"]["approved_tool_calls"][
                permit["fingerprint"]
            ]["state"],
            "ready",
        )

    async def test_human_denial_has_one_record_owner_and_survives_runtime_resume(self) -> None:
        registry = ToolRegistry()
        executed: list[str] = []

        async def shell_tool(command: str, working_directory: str = "") -> dict[str, str]:
            _ = working_directory
            executed.append(command)
            return {"stdout": command}

        tool = ToolDefinition(
            name="shell_exec",
            description="shell",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "working_directory": {"type": "string"},
                },
            },
            func=shell_tool,
            concurrency_safe=False,
            read_only=False,
            requires_confirmation=True,
        )
        registry.register(tool)

        async def approval_callback(tool, arguments, task, on_progress, **kwargs):
            _ = (tool, arguments, task, on_progress, kwargs)
            return False, ApprovalDecision(
                action=ApprovalAction.REJECT,
                risk_level=RiskLevel.HIGH,
                rationale="Human denied this exact action.",
                confidence=1.0,
                policy_source="human_escalation",
            )

        policy = ApprovalEngine(
            llm=object(),
            store=_StoreStub(),
            preferences=_PrefsStub(),
            memory=_MemoryStub(),
            config=AutonomyConfig(),
        )
        adapter = RuntimePermissionAdapter(policy)
        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=registry,
            config=OPCConfig(),
            approval_callback=approval_callback,
        )
        task = Task(
            id="task-denial-owner",
            session_id="session-denial-owner",
            project_id="company-project",
            assigned_to="investment_analyst",
            metadata={
                "work_item_role_id": "investment_analyst",
                "workspace_root": "/tmp/company-case",
                "runtime_session_id": "rt_before_restart",
            },
        )
        arguments = {
            "command": "python3 -m json.tool investment_case/company_analysis.json",
            "working_directory": "/tmp/company-case",
        }
        hook_bus = runtime._build_tool_hook_bus(
            runtime_session_id="rt_before_restart",
            task=task,
            permission_resolver=adapter,
        )
        executor = StreamingToolExecutor(
            registry=registry,
            planner=ToolPlanner(registry),
            permission_resolver=adapter,
            hook_bus=hook_bus,
        )

        first = await executor.execute(
            [{"id": "call-denial-1", "function": "shell_exec", "arguments": arguments}],
            task=task,
        )
        self.assertFalse(first[0]["result"]["success"])
        self.assertEqual(executed, [])
        after_one_human_denial = adapter.predicted_decision(tool, arguments, task=task)
        self.assertEqual(after_one_human_denial.resolution, PermissionResolution.ASK)
        self.assertNotEqual(after_one_human_denial.source, "denial_memory")

        second = await executor.execute(
            [{"id": "call-denial-2", "function": "shell_exec", "arguments": arguments}],
            task=task,
        )
        self.assertFalse(second[0]["result"]["success"])

        # A NativeRuntime/adapter restart does not enter the denial key; the
        # same durable Task, role, and exact command retain the existing count.
        resumed_task = Task(
            id=task.id,
            session_id=task.session_id,
            project_id=task.project_id,
            assigned_to=task.assigned_to,
            metadata={
                "work_item_role_id": "investment_analyst",
                "workspace_root": "/tmp/company-case",
                "runtime_session_id": "rt_after_restart",
            },
        )
        resumed_adapter = RuntimePermissionAdapter(policy)
        repeated = resumed_adapter.predicted_decision(tool, arguments, task=resumed_task)
        self.assertEqual(repeated.resolution, PermissionResolution.DENY)
        self.assertEqual(repeated.source, "denial_memory")
        self.assertEqual(repeated.metadata["repeated_denials"], 2)

    async def test_parallel_batch_failure_converges_remaining_calls(self) -> None:
        registry = ToolRegistry()
        executed: list[str] = []

        async def fail_read(path: str) -> dict[str, str]:
            raise RuntimeError(f"boom:{path}")

        async def second_read(path: str) -> dict[str, str]:
            executed.append(path)
            return {"content": path}

        registry.register(ToolDefinition(
            name="file_read",
            description="read",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            func=fail_read,
            concurrency_safe=True,
            read_only=True,
        ))
        registry.register(ToolDefinition(
            name="web_fetch",
            description="fetch",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            func=second_read,
            concurrency_safe=True,
            read_only=True,
        ))

        runtime = NativeRuntimeV2(
            llm=_StubLLM(),
            tool_registry=registry,
            config=OPCConfig(),
        )
        hook_bus = runtime._build_tool_hook_bus(
            runtime_session_id="rt_parallel",
            task=Task(id="task-parallel", session_id="sess-parallel", project_id="proj1"),
            permission_resolver=_policy_adapter(),
        )
        executor = StreamingToolExecutor(
            registry=registry,
            planner=ToolPlanner(registry, max_parallel_read_tools=1),
            permission_resolver=_policy_adapter(),
            hook_bus=hook_bus,
            max_parallel_read_tools=1,
            converge_on_parallel_failure=True,
        )

        results = await executor.execute([
            {"id": "call-a", "function": "file_read", "arguments": {"path": "a.txt"}},
            {"id": "call-b", "function": "web_fetch", "arguments": {"path": "b.txt"}},
        ])

        self.assertFalse(results[0]["result"]["success"])
        self.assertFalse(results[1]["result"]["success"])
        self.assertTrue(results[1]["result"]["converged"])
        self.assertEqual(executed, [])


class SubagentPermissionBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_permission_bridge_preserves_child_execution_identity(self) -> None:
        captured: dict[str, object] = {}

        class _ApprovalEngine:
            async def authorize_tool_call(self, **kwargs):
                captured["approval_kwargs"] = dict(kwargs)
                return True, ApprovalDecision(
                    action=ApprovalAction.AUTO_APPROVE,
                    risk_level=RiskLevel.LOW,
                    rationale="approved",
                    confidence=1.0,
                    policy_source="human_escalation",
                    metadata={"human_reply": "approve_session"},
                )

        approval_engine = _ApprovalEngine()

        class _ChildAgent:
            async def execute(self, child_task: Task) -> TaskResult:
                captured["child_task_metadata"] = dict(child_task.metadata)
                bridge = getattr(child_task, "_runtime_permission_bridge")
                tool = ToolDefinition(
                    name="shell_exec",
                    description="shell",
                    parameters={"type": "object", "properties": {"command": {"type": "string"}}},
                    func=lambda **_: None,  # type: ignore[arg-type]
                    concurrency_safe=False,
                    read_only=False,
                    requires_confirmation=True,
                )
                allowed, decision = await bridge(
                    tool=tool,
                    arguments={"command": "git status"},
                    approval_engine=approval_engine,
                    on_progress=None,
                    call_context={
                        "id": "child-call-1",
                        "runtime_session_id": "rt_child",
                    },
                )
                captured["allowed"] = allowed
                captured["decision"] = decision
                return TaskResult(status=TaskStatus.DONE, content="bridge-ok")

        manager = SubagentManager(
            parent_task=Task(id="parent-task", session_id="parent-session", project_id="proj1"),
            config=OPCConfig(),
            child_agent_factory=lambda profile, allowed_tools, prompt, overrides: _ChildAgent(),
            runtime_session_id="rt_parent",
        )

        result = await manager.spawn(
            profile="implement",
            prompt="Need approval",
            background=False,
            name="bridge-worker",
        )

        self.assertTrue(result["success"])
        self.assertTrue(captured["allowed"])
        approval_kwargs = dict(captured["approval_kwargs"])
        self.assertNotEqual(approval_kwargs["task"].id, "parent-task")
        self.assertEqual(approval_kwargs["task"].parent_id, "parent-task")
        self.assertEqual(approval_kwargs["task"].parent_session_id, "parent-session")
        self.assertEqual(
            approval_kwargs["call_context"]["runtime_session_id"],
            "rt_child",
        )
        self.assertEqual(approval_kwargs["metadata"]["subagent_name"], "bridge-worker")
        self.assertEqual(captured["child_task_metadata"]["_permission_bridge_runtime_session_id"], "rt_parent")


if __name__ == "__main__":
    unittest.main()
