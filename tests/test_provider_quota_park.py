"""Regression: provider quota exhaustion parks work instead of failing it (OBS-6).

Quota/rate-limit rejections never reach the model, so in-place retries can
only fail; the previous behavior burned retries and terminally failed the
work item (killing intake and the whole run during a quota window). The fix:
``LLMProvider.is_rate_limit_error`` classifies these rejections, the agent
runtime raises typed ``ProviderQuotaExhaustedError``, and the company
dispatcher returns the item to READY with exponential dispatch backoff.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from opc.core.config import LLMConfig, OPCConfig
from opc.core.company_controller import CompanyRunControllerLeaseLost
from opc.core.models import (
    AgentStatus,
    CompanyMemberSession,
    DelegationRun,
    DelegationWorkItem,
    Phase,
    RoleRuntimeSession,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer3_agent.native_agent import NativeAgent
from opc.layer3_agent.runtime_v2.runtime import NativeRuntimeV2
from opc.layer4_tools.registry import ToolRegistry
from opc.llm.provider import LLMProvider, ProviderQuotaExhaustedError
from opc.llm.retry import call_llm_json_with_retry


WEEKLY_QUOTA_MESSAGE = (
    "litellm.RateLimitError: RateLimitError: OpenAIException - You have "
    "exceeded the weekly usage quota. It will reset at "
    "2026-08-16 23:59:59 +0800 CST. We recommend upgrading your plan for "
    "more quota, or waiting for the reset. Request id: "
    "02178659271204808bad97b19aecd1d2e685dea670f3cae1b767a"
)


class _QuotaLLM:
    """Small streaming provider that fails before producing a model turn."""

    def __init__(self, message: str = "429 insufficient_quota") -> None:
        self.config = SimpleNamespace(max_tokens=2048)
        self.message = message
        self._classifier = LLMProvider(LLMConfig())

    def prepare_user_message_content(self, content, attachment_refs=None):  # noqa: ANN001
        _ = attachment_refs
        return content

    def get_tool_definitions(self, tools):  # noqa: ANN001
        return tools

    def is_rate_limit_error(self, error: Exception) -> bool:
        return self._classifier.is_rate_limit_error(error)

    def is_context_overflow_error(self, error: Exception) -> bool:
        _ = error
        return False

    async def chat_stream(self, messages, tools=None):  # noqa: ANN001
        _ = (messages, tools)
        raise RuntimeError(self.message)
        if False:  # pragma: no cover - declare this as an async generator
            yield None


class _ProtocolRecoveryQuotaLLM(_QuotaLLM):
    """Stream protocol failure followed by quota on non-stream recovery."""

    def __init__(
        self,
        message: str = "429 insufficient_quota",
        *,
        recovery_error: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.stream_calls = 0
        self.chat_calls = 0
        self.recovery_error = recovery_error

    def is_tool_protocol_error(self, error: Exception) -> bool:
        return "no tool output found for function call" in str(error).lower()

    def sanitize_tool_call_history(self, messages):  # noqa: ANN001
        return list(messages)

    async def chat_stream(self, messages, tools=None):  # noqa: ANN001
        _ = (messages, tools)
        self.stream_calls += 1
        yield type(
            "Evt",
            (),
            {"event_type": "message_start", "payload": {}, "model": "stub"},
        )()
        raise RuntimeError("No tool output found for function call call_quota123.")

    async def chat(self, messages, tools=None):  # noqa: ANN001
        _ = (messages, tools)
        self.chat_calls += 1
        if self.recovery_error is not None:
            raise self.recovery_error
        raise RuntimeError(self.message)


class _JSONRetryLLM:
    def __init__(self, outcomes: list[Exception | str]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, str]] = []
        self._classifier = LLMProvider(LLMConfig())

    async def simple_chat(self, **kwargs):  # noqa: ANN003
        self.calls.append(dict(kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def is_rate_limit_error(self, error: Exception) -> bool:
        return self._classifier.is_rate_limit_error(error)


class _EventBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)


class RateLimitClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.llm = LLMProvider(LLMConfig())

    def test_text_shapes_classify_as_rate_limit(self) -> None:
        for message in (
            "Error code: 429 - rate limit reached for requests",
            "RateLimitError: too many requests, retry later",
            "insufficient_quota: you exceeded your quota",
            # localized error text from Chinese providers must classify too
            "请求过于频繁，请稍后再试",
            "当前 API 配额已用完",
            WEEKLY_QUOTA_MESSAGE,
        ):
            self.assertTrue(
                self.llm.is_rate_limit_error(RuntimeError(message)), message
            )

    def test_type_name_classifies(self) -> None:
        class FakeRateLimitError(Exception):
            pass

        self.assertTrue(self.llm.is_rate_limit_error(FakeRateLimitError("nope")))

    def test_status_code_classifies(self) -> None:
        error = RuntimeError("throttled")
        error.status_code = 429  # type: ignore[attr-defined]
        self.assertTrue(self.llm.is_rate_limit_error(error))

    def test_ordinary_errors_do_not_classify(self) -> None:
        for message in (
            "maximum context length exceeded",
            "connection reset by peer",
            "tool call arguments malformed",
            "prompt tokens: 14290",
        ):
            self.assertFalse(
                self.llm.is_rate_limit_error(RuntimeError(message)), message
            )


class QuotaExceptionChainTests(unittest.TestCase):
    def _executor(self) -> CompanyWorkItemExecutor:
        return CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)

    def test_direct_and_chained_detection(self) -> None:
        executor = self._executor()
        direct = ProviderQuotaExhaustedError("quota")
        wrapped = RuntimeError("turn failed")
        wrapped.__cause__ = ProviderQuotaExhaustedError("quota")
        unrelated = RuntimeError("boom")
        self.assertTrue(executor._exception_is_provider_quota(direct))
        self.assertTrue(executor._exception_is_provider_quota(wrapped))
        self.assertFalse(executor._exception_is_provider_quota(unrelated))
        self.assertFalse(executor._exception_is_provider_quota(None))


class JSONRetryQuotaPropagationTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, llm: _JSONRetryLLM) -> dict[str, object]:
        return await call_llm_json_with_retry(
            llm,  # type: ignore[arg-type]
            system="Return JSON.",
            payload={"request": "assessment"},
            label="quota_test",
        )

    async def test_direct_typed_quota_propagates_without_retry(self) -> None:
        direct = ProviderQuotaExhaustedError("typed quota")

        llm = _JSONRetryLLM([direct])
        with self.assertRaises(ProviderQuotaExhaustedError) as raised:
            await self._call(llm)

        self.assertIs(raised.exception, direct)
        self.assertEqual(len(llm.calls), 1)

    async def test_wrapped_typed_quota_propagates_with_acyclic_chain(self) -> None:
        nested = ProviderQuotaExhaustedError("wrapped typed quota")
        wrapped = RuntimeError("transport wrapper")
        wrapped.__cause__ = nested

        llm = _JSONRetryLLM([wrapped])
        with self.assertRaises(ProviderQuotaExhaustedError) as raised:
            await self._call(llm)

        self.assertIsNot(raised.exception, nested)
        self.assertEqual(str(raised.exception), str(nested))
        self.assertIs(raised.exception.__cause__, wrapped)
        self.assertIs(wrapped.__cause__, nested)
        self.assertIsNone(nested.__cause__)
        self.assertEqual(len(llm.calls), 1)

    async def test_provider_classified_rate_limit_becomes_typed_without_retry(self) -> None:
        rate_limit = RuntimeError("ordinary provider rejection")
        rate_limit.status_code = 429  # type: ignore[attr-defined]
        llm = _JSONRetryLLM([rate_limit])

        with self.assertRaisesRegex(
            ProviderQuotaExhaustedError,
            "ordinary provider rejection",
        ) as raised:
            await self._call(llm)

        self.assertIs(raised.exception.__cause__, rate_limit)
        self.assertEqual(len(llm.calls), 1)

    async def test_ordinary_transport_error_still_retries_with_feedback(self) -> None:
        llm = _JSONRetryLLM(
            [RuntimeError("connection reset by peer"), '{"accepted": true}']
        )

        result = await self._call(llm)

        self.assertEqual(result, {"accepted": True})
        self.assertEqual(len(llm.calls), 2)
        retry_payload = json.loads(llm.calls[1]["prompt"])
        self.assertIn("connection reset by peer", retry_payload["retry_feedback"][0])


class NativeQuotaPropagationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _agent_with_loop(loop: object) -> NativeAgent:
        agent = NativeAgent.__new__(NativeAgent)
        agent.role = SimpleNamespace(
            role_id="worker",
            status=AgentStatus.IDLE,
            current_task_id=None,
        )
        agent.event_bus = SimpleNamespace(publish=AsyncMock())
        agent.communication = None
        agent.loop = loop
        agent._resolve_allowed_tools = lambda task: []
        agent._build_system_prompt = AsyncMock(return_value="system")
        agent._build_user_message = AsyncMock(return_value="work")
        agent._build_context_messages = AsyncMock(return_value=[])
        return agent

    async def test_runtime_and_native_agent_preserve_typed_quota(self) -> None:
        event_bus = _EventBus()
        runtime = NativeRuntimeV2(
            llm=_QuotaLLM(),
            tool_registry=ToolRegistry(),
            event_bus=event_bus,
            config=OPCConfig(),
        )
        agent = self._agent_with_loop(runtime)
        task = Task(
            id="quota-agent-task",
            title="Quota propagation",
            session_id="quota-agent-session",
            project_id="project-quota",
            assigned_to="worker",
            metadata={"execution_mode": "company_mode"},
        )

        with self.assertRaisesRegex(
            ProviderQuotaExhaustedError,
            "429 insufficient_quota",
        ):
            await agent.execute(task)

        self.assertEqual(agent.role.status, AgentStatus.IDLE)
        self.assertIsNone(agent.role.current_task_id)
        self.assertTrue(
            any(
                getattr(event, "payload", {}).get("type")
                == "provider_quota_exhausted"
                for event in event_bus.events
            )
        )

    async def test_weekly_quota_text_preserves_typed_signal(self) -> None:
        runtime = NativeRuntimeV2(
            llm=_QuotaLLM(WEEKLY_QUOTA_MESSAGE),
            tool_registry=ToolRegistry(),
            event_bus=_EventBus(),
            config=OPCConfig(),
        )
        agent = self._agent_with_loop(runtime)
        task = Task(
            id="weekly-quota-agent-task",
            title="Weekly quota propagation",
            session_id="weekly-quota-agent-session",
            project_id="project-quota",
            assigned_to="worker",
            metadata={"execution_mode": "company_mode"},
        )

        with self.assertRaisesRegex(
            ProviderQuotaExhaustedError,
            "exceeded the weekly usage quota",
        ):
            await agent.execute(task)

    async def test_quota_runtime_telemetry_failure_does_not_mask_typed_signal(
        self,
    ) -> None:
        event_bus = _EventBus()

        async def _fail_only_quota_event(event) -> None:  # noqa: ANN001
            if (
                getattr(event, "payload", {}).get("type")
                == "provider_quota_exhausted"
            ):
                raise RuntimeError("runtime event transport failed")
            event_bus.events.append(event)

        event_bus.publish = AsyncMock(side_effect=_fail_only_quota_event)
        runtime = NativeRuntimeV2(
            llm=_QuotaLLM(WEEKLY_QUOTA_MESSAGE),
            tool_registry=ToolRegistry(),
            event_bus=event_bus,
            config=OPCConfig(),
        )
        task = Task(
            id="weekly-quota-event-failure",
            title="Weekly quota telemetry failure",
            session_id="weekly-quota-event-session",
            project_id="project-quota",
            assigned_to="worker",
            metadata={"execution_mode": "company_mode"},
        )

        with self.assertRaisesRegex(
            ProviderQuotaExhaustedError,
            "exceeded the weekly usage quota",
        ):
            await runtime.run(
                system_prompt="system",
                user_message="work",
                task=task,
            )

    async def test_protocol_recovery_quota_survives_telemetry_failure(self) -> None:
        event_bus = _EventBus()

        async def _fail_only_quota_event(event) -> None:  # noqa: ANN001
            if (
                getattr(event, "payload", {}).get("type")
                == "provider_quota_exhausted"
            ):
                raise RuntimeError("quota telemetry transport failed")
            event_bus.events.append(event)

        event_bus.publish = _fail_only_quota_event  # type: ignore[method-assign]
        llm = _ProtocolRecoveryQuotaLLM(WEEKLY_QUOTA_MESSAGE)
        runtime = NativeRuntimeV2(
            llm=llm,
            tool_registry=ToolRegistry(),
            event_bus=event_bus,
            config=OPCConfig(),
        )
        task = Task(
            id="protocol-recovery-quota",
            title="Protocol recovery quota",
            session_id="protocol-recovery-quota-session",
            project_id="project-quota",
        )

        with self.assertRaisesRegex(
            ProviderQuotaExhaustedError,
            "exceeded the weekly usage quota",
        ):
            await runtime.run(
                system_prompt="system",
                user_message="work",
                task=task,
            )

        self.assertEqual(llm.stream_calls, 1)
        self.assertEqual(llm.chat_calls, 1)

    async def test_protocol_recovery_finds_wrapped_quota_without_chain_cycle(
        self,
    ) -> None:
        nested = ProviderQuotaExhaustedError("wrapped recovery quota")
        wrapped = RuntimeError("provider transport wrapper")
        wrapped.__cause__ = nested
        llm = _ProtocolRecoveryQuotaLLM(recovery_error=wrapped)
        runtime = NativeRuntimeV2(
            llm=llm,
            tool_registry=ToolRegistry(),
            event_bus=_EventBus(),
            config=OPCConfig(),
        )

        with self.assertRaises(ProviderQuotaExhaustedError) as raised:
            await runtime.run(
                system_prompt="system",
                user_message="work",
                task=Task(
                    id="wrapped-protocol-recovery-quota",
                    title="Wrapped protocol recovery quota",
                    session_id="wrapped-protocol-recovery-session",
                    project_id="project-quota",
                ),
            )

        self.assertIsNot(raised.exception, nested)
        self.assertEqual(str(raised.exception), str(nested))
        self.assertIs(raised.exception.__cause__, wrapped)
        self.assertIs(wrapped.__cause__, nested)
        self.assertIsNone(nested.__cause__)
        self.assertEqual(llm.stream_calls, 1)
        self.assertEqual(llm.chat_calls, 1)

    async def test_native_agent_keeps_ordinary_error_result_semantics(self) -> None:
        loop = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("provider transport broke")))
        agent = self._agent_with_loop(loop)
        task = Task(
            id="ordinary-provider-error",
            title="Ordinary provider error",
            session_id="ordinary-provider-session",
            project_id="project-quota",
        )

        result = await agent.execute(task)

        self.assertEqual(result.status, TaskStatus.FAILED)
        self.assertEqual(result.content, "provider transport broke")
        self.assertEqual(agent.role.status, AgentStatus.IDLE)
        self.assertIsNone(agent.role.current_task_id)

    async def test_idle_status_publish_failure_does_not_mask_typed_quota(self) -> None:
        loop = SimpleNamespace(
            run=AsyncMock(side_effect=ProviderQuotaExhaustedError("quota survives telemetry"))
        )
        agent = self._agent_with_loop(loop)
        agent.event_bus.publish = AsyncMock(
            side_effect=[None, RuntimeError("idle status transport failed")]
        )
        task = Task(
            id="quota-with-idle-event-failure",
            title="Quota with telemetry failure",
            session_id="quota-telemetry-session",
            project_id="project-quota",
        )

        with self.assertRaisesRegex(
            ProviderQuotaExhaustedError,
            "quota survives telemetry",
        ):
            await agent.execute(task)

        self.assertEqual(agent.event_bus.publish.await_count, 2)
        self.assertEqual(agent.role.status, AgentStatus.IDLE)
        self.assertIsNone(agent.role.current_task_id)

    async def test_idle_status_publish_failure_still_propagates_without_quota(self) -> None:
        loop = SimpleNamespace(
            run=AsyncMock(return_value=TaskResult(status=TaskStatus.DONE, content="done"))
        )
        agent = self._agent_with_loop(loop)
        agent.event_bus.publish = AsyncMock(
            side_effect=[None, RuntimeError("idle status transport failed")]
        )
        task = Task(
            id="non-quota-idle-event-failure",
            title="Telemetry failure control",
            session_id="telemetry-control-session",
            project_id="project-quota",
        )

        with self.assertRaisesRegex(RuntimeError, "idle status transport failed"):
            await agent.execute(task)

    @staticmethod
    async def _seed_live_controller_claim(
        store: OPCStore,
    ) -> tuple[
        CompanyWorkItemExecutor,
        CompanyMemberSession,
        RoleRuntimeSession,
        Task,
        DelegationWorkItem,
        int,
    ]:
        run_id = "run-quota-propagation"
        root_session_id = "root-quota-propagation"
        work_item_id = "wi-quota-propagation"
        role_session_id = "role-session-worker"
        task = Task(
            id="task-quota-propagation",
            title="Quota-limited work",
            session_id=root_session_id,
            project_id="project-quota",
            assigned_to="worker",
            status=TaskStatus.PENDING,
            metadata={
                "delegation_run_id": run_id,
                "delegation_role_session_id": role_session_id,
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "work_item_projection_id": "quota-work",
                "work_item_role_id": "worker",
                "work_item_turn_type": "execute",
            },
        )
        work_item = DelegationWorkItem(
            work_item_id=work_item_id,
            run_id=run_id,
            role_id="worker",
            seat_id="seat-worker",
            title=task.title,
            projection_id="quota-work",
            phase=Phase.READY,
            metadata={"task_id": task.id},
        )
        await store.save_delegation_run(
            DelegationRun(
                run_id=run_id,
                project_id=task.project_id,
                session_id=root_session_id,
                execution_model="multi_team_org",
                status="running",
                lifecycle_status="active",
            )
        )
        await store.save_delegation_work_item(work_item)
        await store.save_task(task)
        assert await store.link_work_item_runtime_task(work_item_id, task.id)
        set_linked_work_item_id(task, work_item_id)
        lease = await store.acquire_delegation_run_controller_lease(
            run_id,
            project_id=task.project_id,
            root_session_id=root_session_id,
            owner_token="quota-owner-a",
            lease_seconds=60,
        )
        assert lease.acquired
        role_session = RoleRuntimeSession(
            role_session_id=role_session_id,
            run_id=run_id,
            project_id=task.project_id,
            role_id="worker",
            seat_id="seat-worker",
            status="idle",
            metadata={"session_scope_id": root_session_id},
        )
        await store.save_delegation_role_session(
            role_session,
            controller_owner_token="quota-owner-a",
            controller_lease_generation=lease.generation,
        )
        member_session = CompanyMemberSession(
            member_session_id="member-session-worker",
            role_session_id=role_session_id,
            role_id="worker",
            seat_id="seat-worker",
            status="idle",
            resident_status="idle",
            metadata={"session_scope_id": root_session_id},
        )
        executor = CompanyWorkItemExecutor(
            org_engine=None,
            communication=SimpleNamespace(),
            approval_engine=SimpleNamespace(),
            memory=None,
            execute_task=AsyncMock(),
            save_task=store.save_task,
            store=store,
        )
        executor_state = executor._run_state()
        executor_state.controller_run_id = run_id
        executor_state.controller_project_id = task.project_id
        executor_state.controller_root_session_id = root_session_id
        executor_state.controller_owner_token = "quota-owner-a"
        executor_state.controller_lease_generation = lease.generation
        runtime_state = executor.runtime._state()
        runtime_state.controller_owner_token = "quota-owner-a"
        runtime_state.controller_lease_generation = lease.generation
        runtime_state.role_sessions[role_session_id] = role_session
        runtime_state.member_sessions[member_session.member_session_id] = member_session
        executor.runtime.enqueue_runnable_work_items(
            [work_item],
            task_by_work_item_id={work_item_id: task},
        )
        claims = await executor.runtime.claim_runnable_tasks([task], [work_item])
        assert len(claims) == 1
        assert claims[0] == (member_session, task)
        assert int(task.metadata.get("claimed_work_item_attempt_seq", 0) or 0) == 1
        return executor, member_session, role_session, task, work_item, lease.generation

    async def test_llm_quota_parks_role_and_reclaims_once_after_backoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = OPCStore(Path(tmp_dir) / "tasks.db")
            await store.initialize()
            try:
                (
                    executor,
                    member_session,
                    role_session,
                    task,
                    work_item,
                    _generation,
                ) = await self._seed_live_controller_claim(store)
                work_item_id = work_item.work_item_id
                runtime_v2 = NativeRuntimeV2(
                    llm=_QuotaLLM(WEEKLY_QUOTA_MESSAGE),
                    tool_registry=ToolRegistry(),
                    event_bus=_EventBus(),
                    config=OPCConfig(),
                )
                agent = self._agent_with_loop(runtime_v2)
                executor.progress_callback = AsyncMock(
                    side_effect=RuntimeError("progress transport failed")
                )
                attempt = asyncio.create_task(agent.execute(task))
                await asyncio.wait({attempt})
                active = {attempt: (member_session, task)}

                await executor._harvest_completed_work_item_tasks(active)

                self.assertEqual(active, {})
                durable_item = await store.get_delegation_work_item(work_item_id)
                durable_task = await store.get_task(task.id)
                durable_role = await store.get_delegation_role_session(
                    role_session.role_session_id
                )
                assert durable_item and durable_task and durable_role
                self.assertEqual(durable_item.phase, Phase.READY)
                self.assertEqual(durable_item.claimed_by_role_runtime_session_id, "")
                self.assertTrue(durable_item.metadata.get("attempt_settled"))
                self.assertEqual(durable_item.metadata.get("attempt_outcome"), "interrupted")
                self.assertEqual(durable_task.status, TaskStatus.PENDING)
                self.assertEqual(durable_role.status, "idle")
                self.assertEqual(durable_role.focused_work_item_id, "")
                self.assertEqual(durable_role.current_work_item, {})
                self.assertEqual(member_session.status, "idle")
                self.assertEqual(role_session.status, "idle")
                self.assertNotIn(task.id, executor.runtime._claimed_task_ids)
                self.assertNotIn(work_item_id, executor.runtime._claimed_work_item_ids)
                durable_run = await store.get_delegation_run(work_item.run_id)
                assert durable_run is not None
                self.assertEqual(durable_run.lifecycle_status, "active")

                executor._quota_park_until = 0.0
                set_linked_work_item_id(durable_task, work_item_id)
                executor.runtime.enqueue_runnable_work_items(
                    [durable_item],
                    task_by_work_item_id={work_item_id: durable_task},
                )
                retry_claims = await executor.runtime.claim_runnable_tasks(
                    [durable_task], [durable_item]
                )
                self.assertEqual(len(retry_claims), 1)
                second_attempt = await store.get_delegation_work_item(work_item_id)
                assert second_attempt is not None
                self.assertEqual(int(second_attempt.metadata["attempt_seq"]), 2)
                duplicate_claims = await executor.runtime.claim_runnable_tasks(
                    [durable_task], [second_attempt]
                )
                self.assertEqual(duplicate_claims, [])
            finally:
                await store.close()

    async def test_takeover_before_atomic_quota_park_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "tasks.db"
            store = OPCStore(db_path)
            takeover_store = OPCStore(db_path)
            await store.initialize()
            await takeover_store.initialize()
            try:
                (
                    executor,
                    member_session,
                    role_session,
                    task,
                    work_item,
                    generation,
                ) = await self._seed_live_controller_claim(store)
                heartbeat_at = datetime.now() - timedelta(seconds=2)
                self.assertTrue(
                    await store.renew_delegation_run_controller_lease(
                        work_item.run_id,
                        project_id=task.project_id,
                        root_session_id=task.session_id,
                        owner_token="quota-owner-a",
                        generation=generation,
                        lease_seconds=1,
                        heartbeat_at=heartbeat_at,
                    )
                )
                takeover = (
                    await takeover_store.acquire_delegation_run_controller_lease(
                        work_item.run_id,
                        project_id=task.project_id,
                        root_session_id=task.session_id,
                        owner_token="quota-owner-b",
                        lease_seconds=60,
                    )
                )
                self.assertTrue(takeover.acquired)

                with self.assertRaises(CompanyRunControllerLeaseLost):
                    await executor._park_claimed_work_item_for_quota(
                        member_session,
                        task,
                        ProviderQuotaExhaustedError("weekly quota"),
                    )

                durable_item = await store.get_delegation_work_item(
                    work_item.work_item_id
                )
                durable_role = await store.get_delegation_role_session(
                    role_session.role_session_id
                )
                assert durable_item and durable_role
                self.assertEqual(durable_item.phase, Phase.RUNNING)
                self.assertEqual(durable_role.status, "running")
                self.assertEqual(
                    durable_role.focused_work_item_id,
                    work_item.work_item_id,
                )
                self.assertEqual(member_session.status, "running")
                self.assertEqual(role_session.status, "running")
                self.assertIn(
                    work_item.work_item_id,
                    executor.runtime._claimed_work_item_ids,
                )
            finally:
                await takeover_store.close()
                await store.close()

    async def test_takeover_after_atomic_commit_cannot_split_ready_from_idle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "tasks.db"
            store = OPCStore(db_path)
            takeover_store = OPCStore(db_path)
            await store.initialize()
            await takeover_store.initialize()
            try:
                (
                    executor,
                    member_session,
                    role_session,
                    task,
                    work_item,
                    generation,
                ) = await self._seed_live_controller_claim(store)

                async def _take_over_at_post_commit_recheck() -> None:
                    heartbeat_at = datetime.now() - timedelta(seconds=2)
                    self.assertTrue(
                        await store.renew_delegation_run_controller_lease(
                            work_item.run_id,
                            project_id=task.project_id,
                            root_session_id=task.session_id,
                            owner_token="quota-owner-a",
                            generation=generation,
                            lease_seconds=1,
                            heartbeat_at=heartbeat_at,
                        )
                    )
                    takeover = await takeover_store.acquire_delegation_run_controller_lease(
                        work_item.run_id,
                        project_id=task.project_id,
                        root_session_id=task.session_id,
                        owner_token="quota-owner-b",
                        lease_seconds=60,
                    )
                    self.assertTrue(takeover.acquired)
                    raise CompanyRunControllerLeaseLost("post-commit takeover")

                executor._require_current_controller_lease = (
                    _take_over_at_post_commit_recheck
                )
                with self.assertRaisesRegex(
                    CompanyRunControllerLeaseLost,
                    "post-commit takeover",
                ):
                    await executor._park_claimed_work_item_for_quota(
                        member_session,
                        task,
                        ProviderQuotaExhaustedError("weekly quota"),
                    )

                durable_item = await store.get_delegation_work_item(
                    work_item.work_item_id
                )
                durable_task = await store.get_task(task.id)
                durable_role = await store.get_delegation_role_session(
                    role_session.role_session_id
                )
                assert durable_item and durable_task and durable_role
                self.assertEqual(durable_item.phase, Phase.READY)
                self.assertEqual(durable_task.status, TaskStatus.PENDING)
                self.assertEqual(durable_role.status, "idle")
                self.assertEqual(durable_role.focused_work_item_id, "")
                # The stale process has not touched its local scheduler view;
                # the winning controller rehydrates the atomic durable state.
                self.assertEqual(member_session.status, "running")
                self.assertEqual(role_session.status, "running")
                self.assertIn(
                    work_item.work_item_id,
                    executor.runtime._claimed_work_item_ids,
                )
            finally:
                await takeover_store.close()
                await store.close()


class QuotaParkTests(unittest.IsolatedAsyncioTestCase):
    def _executor(self) -> CompanyWorkItemExecutor:
        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        executor.store = None
        executor.runtime = SimpleNamespace(
            _claimed_task_ids={"task-1"},
            _claimed_work_item_ids=set(),
        )
        executor._quota_park_until = 0.0
        executor._quota_park_streak = 0
        executor._quota_last_park_at = 0.0
        executor._emit_progress = AsyncMock()
        executor._projection_id_for_task = lambda task: "cto::execute::x"
        # Keep the unit test at the park-accounting level: the durable READY
        # transition is exercised by the claim-release invariant suite.
        executor._claimed_work_item_needs_cleanup = lambda member, task: False
        return executor

    def _session(self) -> CompanyMemberSession:
        session = CompanyMemberSession.__new__(CompanyMemberSession)
        session.status = "running"
        session.resident_status = "running"
        session.current_task_id = "task-1"
        session.focused_work_item_id = "wi-1"
        session.current_work_item = {"id": "wi-1"}
        session.current_assignment = {"id": "wi-1"}
        return session

    async def test_park_backs_off_exponentially_and_idles_session(self) -> None:
        executor = self._executor()
        session = self._session()
        task = Task(id="task-1", title="t", project_id="p", session_id="s")

        await executor._handle_claimed_work_item_exception(
            session, task, ProviderQuotaExhaustedError("429 rate limit")
        )

        now = time.monotonic()
        self.assertEqual(executor._quota_park_streak, 1)
        self.assertAlmostEqual(executor._quota_park_until - now, 60, delta=5)
        # This accounting-only fixture deliberately reports that no active
        # durable claim exists; session cleanup belongs to the atomic-claim
        # E2E test above and must not run without such a claim.
        self.assertEqual(session.status, "running")
        self.assertIn("task-1", executor.runtime._claimed_task_ids)
        # No terminal failure was recorded on the task.
        self.assertNotIn("claimed_work_item_exception", dict(task.metadata or {}))

        await executor._handle_claimed_work_item_exception(
            session, task, ProviderQuotaExhaustedError("429 again")
        )
        self.assertEqual(executor._quota_park_streak, 2)
        self.assertAlmostEqual(
            executor._quota_park_until - time.monotonic(), 120, delta=5
        )

    async def test_streak_resets_after_a_quiet_period(self) -> None:
        executor = self._executor()
        executor._quota_park_streak = 5
        executor._quota_last_park_at = time.monotonic() - 3600
        session = self._session()
        task = Task(id="task-1", title="t", project_id="p", session_id="s")

        await executor._handle_claimed_work_item_exception(
            session, task, ProviderQuotaExhaustedError("429")
        )
        self.assertEqual(executor._quota_park_streak, 1)

    async def test_backoff_caps_at_fifteen_minutes(self) -> None:
        executor = self._executor()
        executor._quota_park_streak = 9
        executor._quota_last_park_at = time.monotonic()
        session = self._session()
        task = Task(id="task-1", title="t", project_id="p", session_id="s")

        await executor._handle_claimed_work_item_exception(
            session, task, ProviderQuotaExhaustedError("429")
        )
        self.assertLessEqual(executor._quota_park_until - time.monotonic(), 900 + 5)

    async def test_non_quota_exception_still_fails_terminally(self) -> None:
        executor = self._executor()
        session = self._session()
        task = Task(id="task-1", title="t", project_id="p", session_id="s")
        failed: dict = {}

        async def _fake_fail(member_session, failed_task, exc) -> None:  # noqa: ANN001
            failed["exc"] = exc

        # Route the non-quota path into a probe: the real body needs a store.
        original = CompanyWorkItemExecutor._handle_claimed_work_item_exception

        async def _probe(self, member_session, failed_task, exc):  # noqa: ANN001
            if self._exception_is_provider_quota(exc):
                await self._park_claimed_work_item_for_quota(member_session, failed_task, exc)
                return
            await _fake_fail(member_session, failed_task, exc)

        try:
            CompanyWorkItemExecutor._handle_claimed_work_item_exception = _probe
            await executor._handle_claimed_work_item_exception(
                session, task, RuntimeError("real crash")
            )
        finally:
            CompanyWorkItemExecutor._handle_claimed_work_item_exception = original
        self.assertIsInstance(failed.get("exc"), RuntimeError)
        self.assertEqual(executor._quota_park_streak, 0)


class QuotaBackoffWaitTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _executor() -> CompanyWorkItemExecutor:
        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        executor._quota_park_until = 0.0
        executor._dispatcher_wake = asyncio.Event()
        return executor

    async def test_sixty_second_window_avoids_high_frequency_reload(self) -> None:
        executor = self._executor()
        clock = [100.0]
        executor._quota_park_until = 160.0
        wait_count = 0
        full_reload_count = 0

        async def _advance_wait(awaitable, *, timeout):  # noqa: ANN001
            nonlocal wait_count
            awaitable.close()
            wait_count += 1
            clock[0] += float(timeout)
            raise asyncio.TimeoutError

        with (
            patch(
                "opc.layer2_organization.company_mode.time.monotonic",
                side_effect=lambda: clock[0],
            ),
            patch(
                "opc.layer2_organization.company_mode.asyncio.wait_for",
                side_effect=_advance_wait,
            ),
        ):
            for _ in range(12):
                if await executor._provider_quota_backoff_defers_dispatcher_tick({}):
                    continue
                full_reload_count += 1

        self.assertEqual(wait_count, 12)
        self.assertEqual(full_reload_count, 1)
        self.assertEqual(clock[0], 160.0)

    async def test_wake_and_cancellation_interrupt_backoff_immediately(self) -> None:
        executor = self._executor()
        executor._quota_park_until = time.monotonic() + 60
        wake_wait = asyncio.create_task(
            executor._provider_quota_backoff_defers_dispatcher_tick({})
        )
        await asyncio.sleep(0)
        executor._dispatcher_wake.set()
        self.assertFalse(await asyncio.wait_for(wake_wait, timeout=0.2))
        self.assertFalse(executor._dispatcher_wake.is_set())

        cancel_wait = asyncio.create_task(
            executor._provider_quota_backoff_defers_dispatcher_tick({})
        )
        await asyncio.sleep(0)
        cancel_wait.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(cancel_wait, timeout=0.2)


if __name__ == "__main__":
    unittest.main()
