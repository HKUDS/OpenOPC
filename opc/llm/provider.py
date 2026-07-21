"""LLM provider layer built on LiteLLM for unified model access."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlparse

from opc.core.windows_ssl import sanitize_windows_sslkeylogfile

sanitize_windows_sslkeylogfile()

import litellm
from loguru import logger

litellm.suppress_debug_info = True
litellm.drop_params = True

from opc.core.attachment_content import attachment_suffix
from opc.core.attachment_store import AttachmentRef
from opc.core.config import LLMConfig
from opc.core.models import ModelCapabilitySet, RuntimeLLMEvent

_MULTIMODAL_MODEL_HINTS = (
    "gpt-4.1",
    "gpt-4o",
    "gpt-4.5",
    "gpt-5",
    "o1",
    "o3",
    "o4",
    "claude-3",
    "claude-sonnet-4",
    "claude-opus-4",
    "gemini",
    "pixtral",
    "llava",
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "internvl",
    "minicpm-v",
    "glm-4v",
)

_DOCUMENT_MODEL_HINTS = (
    "gpt-4.1",
    "gpt-4o",
    "gpt-4.5",
    "gpt-5",
    "claude-3",
    "claude-sonnet-4",
    "claude-opus-4",
    "gemini",
)

_VIDEO_MODEL_HINTS = (
    "gemini",
    "veo",
    "video",
)

_TOOL_PROTOCOL_ERROR_HINTS = (
    "no tool output found for function call",
    "no tool output found",
    "messages with role 'tool' must be a response to a preceding message with 'tool_calls'",
    "assistant message with tool_calls",
    "tool_calls must be followed by tool messages",
    "missing tool response",
    "missing tool output",
    "tool_call_id",
)


def _normalized_model_name(model: str) -> str:
    if "/" in model:
        return model.split("/", 1)[1].strip().lower()
    return model.strip().lower()


# Used when neither user config nor litellm can supply a window. Conservative
# enough for modern models so compaction still has a real denominator.
_CONTEXT_WINDOW_FALLBACK = 128_000
_context_window_fallback_warned: set[str] = set()
_max_tokens_clamp_warned: set[str] = set()


def _clamp_max_tokens(model: str, requested: int) -> int:
    """Cap the requested output tokens at the model's known output limit.

    Providers disagree on how to handle an oversized max_tokens: some clamp
    silently, others (e.g. DeepSeek) reject the request outright. Clamping
    here keeps a generous config default (32768) safe on small-cap models.
    Unknown models pass through unchanged.
    """
    try:
        info = litellm.get_model_info(model)
        cap = info.get("max_output_tokens") or info.get("max_tokens")
    except Exception:
        return requested
    if not cap or requested <= int(cap):
        return requested
    if model not in _max_tokens_clamp_warned:
        _max_tokens_clamp_warned.add(model)
        logger.info(
            "max_tokens {} exceeds output limit {} of model={}; clamping.",
            requested,
            cap,
            model,
        )
    return int(cap)


_CONTEXT_WINDOW_OVERRIDES: tuple[tuple[str, int], ...] = (
    ("gpt-5.4-pro", 1_050_000),
    ("gpt-5.4-mini", 400_000),
    ("gpt-5.4-nano", 400_000),
    ("gpt-5.4", 1_050_000),
    ("gpt-5-pro", 400_000),
    ("gpt-5", 400_000),
)


_POE_CONTEXT_WINDOW_OVERRIDES: tuple[tuple[str, int], ...] = (
    ("claude-sonnet-4.5", 64_000),
    ("claude-sonnet-4-5", 64_000),
)


def _context_window_override(model: str) -> int | None:
    normalized = _normalized_model_name(model)
    for prefix, window in _CONTEXT_WINDOW_OVERRIDES:
        if normalized == prefix or normalized.startswith(f"{prefix}-"):
            return window
    return None


def _poe_context_window_override(model: str) -> int | None:
    normalized = _normalized_model_name(model)
    for prefix, window in _POE_CONTEXT_WINDOW_OVERRIDES:
        if normalized == prefix or normalized.startswith(f"{prefix}-"):
            return window
    return None


def _is_official_openai_base(api_base: str | None) -> bool:
    normalized = str(api_base or "").strip()
    if not normalized:
        return True
    try:
        parsed = urlparse(normalized)
    except Exception:
        return False
    hostname = (parsed.hostname or "").strip().lower()
    return hostname in {"api.openai.com", "openai.com"}


def _is_poe_base(api_base: str | None) -> bool:
    normalized = str(api_base or "").strip()
    if not normalized:
        return False
    try:
        parsed = urlparse(normalized)
    except Exception:
        return False
    hostname = (parsed.hostname or "").strip().lower()
    return hostname == "api.poe.com"


def _looks_like_multimodal_model(model: str) -> bool:
    normalized = _normalized_model_name(model)
    if any(hint in normalized for hint in _MULTIMODAL_MODEL_HINTS):
        return True
    return normalized.endswith("-vl") or "-vl-" in normalized or "_vl" in normalized


def _looks_like_document_capable_model(model: str) -> bool:
    normalized = _normalized_model_name(model)
    return any(hint in normalized for hint in _DOCUMENT_MODEL_HINTS)


def _looks_like_video_capable_model(model: str) -> bool:
    normalized = _normalized_model_name(model)
    return any(hint in normalized for hint in _VIDEO_MODEL_HINTS)


def _parse_tool_arguments(tool_name: str, arguments: Any) -> tuple[Any, str | None, str | None]:
    """Parse tool-call arguments and preserve failures for downstream recovery."""
    if not isinstance(arguments, str):
        return arguments, None, None

    raw = arguments
    try:
        parsed = json.loads(raw)
        return parsed, raw, None
    except json.JSONDecodeError as e:
        snippet = raw[:500].replace("\n", "\\n")
        error = f"Invalid tool arguments JSON for `{tool_name}`: {e.msg} at char {e.pos}"
        logger.warning(f"{error}. Raw snippet: {snippet}")
        return raw, raw, error


class LLMProvider:
    """Unified LLM interface via LiteLLM supporting tool calls."""

    # Well-known provider API-key env vars that litellm reads directly when no
    # explicit api_key is passed. Used only by ``has_credentials()`` to avoid a
    # false "no credentials" verdict for env-based setups. Missing a provider
    # here just preserves the old behavior (a real LLM attempt), never a wrong
    # skip of a working key.
    _CREDENTIAL_ENV_VARS = (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "AZURE_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "MISTRAL_API_KEY",
        "GROQ_API_KEY",
        "DEEPSEEK_API_KEY",
        "TOGETHERAI_API_KEY",
        "ARK_API_KEY",
    )

    def __init__(self, config: LLMConfig, opc_home: Path | None = None) -> None:
        self.config = config
        self.opc_home = opc_home
        self._total_tokens_in = 0
        self._total_tokens_out = 0
        self._total_cost = 0.0

        self._api_key = config.api_key or (
            os.environ.get(config.api_key_env) if config.api_key_env else None
        ) or None
        self._api_base = config.api_base or None

    def has_credentials(self) -> bool:
        """Whether an LLM call can plausibly authenticate.

        True when a key is configured (``api_key`` / ``api_key_env``) or a
        well-known provider env var is present. False only when no credential
        is found anywhere — callers use that to skip LLM work that would
        certainly fail (e.g. native agent selection when an external agent can
        run the task instead). A False at worst degrades to rule-based behavior,
        which stays functional; it never blocks execution.
        """
        if self._api_key:
            return True
        return any(os.environ.get(var) for var in self._CREDENTIAL_ENV_VARS)

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "tokens_in": self._total_tokens_in,
            "tokens_out": self._total_tokens_out,
            "estimated_cost": self._total_cost,
        }

    def _select_model(self, task_type: str | None = None) -> str:
        if task_type and task_type in self.config.routing:
            return self.config.routing[task_type]
        return self.config.default_model

    def _config_context_window_override(self, model: str) -> int | None:
        """User-configured context window for models litellm cannot map.

        Per-model overrides win over the scalar default. Both come from
        ``LLMConfig`` so proxy/self-hosted models (doubao, minimax, glm, …)
        can report a real context window to the usage ring and compaction.
        """
        per_model = getattr(self.config, "context_window_overrides", None) or {}
        normalized = _normalized_model_name(model)
        for key, window in per_model.items():
            candidate = _normalized_model_name(str(key))
            if candidate and (normalized == candidate or normalized.startswith(f"{candidate}-")):
                try:
                    value = int(window)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    return value
        try:
            scalar = int(getattr(self.config, "context_window", 0) or 0)
        except (TypeError, ValueError):
            scalar = 0
        return scalar if scalar > 0 else None

    def get_context_window(self, task_type: str | None = None, model: str | None = None) -> int | None:
        resolved_model = model or self._select_model(task_type)
        config_override = self._config_context_window_override(resolved_model)
        if config_override is not None:
            return config_override
        poe_override = _poe_context_window_override(resolved_model) if _is_poe_base(self._api_base) else None
        if poe_override is not None:
            return poe_override
        override = _context_window_override(resolved_model) if _is_official_openai_base(self._api_base) else None
        if override is not None:
            return override
        try:
            # max_input_tokens is the context window; litellm.get_max_tokens()
            # returns the "max_tokens" map entry, which for many models (e.g.
            # deepseek) is the OUTPUT cap and would wildly under-report here.
            info = litellm.get_model_info(resolved_model)
            limit = info.get("max_input_tokens") or info.get("max_tokens")
            if limit:
                return int(limit)
            reason = "model is not mapped in litellm"
        except Exception as e:
            reason = str(e)
        if resolved_model not in _context_window_fallback_warned:
            _context_window_fallback_warned.add(resolved_model)
            logger.warning(
                "Unable to resolve context window for model={} ({}); assuming {} tokens. "
                "Set llm.context_window or llm.context_window_overrides in llm_config.yaml "
                "to use the model's real window.",
                resolved_model,
                reason,
                _CONTEXT_WINDOW_FALLBACK,
            )
        return _CONTEXT_WINDOW_FALLBACK

    def count_input_tokens(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        task_type: str | None = None,
        model: str | None = None,
    ) -> int | None:
        resolved_model = model or self._select_model(task_type)
        try:
            return int(litellm.token_counter(
                model=resolved_model,
                messages=messages,
                tools=tools,
            ))
        except Exception as e:
            logger.warning(f"Unable to count prompt tokens for model={resolved_model}: {e}")
            return None

    def count_text_tokens(
        self,
        text: str,
        task_type: str | None = None,
        model: str | None = None,
    ) -> int | None:
        resolved_model = model or self._select_model(task_type)
        try:
            return int(litellm.token_counter(
                model=resolved_model,
                text=text,
            ))
        except Exception as e:
            logger.warning(f"Unable to count text tokens for model={resolved_model}: {e}")
            return None

    def get_capabilities(
        self,
        task_type: str | None = None,
        model: str | None = None,
    ) -> ModelCapabilitySet:
        resolved_model = model or self._select_model(task_type)
        normalized = _normalized_model_name(resolved_model)
        provider_family = resolved_model.split("/", 1)[0].strip().lower() if "/" in resolved_model else ""
        supports_thinking = any(hint in normalized for hint in ("o1", "o3", "o4", "gpt-5", "claude", "reason"))
        return ModelCapabilitySet(
            model=resolved_model,
            supports_streaming=True,
            supports_tool_calling=True,
            supports_streaming_tool_calls=True,
            supports_thinking=supports_thinking,
            supports_multimodal=_looks_like_multimodal_model(resolved_model),
            supports_documents=_looks_like_document_capable_model(resolved_model),
            supports_video=_looks_like_video_capable_model(resolved_model),
            provider_family=provider_family,
            metadata={
                "api_base": self._api_base or "",
            },
        )

    def build_cache_fingerprint(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        task_type: str | None = None,
        model: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        resolved_model = model or self._select_model(task_type)
        payload = {
            "model": resolved_model,
            "messages": messages,
            "tools": tools or [],
            "extra": extra or {},
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def is_context_overflow_error(self, error: Exception) -> bool:
        if isinstance(error, litellm.exceptions.ContextWindowExceededError):
            return True
        message = str(error).lower()
        keywords = (
            "context window",
            "context length",
            "maximum context length",
            "prompt is too long",
            "too many tokens",
            "context_length_exceeded",
            "token limit exceeded",
        )
        return any(keyword in message for keyword in keywords)

    def is_tool_protocol_error(self, error: Exception) -> bool:
        message = str(error).lower()
        return any(hint in message for hint in _TOOL_PROTOCOL_ERROR_HINTS)

    @staticmethod
    def sanitize_tool_call_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop incomplete or stray assistant/tool tool-call transcripts."""
        sanitized: list[dict[str, Any]] = []
        pending_ids: list[str] = []
        buffered_block: list[dict[str, Any]] = []

        def _copy_message(message: dict[str, Any]) -> dict[str, Any]:
            cloned = dict(message)
            if isinstance(message.get("tool_calls"), list):
                cloned["tool_calls"] = [dict(item) for item in message["tool_calls"]]
            return cloned

        for message in messages:
            role = str(message.get("role", "") or "").strip()
            if not pending_ids:
                if role == "assistant" and isinstance(message.get("tool_calls"), list) and message["tool_calls"]:
                    ids = [
                        str(item.get("id", "") or "").strip()
                        for item in message["tool_calls"]
                        if isinstance(item, dict) and str(item.get("id", "") or "").strip()
                    ]
                    if not ids:
                        continue
                    buffered_block = [_copy_message(message)]
                    pending_ids = ids
                    continue
                if role == "tool":
                    continue
                sanitized.append(_copy_message(message))
                continue

            if role == "tool":
                tool_call_id = str(message.get("tool_call_id", "") or "").strip()
                if tool_call_id and tool_call_id in pending_ids:
                    buffered_block.append(_copy_message(message))
                    pending_ids = [item for item in pending_ids if item != tool_call_id]
                    if not pending_ids:
                        sanitized.extend(buffered_block)
                        buffered_block = []
                continue

            if role == "assistant" and isinstance(message.get("tool_calls"), list) and message["tool_calls"]:
                ids = [
                    str(item.get("id", "") or "").strip()
                    for item in message["tool_calls"]
                    if isinstance(item, dict) and str(item.get("id", "") or "").strip()
                ]
                buffered_block = [_copy_message(message)] if ids else []
                pending_ids = ids
                continue

            buffered_block = []
            pending_ids = []
            sanitized.append(_copy_message(message))

        return sanitized

    def prepare_user_message_content(
        self,
        content: str,
        *,
        attachment_refs: list[dict[str, Any]] | None = None,
        task_type: str | None = None,
    ) -> str | list[dict[str, Any]]:
        text = str(content or "")
        refs = list(attachment_refs or [])
        if not refs:
            return text

        model = self._select_model(task_type)
        parts = self._build_direct_attachment_parts(model, refs)
        if not parts:
            return text

        content_parts: list[dict[str, Any]] = []
        if text:
            content_parts.append({"type": "text", "text": text})
        content_parts.extend(parts)
        return content_parts

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        task_type: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        model = self._select_model(task_type)
        temp = temperature if temperature is not None else self.config.temperature
        max_tok = _clamp_max_tokens(model, max_tokens if max_tokens is not None else self.config.max_tokens)

        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": max_tok,
            **kwargs,
        }
        if self._api_base:
            call_kwargs["api_base"] = self._api_base
        if self._api_key:
            call_kwargs["api_key"] = self._api_key
        if tools:
            call_kwargs["tools"] = tools
            call_kwargs["tool_choice"] = "auto"

        logger.debug(f"LLM call: model={model}, base={self._api_base or 'default'}, msgs={len(messages)}, tools={len(tools or [])}")

        try:
            response = await litellm.acompletion(**call_kwargs)
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            raise

        usage = getattr(response, "usage", None)
        cost = 0.0
        if usage:
            self._total_tokens_in += getattr(usage, "prompt_tokens", 0)
            self._total_tokens_out += getattr(usage, "completion_tokens", 0)
            try:
                cost = litellm.completion_cost(completion_response=response)
                self._total_cost += cost
            except Exception:
                pass

        choice = response.choices[0]
        message = choice.message

        result: dict[str, Any] = {
            "content": message.content or "",
            "tool_calls": [],
            "finish_reason": choice.finish_reason,
            "model": model,
            "cost": cost,
            "usage": {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) if usage else 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) if usage else 0,
            },
        }

        if message.tool_calls:
            for tc in message.tool_calls:
                args, raw_args, parse_error = _parse_tool_arguments(tc.function.name, tc.function.arguments)
                tool_call = {
                    "id": tc.id,
                    "function": tc.function.name,
                    "arguments": args,
                }
                if raw_args is not None:
                    tool_call["arguments_raw"] = raw_args
                if parse_error:
                    tool_call["arguments_parse_error"] = parse_error
                result["tool_calls"].append(tool_call)

        return result

    def normalize_stream_event(
        self,
        chunk: Any,
        *,
        model: str,
    ) -> list[RuntimeLLMEvent]:
        events: list[RuntimeLLMEvent] = []
        usage = getattr(chunk, "usage", None)
        if usage:
            events.append(RuntimeLLMEvent(
                event_type="usage",
                model=model,
                payload={
                    "prompt_tokens": getattr(usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(usage, "completion_tokens", 0),
                    "context_window": self.get_context_window(model=model),
                },
            ))

        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return events

        choice = choices[0]
        delta = getattr(choice, "delta", None)
        if delta is not None:
            text = getattr(delta, "content", None)
            if text:
                events.append(RuntimeLLMEvent(
                    event_type="assistant_delta",
                    model=model,
                    payload={"text": text},
                ))
            thinking_text = (
                getattr(delta, "reasoning", None)
                or getattr(delta, "reasoning_content", None)
                or getattr(delta, "thinking", None)
            )
            if thinking_text:
                events.append(RuntimeLLMEvent(
                    event_type="thinking_delta",
                    model=model,
                    payload={"text": str(thinking_text)},
                ))
            tool_calls = getattr(delta, "tool_calls", None) or []
            for tool_call in tool_calls:
                function = getattr(tool_call, "function", None)
                events.append(RuntimeLLMEvent(
                    event_type="tool_call_delta",
                    model=model,
                    payload={
                        "index": getattr(tool_call, "index", 0),
                        "id": getattr(tool_call, "id", ""),
                        "name": getattr(function, "name", "") if function else "",
                        "arguments": getattr(function, "arguments", "") if function else "",
                    },
                ))

        finish_reason = getattr(choice, "finish_reason", None)
        if finish_reason:
            events.append(RuntimeLLMEvent(
                event_type="message_stop",
                model=model,
                payload={"finish_reason": finish_reason},
            ))
        return events

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        task_type: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[RuntimeLLMEvent]:
        model = self._select_model(task_type)
        temp = temperature if temperature is not None else self.config.temperature
        max_tok = _clamp_max_tokens(model, max_tokens if max_tokens is not None else self.config.max_tokens)

        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": max_tok,
            "stream": True,
            **kwargs,
        }
        if self._api_base:
            call_kwargs["api_base"] = self._api_base
        if self._api_key:
            call_kwargs["api_key"] = self._api_key
        if tools:
            call_kwargs["tools"] = tools
            call_kwargs["tool_choice"] = "auto"

        logger.debug(
            f"LLM stream call: model={model}, base={self._api_base or 'default'}, msgs={len(messages)}, tools={len(tools or [])}"
        )

        last_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        yield RuntimeLLMEvent(event_type="message_start", model=model, payload={"model": model})

        try:
            stream = await litellm.acompletion(**call_kwargs)
            if hasattr(stream, "__aiter__"):
                async for chunk in stream:
                    for event in self.normalize_stream_event(chunk, model=model):
                        if event.event_type == "usage":
                            total_prompt = int(event.payload.get("prompt_tokens", 0) or 0)
                            total_completion = int(event.payload.get("completion_tokens", 0) or 0)
                            delta_prompt = max(0, total_prompt - last_usage["prompt_tokens"])
                            delta_completion = max(0, total_completion - last_usage["completion_tokens"])
                            last_usage["prompt_tokens"] = total_prompt
                            last_usage["completion_tokens"] = total_completion
                            cost = 0.0
                            try:
                                prompt_cost, completion_cost = litellm.cost_per_token(
                                    model=model,
                                    prompt_tokens=delta_prompt,
                                    completion_tokens=delta_completion,
                                )
                                cost = float(prompt_cost or 0.0) + float(completion_cost or 0.0)
                            except Exception:
                                cost = 0.0
                            self._total_tokens_in += delta_prompt
                            self._total_tokens_out += delta_completion
                            self._total_cost += cost
                            event.payload = {
                                **dict(event.payload),
                                "prompt_tokens": delta_prompt,
                                "completion_tokens": delta_completion,
                                "prompt_tokens_total": total_prompt,
                                "completion_tokens_total": total_completion,
                                "estimated_cost_delta": cost,
                                "estimated_cost_total": self._total_cost,
                                "context_window": event.payload.get("context_window") or self.get_context_window(model=model),
                                "model": model,
                            }
                        yield event
            else:
                # Provider fallback: treat the response as a single non-streaming completion.
                choice = stream.choices[0]
                message = choice.message
                if getattr(message, "content", None):
                    yield RuntimeLLMEvent(
                        event_type="assistant_delta",
                        model=model,
                        payload={"text": message.content},
                    )
                for tc in getattr(message, "tool_calls", None) or []:
                    yield RuntimeLLMEvent(
                        event_type="tool_call_delta",
                        model=model,
                        payload={
                            "index": 0,
                            "id": getattr(tc, "id", ""),
                            "name": getattr(getattr(tc, "function", None), "name", ""),
                            "arguments": getattr(getattr(tc, "function", None), "arguments", ""),
                        },
                    )
                usage = getattr(stream, "usage", None)
                if usage:
                    cost = 0.0
                    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                    try:
                        prompt_cost, completion_cost = litellm.cost_per_token(
                            model=model,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                        )
                        cost = float(prompt_cost or 0.0) + float(completion_cost or 0.0)
                    except Exception:
                        cost = 0.0
                    self._total_tokens_in += prompt_tokens
                    self._total_tokens_out += completion_tokens
                    self._total_cost += cost
                    yield RuntimeLLMEvent(
                        event_type="usage",
                        model=model,
                        payload={
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "prompt_tokens_total": prompt_tokens,
                            "completion_tokens_total": completion_tokens,
                            "estimated_cost_delta": cost,
                            "estimated_cost_total": self._total_cost,
                            "context_window": self.get_context_window(model=model),
                            "model": model,
                        },
                    )
                yield RuntimeLLMEvent(
                    event_type="message_stop",
                    model=model,
                    payload={"finish_reason": getattr(choice, "finish_reason", "stop")},
                )
        except Exception as e:
            logger.error(f"LLM stream failed: {e}")
            yield RuntimeLLMEvent(
                event_type="error",
                model=model,
                payload={"message": str(e)},
            )
            raise

    async def simple_chat(
        self,
        prompt: str,
        system: str | None = None,
        task_type: str | None = None,
    ) -> str:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        result = await self.chat(messages, task_type=task_type)
        return result["content"]

    def get_tool_definitions(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert internal tool definitions to OpenAI function-calling format."""
        formatted = []
        for tool in tools:
            formatted.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("parameters", {"type": "object", "properties": {}}),
                },
            })
        return formatted

    def _build_direct_attachment_parts(
        self,
        model: str,
        attachment_refs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        capabilities = self._attachment_capabilities(model)
        if not capabilities["enabled"]:
            return []

        parts: list[dict[str, Any]] = []
        for ref_dict in attachment_refs:
            try:
                ref = AttachmentRef.from_dict(ref_dict)
                data_url = self._attachment_data_url(ref)
            except Exception as exc:
                logger.warning(f"Skipping direct attachment payload: {exc}")
                continue

            if not data_url:
                continue

            if ref.mime_type.startswith("image/") and capabilities["image_mode"]:
                parts.append(self._build_attachment_part("image_url", ref, data_url))
            elif ref.mime_type == "application/pdf" and capabilities["pdf_mode"]:
                parts.append(self._build_attachment_part(str(capabilities["pdf_mode"]), ref, data_url))
            elif ref.mime_type.startswith("video/") and capabilities["video_mode"]:
                parts.append(self._build_attachment_part(str(capabilities["video_mode"]), ref, data_url))

        return parts

    def _build_attachment_part(
        self,
        mode: str,
        ref: AttachmentRef,
        data_url: str,
    ) -> dict[str, Any]:
        if mode == "image_url":
            return {
                "type": "image_url",
                "image_url": {"url": data_url},
            }
        if mode == "video_url":
            return {
                "type": "video_url",
                "video_url": {"url": data_url},
            }
        if mode == "file":
            return {
                "type": "file",
                "file": {
                    "file_data": data_url,
                    "filename": ref.filename,
                },
            }
        raise ValueError(f"Unsupported attachment transport mode: {mode}")

    def _attachment_capabilities(self, model: str) -> dict[str, Any]:
        provider = model.split("/", 1)[0].strip().lower()
        api_base = (self._api_base or "").lower()

        image_mode: str | None = None
        pdf_mode: str | None = None
        video_mode: str | None = None

        if "api.poe.com" in api_base:
            image_mode = "image_url"
            pdf_mode = "file"
            if _looks_like_multimodal_model(model):
                video_mode = "file"
        elif "openrouter.ai" in api_base:
            image_mode = "image_url"
            pdf_mode = "file"
            if _looks_like_video_capable_model(model):
                video_mode = "video_url"
        elif provider in {"openai", "azure", "anthropic"}:
            image_mode = "image_url"
            pdf_mode = "file"
        elif provider in {"google", "gemini", "vertex_ai", "vertex"}:
            image_mode = "image_url"
            pdf_mode = "file"
            if _looks_like_video_capable_model(model):
                video_mode = "video_url"
        elif _looks_like_multimodal_model(model):
            image_mode = "image_url"
            pdf_mode = "file" if _looks_like_document_capable_model(model) else None
            if _looks_like_video_capable_model(model):
                video_mode = "video_url"

        return {
            "enabled": bool(image_mode or pdf_mode or video_mode),
            "image_mode": image_mode,
            "pdf_mode": pdf_mode,
            "video_mode": video_mode,
        }

    def _attachment_data_url(self, ref: AttachmentRef) -> str | None:
        path = self._resolve_attachment_path(ref)
        if path is None:
            return None
        payload = base64.b64encode(path.read_bytes()).decode("ascii")
        mime_type = ref.mime_type or _guess_mime_from_filename(ref.filename)
        return f"data:{mime_type};base64,{payload}"

    def _resolve_attachment_path(self, ref: AttachmentRef) -> Path | None:
        if not self.opc_home or not ref.disk_path:
            return None
        resolved = (self.opc_home / ref.disk_path).resolve()
        opc_home_resolved = self.opc_home.resolve()
        if not str(resolved).startswith(str(opc_home_resolved)):
            raise ValueError(f"Attachment path escapes OPC home: {ref.disk_path}")
        return resolved


def _guess_mime_from_filename(filename: str) -> str:
    suffix = attachment_suffix(filename)
    if suffix == ".pdf":
        return "application/pdf"
    if suffix == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if suffix == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if suffix == ".pptx":
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if suffix == ".mp4":
        return "video/mp4"
    if suffix in {".mpeg", ".mpg"}:
        return "video/mpeg"
    if suffix == ".mov":
        return "video/quicktime"
    if suffix == ".webm":
        return "video/webm"
    return "application/octet-stream"
