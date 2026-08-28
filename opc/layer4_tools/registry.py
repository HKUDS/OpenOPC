"""Tool registry — central registry for all tools available to agents."""

from __future__ import annotations

import inspect
import traceback
from typing import Any, Callable, Coroutine

from loguru import logger

from opc.layer4_tools.output_budget import budget_tool_output
from opc.core.native_permissions import NATIVE_PERMISSION_EFFECTS

# Maximum serialized tool output size (characters). Outputs exceeding this
# limit are previewed before being returned to the agent loop; recoverable
# tools persist full output to disk.
_OUTPUT_LIMIT = 20_000


ToolFunc = Callable[..., Coroutine[Any, Any, Any]]

COMPANY_EFFECT_UNKNOWN = "unknown"
COMPANY_EFFECT_NO_LOCAL_FS = "no_local_fs"
COMPANY_EFFECT_STRUCTURED_PATHS = "structured_file_paths"
COMPANY_EFFECT_OPAQUE_EXACT = "opaque_exact"
COMPANY_EFFECT_RUNTIME_INTERNAL = "runtime_internal"
COMPANY_EFFECT_FORBIDDEN = "forbidden"
_COMPANY_EFFECT_KINDS = frozenset(
    {
        COMPANY_EFFECT_UNKNOWN,
        COMPANY_EFFECT_NO_LOCAL_FS,
        COMPANY_EFFECT_STRUCTURED_PATHS,
        COMPANY_EFFECT_OPAQUE_EXACT,
        COMPANY_EFFECT_RUNTIME_INTERNAL,
        COMPANY_EFFECT_FORBIDDEN,
    }
)

_PARAM_ALIASES: dict[str, str] = {
    "cmd": "command",
    "dir": "working_directory",
    "cwd": "working_directory",
    "directory": "working_directory",
    "pattern": "query",
    "search_query": "query",
    "search_term": "query",
    "keyword": "query",
    "filepath": "file_path",
    "filename": "file_path",
    "file": "file_path",
    "text": "content",
    "body": "content",
}

# These values are supplied by the runtime execution envelope, never by a
# model-authored ToolCall.  Treating them as ordinary function parameters lets
# an argument such as ``{"task": null}`` replace the durable Task and erase
# its workspace/controller identity before a tool executes.
_TRUSTED_RUNTIME_ARGUMENTS = ("task", "on_progress")


