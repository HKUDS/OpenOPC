from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from opc.core.config import LLMConfig
from opc.llm.provider import LLMProvider


def _completion_response() -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="ok", tool_calls=[]),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
    )


async def _completion_stream():
    yield SimpleNamespace(
        usage=None,
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content="ok",
                    reasoning=None,
                    reasoning_content=None,
                    thinking=None,
                    tool_calls=[],
                ),
                finish_reason="stop",
            )
        ],
    )


class TestLLMProviderReasoningEffort(unittest.IsolatedAsyncioTestCase):
    def test_config_retains_reasoning_effort(self) -> None:
        config = LLMConfig.model_validate({
            "default_model": "openai/gpt-5.6-luna",
            "reasoning_effort": "max",
        })

        assert config.reasoning_effort == "max"

    async def test_chat_forwards_configured_reasoning_effort(self) -> None:
        provider = LLMProvider(LLMConfig(
            default_model="openai/gpt-5.6-luna",
            reasoning_effort="max",
        ))

        with (
            patch("opc.llm.provider._clamp_max_tokens", return_value=128),
            patch(
                "opc.llm.provider.litellm.acompletion",
                new=AsyncMock(return_value=_completion_response()),
            ) as completion,
        ):
            await provider.chat([{"role": "user", "content": "hello"}])

        assert completion.await_args.kwargs["reasoning_effort"] == "max"

    async def test_chat_allows_per_call_reasoning_effort_override(self) -> None:
        provider = LLMProvider(LLMConfig(
            default_model="openai/gpt-5.6-luna",
            reasoning_effort="high",
        ))

        with (
            patch("opc.llm.provider._clamp_max_tokens", return_value=128),
            patch(
                "opc.llm.provider.litellm.acompletion",
                new=AsyncMock(return_value=_completion_response()),
            ) as completion,
        ):
            await provider.chat(
                [{"role": "user", "content": "hello"}],
                reasoning_effort="low",
            )

        assert completion.await_args.kwargs["reasoning_effort"] == "low"

    async def test_chat_stream_forwards_configured_reasoning_effort(self) -> None:
        provider = LLMProvider(LLMConfig(
            default_model="openai/gpt-5.6-luna",
            reasoning_effort="max",
        ))

        with (
            patch("opc.llm.provider._clamp_max_tokens", return_value=128),
            patch(
                "opc.llm.provider.litellm.acompletion",
                new=AsyncMock(return_value=_completion_stream()),
            ) as completion,
        ):
            events = [
                event
                async for event in provider.chat_stream([
                    {"role": "user", "content": "hello"},
                ])
            ]

        assert events
        assert completion.await_args.kwargs["reasoning_effort"] == "max"
        assert completion.await_args.kwargs["stream"] is True

    async def test_chat_stream_allows_per_call_reasoning_effort_override(self) -> None:
        provider = LLMProvider(LLMConfig(
            default_model="openai/gpt-5.6-luna",
            reasoning_effort="high",
        ))

        with (
            patch("opc.llm.provider._clamp_max_tokens", return_value=128),
            patch(
                "opc.llm.provider.litellm.acompletion",
                new=AsyncMock(return_value=_completion_stream()),
            ) as completion,
        ):
            events = [
                event
                async for event in provider.chat_stream(
                    [{"role": "user", "content": "hello"}],
                    reasoning_effort="low",
                )
            ]

        assert events
        assert completion.await_args.kwargs["reasoning_effort"] == "low"

    async def test_unset_reasoning_effort_is_not_added_to_requests(self) -> None:
        provider = LLMProvider(LLMConfig(default_model="openai/gpt-5.6-luna"))

        with (
            patch("opc.llm.provider._clamp_max_tokens", return_value=128),
            patch(
                "opc.llm.provider.litellm.acompletion",
                new=AsyncMock(return_value=_completion_response()),
            ) as completion,
        ):
            await provider.chat([{"role": "user", "content": "hello"}])

        assert "reasoning_effort" not in completion.await_args.kwargs
