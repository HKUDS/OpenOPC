"""Unified retry helper for LLM calls that expect structured (JSON) output.

The orchestration layers of OPC frequently ask an LLM to produce a JSON object
that is then parsed, type-validated, and fed into downstream business logic
(work-item assignments, gate harness decisions, approval reviews, memory
extraction, permission classifications, etc.).

Historically, each call site re-implemented its own markdown-fence stripping,
`json.loads` wrapping, and silent fallback. When the model occasionally
produced malformed JSON or an unrecognised enum value, the whole orchestration
step would either:

* raise an unhandled exception (breaking the run), or
* silently fall back to a degraded default (masking the real failure and
  producing the wrong answer).

This helper replaces that ad-hoc handling with a unified 3-attempt retry loop
that feeds the model's previous error back into the next prompt so it can
self-correct. It mirrors the retry pattern already used by
the company runtime coordination builder and the recruiter:
append a `retry_feedback` list to the payload on each retry, then re-invoke
`LLMProvider.simple_chat`.

Call sites that need more than a simple JSON parse can also pass a
`validator` callable; if it returns a non-empty string, that string is treated
as the error message for the next retry.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from loguru import logger

from opc.llm.provider import LLMProvider, ProviderQuotaExhaustedError


Validator = Callable[[Any], "str | None"]


class LLMRetryError(RuntimeError):
    """Raised when the retry loop exhausts all attempts without success."""

    def __init__(self, label: str, attempts: int, last_error: str, last_raw: str | None) -> None:
        super().__init__(
            f"[{label}] LLM JSON retry exhausted after {attempts} attempts. "
            f"Last error: {last_error}"
        )
        self.label = label
        self.attempts = attempts
        self.last_error = last_error
        self.last_raw = last_raw


def _strip_fences(raw: str) -> str:
    """Remove surrounding ```...``` fences, tolerant of missing newlines."""
    text = str(raw or "").strip()
    if text.startswith("```"):
        parts = text.split("\n", 1)
        text = parts[1] if len(parts) == 2 else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


def _compose_prompt(
    payload: Any,
    retry_feedback: list[str],
) -> str:
    """Build the prompt body for an attempt.

    For dict payloads we copy and inject `retry_feedback` so the model can see
    exactly why its previous attempt was rejected. For string payloads we
    append a labelled section so we don't silently mutate the caller's text.
    """
    if isinstance(payload, dict):
        attempt_payload = dict(payload)
        if retry_feedback:
            attempt_payload["retry_feedback"] = list(retry_feedback)
        return json.dumps(attempt_payload, ensure_ascii=False)
    text = str(payload or "")
    if retry_feedback:
        feedback_block = "\n".join(f"- {item}" for item in retry_feedback)
        return (
            f"{text}\n\n"
            "# retry_feedback (your previous attempt failed; correct and retry)\n"
            f"{feedback_block}"
        )
    return text


def _provider_quota_error(
    llm: LLMProvider,
    error: Exception,
) -> ProviderQuotaExhaustedError | None:
    """Return the typed quota error represented by a transport failure."""
    chain: list[Exception] = []
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, Exception):
            chain.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)

    for current in chain:
        if isinstance(current, ProviderQuotaExhaustedError):
            if current is error:
                return current
            # A fresh boundary error preserves the wrapper -> nested chain.
            # Re-raising the nested instance *from* its own wrapper would
            # mutate it into the cycle wrapper -> nested -> wrapper.
            return ProviderQuotaExhaustedError(str(current))

    rate_limit_checker = getattr(llm, "is_rate_limit_error", None)
    if callable(rate_limit_checker):
        for current in chain:
            if rate_limit_checker(current):
                return ProviderQuotaExhaustedError(str(current))
    return None


async def call_llm_json_with_retry(
    llm: LLMProvider,
    *,
    system: str,
    payload: Any,
    task_type: str = "quick_tasks",
    validator: Validator | None = None,
    max_attempts: int = 3,
    label: str = "llm_json",
    require_object: bool = True,
) -> Any:
    """Call `llm.simple_chat` expecting a JSON object, retrying on failure.

    Args:
        llm: LLMProvider instance.
        system: System prompt describing the JSON contract.
        payload: dict (preferred) or string. When a dict, the helper injects
            a `retry_feedback` key on retries so the model can see its
            previous error. When a string, the feedback is appended as a
            trailing section.
        task_type: routing hint forwarded to `simple_chat`.
        validator: optional callable `validator(parsed) -> Optional[str]`.
            Return `None` when the parsed object passes validation; return an
            error description string to trigger a retry with that feedback.
        max_attempts: total attempts including the first one (default 3).
        label: short tag used in log messages and the raised exception.
        require_object: when True (default) the parsed top-level value must
            be a dict; otherwise any JSON value (list, scalar) is accepted.

    Returns:
        The parsed, validated JSON value.

    Raises:
        ProviderQuotaExhaustedError: when the provider rejects the request for
            quota/rate-limit reasons. These failures must be parked by the
            caller rather than consumed by the JSON correction retry loop.
        LLMRetryError: when all attempts fail. Callers typically catch this
            and fall back to their heuristic default so the orchestration
            stays alive.
    """
    retry_feedback: list[str] = []
    last_error: str = "no attempt made"
    last_raw: str | None = None

    for attempt in range(1, max_attempts + 1):
        prompt = _compose_prompt(payload, retry_feedback)
        try:
            raw = await llm.simple_chat(
                prompt=prompt,
                system=system,
                task_type=task_type,
            )
        except Exception as exc:
            quota_error = _provider_quota_error(llm, exc)
            if quota_error is not None:
                if quota_error is exc:
                    raise
                raise quota_error from exc
            last_error = f"LLM transport error: {exc}"
            last_raw = None
            logger.warning(
                f"[{label}] attempt {attempt}/{max_attempts} transport error: {exc}"
            )
            retry_feedback.append(last_error)
            continue

        last_raw = raw
        text = _strip_fences(raw)
        if not text:
            last_error = "Response was empty after stripping markdown fences."
            logger.warning(
                f"[{label}] attempt {attempt}/{max_attempts} empty response."
            )
            retry_feedback.append(last_error)
            continue

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            snippet = text[:200].replace("\n", "\\n")
            last_error = (
                f"Response was not valid JSON: {exc.msg} at char {exc.pos}. "
                f"Snippet: {snippet}"
            )
            logger.warning(
                f"[{label}] attempt {attempt}/{max_attempts} JSON parse failed: {exc}"
            )
            retry_feedback.append(last_error)
            continue

        if require_object and not isinstance(data, dict):
            last_error = (
                f"Top-level response must be a JSON object; got {type(data).__name__}."
            )
            logger.warning(f"[{label}] attempt {attempt}/{max_attempts}: {last_error}")
            retry_feedback.append(last_error)
            continue

        if validator is not None:
            try:
                validation_error = validator(data)
            except Exception as exc:
                validation_error = f"Validator raised: {exc}"
            if validation_error:
                last_error = str(validation_error)
                logger.warning(
                    f"[{label}] attempt {attempt}/{max_attempts} validation failed: {last_error}"
                )
                retry_feedback.append(last_error)
                continue

        return data

    raise LLMRetryError(
        label=label,
        attempts=max_attempts,
        last_error=last_error,
        last_raw=last_raw,
    )


async def call_llm_json_with_retry_custom(
    llm: LLMProvider,
    *,
    system: str,
    payload: Any,
    task_type: str = "quick_tasks",
    builder: Callable[[Any], Awaitable[Any]] | Callable[[Any], Any],
    max_attempts: int = 3,
    label: str = "llm_json_custom",
) -> Any:
    """Variant that lets the caller build/validate the final object in one pass.

    The `builder` callable receives the parsed JSON value and must either
    return the caller's normalized object (any type) or raise an exception
    with a human-readable message. A raised exception is converted into
    retry feedback, so the model can see exactly what failed during its
    last attempt (e.g. dataclass construction, enum coercion, schema
    normalization). This matches how the work-item assignment planner wraps its
    `_sanitize_work_item_assignment_packet` call in an exception handler.
    """
    retry_feedback: list[str] = []
    last_error: str = "no attempt made"
    last_raw: str | None = None

    for attempt in range(1, max_attempts + 1):
        prompt = _compose_prompt(payload, retry_feedback)
        try:
            raw = await llm.simple_chat(
                prompt=prompt,
                system=system,
                task_type=task_type,
            )
        except Exception as exc:
            last_error = f"LLM transport error: {exc}"
            last_raw = None
            logger.warning(
                f"[{label}] attempt {attempt}/{max_attempts} transport error: {exc}"
            )
            retry_feedback.append(last_error)
            continue

        last_raw = raw
        text = _strip_fences(raw)
        if not text:
            last_error = "Response was empty after stripping markdown fences."
            logger.warning(
                f"[{label}] attempt {attempt}/{max_attempts} empty response."
            )
            retry_feedback.append(last_error)
            continue

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            snippet = text[:200].replace("\n", "\\n")
            last_error = (
                f"Response was not valid JSON: {exc.msg} at char {exc.pos}. "
                f"Snippet: {snippet}"
            )
            logger.warning(
                f"[{label}] attempt {attempt}/{max_attempts} JSON parse failed: {exc}"
            )
            retry_feedback.append(last_error)
            continue

        try:
            result = builder(data)
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[assignment]
        except Exception as exc:
            last_error = f"Response could not be normalized: {exc}"
            logger.warning(
                f"[{label}] attempt {attempt}/{max_attempts} build failed: {exc}"
            )
            retry_feedback.append(last_error)
            continue

        return result

    raise LLMRetryError(
        label=label,
        attempts=max_attempts,
        last_error=last_error,
        last_raw=last_raw,
    )