class ToolInvocationValidationError(ValueError):
    """Expected, model-correctable tool input validation failure.

    These failures are part of the tool protocol rather than implementation
    crashes.  Returning a stable correction payload lets the agent repair its
    call without exposing an internal Python traceback in the conversation.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "tool_input_validation",
        correction: str = "Correct the tool arguments and retry.",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "tool_input_validation").strip()
        self.correction = str(correction or "").strip()
        self.details = dict(details or {})


class ToolDefinition:
    """Metadata and callable for a single tool."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        func: ToolFunc,
        category: str = "general",
        requires_confirmation: bool = False,
        concurrency_safe: bool | None = None,
        read_only: bool | None = None,
        runtime_managed: bool = False,
        max_result_chars: int = _OUTPUT_LIMIT,
        persist_large_results: bool = True,
        self_bounded_output: bool = False,
        preview_chars: int | None = None,
        company_effect_kind: str = COMPANY_EFFECT_UNKNOWN,
        permission_effects: list[str] | tuple[str, ...] | set[str] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.func = func
        self.category = category
        self.requires_confirmation = requires_confirmation
        self.concurrency_safe = concurrency_safe
        self.read_only = read_only
        self.runtime_managed = runtime_managed
        self.max_result_chars = max_result_chars
        self.persist_large_results = persist_large_results
        self.self_bounded_output = self_bounded_output
        self.preview_chars = preview_chars
        normalized_effect_kind = str(company_effect_kind or "").strip().lower()
        if normalized_effect_kind not in _COMPANY_EFFECT_KINDS:
            raise ValueError(
                f"Unknown company tool-effect capability: {company_effect_kind}"
            )
        self.company_effect_kind = normalized_effect_kind
        if permission_effects is None:
            self.permission_effects = None
        else:
            normalized_permission_effects = tuple(
                dict.fromkeys(str(item or "").strip().lower() for item in permission_effects)
            )
            invalid = set(normalized_permission_effects) - NATIVE_PERMISSION_EFFECTS
            if invalid:
                raise ValueError(
                    "Unknown native permission effect(s): " + ", ".join(sorted(invalid))
                )
            self.permission_effects = normalized_permission_effects

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class ToolRegistry:
    """Manages all available tools and dispatches execution."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self._approval_callback: Any = None

    def register(self, tool: ToolDefinition) -> None:
        self._tools[tool.name] = tool
        logger.debug(f"Tool registered: {tool.name} [{tool.category}]")

    def unregister(self, name: str) -> None:
        """Remove a tool by name. No-op if not found."""
        if self._tools.pop(name, None):
            logger.debug(f"Tool unregistered: {name}")

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def list_tools(self, category: str | None = None, allowed: list[str] | None = None) -> list[ToolDefinition]:
        tools = list(self._tools.values())
        if category:
            tools = [t for t in tools if t.category == category]
        if allowed:
            tools = [t for t in tools if t.name in allowed]
        return tools

    def get_schemas(self, allowed: list[str] | None = None) -> list[dict[str, Any]]:
        tools = self.list_tools(allowed=allowed)
        return [t.to_schema() for t in tools]

    def set_approval_callback(self, callback: Any) -> None:
        self._approval_callback = callback

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        task: Any = None,
        on_progress: Any = None,
        skip_approval: bool = False,
    ) -> dict[str, Any]:
        tool = self._tools.get(name)
        if not tool:
            return {"error": f"Unknown tool: {name}", "success": False}

        if self._approval_callback and not skip_approval:
            allowed, decision = await self._approval_callback(tool, arguments, task, on_progress)
            if not allowed:
                return {
                    "error": f"Tool execution blocked by autonomy policy: {decision.rationale}",
                    "approval": {
                        "action": decision.action.value,
                        "risk_level": decision.risk_level.value,
                        "confidence": decision.confidence,
                        "policy_source": decision.policy_source,
                        "rationale": decision.rationale,
                        **dict(decision.metadata or {}),
                    },
                    "success": False,
                }

        return await self.invoke(name, arguments, task=task, on_progress=on_progress)

    async def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        task: Any = None,
        on_progress: Any = None,
    ) -> dict[str, Any]:
        tool = self._tools.get(name)
        if not tool:
            return {"error": f"Unknown tool: {name}", "success": False}

        try:
            call_args = self._prepare_call_args(tool, arguments, task=task, on_progress=on_progress)
            result = await tool.func(**call_args)
            output = {"result": result, "success": True}
        except ToolInvocationValidationError as e:
            logger.warning(
                "Tool {} rejected model-authored input ({}): {}",
                name,
                e.code,
                e,
            )
            output = {
                "error": str(e),
                "error_type": "validation_error",
                "error_code": e.code,
                "retryable": True,
                "correction": e.correction,
                "details": dict(e.details),
                "success": False,
            }
        except Exception as e:
            # Loguru has no stdlib-style ``exc_info`` kwarg: extra kwargs are
            # format() arguments, which forces str.format() on the message — an
            # error message containing ``{...}`` (e.g. a JSON error body) then
            # raises KeyError FROM the logging call, escaping this handler and
            # killing the caller instead of returning the error output below.
            # Positional formatting keeps brace-containing values inert, and
            # opt(exception=True) is the loguru way to log the traceback.
            logger.opt(exception=True).error(
                "Tool {} failed ({}): {}", name, type(e).__name__, e
            )
            output = {
                "error": str(e),
                "traceback": traceback.format_exc(),
                "success": False,
            }

        return self._truncate_output(output, tool=tool, task=task)

    def _prepare_call_args(
        self,
        tool: ToolDefinition,
        arguments: dict[str, Any],
        *,
        task: Any = None,
        on_progress: Any = None,
    ) -> dict[str, Any]:
        call_args = dict(arguments)
        signature = inspect.signature(tool.func)
        for alias, canonical in _PARAM_ALIASES.items():
            if alias in call_args and alias not in signature.parameters and canonical in signature.parameters:
                call_args[canonical] = call_args.pop(alias)

        trusted_values = {
            "task": task,
            "on_progress": on_progress,
        }
        for name in _TRUSTED_RUNTIME_ARGUMENTS:
            supplied_by_model = name in call_args
            accepts_runtime_value = name in signature.parameters
            if supplied_by_model and trusted_values[name] is None:
                raise ValueError(
                    f"Tool `{tool.name}` received reserved runtime argument "
                    f"`{name}` without a trusted runtime value. Remove `{name}` "
                    "from the tool arguments and retry."
                )
            if supplied_by_model and not accepts_runtime_value:
                raise ValueError(
                    f"Tool `{tool.name}` received reserved runtime argument "
                    f"`{name}`. Models cannot supply runtime context arguments; "
                    f"remove `{name}` and retry."
                )
            if accepts_runtime_value:
                # Always overwrite model input, including null/fake values.
                # The outer executor owns this capability-bearing context.
                call_args[name] = trusted_values[name]
        # Reject unknown arguments with a helpful error instead of silently
        # dropping them. The error is caught by `invoke()` and packaged as
        # `{"success": False, "error": ...}`, which the agent's tool-call
        # loop feeds back into the model so it can retry with the right
        # parameter names. Silent dropping would hide data loss when a
        # tool signature is changed without updating agent prompts.
        has_var_keyword = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in signature.parameters.values()
        )
        if not has_var_keyword:
            valid_params = [
                name for name in signature.parameters
                if name not in {"task", "on_progress"}
            ]
            extra = sorted(set(call_args) - set(signature.parameters))
            if extra:
                raise ValueError(
                    f"Tool `{tool.name}` received unknown argument(s): "
                    f"{', '.join(repr(key) for key in extra)}. "
                    f"Valid arguments: {', '.join(repr(p) for p in valid_params)}. "
                    "Please retry with a supported argument name."
                )
        return call_args

    @staticmethod
    def _truncate_output(
        output: dict[str, Any],
        *,
        tool: ToolDefinition,
        task: Any = None,
    ) -> dict[str, Any]:
        """Apply a recoverable output budget when serialized output is large."""
        return budget_tool_output(
            output,
            tool_name=tool.name,
            task=task,
            max_chars=int(tool.max_result_chars or _OUTPUT_LIMIT),
            preview_chars=tool.preview_chars,
            persist_large_results=bool(tool.persist_large_results),
            self_bounded_output=bool(tool.self_bounded_output),
        )
