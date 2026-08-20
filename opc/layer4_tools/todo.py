"""TODO tool — structured task tracking for agents.

Schema definitions only. Execution is intercepted by NativeRuntimeV2, which
persists the task ledger in runtime session state for resume and verification.
"""

from __future__ import annotations

from opc.layer4_tools.registry import COMPANY_EFFECT_RUNTIME_INTERNAL, ToolDefinition


async def _todo_noop(**kwargs):  # type: ignore[no-untyped-def]
    """Placeholder — never called; NativeRuntimeV2 intercepts todo_* calls."""
    return {"error": "todo tool must be intercepted by NativeRuntimeV2", "success": False}


def create_todo_tools() -> list[ToolDefinition]:
    """Return the two TODO tool definitions (todo_write, todo_read)."""
    todo_write = ToolDefinition(
        name="todo_write",
        description=(
            "Create or update a structured TODO list for tracking multi-step tasks. "
            "Pass either a JSON string or a real array of items. "
            "Preferred OpenOPC task-ledger fields are 'content', 'active_form', and "
            "'status' ('pending' | 'in_progress' | 'completed'). "
            "Fields 'id'/'title'/'status' are also accepted. "
            "Keep only ONE item 'in_progress' at a time and add new items dynamically. "
            "The runtime persists this ledger across pause/resume and verification."
        ),
        parameters={
            "type": "object",
            "properties": {
                "todos": {
                    "oneOf": [
                        {
                            "type": "string",
                            "description": "JSON array of todo items.",
                        },
                        {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "title": {"type": "string"},
                                    "content": {"type": "string"},
                                    "active_form": {"type": "string"},
                                    "status": {
                                        "type": "string",
                                        "enum": ["pending", "in_progress", "completed", "done"],
                                    },
                                },
                            },
                        },
                    ],
                    "description": "TODO items, e.g. "
                    '[{"content":"Inspect runtime_v2","active_form":"Inspecting runtime_v2","status":"in_progress"}]',
                },
            },
            "required": ["todos"],
        },
        func=_todo_noop,
        category="planning",
        concurrency_safe=False,
        read_only=False,
        runtime_managed=True,
        company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL,
    )

    todo_read = ToolDefinition(
        name="todo_read",
        description="Read the current runtime task ledger snapshot. Returns all items with their statuses.",
        parameters={
            "type": "object",
            "properties": {},
        },
        func=_todo_noop,
        category="planning",
        concurrency_safe=True,
        read_only=True,
        runtime_managed=True,
        company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL,
    )

    return [todo_write, todo_read]
