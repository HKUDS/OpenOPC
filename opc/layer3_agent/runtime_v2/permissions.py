"""Runtime-side permission adapter for Native Runtime V2.

This module intentionally contains NO permission policy. All decisions come
from the single ApprovalEngine (``opc/layer2_organization/approval.py``):
its synchronous ``predict()`` is the fast path consulted before every tool
call, and its async ``authorize_tool_call()`` (reached via the runtime's
approval callback) is the escalation path. The adapter only bridges the
engine into the executor and maps tool results back into permission events.
"""

from __future__ import annotations

import inspect
from typing import Any

from loguru import logger

from opc.core.models import PermissionResolution, PermissionScope, RiskLevel, RuntimePermissionDecision


_CANDIDATE_KEYS = (
    "path",
    "file_path",
    "directory",
    "working_directory",
    "target_output_dir",
    "workspace_path",
    "command",
    "cmd",
    "url",
)


def _candidate(arguments: dict[str, Any] | None) -> str:
    for key in _CANDIDATE_KEYS:
        value = str((arguments or {}).get(key, "") or "").strip()
        if value:
            return value
    return "*"


def _risk(value: Any, default: RiskLevel) -> RiskLevel:
    try:
        return RiskLevel(str(value or default.value))
    except Exception:
        return default


class RuntimePermissionAdapter:
    """Thin, policy-free bridge between the tool executor and ApprovalEngine.

    ``policy`` is the ApprovalEngine (duck-typed: ``predict`` and
    ``record_denial``). Without a policy (bare runtimes, unit tests) the
    adapter falls back to a static conservative default: unknown tools and
    confirmation-required tools ask, everything else runs — matching the
    pre-unification behavior of a runtime without an approval callback.
    """

    def __init__(self, policy: Any = None, *, guardian: Any = None) -> None:
        self.policy = policy
        self.guardian = guardian if guardian is not None else getattr(
            getattr(getattr(policy, "config", None), "permissions_v2", None), "guardian", None
        )

    def predicted_decision(
        self,
        tool: Any,
        arguments: dict[str, Any] | None = None,
        *,
        task: Any = None,
    ) -> RuntimePermissionDecision:
        if self.policy is not None:
            try:
                return self.policy.predict(tool, arguments, task=task)
            except Exception:
                logger.opt(exception=True).warning(
                    "Permission predictor failed; falling back to ask-first default"
                )
                return RuntimePermissionDecision(
                    resolution=PermissionResolution.ASK,
                    scope=PermissionScope.ONCE,
                    risk_level=RiskLevel.MEDIUM,
                    rationale="Permission predictor failed; requiring explicit review.",
                    source="runtime_prediction",
                )
        if tool is None:
            return RuntimePermissionDecision(
                resolution=PermissionResolution.ASK,
                scope=PermissionScope.ONCE,
                risk_level=RiskLevel.HIGH,
                rationale="Unknown tool requires manual review.",
                source="runtime_prediction",
            )
        if bool(getattr(tool, "requires_confirmation", False)):
            return RuntimePermissionDecision(
                resolution=PermissionResolution.ASK,
                scope=PermissionScope.ONCE,
                risk_level=RiskLevel.MEDIUM,
                rationale="Tool is marked as requiring confirmation.",
                source="runtime_prediction",
            )
        return RuntimePermissionDecision(
            resolution=PermissionResolution.ALLOW,
            scope=PermissionScope.ONCE,
            risk_level=RiskLevel.LOW,
            rationale="No permission policy configured.",
            source="runtime_prediction",
        )

    def record_denial(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        task: Any = None,
        denial_id: str = "",
    ) -> None:
        if self.policy is not None and hasattr(self.policy, "record_denial"):
            try:
                record = self.policy.record_denial
                parameters = inspect.signature(record).parameters
                accepts_kwargs = any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    for parameter in parameters.values()
                )
                kwargs: dict[str, Any] = {}
                if accepts_kwargs or "task" in parameters:
                    kwargs["task"] = task
                if accepts_kwargs or "denial_id" in parameters:
                    kwargs["denial_id"] = denial_id
                record(tool_name, arguments, **kwargs)
            except Exception:
                logger.opt(exception=True).debug("Failed to record permission denial")

    def sandbox_for_task(self, task: Any = None) -> dict[str, Any]:
        native_policy = getattr(self.policy, "native_policy", None)
        if native_policy is None:
            return {}
        try:
            return dict(native_policy.sandbox_for_task(task) or {})
        except Exception:
            logger.opt(exception=True).warning(
                "Native sandbox policy resolution failed; keeping the current sandbox"
            )
            return {}

    def build_blocked_result(
        self,
        decision: RuntimePermissionDecision,
        *,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        action = "reject" if decision.resolution == PermissionResolution.DENY else "require_input"
        candidate = _candidate(arguments)
        return {
            "error": decision.rationale or f"Runtime permission blocked `{tool_name}`.",
            "success": False,
            "approval": {
                "action": action,
                "risk_level": decision.risk_level.value,
                "policy_source": decision.source,
                "scope": decision.scope.value,
                "candidate": candidate,
                "explanation": decision.rationale,
                "metadata": dict(decision.metadata or {}),
            },
            "permission_context": {
                "tool_name": tool_name,
                "candidate": candidate,
                "resolution": decision.resolution.value,
                "risk_level": decision.risk_level.value,
                "source": decision.source,
            },
        }

    def decision_from_result(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        result: dict[str, Any],
    ) -> RuntimePermissionDecision:
        """Map a tool result back to the permission decision it reflects.

        Pure event classification — grants and denials are recorded at their
        decision points, never while classifying an already-produced result.
        """
        approval = dict(result.get("approval", {}) or {})
        action = str(approval.get("action", "") or "").strip().lower()
        if action in {"require_input", "escalate"}:
            return RuntimePermissionDecision(
                resolution=PermissionResolution.ASK,
                scope=PermissionScope.ONCE,
                risk_level=_risk(approval.get("risk_level"), RiskLevel.MEDIUM),
                rationale=str(result.get("error", "") or "Awaiting explicit permission."),
                source="approval_engine",
                metadata=approval,
            )
        if action == "reject":
            return RuntimePermissionDecision(
                resolution=PermissionResolution.DENY,
                scope=PermissionScope.ONCE,
                risk_level=_risk(approval.get("risk_level"), RiskLevel.HIGH),
                rationale=str(result.get("error", "") or "Permission denied."),
                source="approval_engine",
                metadata=approval,
            )
        human_reply = str(approval.get("human_reply") or result.get("human_reply") or "").strip().lower()
        scope = {
            "approve_session": PermissionScope.SESSION,
            "always_project": PermissionScope.PROJECT,
            "always_global": PermissionScope.GLOBAL,
        }.get(human_reply, PermissionScope.ONCE)
        return RuntimePermissionDecision(
            resolution=PermissionResolution.ALLOW,
            scope=scope,
            risk_level=RiskLevel.LOW,
            rationale="Tool execution allowed.",
            source="approval_engine",
            metadata=approval,
        )
