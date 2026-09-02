"""Streaming-friendly tool executor for Native Runtime V2."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Awaitable, Callable

from opc.core.models import PermissionResolution
from opc.layer2_organization.company_runtime_identity import (
    is_company_runtime_task,
)
from opc.layer3_agent.runtime_v2.permissions import RuntimePermissionAdapter
from opc.layer3_agent.runtime_v2.tool_hooks import (
    RuntimeCompanyControllerToolFence,
    RuntimeToolHookBus,
    RuntimeToolHookContext,
)
from opc.layer3_agent.runtime_v2.tool_planner import ToolBatch, ToolPlanner
from opc.layer4_tools.execution_context import (
    install_execution_context_override,
    reset_execution_context_override,
)
from opc.layer4_tools.registry import ToolRegistry


RuntimeToolHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
RuntimeEventCallback = Callable[[str, dict[str, Any]], Awaitable[None]]
_HEARTBEAT_INTERVAL_SECONDS = 1.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _result_summary(result: dict[str, Any], *, limit: int = 240) -> str:
    if not result:
        return ""
    if result.get("error"):
        summary = str(result.get("error", "") or "").strip()
    else:
        payload = result.get("result", {})
        summary = ""
        if isinstance(payload, dict):
            for key in ("summary", "rendered", "stdout", "stderr", "content", "message"):
                value = payload.get(key)
                if value:
                    summary = str(value).strip()
                    break
        if not summary:
            summary = json.dumps(result, ensure_ascii=False, default=str)
    if len(summary) <= limit:
        return summary
    return summary[:limit].rstrip() + "..."


class StreamingToolExecutor:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        planner: ToolPlanner,
        permission_resolver: RuntimePermissionAdapter,
        hook_bus: RuntimeToolHookBus | None = None,
        controller_tool_fence: RuntimeCompanyControllerToolFence | None = None,
        runtime_tool_handler: RuntimeToolHandler | None = None,
        emit_event: RuntimeEventCallback | None = None,
        max_parallel_read_tools: int = 6,
        converge_on_parallel_failure: bool = True,
    ) -> None:
        self.registry = registry
        self.planner = planner
        self.permission_resolver = permission_resolver
        self.hook_bus = hook_bus
        self.controller_tool_fence = (
            controller_tool_fence or RuntimeCompanyControllerToolFence()
        )
        self.runtime_tool_handler = runtime_tool_handler
        self.emit_event = emit_event
        self.max_parallel_read_tools = max(1, int(max_parallel_read_tools or 1))
        self.converge_on_parallel_failure = bool(converge_on_parallel_failure)

    async def execute(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        task: Any = None,
        on_progress: Any = None,
    ) -> list[dict[str, Any]]:
        ordered_results: list[dict[str, Any]] = []
        for batch in self.planner.partition(tool_calls):
            batch_id = f"tb_{uuid.uuid4().hex[:12]}"
            batch_started_at_ms = _now_ms()
            if self.emit_event:
                await self.emit_event(
                    "tool_batch_started",
                    {
                        "batch_id": batch_id,
                        "started_at_ms": batch_started_at_ms,
                        "concurrency_safe": batch.concurrency_safe,
                        "tool_names": [str(call.get("function", "") or "") for call in batch.calls],
                        "tool_call_ids": [str(call.get("id", "") or "") for call in batch.calls],
                    },
                )
            if batch.concurrency_safe:
                batch_results = await self._run_parallel(batch, task=task, on_progress=on_progress, batch_id=batch_id)
            else:
                batch_results: list[dict[str, Any]] = []
                for call in batch.calls:
                    batch_results.append(await self._run_one(call, task=task, on_progress=on_progress, batch_id=batch_id))
            ordered_results.extend(batch_results)
            if self.emit_event:
                batch_completed_at_ms = _now_ms()
                await self.emit_event(
                    "tool_batch_completed",
                    {
                        "batch_id": batch_id,
                        "started_at_ms": batch_started_at_ms,
                        "completed_at_ms": batch_completed_at_ms,
                        "elapsed_ms": max(0, batch_completed_at_ms - batch_started_at_ms),
                        "concurrency_safe": batch.concurrency_safe,
                        "success": all(bool(item.get("result", {}).get("success", True)) for item in batch_results),
                        "tool_count": len(batch_results),
                    },
                )
        return ordered_results

    async def _run_parallel(
        self,
        batch: ToolBatch,
        *,
        task: Any = None,
        on_progress: Any = None,
        batch_id: str = "",
    ) -> list[dict[str, Any]]:
        semaphore = asyncio.Semaphore(self.max_parallel_read_tools)
        batch_state: dict[str, Any] = {
            "cascade_event": asyncio.Event(),
            "failed_call_id": "",
            "failed_tool_name": "",
        }

        async def _wrapped(call: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                if self.converge_on_parallel_failure and batch_state["cascade_event"].is_set():
                    return await self._build_converged_result(call, batch_state, batch_id=batch_id)
                result = await self._run_one(call, task=task, on_progress=on_progress, batch_state=batch_state, batch_id=batch_id)
                if self.converge_on_parallel_failure and self._should_converge_batch(result):
                    batch_state["failed_call_id"] = str(call.get("id", "") or "")
                    batch_state["failed_tool_name"] = str(call.get("function", "") or "")
                    batch_state["cascade_event"].set()
                return result

        return list(await asyncio.gather(*[_wrapped(call) for call in batch.calls]))

    async def _run_one(
        self,
        call: dict[str, Any],
        *,
        task: Any = None,
        on_progress: Any = None,
        batch_state: dict[str, Any] | None = None,
        batch_id: str = "",
    ) -> dict[str, Any]:
        tool_name = str(call.get("function", "") or "")
        arguments = dict(call.get("arguments", {}) or {})
        tool = self.registry.get(tool_name)
        predicted = self.permission_resolver.predicted_decision(tool, arguments, task=task)
        sandbox = self.permission_resolver.sandbox_for_task(task)
        if task is not None and sandbox:
            task.metadata = dict(getattr(task, "metadata", {}) or {})
            execution_context = dict(task.metadata.get("_execution_context", {}) or {})
            execution_context["sandbox"] = sandbox
            task.metadata["_execution_context"] = execution_context
        request_started_at_ms = _now_ms()
        request_started_at_monotonic = time.monotonic()
        execution_started_at_ms: int | None = None
        execution_started_at_monotonic: float | None = None
        if self.emit_event:
            await self.emit_event(
                "permission_predicted",
                {
                    "batch_id": batch_id,
                    "tool_call_id": call.get("id", ""),
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "resolution": predicted.resolution.value,
                    "scope": predicted.scope.value,
                    "risk_level": predicted.risk_level.value,
                    "rationale": predicted.rationale,
                    "source": predicted.source,
                    "started_at_ms": request_started_at_ms,
                },
            )
        if self.emit_event and predicted.resolution != PermissionResolution.ALLOW:
            await self.emit_event(
                "permission_requested",
                {
                    "batch_id": batch_id,
                    "tool_call_id": call.get("id", ""),
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "resolution": predicted.resolution.value,
                    "scope": predicted.scope.value,
                    "risk_level": predicted.risk_level.value,
                    "rationale": predicted.rationale,
                    "source": predicted.source,
                },
            )
        if batch_state is not None and self.converge_on_parallel_failure and batch_state["cascade_event"].is_set():
            return await self._build_converged_result(call, batch_state, batch_id=batch_id)

        hook_call = dict(call)
        hook_call["batch_id"] = batch_id
        hook_context = RuntimeToolHookContext(
            phase="pre",
            tool_name=tool_name,
            call=hook_call,
            task=task,
            tool=tool,
            arguments=dict(arguments),
            predicted_permission=predicted,
        )
        if self.hook_bus is not None:
            hook_context = await self.hook_bus.run_pre_hooks(hook_context)
            arguments = dict(hook_context.arguments)
        elif predicted.resolution == PermissionResolution.DENY:
            hook_context.result = self.permission_resolver.build_blocked_result(
                predicted,
                tool_name=tool_name,
                arguments=arguments,
            )
            hook_context.state["stop_batch_on_failure"] = True

        if hook_context.result is not None:
            result = dict(hook_context.result)
            decision = self.permission_resolver.decision_from_result(tool_name, arguments, result)
        elif call.get("arguments_parse_error"):
            result = {
                "error": str(call.get("arguments_parse_error", "")),
                "invalid_arguments": True,
                "success": False,
                "raw_arguments": str(call.get("arguments_raw", "")),
            }
            decision = self.permission_resolver.decision_from_result(tool_name, arguments, result)
        else:
            decision = self.permission_resolver.decision_from_result(
                tool_name,
                arguments,
                {"approval": dict(hook_context.state.get("approval", {}) or {})},
            )
            if self.emit_event:
                await self.emit_event(
                    "permission_resolved",
                    {
                        "batch_id": batch_id,
                        "tool_call_id": call.get("id", ""),
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "resolution": decision.resolution.value,
                        "scope": decision.scope.value,
                        "rationale": decision.rationale,
                    },
                )
            execution_started_at_ms = _now_ms()
            tool_started_monotonic = time.monotonic()
            execution_started_at_monotonic = tool_started_monotonic
            if self.emit_event:
                await self.emit_event(
                    "tool_started",
                    {
                        "batch_id": batch_id,
                        "tool_call_id": call.get("id", ""),
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "predicted_permission": predicted.resolution.value,
                        "started_at_ms": execution_started_at_ms,
                    },
                )
            last_progress: dict[str, str] = {"stream": "", "text": ""}
            last_progress_at = {"value": time.monotonic()}
            heartbeat_active = {"value": True}

            async def _heartbeat() -> None:
                while heartbeat_active["value"]:
                    await asyncio.sleep(_HEARTBEAT_INTERVAL_SECONDS)
                    if not heartbeat_active["value"]:
                        return
                    now = time.monotonic()
                    if now - last_progress_at["value"] < _HEARTBEAT_INTERVAL_SECONDS:
                        continue
                    if self.emit_event:
                        await self.emit_event(
                            "tool_progress",
                            {
                                "batch_id": batch_id,
                                "tool_call_id": call.get("id", ""),
                                "tool_name": tool_name,
                                "phase": "running",
                                "message": f"{tool_name} still running",
                                "heartbeat": True,
                                "elapsed_ms": int((now - tool_started_monotonic) * 1000),
                            },
                        )

            async def _tool_progress(progress: Any, **progress_kw: Any) -> None:
                if isinstance(progress, dict):
                    payload = dict(progress)
                    text = str(payload.get("text", "") or payload.get("message", "") or "").strip()
                    stream_name = str(payload.get("stream", "") or "").strip()
                else:
                    text = str(progress or "").strip()
                    stream_name = str(progress_kw.get("stream", "") or "").strip()
                    payload = {
                        "text": text,
                        "stream": stream_name,
                    }
                if not text:
                    return
                if last_progress["text"] == text and last_progress["stream"] == stream_name:
                    return
                last_progress["text"] = text
                last_progress["stream"] = stream_name
                last_progress_at["value"] = time.monotonic()
                if self.emit_event:
                    await self.emit_event(
                        "tool_progress",
                        {
                            "batch_id": batch_id,
                            "tool_call_id": call.get("id", ""),
                            "tool_name": tool_name,
                            "stream": stream_name,
                            "elapsed_ms": int((last_progress_at["value"] - tool_started_monotonic) * 1000),
                            **payload,
                        },
                    )
                if on_progress:
                    try:
                        await on_progress(text, task_id=getattr(task, "id", None))
                    except TypeError:
                        await on_progress(text)

            heartbeat_task = asyncio.create_task(_heartbeat())
            try:
                result = await self._invoke_tool_effect(
                    tool_name=tool_name,
                    arguments=arguments,
                    task=task,
                    on_progress=_tool_progress,
                    call=hook_context.call,
                )
                result = await self._maybe_retry_with_escalated_sandbox(
                    tool_name=tool_name,
                    arguments=arguments,
                    task=task,
                    result=result,
                    on_progress=_tool_progress,
                    batch_id=batch_id,
                    call=hook_context.call,
                )
            finally:
                heartbeat_active["value"] = False
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            hook_context.phase = "post"
            hook_context.arguments = dict(arguments)
            hook_context.result = dict(result)
            if self.hook_bus is not None:
                hook_context = await self.hook_bus.run_post_hooks(hook_context)
                result = dict(hook_context.result or result)
                if not bool(result.get("success", True)):
                    hook_context.phase = "failure"
                    hook_context.result = dict(result)
                    hook_context = await self.hook_bus.run_failure_hooks(hook_context)
                    result = dict(hook_context.result or result)
            decision = self.permission_resolver.decision_from_result(tool_name, arguments, result)
        if self.emit_event and execution_started_at_ms is None:
            await self.emit_event(
                "permission_resolved",
                {
                    "batch_id": batch_id,
                    "tool_call_id": call.get("id", ""),
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "resolution": decision.resolution.value,
                    "scope": decision.scope.value,
                    "rationale": decision.rationale,
                },
            )
        if self.emit_event:
            completed_started_at_ms = (
                execution_started_at_ms or request_started_at_ms
            )
            completed_started_at_monotonic = (
                execution_started_at_monotonic
                if execution_started_at_monotonic is not None
                else request_started_at_monotonic
            )
            await self.emit_event(
                "tool_completed",
                {
                    "batch_id": batch_id,
                    "tool_call_id": call.get("id", ""),
                    "tool_name": tool_name,
                    "started_at_ms": completed_started_at_ms,
                    "completed_at_ms": _now_ms(),
                    "elapsed_ms": int(
                        (time.monotonic() - completed_started_at_monotonic) * 1000
                    ),
                    "success": bool(result.get("success", True)),
                    "result_summary": _result_summary(result),
                    "result_preview": json.dumps(result, ensure_ascii=False, default=str)[:800],
                },
            )

        return {
            "tool_call": call,
            "result": result,
            "permission_decision": decision,
            "stop_batch_on_failure": bool(hook_context.state.get("stop_batch_on_failure")),
            "hook_metadata": {"batch_id": batch_id, **dict(hook_context.state.get("metadata", {}))},
        }

    async def _maybe_retry_with_escalated_sandbox(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        task: Any,
        result: dict[str, Any],
        on_progress: Any,
        batch_id: str,
        call: dict[str, Any],
    ) -> dict[str, Any]:
        # Never widen workspace-write/read-only to elevated/off after a
        # failure. More capability is a new, explicitly approved call.
        return result

    async def _invoke_tool_effect(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        task: Any,
        on_progress: Any,
        call: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The sole registry/runtime-managed handler effect boundary."""

        tool = self.registry.get(tool_name)

        async def _effect() -> dict[str, Any]:
            if (
                tool is not None
                and tool.runtime_managed
                and self.runtime_tool_handler is not None
            ):
                return await self.runtime_tool_handler(tool_name, arguments)
            return await self.registry.execute(
                tool_name,
                arguments,
                task=task,
                on_progress=on_progress,
                skip_approval=True,
            )

        sandbox_override = dict((call or {}).get("_sandbox_override", {}) or {})
        override_token = None
        # Company shell/Python handlers consume the immutable plan installed by
        # the controller fence. Other native tools resolve this coroutine-local
        # context directly. Never mutate the shared Task: parallel calls may
        # carry different one-shot grants.
        if task is not None and sandbox_override and not is_company_runtime_task(task):
            override_token = install_execution_context_override(
                {"sandbox": sandbox_override}
            )
        try:
            return await self.controller_tool_fence.run(
                task=task,
                tool_name=tool_name,
                tool_category=str(getattr(tool, "category", "") or ""),
                tool_effect_kind=str(
                    getattr(tool, "company_effect_kind", "") or ""
                ),
                arguments=arguments,
                tool_call=dict(call or {}),
                effect=_effect,
            )
        finally:
            if override_token is not None:
                reset_execution_context_override(override_token)

    async def _build_converged_result(
        self,
        call: dict[str, Any],
        batch_state: dict[str, Any],
        *,
        batch_id: str = "",
    ) -> dict[str, Any]:
        tool_name = str(call.get("function", "") or "")
        result = {
            "error": (
                "Skipped because a concurrent sibling tool failed and the runtime converged the batch. "
                f"Source: {batch_state.get('failed_tool_name', '') or 'unknown'}"
            ),
            "success": False,
            "converged": True,
            "converged_from_tool": batch_state.get("failed_tool_name", ""),
            "converged_from_call_id": batch_state.get("failed_call_id", ""),
        }
        if self.emit_event:
            await self.emit_event(
                "tool_skipped",
                {
                    "batch_id": batch_id,
                    "tool_call_id": call.get("id", ""),
                    "tool_name": tool_name,
                    "reason": "parallel_batch_converged",
                    "source_tool_name": batch_state.get("failed_tool_name", ""),
                    "source_call_id": batch_state.get("failed_call_id", ""),
                },
            )
        decision = self.permission_resolver.decision_from_result(tool_name, dict(call.get("arguments", {}) or {}), result)
        return {
            "tool_call": call,
            "result": result,
            "permission_decision": decision,
            "stop_batch_on_failure": False,
            "hook_metadata": {"converged": True, "batch_id": batch_id},
        }

    @staticmethod
    def _should_converge_batch(result: dict[str, Any]) -> bool:
        if bool(result.get("stop_batch_on_failure")):
            return True
        payload = result.get("result", {})
        if isinstance(payload, dict) and payload.get("prevent_continuation"):
            return True
        if isinstance(payload, dict):
            return not bool(payload.get("success", True))
        return False
