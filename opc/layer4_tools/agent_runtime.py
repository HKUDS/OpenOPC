"""Runtime-managed native subagent tools.

These tools are intercepted by Native Runtime V2 rather than executed by the
global registry. Their schemas must still be exposed to the model.
"""

from __future__ import annotations

from typing import Any

from opc.layer4_tools.registry import COMPANY_EFFECT_RUNTIME_INTERNAL, ToolDefinition


async def _runtime_noop(**_kwargs: Any) -> dict[str, Any]:
    return {"error": "runtime managed tool must be intercepted by Native Runtime V2", "success": False}


def create_agent_runtime_tools() -> list[ToolDefinition]:
    shared_profile_desc = "Specialist profile to use: general | explore | plan | implement | verify."
    return [
        ToolDefinition(
            name="agent_spawn",
            description=(
                "Spawn a native subagent. Use explore/plan for read-only investigation, "
                "implement for isolated coding, and verify for adversarial validation. "
                "Supports OpenOPC native subagent fields such as description, name, model, "
                "background, mode, and worktree isolation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Short description of the subagent task",
                    },
                    "profile": {"type": "string", "description": shared_profile_desc},
                    "prompt": {"type": "string", "description": "Task for the subagent"},
                    "name": {
                        "type": "string",
                        "description": "Optional stable nickname for addressing the subagent later",
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional model override for this subagent",
                    },
                    "mode": {
                        "type": "string",
                        "description": "Permission mode hint: default | plan | accept_edits | bypass_permissions | dont_ask",
                        "default": "default",
                    },
                    "background": {"type": "boolean", "description": "Run in background", "default": False},
                    "resident": {
                        "type": "boolean",
                        "description": "Keep a background worker resident so it can resume with follow-up input after going idle.",
                        "default": False,
                    },
                    "isolation": {
                        "type": "string",
                        "description": "Isolation mode: shared | worktree",
                        "default": "",
                    },
                },
                "required": ["profile", "prompt"],
            },
            func=_runtime_noop,
            category="orchestration",
            concurrency_safe=False,
            read_only=False,
            runtime_managed=True,
            company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL,
            permission_effects=["runtime_internal"],
        ),
        ToolDefinition(
            name="agent_wait",
            description="Wait for a previously spawned subagent to complete and return its latest result.",
            parameters={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Subagent id returned by agent_spawn"},
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Maximum time to wait before returning still-running status",
                        "default": 300,
                    },
                },
                "required": ["agent_id"],
            },
            func=_runtime_noop,
            category="orchestration",
            concurrency_safe=False,
            read_only=True,
            runtime_managed=True,
            company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL,
            permission_effects=["runtime_internal"],
        ),
        ToolDefinition(
            name="agent_send",
            description="Send a follow-up message to a running native subagent.",
            parameters={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Subagent id returned by agent_spawn"},
                    "message": {"type": "string", "description": "Follow-up instruction for the subagent"},
                },
                "required": ["agent_id", "message"],
            },
            func=_runtime_noop,
            category="orchestration",
            concurrency_safe=False,
            read_only=False,
            runtime_managed=True,
            company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL,
            permission_effects=["runtime_internal"],
        ),
        ToolDefinition(
            name="agent_list",
            description="List currently known native subagents and their status.",
            parameters={
                "type": "object",
                "properties": {},
            },
            func=_runtime_noop,
            category="orchestration",
            concurrency_safe=True,
            read_only=True,
            runtime_managed=True,
            company_effect_kind=COMPANY_EFFECT_RUNTIME_INTERNAL,
            permission_effects=["runtime_internal"],
        ),
    ]
