"""User input request tool for structured pause/resume."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from opc.layer4_tools.registry import COMPANY_EFFECT_RUNTIME_INTERNAL, ToolDefinition


_OPTION_IDS: tuple[str, ...] = ("a", "b", "c")


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _bool_or_default(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = _clean_text(value).lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return bool(value)


def _unique_id(candidate: str, used: set[str], fallback: str) -> str:
    base = _clean_text(candidate) or fallback
    if base not in used:
        used.add(base)
        return base
    index = 2
    while f"{base}_{index}" in used:
        index += 1
    resolved = f"{base}_{index}"
    used.add(resolved)
    return resolved


def _normalize_options(raw_options: Any) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    used_ids: set[str] = set()
    for index, raw_option in enumerate(_as_list(raw_options)[:3]):
        default_id = _OPTION_IDS[index]
        if isinstance(raw_option, str):
            label = _clean_text(raw_option)
            option_id = default_id
            description = ""
        elif isinstance(raw_option, Mapping):
            label = _clean_text(
                raw_option.get("label")
                or raw_option.get("title")
                or raw_option.get("value")
                or raw_option.get("id")
            )
            option_id = _clean_text(raw_option.get("id")) or default_id
            description = _clean_text(raw_option.get("description"))
        else:
            label = _clean_text(raw_option)
            option_id = default_id
            description = ""
        if not label:
            continue
        option_id = _unique_id(option_id, used_ids, default_id)
        option: dict[str, str] = {"id": option_id, "label": label}
        if description:
            option["description"] = description
        options.append(option)
    return options


def normalize_user_input_questions(questions: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Normalize legacy and structured user-input questions.

    The returned tuple is ``(input_questions, legacy_question_texts)``.  The
    legacy texts keep older checkpoint consumers working, while
    ``input_questions`` drives the newer choice/freeform UI.
    """
    normalized: list[dict[str, Any]] = []
    legacy_texts: list[str] = []
    used_question_ids: set[str] = set()
    for index, raw_question in enumerate(_as_list(questions)):
        fallback_id = f"question_{index + 1}"
        if isinstance(raw_question, str):
            question_text = _clean_text(raw_question)
            if not question_text:
                continue
            question_id = _unique_id("", used_question_ids, fallback_id)
            question = {
                "id": question_id,
                "header": "",
                "question": question_text,
                "options": [],
                "allow_freeform": True,
                "required": True,
            }
        elif isinstance(raw_question, Mapping):
            question_text = _clean_text(
                raw_question.get("question")
                or raw_question.get("prompt")
                or raw_question.get("body")
                or raw_question.get("text")
            )
            header = _clean_text(raw_question.get("header") or raw_question.get("title"))
            if not question_text and header:
                question_text = header
            if not question_text:
                continue
            question_id = _unique_id(_clean_text(raw_question.get("id")), used_question_ids, fallback_id)
            question = {
                "id": question_id,
                "header": header,
                "question": question_text,
                "options": _normalize_options(raw_question.get("options") or raw_question.get("choices") or []),
                "allow_freeform": _bool_or_default(raw_question.get("allow_freeform"), True),
                "required": _bool_or_default(raw_question.get("required"), True),
            }
        else:
            question_text = _clean_text(raw_question)
            if not question_text:
                continue
            question_id = _unique_id("", used_question_ids, fallback_id)
            question = {
                "id": question_id,
                "header": "",
                "question": question_text,
                "options": [],
                "allow_freeform": True,
                "required": True,
            }
        normalized.append(question)
        legacy_texts.append(str(question.get("question", "")).strip())
    return normalized, legacy_texts


def normalize_user_input_request(
    *,
    reason: str,
    questions: Any = None,
    required_fields: Any = None,
    context_note: str = "",
) -> dict[str, Any]:
    input_questions, legacy_questions = normalize_user_input_questions(questions)
    normalized_required_fields = [
        field
        for field in (_clean_text(item) for item in _as_list(required_fields))
        if field
    ]
    return {
        "requires_user_input": True,
        "reason": _clean_text(reason),
        "questions": legacy_questions,
        "input_questions": input_questions,
        "required_fields": normalized_required_fields,
        "context_note": _clean_text(context_note),
        "resume_hint": "Choose an option or provide the missing details, and OpenOPC will continue the task.",
    }


async def request_user_input(
    reason: str,
    questions: list[Any] | None = None,
    required_fields: list[str] | None = None,
    context_note: str = "",
) -> dict[str, Any]:
    """Return a structured user-input request that pauses execution."""
    return normalize_user_input_request(
        reason=reason,
        questions=questions,
        required_fields=required_fields,
        context_note=context_note,
    )


def create_user_input_tool() -> ToolDefinition:
    return ToolDefinition(
        name="request_user_input",
        description=(
            "Pause execution and request missing information from the user. "
            "Use this only when the latest user reply still leaves a specific blocking gap. "
            "If you follow up, ask only for that gap and do not repeat the same broad question."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Brief explanation of the specific missing information that blocks execution",
                },
                "questions": {
                    "type": "array",
                    "items": {
                        "oneOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "header": {"type": "string"},
                                    "question": {"type": "string"},
                                    "options": {
                                        "type": "array",
                                        "maxItems": 3,
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "id": {"type": "string"},
                                                "label": {"type": "string"},
                                                "description": {"type": "string"},
                                            },
                                            "required": ["label"],
                                        },
                                    },
                                    "allow_freeform": {"type": "boolean", "default": True},
                                    "required": {"type": "boolean", "default": True},
                                },
                                "required": ["question"],
                            },
                        ],
                    },
                    "description": (
                        "Specific questions the user should answer. Each structured question can have up to "
                        "three selectable options; freeform Other is allowed by default."
                    ),
                    "default": [],
                },
                "required_fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Required field names still missing after considering the latest user reply",
                    "default": [],
                },
                "context_note": {
                    "type": "string",
                    "description": "Optional note stating what is already understood and what remains unresolved",
                    "default": "",
                },
            },
            "required": ["reason"],
        },
        func=request_user_input,
        company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL,
        category="interaction",
        requires_confirmation=False,
    )
