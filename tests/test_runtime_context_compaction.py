"""Runtime V2 context compaction behavior.

Contract (aligned with the claude-code / codex reference implementations):
history below the hard threshold is never rewritten; at the threshold the
old span is folded into one durable LLM summary with the recent tail kept
verbatim; mechanical trimming is an emergency-only fallback under overflow
pressure when the summarizer is unavailable.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opc.core.config import OPCConfig
from opc.core.models import Task, TaskStatus
from opc.layer3_agent.runtime_v2.runtime import NativeRuntimeV2
from opc.layer4_tools.registry import ToolDefinition, ToolRegistry
from opc.layer5_memory.history_compactor import HistoryCompactor
from opc.layer5_memory.memory_manager import MemoryManager


class _CountingLLM:
    """Minimal LLM stub with controllable token accounting."""

    def __init__(self, *, token_count: int = 0, context_window: int = 100_000) -> None:
        self.token_count = token_count
        self.context_window = context_window
        self.config = type("Cfg", (), {"max_tokens": 2048})()

    def count_input_tokens(self, messages, tools=None):
        _ = (messages, tools)
        return self.token_count

    def get_context_window(self):
        return self.context_window

    def is_context_overflow_error(self, error: Exception) -> bool:
        _ = error
        return False


def _runtime(llm, *, compactor=None) -> NativeRuntimeV2:
    return NativeRuntimeV2(
        llm=llm,
        tool_registry=ToolRegistry(),
        config=OPCConfig(),
        history_compactor=compactor,
    )


def _paired_tool_round(index: int) -> list[dict[str, object]]:
    call_id = f"call-{index}"
    return [
        {
            "role": "assistant",
            "content": f"step {index}",
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": "demo", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": f"tool output {index} " + ("x" * 200)},
    ]


def _long_history(rounds: int) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = [{"role": "system", "content": "system prompt"}]
    messages.append({"role": "user", "content": "original request"})
    for index in range(rounds):
        messages.extend(_paired_tool_round(index))
    return messages


class ContextPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def _pipeline(
        self,
        runtime: NativeRuntimeV2,
        messages: list[dict[str, object]],
        *,
        boundaries: list[dict[str, object]] | None = None,
        runtime_notes: dict[str, object] | None = None,
        force: bool = False,
        observed: int = 0,
    ) -> list[dict[str, object]]:
        return await runtime._apply_context_pipeline(
            messages,
            tool_schemas=None,
            task=None,
            base_prefix_len=1,
            runtime_session_id="rt-test",
            compaction_boundaries=boundaries if boundaries is not None else [],
            todo_state=[],
            runtime_notes=runtime_notes if runtime_notes is not None else {},
            active_subagents=[],
            force_compact=force,
            observed_tokens=observed,
        )

    @staticmethod
    def _originals_in(result: list[dict[str, object]], originals: list[dict[str, object]]) -> list[dict[str, object]]:
        """The original messages that survived, in result order.

        The pipeline may prepend injected context (session memory, runtime
        artifacts); the compaction contract is about the original messages
        staying verbatim and in order.
        """
        return [item for item in result if item in originals]

    async def test_history_below_threshold_is_never_rewritten(self) -> None:
        # 50% usage, 62 messages: the legacy pipeline would microcompact and
        # snip this history; the new contract keeps every message verbatim.
        llm = _CountingLLM(token_count=50_000)
        runtime = _runtime(llm)
        messages = _long_history(rounds=30)

        result = await self._pipeline(runtime, [dict(item) for item in messages])

        self.assertEqual(self._originals_in(result, messages), messages)
        flattened = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("[runtime_v2 snip]", flattened)
        self.assertNotIn("microcompacted", flattened)
        self.assertNotIn("truncated by runtime_v2", flattened)
        self.assertNotIn("durable compaction", flattened)

    async def test_durable_compaction_folds_old_history_into_summary(self) -> None:
        llm = _CountingLLM(token_count=95_000)
        compactor = SimpleNamespace(
            summarize_runtime_history=AsyncMock(return_value="SUMMARY-OF-EARLIER-WORK"),
        )
        runtime = _runtime(llm, compactor=compactor)
        messages = _long_history(rounds=20)
        # One trailing assistant message so the naive tail cut would land on a
        # tool result and must walk back to its assistant tool_calls message.
        messages.append({"role": "assistant", "content": "wrap up"})
        boundaries: list[dict[str, object]] = []
        notes: dict[str, object] = {}

        result = await self._pipeline(
            runtime, list(messages), boundaries=boundaries, runtime_notes=notes
        )

        self.assertEqual(result[0], messages[0])
        summary_indexes = [
            index
            for index, item in enumerate(result)
            if "[runtime_v2 durable compaction]" in str(item.get("content", ""))
        ]
        self.assertEqual(len(summary_indexes), 1)
        summary_message = result[summary_indexes[0]]
        self.assertEqual(summary_message["role"], "user")
        self.assertIn("SUMMARY-OF-EARLIER-WORK", summary_message["content"])
        # Survivors are the prefix, the seed user request (kept verbatim on
        # every round), and the recent tail, which starts at the assistant
        # message owning the tool results (a naive cut would split the pair).
        survivors = self._originals_in(result, messages)
        tail = survivors[2:]
        self.assertEqual([messages[0], messages[1], *tail], survivors)
        self.assertEqual(messages[1]["content"], "original request")
        self.assertEqual(tail, messages[len(messages) - len(tail):])
        self.assertEqual(tail[0]["role"], "assistant")
        self.assertTrue(tail[0].get("tool_calls"))
        compactor.summarize_runtime_history.assert_awaited_once()
        rendered = compactor.summarize_runtime_history.await_args.kwargs["messages"]
        self.assertTrue(all(set(item) == {"role", "content"} for item in rendered))
        self.assertEqual(notes.get("durable_compaction_failures"), 0)
        self.assertEqual(len(boundaries), 1)
        self.assertIn("durable_compaction", boundaries[0]["pipeline"])
        self.assertNotIn("emergency_microcompact", boundaries[0]["pipeline"])

    async def test_repeated_compaction_keeps_one_summary_and_seed_request(self) -> None:
        llm = _CountingLLM(token_count=95_000)
        compactor = SimpleNamespace(
            summarize_runtime_history=AsyncMock(
                side_effect=[f"SUMMARY-ROUND-{n}" for n in range(1, 10)]
            ),
        )
        runtime = _runtime(llm, compactor=compactor)
        # Simulate injected context having shifted the prefix boundary: an
        # artifact-style system message sits between the system prompt and the
        # seed request, so the seed request lives beyond base_prefix_len.
        current: list[dict[str, object]] = [
            {"role": "system", "content": "system prompt"},
            {"role": "system", "content": "## Runtime Artifact: injected context"},
            {"role": "user", "content": "SEED-REQUEST keep me verbatim"},
        ]
        next_round = 0
        for _ in range(12):
            current.extend(_paired_tool_round(next_round))
            next_round += 1
        notes: dict[str, object] = {}

        for round_no in range(1, 4):
            current = await self._pipeline(runtime, current, runtime_notes=notes)
            markers = [
                item
                for item in current
                if "[runtime_v2 durable compaction]" in str(item.get("content", ""))
            ]
            self.assertEqual(len(markers), 1, f"round {round_no}: exactly one summary must exist")
            self.assertIn(f"SUMMARY-ROUND-{round_no}", str(markers[0]["content"]))
            seeds = [item for item in current if "SEED-REQUEST" in str(item.get("content", ""))]
            self.assertEqual(len(seeds), 1, f"round {round_no}: seed request must survive")
            self.assertEqual(seeds[0]["content"], "SEED-REQUEST keep me verbatim")
            self.assertEqual(seeds[0]["role"], "user")
            for _ in range(4):
                current.extend(_paired_tool_round(next_round))
                next_round += 1

        self.assertEqual(compactor.summarize_runtime_history.await_count, 3)
        # Chain continuity: each round re-summarizes the previous summary.
        second_input = json.dumps(
            compactor.summarize_runtime_history.await_args_list[1].kwargs["messages"],
            ensure_ascii=False,
        )
        self.assertIn("SUMMARY-ROUND-1", second_input)
        third_input = json.dumps(
            compactor.summarize_runtime_history.await_args_list[2].kwargs["messages"],
            ensure_ascii=False,
        )
        self.assertIn("SUMMARY-ROUND-2", third_input)

    async def test_summarizer_failure_keeps_history_and_trips_breaker(self) -> None:
        llm = _CountingLLM(token_count=95_000)
        compactor = SimpleNamespace(
            summarize_runtime_history=AsyncMock(side_effect=RuntimeError("summarizer down")),
        )
        runtime = _runtime(llm, compactor=compactor)
        messages = _long_history(rounds=10)
        notes: dict[str, object] = {}

        first = await self._pipeline(runtime, list(messages), runtime_notes=notes)
        second = await self._pipeline(runtime, list(messages), runtime_notes=notes)
        third = await self._pipeline(runtime, list(messages), runtime_notes=notes)

        self.assertEqual(self._originals_in(first, messages), messages)
        self.assertEqual(self._originals_in(second, messages), messages)
        self.assertEqual(self._originals_in(third, messages), messages)
        self.assertEqual(notes.get("durable_compaction_failures"), 2)
        # circuit_breaker_failures defaults to 2: the third round must not
        # have attempted another summary.
        self.assertEqual(compactor.summarize_runtime_history.await_count, 2)

    async def test_overflow_force_uses_emergency_fallback_without_compactor(self) -> None:
        llm = _CountingLLM(token_count=95_000)
        runtime = _runtime(llm)
        messages = _long_history(rounds=30)
        boundaries: list[dict[str, object]] = []

        result = await self._pipeline(runtime, list(messages), boundaries=boundaries, force=True)

        self.assertNotEqual(result, messages)
        flattened = json.dumps(result, ensure_ascii=False)
        self.assertIn("[runtime_v2 snip]", flattened)
        self.assertEqual(len(boundaries), 1)
        self.assertIn("emergency_microcompact", boundaries[0]["pipeline"])
        self.assertNotIn("durable_compaction", boundaries[0]["pipeline"])

    async def test_tool_result_budget_keeps_head_and_tail(self) -> None:
        runtime = _runtime(_CountingLLM())
        content = "HEAD" + ("x" * 30_000) + "TAIL-MARKER"

        [message] = runtime._apply_tool_result_budget(
            [{"role": "tool", "tool_call_id": "c", "content": content}]
        )

        self.assertTrue(message["content"].startswith("HEAD"))
        self.assertTrue(message["content"].endswith("TAIL-MARKER"))
        self.assertIn("chars omitted", message["content"])
        self.assertLess(len(message["content"]), 13_000)

    async def test_threshold_uses_observed_prompt_tokens_anchor(self) -> None:
        # The local estimator undercounts badly; the provider-reported prompt
        # size of the latest request must still trigger compaction.
        llm = _CountingLLM(token_count=1_000)
        runtime = _runtime(llm)
        messages = _long_history(rounds=5)

        self.assertFalse(runtime._should_apply_hard_compaction(messages, None))
        self.assertTrue(
            runtime._should_apply_hard_compaction(messages, None, observed_tokens=95_000)
        )


class SummarizeRuntimeHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_prompt_contract_and_parsing(self) -> None:
        captured: dict[str, str] = {}

        class _LLM:
            async def simple_chat(self, *, prompt: str, system: str, task_type: str) -> str:
                captured["system"] = system
                captured["prompt"] = prompt
                return json.dumps({"history_summary": "NINE-SECTION-SUMMARY"})

        compactor = HistoryCompactor(llm=_LLM(), store=None, memory_manager=None)

        summary = await compactor.summarize_runtime_history(
            project_id="proj1",
            session_id="rt-1",
            messages=[{"role": "user", "content": "build the feature"}],
        )

        self.assertEqual(summary, "NINE-SECTION-SUMMARY")
        self.assertIn("Primary Request and Intent", captured["system"])
        self.assertIn("All User Messages", captured["system"])
        self.assertIn("build the feature", captured["prompt"])

    async def test_without_llm_falls_back_to_mechanical_summary(self) -> None:
        compactor = HistoryCompactor(llm=None, store=None, memory_manager=None)

        summary = await compactor.summarize_runtime_history(
            project_id="proj1",
            session_id="rt-1",
            messages=[{"role": "user", "content": "important detail"}],
        )

        self.assertTrue(summary.strip())
        self.assertIn("important detail", summary)


class MaybeCompactSessionHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_calls_compactor_with_resolved_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = MemoryManager(Path(tmpdir), "proj1", store=None)
            compactor = SimpleNamespace(maybe_compact_session=AsyncMock(return_value=True))
            memory.set_history_compactor(compactor)

            self.assertTrue(await memory.maybe_compact_session_history("sess-1"))

            compactor.maybe_compact_session.assert_awaited_once_with(
                project_id="proj1", session_id="sess-1", force=False
            )

    async def test_absent_or_failing_compactor_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            memory = MemoryManager(Path(tmpdir), "proj1", store=None)

            self.assertFalse(await memory.maybe_compact_session_history("sess-1"))

            memory.set_history_compactor(
                SimpleNamespace(maybe_compact_session=AsyncMock(side_effect=RuntimeError("boom")))
            )
            self.assertFalse(await memory.maybe_compact_session_history("sess-1"))


class OverflowRecoveryEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_overflow_error_recovers_via_forced_compaction(self) -> None:
        """A real provider overflow mid-run must force-compact and retry.

        Token accounting is deliberately kept far below the threshold so the
        ONLY thing that can rescue the run is the reactive overflow path.
        """

        def _event(event_type: str, payload: dict[str, object]):
            return type("Evt", (), {"event_type": event_type, "payload": payload, "model": "stub"})()

        class _OverflowThenRecoverLLM:
            def __init__(self) -> None:
                self.calls = 0
                self.overflow_thrown = False
                self.prompts: list[list[dict[str, object]]] = []
                self.config = type("Cfg", (), {"max_tokens": 2048})()

            def prepare_user_message_content(self, content: str, attachment_refs=None):
                _ = attachment_refs
                return content

            def get_tool_definitions(self, tools):
                return tools

            def is_context_overflow_error(self, error: Exception) -> bool:
                return "maximum context length" in str(error)

            def count_input_tokens(self, messages, tools=None):
                _ = (messages, tools)
                return 100

            def get_context_window(self):
                return 10_000

            async def chat_stream(self, messages, tools=None):
                _ = tools
                self.calls += 1
                self.prompts.append([dict(item) for item in messages])
                if self.calls > 6 and not self.overflow_thrown:
                    self.overflow_thrown = True
                    raise RuntimeError("provider rejected: maximum context length exceeded")
                yield _event("message_start", {})
                if self.calls <= 6:
                    yield _event("assistant_delta", {"text": f"working {self.calls}"})
                    yield _event(
                        "tool_call_delta",
                        {
                            "index": 0,
                            "id": f"tool-{self.calls}",
                            "name": "demo_tool",
                            "arguments": "{\"value\": \"go\"}",
                        },
                    )
                else:
                    yield _event("assistant_delta", {"text": "final answer"})
                yield _event("usage", {"prompt_tokens": 1_000, "completion_tokens": 10})
                yield _event("message_stop", {"finish_reason": "stop"})

        async def demo_tool(value: str) -> dict[str, str]:
            return {"echo": value + ("-detail" * 50)}

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="demo_tool",
                description="Demo runtime tool",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
                func=demo_tool,
                concurrency_safe=True,
                read_only=True,
            )
        )

        llm = _OverflowThenRecoverLLM()
        compactor = SimpleNamespace(
            summarize_runtime_history=AsyncMock(return_value="OVERFLOW-RECOVERY-SUMMARY"),
        )
        runtime = NativeRuntimeV2(
            llm=llm,
            tool_registry=registry,
            config=OPCConfig(),
            history_compactor=compactor,
            max_iterations=12,
        )

        result = await runtime.run(
            system_prompt="You are a runtime.",
            user_message="run a long task",
            task=Task(
                id="overflow-task",
                title="overflow task",
                session_id="sess-overflow",
                project_id="proj1",
                metadata={"mode": "task", "execution_mode": "task_mode"},
            ),
        )

        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertIn("final answer", str(result.content))
        # The failing request carried no summary; the retry after the forced
        # compaction did, and the summarizer ran exactly once.
        compactor.summarize_runtime_history.assert_awaited_once()
        failing_prompt = json.dumps(llm.prompts[-2], ensure_ascii=False)
        retry_prompt = json.dumps(llm.prompts[-1], ensure_ascii=False)
        self.assertNotIn("[runtime_v2 durable compaction]", failing_prompt)
        self.assertIn("[runtime_v2 durable compaction]", retry_prompt)
        self.assertIn("OVERFLOW-RECOVERY-SUMMARY", retry_prompt)


class DurableCompactionEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_compacts_midway_on_observed_usage_and_completes(self) -> None:
        def _event(event_type: str, payload: dict[str, object]):
            return type("Evt", (), {"event_type": event_type, "payload": payload, "model": "stub"})()

        class _LongRunLLM:
            """Tool-looping stub whose provider usage crosses the threshold mid-run.

            count_input_tokens deliberately returns 0 so the trigger can only
            come from the observed usage anchor.
            """

            def __init__(self) -> None:
                self.calls = 0
                self.prompts: list[list[dict[str, object]]] = []
                self.config = type("Cfg", (), {"max_tokens": 2048})()

            def prepare_user_message_content(self, content: str, attachment_refs=None):
                _ = attachment_refs
                return content

            def get_tool_definitions(self, tools):
                return tools

            def is_context_overflow_error(self, error: Exception) -> bool:
                _ = error
                return False

            def count_input_tokens(self, messages, tools=None):
                _ = (messages, tools)
                return 0

            def get_context_window(self):
                return 10_000

            async def chat_stream(self, messages, tools=None):
                _ = tools
                self.calls += 1
                self.prompts.append([dict(item) for item in messages])
                yield _event("message_start", {})
                if self.calls <= 12:
                    yield _event("assistant_delta", {"text": f"working {self.calls}"})
                    yield _event(
                        "tool_call_delta",
                        {
                            "index": 0,
                            "id": f"tool-{self.calls}",
                            "name": "demo_tool",
                            "arguments": "{\"value\": \"go\"}",
                        },
                    )
                else:
                    yield _event("assistant_delta", {"text": "final answer"})
                yield _event(
                    "usage",
                    {
                        "prompt_tokens": 9_500 if self.calls >= 5 else 1_000,
                        "completion_tokens": 10,
                    },
                )
                yield _event("message_stop", {"finish_reason": "stop"})

        async def demo_tool(value: str) -> dict[str, str]:
            return {"echo": value + ("-detail" * 50)}

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="demo_tool",
                description="Demo runtime tool",
                parameters={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
                func=demo_tool,
                concurrency_safe=True,
                read_only=True,
            )
        )

        llm = _LongRunLLM()
        compactor = SimpleNamespace(
            summarize_runtime_history=AsyncMock(
                side_effect=[f"MIDRUN-SUMMARY-{n}" for n in range(1, 10)]
            ),
        )
        runtime = NativeRuntimeV2(
            llm=llm,
            tool_registry=registry,
            config=OPCConfig(),
            history_compactor=compactor,
            max_iterations=16,
        )

        result = await runtime.run(
            system_prompt="You are a runtime.",
            user_message="run a long task",
            task=Task(
                id="long-task",
                title="long task",
                session_id="sess-long",
                project_id="proj1",
                metadata={"mode": "task", "execution_mode": "task_mode"},
            ),
        )

        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertIn("final answer", str(result.content))
        # The run must compact more than once, and every request must hold at
        # most ONE summary message with the seed request still verbatim.
        rounds = compactor.summarize_runtime_history.await_count
        self.assertGreaterEqual(rounds, 2)
        for index, prompt in enumerate(llm.prompts):
            marker_messages = [
                item
                for item in prompt
                if "[runtime_v2 durable compaction]" in str(item.get("content", ""))
            ]
            self.assertLessEqual(len(marker_messages), 1, f"request {index}")
            self.assertTrue(
                any(
                    item.get("role") == "user" and "run a long task" in str(item.get("content", ""))
                    for item in prompt
                ),
                f"request {index}: seed request must stay verbatim",
            )
        final_prompt = json.dumps(llm.prompts[-1], ensure_ascii=False)
        self.assertIn(f"MIDRUN-SUMMARY-{rounds}", final_prompt)
        # Chain continuity: the second summary round saw the first summary.
        second_input = json.dumps(
            compactor.summarize_runtime_history.await_args_list[1].kwargs["messages"],
            ensure_ascii=False,
        )
        self.assertIn("MIDRUN-SUMMARY-1", second_input)


if __name__ == "__main__":
    unittest.main()
