"""Autonomy approval engine for native tools and external agents."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any, Callable, Coroutine

from loguru import logger

from opc.core.company_tools import COMPANY_APPROVAL_EXEMPT_TOOL_NAMES
from opc.core.config import AutonomyConfig, get_opc_home
from opc.core.models import (
    ApprovalAction,
    ApprovalDecision,
    PermissionResolution,
    PermissionScope,
    RiskLevel,
    RuntimePermissionDecision,
    Task,
)
from opc.database.store import OPCStore
from opc.layer2_organization.data_acquisition_policy import (
    ACQUISITION_SHELL_PREFIXES,
    is_projection_scoped_acquisition_shell_command,
)
from opc.layer2_organization.escalation import EscalationEngine
from opc.layer2_organization import shell_safety
from opc.layer2_organization.work_item_identity import (
    work_item_identity_payload_for_task,
    work_item_projection_id_from_metadata,
)
from opc.layer5_memory.approval_allowlist import ApprovalAllowlistManager
from opc.layer5_memory.memory_manager import MemoryManager
from opc.layer5_memory.preference import PreferenceManager
from opc.layer5_memory.secretary_policy import SecretaryPolicyManager
from opc.llm.provider import LLMProvider
from opc.llm.retry import LLMRetryError, call_llm_json_with_retry


_LOW_RISK_SHELL_PREFIXES = set(ACQUISITION_SHELL_PREFIXES)
_SHELL_LIKE_TOOL_NAMES = {"shell_exec", "python_exec", "git_commit"}
_PREDICT_PATH_KEYS = (
    "path",
    "file_path",
    "directory",
    "working_directory",
    "target_output_dir",
    "workspace_path",
)
_PREDICT_COMMAND_KEYS = ("command", "cmd")
_EXTERNAL_AGENT_DIRECT_HUMAN_MARKERS = (
    "--dangerously-bypass-approvals-and-sandbox",
    "--dangerously-skip-permissions",
    "--force",
    "bypasspermissions",
    "bypass-permissions",
    "permission-mode bypass",
)
_SHELL_COMMAND_PREFIX_ARITY = {
    "aws": 3,
    "az": 3,
    "bun": 2,
    "bun run": 3,
    "bun x": 3,
    "cargo": 2,
    "cargo add": 3,
    "cargo run": 3,
    "deno": 2,
    "deno task": 3,
    "docker": 2,
    "docker builder": 3,
    "docker compose": 3,
    "docker container": 3,
    "docker image": 3,
    "docker network": 3,
    "docker volume": 3,
    "gh": 3,
    "git": 2,
    "git config": 3,
    "git remote": 3,
    "git stash": 3,
    "go": 2,
    "kubectl": 2,
    "kubectl kustomize": 3,
    "kubectl rollout": 3,
    "make": 2,
    "npm": 2,
    "npm exec": 3,
    "npm init": 3,
    "npm run": 3,
    "npm view": 3,
    "pip": 2,
    "pnpm": 2,
    "pnpm dlx": 3,
    "pnpm exec": 3,
    "pnpm run": 3,
    "poetry": 2,
    "python": 2,
    "python3": 2,
    "terraform": 2,
    "terraform workspace": 3,
    "yarn": 2,
    "yarn dlx": 3,
    "yarn run": 3,
}


_ESCALATION_DECISION_TOKENS: frozenset[str] = frozenset({
    "approve_once",
    "approve_session",
    "always_project",
    "always_global",
    "deny",
})

_ESCALATION_APPROVE_SYNONYMS: frozenset[str] = frozenset({
    "approve", "approved", "yes", "y", "ok", "allow", "同意", "批准", "允许",
})

_ESCALATION_DENY_SYNONYMS: frozenset[str] = frozenset({
    "no", "n", "denied", "reject", "rejected", "拒绝", "不允许",
})


def normalize_escalation_reply(reply: str) -> str:
    """Map a human reply to an approval decision token; ``""`` when the text
    is not a decision (it is then ordinary task input, never a silent deny)."""
    text = str(reply or "").strip().lower()
    if text in _ESCALATION_DECISION_TOKENS:
        return text
    if text in _ESCALATION_APPROVE_SYNONYMS:
        return "approve_once"
    if text in _ESCALATION_DENY_SYNONYMS:
        return "deny"
    return ""


class ApprovalEngine:
    """Bounded-autonomy policy engine."""

    def __init__(
        self,
        llm: LLMProvider,
        store: OPCStore,
        preferences: PreferenceManager,
        memory: MemoryManager,
        escalation: EscalationEngine | None,
        config: AutonomyConfig,
        secretary_policies: SecretaryPolicyManager | None = None,
    ) -> None:
        self.llm = llm
        self.store = store
        self.preferences = preferences
        self.memory = memory
        self.escalation = escalation
        self.config = config
        self.secretary_policies = secretary_policies
        opc_home = getattr(preferences, "opc_home", None)
        self.allowlist = ApprovalAllowlistManager(opc_home) if opc_home else None
        self._session_allowlist: dict[str, dict[str, dict[str, list[str]]]] = {}
        self._denial_counts: dict[str, int] = {}
        if self.allowlist:
            self.allowlist.ensure_file()

    async def authorize_tool_call(
        self,
        task: Task | None,
        tool_name: str,
        arguments: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> tuple[bool, ApprovalDecision]:
        action_name = tool_name
        payload = {
            "tool": tool_name,
            "arguments": arguments,
            "metadata": dict(metadata or {}),
            **work_item_identity_payload_for_task(task),
            "role_id": str((task.assigned_to if task else "") or (task.metadata if task else {}).get("work_item_role_id", "") or ""),
            "target_output_dir": str((task.metadata if task else {}).get("target_output_dir", "") or ""),
        }
        return await self._authorize(
            task=task,
            action_kind="tool",
            action_name=action_name,
            summary=json.dumps(payload, ensure_ascii=False, default=str)[:4000],
            target_agent="native",
            metadata=payload,
            on_progress=on_progress,
            allow_auto=self.config.allow_native_tool_auto_approval,
        )

    async def authorize_tool_permission_decision(
        self,
        task: Task | None,
        tool_name: str,
        arguments: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> RuntimePermissionDecision:
        _, decision = await self.authorize_tool_call(
            task=task,
            tool_name=tool_name,
            arguments=arguments,
            metadata=metadata,
            on_progress=on_progress,
        )
        return self.to_permission_decision(decision)

    async def authorize_external_action(
        self,
        task: Task,
        agent_name: str,
        metadata: dict[str, Any],
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> tuple[bool, ApprovalDecision]:
        command_preview = self._command_preview(str(metadata.get("command", "")), drop_last_token=True)
        summary = (
            f"agent={agent_name}; command={command_preview}; "
            f"model={metadata.get('model', '(cli default)')}; "
            f"session_mode={metadata.get('session_mode', 'auto')}; "
            f"run_mode={metadata.get('run_mode', 'batch')}; "
            f"approval_mode={metadata.get('approval_mode', 'auto')}"
        )
        explicit_user_selected_agent = bool(metadata.get("explicit_user_selected_agent"))
        external_session_continuation = bool(metadata.get("external_session_continuation"))
        if explicit_user_selected_agent:
            rationale = "The user explicitly selected this external agent for the current task session."
            confidence = 0.98
            policy_source = "explicit_user_agent_selection"
            risk = RiskLevel.LOW
            decision = ApprovalDecision(
                action=ApprovalAction.AUTO_APPROVE,
                risk_level=risk,
                rationale=rationale,
                confidence=confidence,
                policy_source=policy_source,
                metadata=metadata,
            )
            await self._record(task, "external_agent", agent_name, agent_name, decision)
            if on_progress:
                await on_progress(
                    f"[Autonomy] external_agent:{agent_name} -> {decision.action.value} "
                    f"(risk={decision.risk_level.value}, confidence={decision.confidence:.2f})"
                )
            return True, decision
        if external_session_continuation:
            rationale = "Continuing an already selected external-agent session within the same task session."
            confidence = 0.99
            policy_source = "external_session_continuation"
            risk = RiskLevel.LOW
            decision = ApprovalDecision(
                action=ApprovalAction.AUTO_APPROVE,
                risk_level=risk,
                rationale=rationale,
                confidence=confidence,
                policy_source=policy_source,
                metadata=metadata,
            )
            await self._record(task, "external_agent", agent_name, agent_name, decision)
            if on_progress:
                await on_progress(
                    f"[Autonomy] external_agent:{agent_name} -> {decision.action.value} "
                    f"(risk={decision.risk_level.value}, confidence={decision.confidence:.2f})"
                )
            return True, decision
        return await self._authorize(
            task=task,
            action_kind="external_agent",
            action_name=agent_name,
            summary=summary,
            target_agent=agent_name,
            metadata=metadata,
            on_progress=on_progress,
            allow_auto=self.config.allow_external_agent_auto_approval,
        )

    async def authorize_external_permission_decision(
        self,
        task: Task,
        agent_name: str,
        metadata: dict[str, Any],
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
    ) -> RuntimePermissionDecision:
        _, decision = await self.authorize_external_action(
            task=task,
            agent_name=agent_name,
            metadata=metadata,
            on_progress=on_progress,
        )
        return self.to_permission_decision(decision)

    async def authorize_work_item_action(
        self,
        task: Task,
        work_item_title: str,
        metadata: dict[str, Any],
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        force_human: bool = False,
    ) -> tuple[bool, ApprovalDecision]:
        summary = (
            f"work_item_projection_title={work_item_title}; role={metadata.get('role_id', '')}; "
            f"gate_type={metadata.get('gate_type', '')}"
        )
        return await self._authorize(
            task=task,
            action_kind="work_item_projection_title",
            action_name=work_item_title,
            summary=summary,
            target_agent=metadata.get("role_id", "company_runtime"),
            metadata=metadata,
            on_progress=on_progress,
            allow_auto=not force_human,
        )

    async def authorize_work_item_permission_decision(
        self,
        task: Task,
        work_item_title: str,
        metadata: dict[str, Any],
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        force_human: bool = False,
    ) -> RuntimePermissionDecision:
        _, decision = await self.authorize_work_item_action(
            task=task,
            work_item_title=work_item_title,
            metadata=metadata,
            on_progress=on_progress,
            force_human=force_human,
        )
        return self.to_permission_decision(decision)

    def to_permission_decision(self, decision: ApprovalDecision) -> RuntimePermissionDecision:
        if decision.action == ApprovalAction.AUTO_APPROVE:
            resolution = PermissionResolution.ALLOW
        elif decision.action == ApprovalAction.REJECT:
            resolution = PermissionResolution.DENY
        else:
            resolution = PermissionResolution.ASK
        return RuntimePermissionDecision(
            resolution=resolution,
            scope=self._decision_scope(decision),
            risk_level=decision.risk_level,
            rationale=decision.rationale,
            source=decision.policy_source,
            metadata=dict(decision.metadata or {}),
        )

    def _decision_scope(self, decision: ApprovalDecision) -> PermissionScope:
        reply = str((decision.metadata or {}).get("human_reply") or "").strip().lower()
        if reply == "approve_session":
            return PermissionScope.SESSION
        if reply == "always_project":
            return PermissionScope.PROJECT
        if reply == "always_global":
            return PermissionScope.GLOBAL
        return PermissionScope.ONCE

    # ------------------------------------------------------------------
    # Synchronous permission prediction (runtime fast path)
    #
    # The native runtime consults predict() before every tool call: ALLOW
    # executes immediately, DENY blocks, ASK routes into the full async
    # authorize_tool_call() pipeline (allowlist, heuristics, LLM review,
    # escalation card). predict() reads the same config and the same
    # persisted allowlist as authorize, so there is exactly one policy.
    # ------------------------------------------------------------------

    def predict(
        self,
        tool: Any,
        arguments: dict[str, Any] | None = None,
        *,
        task: Task | None = None,
    ) -> RuntimePermissionDecision:
        p2 = self.config.permissions_v2
        if tool is None:
            return self._predict_decision(
                PermissionResolution.ASK if p2.fail_closed else PermissionResolution.DENY,
                RiskLevel.HIGH,
                "Unknown tool requires manual review.",
                source="runtime_prediction",
            )
        if not self.config.enabled or not p2.enabled:
            return self._predict_decision(
                PermissionResolution.ALLOW, RiskLevel.LOW,
                "Autonomy policy is disabled.", source="config",
            )
        tool_name = str(getattr(tool, "name", "") or "")
        args = dict(arguments or {})

        repeated = self._repeated_denial_decision(tool_name, args)
        if repeated is not None:
            return repeated
        if tool_name in {str(item or "").strip() for item in p2.deny_tools if str(item or "").strip()}:
            return self._predict_decision(
                PermissionResolution.DENY, RiskLevel.HIGH,
                "Tool is explicitly denied by permission rules.", source="permission_rules",
            )
        if tool_name in COMPANY_APPROVAL_EXEMPT_TOOL_NAMES:
            return self._predict_decision(
                PermissionResolution.ALLOW, RiskLevel.LOW,
                "Built-in company collaboration tool is always auto-approved.",
                source="company_tool_policy",
            )
        if self._memory_path_decision("tool", tool_name, {"arguments": args}):
            return self._predict_decision(
                PermissionResolution.ALLOW, RiskLevel.LOW,
                "Direct agent access to canonical OpenOPC memory files.",
                source="memory_path_policy",
            )
        if tool_name in {str(item or "").strip() for item in p2.allow_tools if str(item or "").strip()}:
            return self._predict_decision(
                PermissionResolution.ALLOW, RiskLevel.LOW,
                "Tool is explicitly allowed by permission rules.", source="permission_rules",
            )

        # Persisted human grants win before path/shell heuristics, matching
        # the order of the async authorize pipeline. Unauditable commands
        # cannot ride through: their candidates degrade to the exact string.
        metadata = {"arguments": args}
        session_hit = self._lookup_session_allowlist_policy(
            task=task, action_kind="tool", action_name=tool_name, metadata=metadata,
        )
        if session_hit:
            return self._predict_decision(
                PermissionResolution.ALLOW, RiskLevel.LOW,
                f"Allowed by session approval ({session_hit['scope']}).",
                source="session_approval", scope=PermissionScope.SESSION,
            )
        persisted_hit = self._lookup_allowlist_policy(
            action_kind="tool", action_name=tool_name, metadata=metadata,
            project_id=task.project_id if task else None,
        )
        if persisted_hit:
            scope = PermissionScope.GLOBAL if persisted_hit["scope"] is None else PermissionScope.PROJECT
            return self._predict_decision(
                PermissionResolution.ALLOW, RiskLevel.LOW,
                "Allowed by persisted allowlist grant.",
                source="approval_allowlist", scope=scope,
            )

        path_decision = self._predict_path_decision(tool, args, task)
        if path_decision is not None:
            return path_decision

        if tool_name in _SHELL_LIKE_TOOL_NAMES:
            shell_decision = self._predict_shell_decision(tool_name, args, task)
            if shell_decision is not None:
                return shell_decision

        if bool(getattr(tool, "requires_confirmation", False)):
            return self._predict_decision(
                PermissionResolution.ASK, RiskLevel.MEDIUM,
                "Tool is marked as requiring confirmation.", source="runtime_prediction",
            )
        guardian = p2.guardian
        if guardian.enabled and guardian.auto_allow_read_only and bool(getattr(tool, "read_only", False)):
            return self._predict_decision(
                PermissionResolution.ALLOW, RiskLevel.LOW,
                "Deterministic read-only tool.", source="guardian",
            )
        return self._predict_decision(
            PermissionResolution.ALLOW, RiskLevel.LOW,
            "No permission warning triggered.", source="runtime_prediction",
        )

    def record_denial(self, tool_name: str, arguments: dict[str, Any] | None = None) -> None:
        if not self.config.permissions_v2.denial_memory.enabled:
            return
        key = self._denial_memory_key(tool_name, arguments)
        self._denial_counts[key] = self._denial_counts.get(key, 0) + 1

    def _denial_memory_key(self, tool_name: str, arguments: dict[str, Any] | None) -> str:
        args = dict(arguments or {})
        for key in (*_PREDICT_PATH_KEYS, *_PREDICT_COMMAND_KEYS, "url"):
            value = str(args.get(key, "") or "").strip()
            if value:
                return f"{tool_name}:{value}"
        return f"{tool_name}:*"

    def _repeated_denial_decision(
        self, tool_name: str, arguments: dict[str, Any] | None
    ) -> RuntimePermissionDecision | None:
        memory = self.config.permissions_v2.denial_memory
        if not memory.enabled:
            return None
        repeats = self._denial_counts.get(self._denial_memory_key(tool_name, arguments), 0)
        if repeats < max(1, memory.repeat_threshold):
            return None
        return self._predict_decision(
            PermissionResolution.DENY, RiskLevel.HIGH,
            "Repeated denials indicate this action should stop and ask for a new plan.",
            source="denial_memory",
            metadata={"repeated_denials": repeats},
        )

    def _predict_shell_decision(
        self,
        tool_name: str,
        args: dict[str, Any],
        task: Task | None,
    ) -> RuntimePermissionDecision | None:
        command = ""
        for key in _PREDICT_COMMAND_KEYS:
            value = str(args.get(key, "") or "").strip()
            if value:
                command = value
                break
        if not command:
            return None
        for pattern in self.config.permissions_v2.dangerous_shell_patterns:
            if pattern and re.search(pattern, command, flags=re.IGNORECASE):
                return self._predict_decision(
                    PermissionResolution.ASK, RiskLevel.CRITICAL,
                    f"Command matched dangerous shell pattern `{pattern}`.",
                    source="shell_pattern",
                )
        if is_projection_scoped_acquisition_shell_command(
            command=command,
            task=task,
            working_directory=str(args.get("working_directory", "") or args.get("workdir", "") or "").strip(),
            target_output_dir=str((getattr(task, "metadata", {}) or {}).get("target_output_dir", "") or "").strip() if task else "",
        ):
            return self._predict_decision(
                PermissionResolution.ALLOW, RiskLevel.LOW,
                "Work-item-scoped acquisition command inside the assigned workspace.",
                source="shell_prefix",
            )
        safe_prefixes = [
            item for item in self.config.safe_command_prefixes
            if str(item or "").strip() not in _LOW_RISK_SHELL_PREFIXES
        ]
        safe, reason = shell_safety.is_read_only_shell_command(command, safe_prefixes)
        if safe:
            return self._predict_decision(
                PermissionResolution.ALLOW, RiskLevel.LOW,
                reason, source="shell_read_only",
            )
        return self._predict_decision(
            PermissionResolution.ASK, RiskLevel.MEDIUM,
            f"Shell command requires approval review: {reason}",
            source="shell_guard",
        )

    def _predict_path_decision(
        self,
        tool: Any,
        args: dict[str, Any],
        task: Task | None,
    ) -> RuntimePermissionDecision | None:
        if not args:
            return None
        p2 = self.config.permissions_v2
        candidate = ""
        for key in _PREDICT_PATH_KEYS:
            value = str(args.get(key, "") or "").strip()
            if value:
                candidate = value
                break
        if not candidate:
            return None
        if self._matches_path_rule(candidate, p2.denied_paths):
            return self._predict_decision(
                PermissionResolution.DENY, RiskLevel.HIGH,
                "Target path matches a denied permission rule.", source="permission_rules",
            )
        if self._matches_path_rule(candidate, p2.allowed_paths):
            return self._predict_decision(
                PermissionResolution.ALLOW, RiskLevel.LOW,
                "Target path matches an explicit allow rule.", source="permission_rules",
                scope=PermissionScope.PROJECT,
            )
        if bool(getattr(tool, "read_only", False)):
            return None
        try:
            resolved = Path(candidate).resolve()
        except Exception:
            return None
        for root in self._predict_workspace_roots(task):
            if resolved == root or root in resolved.parents:
                return None
        return self._predict_decision(
            PermissionResolution.ASK if p2.fail_closed else PermissionResolution.DENY,
            RiskLevel.HIGH,
            "Target path is outside the current workspace roots.",
            source="path_guard",
            metadata={"candidate": candidate},
        )

    @staticmethod
    def _matches_path_rule(candidate: str, rules: list[str]) -> bool:
        raw = str(candidate or "").strip()
        if not raw or raw == "*":
            return False
        for rule in rules:
            token = str(rule or "").strip()
            if not token:
                continue
            if token == "*" or raw == token:
                return True
            try:
                rule_path = Path(token).resolve()
                candidate_path = Path(raw).resolve()
            except Exception:
                if raw.startswith(token.rstrip("\\/")):
                    return True
                continue
            if candidate_path == rule_path or rule_path in candidate_path.parents:
                return True
        return False

    @staticmethod
    def _predict_workspace_roots(task: Task | None) -> list[Path]:
        roots: list[Path] = []
        metadata = getattr(task, "metadata", {}) or {} if task else {}
        for raw in (
            str(metadata.get("workspace_root", "") or "").strip(),
            str(metadata.get("comms_workspace_root", "") or "").strip(),
            str(metadata.get("output_root", "") or "").strip(),
            str(metadata.get("target_output_dir", "") or "").strip(),
        ):
            if not raw:
                continue
            try:
                path = Path(raw).resolve()
            except Exception:
                continue
            if path not in roots:
                roots.append(path)
        try:
            memory_root = (Path(get_opc_home()) / "memory").resolve()
            if memory_root not in roots:
                roots.append(memory_root)
        except Exception:
            pass
        if not roots:
            try:
                roots.append(Path.cwd().resolve())
            except Exception:
                pass
        return roots

    @staticmethod
    def _predict_decision(
        resolution: PermissionResolution,
        risk: RiskLevel,
        rationale: str,
        *,
        source: str,
        scope: PermissionScope = PermissionScope.ONCE,
        metadata: dict[str, Any] | None = None,
    ) -> RuntimePermissionDecision:
        return RuntimePermissionDecision(
            resolution=resolution,
            scope=scope,
            risk_level=risk,
            rationale=rationale,
            source=source,
            metadata=dict(metadata or {}),
        )

    async def _authorize(
        self,
        task: Task | None,
        action_kind: str,
        action_name: str,
        summary: str,
        target_agent: str,
        metadata: dict[str, Any],
        on_progress: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        allow_auto: bool = True,
    ) -> tuple[bool, ApprovalDecision]:
        if not self.config.enabled:
            decision = ApprovalDecision(
                action=ApprovalAction.AUTO_APPROVE,
                risk_level=RiskLevel.LOW,
                rationale="Autonomy policy is disabled.",
                confidence=1.0,
                policy_source="config",
                metadata=metadata,
            )
            await self._record(task, action_kind, action_name, target_agent, decision)
            return True, decision

        memory_decision = self._memory_path_decision(action_kind, action_name, metadata)
        if memory_decision:
            await self._record(task, action_kind, action_name, target_agent, memory_decision)
            if on_progress:
                await on_progress(
                    f"[Autonomy] {action_kind}:{action_name} -> {memory_decision.action.value} "
                    f"(risk={memory_decision.risk_level.value}, confidence={memory_decision.confidence:.2f})"
                )
            return True, memory_decision

        if self.secretary_policies:
            policy_hit = self.secretary_policies.evaluate_tool_policy(
                project_id=task.project_id if task else None,
                tool_name=action_name,
                arguments=metadata.get("arguments", {}) if action_kind == "tool" else metadata,
                safe_command_prefixes=self.config.safe_command_prefixes,
            )
            if policy_hit and policy_hit.get("effect") == "escalate":
                decision = ApprovalDecision(
                    action=ApprovalAction.ESCALATE,
                    risk_level=RiskLevel.HIGH,
                    rationale=str(policy_hit.get("reason", "")).strip() or "Blocked by secretary policy.",
                    confidence=0.95,
                    policy_source="secretary_policy",
                    metadata={**metadata, "secretary_rule_id": policy_hit.get("rule_id", "")},
                )
                if self.escalation and task:
                    approved, decision = await self._ask_user(task, action_kind, action_name, decision, metadata)
                    await self._record(task, action_kind, action_name, target_agent, decision)
                    return approved, decision
                await self._record(task, action_kind, action_name, target_agent, decision)
                return False, decision
            if policy_hit and policy_hit.get("effect") == "auto_allow" and action_kind == "tool":
                decision = ApprovalDecision(
                    action=ApprovalAction.AUTO_APPROVE,
                    risk_level=RiskLevel.LOW,
                    rationale=str(policy_hit.get("reason", "")).strip() or "Allowed by secretary policy.",
                    confidence=0.95,
                    policy_source="secretary_policy",
                    metadata={**metadata, "secretary_rule_id": policy_hit.get("rule_id", "")},
                )
                await self._record(task, action_kind, action_name, target_agent, decision)
                if on_progress:
                    await on_progress(
                        f"[Autonomy] {action_kind}:{action_name} -> {decision.action.value} "
                        f"(risk={decision.risk_level.value}, confidence={decision.confidence:.2f})"
                    )
                return True, decision

        if action_kind == "tool" and action_name in COMPANY_APPROVAL_EXEMPT_TOOL_NAMES:
            decision = ApprovalDecision(
                action=ApprovalAction.AUTO_APPROVE,
                risk_level=RiskLevel.LOW,
                rationale="Built-in company collaboration tool is always auto-approved.",
                confidence=0.99,
                policy_source="company_tool_policy",
                metadata=metadata,
            )
            await self._record(task, action_kind, action_name, target_agent, decision)
            if on_progress:
                await on_progress(
                    f"[Autonomy] {action_kind}:{action_name} -> {decision.action.value} "
                    f"(risk={decision.risk_level.value}, confidence={decision.confidence:.2f})"
                )
            return True, decision

        allowlist_enabled = self._allowlist_enabled_for_action(action_kind, metadata)
        session_allowlist_hit = (
            self._lookup_session_allowlist_policy(
                task=task,
                action_kind=action_kind,
                action_name=action_name,
                metadata=metadata,
            )
            if allowlist_enabled
            else None
        )
        if session_allowlist_hit:
            decision = ApprovalDecision(
                action=ApprovalAction.AUTO_APPROVE,
                risk_level=RiskLevel.LOW,
                rationale=(
                    f"Allowed by session approval ({session_allowlist_hit['scope']}): "
                    + ", ".join(session_allowlist_hit["patterns"][:4])
                ),
                confidence=0.99,
                policy_source="session_approval",
                metadata={
                    **metadata,
                    "allowlist_scope": session_allowlist_hit["scope"],
                    "allowlist_patterns": session_allowlist_hit["patterns"],
                },
            )
            await self._record(task, action_kind, action_name, target_agent, decision)
            if on_progress:
                await on_progress(
                    f"[Autonomy] {action_kind}:{action_name} -> {decision.action.value} "
                    f"(risk={decision.risk_level.value}, confidence={decision.confidence:.2f})"
                )
            return True, decision

        project_id = task.project_id if task else None
        allowlist_hit = (
            self._lookup_allowlist_policy(
                action_kind=action_kind,
                action_name=action_name,
                metadata=metadata,
                project_id=project_id,
            )
            if allowlist_enabled
            else None
        )
        if allowlist_hit:
            scope = "global" if allowlist_hit["scope"] is None else f"project:{allowlist_hit['scope']}"
            decision = ApprovalDecision(
                action=ApprovalAction.AUTO_APPROVE,
                risk_level=RiskLevel.LOW,
                rationale=(
                    f"Allowed by persisted allowlist ({scope}): "
                    + ", ".join(allowlist_hit["patterns"][:4])
                ),
                confidence=0.99,
                policy_source="approval_allowlist",
                metadata={**metadata, "allowlist_scope": scope, "allowlist_patterns": allowlist_hit["patterns"]},
            )
            await self._record(task, action_kind, action_name, target_agent, decision)
            if on_progress:
                await on_progress(
                    f"[Autonomy] {action_kind}:{action_name} -> {decision.action.value} "
                    f"(risk={decision.risk_level.value}, confidence={decision.confidence:.2f})"
                )
            return True, decision

        learned = self._lookup_learned_policy(action_name, project_id)
        heuristic = self._heuristic_decision(
            action_kind=action_kind,
            action_name=action_name,
            summary=summary,
            metadata=metadata,
            learned=learned,
            allow_auto=allow_auto,
        )
        decision = heuristic
        tool_requires_allowlist = self._tool_requires_first_use_approval(
            action_kind,
            action_name,
            metadata=metadata,
        )

        external_direct_prompt_reason = self._external_agent_direct_human_prompt_reason(
            action_kind=action_kind,
            metadata=metadata,
        )

        if tool_requires_allowlist and heuristic.risk_level != RiskLevel.LOW:
            # First-use approval exists to catch unfamiliar, potentially risky
            # actions. Actions the heuristic already classified LOW (read-only
            # safe-prefix shell commands, clean tool arguments) proceed without
            # a card; MEDIUM and above still require the human gate.
            decision = self._force_first_use_approval(heuristic)
        elif external_direct_prompt_reason:
            decision = ApprovalDecision(
                action=ApprovalAction.ESCALATE,
                risk_level=RiskLevel.HIGH
                if heuristic.risk_level != RiskLevel.CRITICAL
                else RiskLevel.CRITICAL,
                rationale=external_direct_prompt_reason,
                confidence=0.96,
                policy_source="external_agent_policy",
                metadata=metadata,
            )
        elif heuristic.risk_level in {RiskLevel.MEDIUM, RiskLevel.HIGH} and allow_auto:
            llm_decision = await self._llm_review(
                task=task,
                action_kind=action_kind,
                action_name=action_name,
                summary=summary,
                metadata=metadata,
                learned=learned,
            )
            if llm_decision:
                decision = self._merge_decisions(heuristic, llm_decision)
            elif heuristic.risk_level == RiskLevel.MEDIUM:
                # LLM review failed (e.g. empty response) — for medium-risk
                # actions with auto-approval enabled, approve rather than
                # escalating on a transient LLM failure.
                decision = ApprovalDecision(
                    action=ApprovalAction.AUTO_APPROVE,
                    risk_level=RiskLevel.MEDIUM,
                    rationale=f"{heuristic.rationale} | LLM review unavailable; auto-approving medium-risk action.",
                    confidence=0.55,
                    policy_source="heuristic_fallback",
                    metadata=metadata,
                )

        if decision.action == ApprovalAction.ESCALATE and self.escalation and task:
            hierarchy_target = self._company_hierarchy_target(task)
            if hierarchy_target:
                decision.metadata = {
                    **dict(decision.metadata or {}),
                    "company_reviewer_role": hierarchy_target,
                    "approval_path": ["manager_or_coordinator", "user"],
                }
                decision.rationale = (
                    f"{decision.rationale} | Company hierarchy prefers `{hierarchy_target}` review before direct user escalation."
                ).strip()
            approved, decision = await self._ask_user(task, action_kind, action_name, decision, metadata)
            await self._record(task, action_kind, action_name, target_agent, decision)
            return approved, decision

        approved = decision.action == ApprovalAction.AUTO_APPROVE
        await self._record(task, action_kind, action_name, target_agent, decision)
        if on_progress:
            await on_progress(
                f"[Autonomy] {action_kind}:{action_name} -> {decision.action.value} "
                f"(risk={decision.risk_level.value}, confidence={decision.confidence:.2f})"
            )
        return approved, decision

    def _company_hierarchy_target(self, task: Task | None) -> str:
        if task is None:
            return ""
        if str(task.metadata.get("execution_mode", "") or "").strip() != "company_mode":
            return ""
        manager_role = str(task.metadata.get("manager_role_id", "") or "").strip()
        if manager_role and manager_role != "owner":
            return manager_role
        review_role = str(task.metadata.get("work_item_role_id", "") or "").strip()
        return review_role

    def _external_agent_direct_human_prompt_reason(
        self,
        *,
        action_kind: str,
        metadata: dict[str, Any],
    ) -> str:
        """Return a deterministic escalation reason for external launches that
        should not wait on LLM approval review before asking the user."""
        if action_kind != "external_agent":
            return ""
        approval_mode = str(metadata.get("approval_mode", "") or "").strip().lower()
        command = self._command_preview(
            str(metadata.get("command", "") or ""),
            drop_last_token=True,
        )
        haystack = " ".join(
            str(part or "")
            for part in (
                command,
                metadata.get("run_mode", ""),
                metadata.get("session_mode", ""),
                metadata.get("approval_mode", ""),
                metadata.get("permission_mode", ""),
            )
        ).lower()
        markers = [marker for marker in _EXTERNAL_AGENT_DIRECT_HUMAN_MARKERS if marker in haystack]
        if approval_mode == "full-auto":
            markers.append("approval_mode=full-auto")
        if not markers:
            return ""
        reasons: list[str] = []
        if "approval_mode=full-auto" in markers:
            reasons.append("full-auto execution")
        if "--force" in markers:
            reasons.append("forced execution")
        if any("bypass" in marker or "dangerously" in marker for marker in markers):
            reasons.append("permission bypass mode")
        detail = ", ".join(dict.fromkeys(reasons)) or "external-agent launch requires user confirmation"
        return (
            f"External agent launch requires direct user approval before start ({detail}). "
            "Skipping LLM approval review so the approval card appears immediately."
        )

    @staticmethod
    def _is_external_agent_launch(action_kind: str, metadata: dict[str, Any]) -> bool:
        return action_kind == "external_agent" and bool(
            str(metadata.get("command", "") or "").strip()
        )

    @staticmethod
    def _allowlist_enabled_for_action(action_kind: str, metadata: dict[str, Any]) -> bool:
        """Whether reusable human approval scopes are meaningful here.

        External-agent launches used to be excluded because their raw command
        contains the per-turn prompt. Reusable external-agent approvals are
        still safe to model at the selected-agent level: persistence is scoped
        by ``action_kind:action_name`` and uses ``*`` for the pattern, so
        "allow for this session/project" means "allow this agent" rather than
        "allow this exact prompt".
        """
        _ = metadata
        return action_kind in {"tool", "external_agent", "work_item_projection_title"}

    def _lookup_learned_policy(self, action_name: str, project_id: str | None) -> dict[str, Any]:
        autonomy = self.preferences.get_autonomy_preferences(project_id)
        return autonomy.get("learned_actions", {}).get(action_name, {})

    def _lookup_allowlist_policy(
        self,
        *,
        action_kind: str,
        action_name: str,
        metadata: dict[str, Any],
        project_id: str | None,
    ) -> dict[str, Any] | None:
        if not self.allowlist:
            return None
        candidates = self._build_allowlist_candidates(action_kind=action_kind, action_name=action_name, metadata=metadata)
        allowed, patterns, scope = self.allowlist.is_allowed(
            action_kind=action_kind,
            action_name=action_name,
            candidates=candidates,
            project_id=project_id,
        )
        if not allowed:
            return None
        return {"patterns": patterns, "scope": scope}

    def _lookup_session_allowlist_policy(
        self,
        *,
        task: Task | None,
        action_kind: str,
        action_name: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any] | None:
        session_scope_id = self._approval_session_scope_id(task)
        if not session_scope_id:
            return None
        scope = self._session_allowlist.get(session_scope_id)
        if scope is None:
            # Hydrate from the persisted allowlist so "Allow for this session"
            # grants survive `opc ui` restarts and re-entering the session.
            scope = {}
            if self.allowlist:
                try:
                    scope = self.allowlist.session_scope(session_scope_id)
                except Exception:
                    logger.opt(exception=True).debug(
                        "Failed to hydrate persisted session allowlist; using empty scope"
                    )
                    scope = {}
            self._session_allowlist[session_scope_id] = scope
        patterns = ApprovalAllowlistManager._scope_patterns(scope, action_kind, action_name)
        if not patterns:
            return None
        candidates = self._build_allowlist_candidates(
            action_kind=action_kind,
            action_name=action_name,
            metadata=metadata,
        )
        normalized_candidates = [
            ApprovalAllowlistManager._normalize_candidate(candidate)
            for candidate in candidates
            if ApprovalAllowlistManager._normalize_candidate(candidate)
        ]
        if not normalized_candidates:
            return None
        matched: list[str] = []
        for candidate in normalized_candidates:
            candidate_patterns = [
                pattern
                for pattern in patterns
                if ApprovalAllowlistManager._matches(pattern, candidate)
            ]
            if not candidate_patterns:
                return None
            matched.extend(candidate_patterns)
        return {"patterns": list(dict.fromkeys(matched)), "scope": f"session:{session_scope_id}"}

    @staticmethod
    def _approval_session_scope_id(task: Task | None) -> str:
        if task is None:
            return ""
        for candidate in (
            getattr(task, "parent_session_id", None),
            getattr(task, "session_id", None),
            getattr(task, "id", None),
        ):
            value = str(candidate or "").strip()
            if value:
                return value
        return ""

    def _add_session_patterns(
        self,
        *,
        task: Task,
        action_kind: str,
        action_name: str,
        patterns: list[str],
    ) -> list[str]:
        return self._add_session_patterns_by_scope(
            session_scope_id=self._approval_session_scope_id(task),
            action_kind=action_kind,
            action_name=action_name,
            patterns=patterns,
        )

    def _add_session_patterns_by_scope(
        self,
        *,
        session_scope_id: str,
        action_kind: str,
        action_name: str,
        patterns: list[str],
    ) -> list[str]:
        if not session_scope_id:
            return []
        normalized_patterns = ApprovalAllowlistManager._normalize_pattern_list(patterns)
        if not normalized_patterns:
            return []
        scope = self._session_allowlist.setdefault(
            session_scope_id,
            ApprovalAllowlistManager._normalize_scope({}),
        )
        action_bucket = scope.setdefault(action_kind, {})
        existing = ApprovalAllowlistManager._normalize_pattern_list(action_bucket.get(action_name, []))
        added: list[str] = []
        for pattern in normalized_patterns:
            if pattern in existing:
                continue
            existing.append(pattern)
            added.append(pattern)
        action_bucket[action_name] = existing
        if added and self.allowlist:
            try:
                self.allowlist.add_session_patterns(session_scope_id, action_kind, action_name, added)
            except Exception:
                logger.opt(exception=True).debug(
                    "Failed to persist session allowlist patterns; grant remains in-memory only"
                )
        return added

    def _tool_requires_first_use_approval(
        self,
        action_kind: str,
        action_name: str,
        *,
        metadata: dict[str, Any],
    ) -> bool:
        if action_kind != "tool":
            return False
        if not self.config.tool_first_use_approval:
            return False
        if action_name in COMPANY_APPROVAL_EXEMPT_TOOL_NAMES:
            return False
        if self._is_low_risk_shell_first_use_exempt(action_name, metadata):
            return False
        exemptions = {item.strip() for item in self.config.tool_approval_exemptions if item.strip()}
        return action_name not in exemptions

    def _force_first_use_approval(self, heuristic: ApprovalDecision) -> ApprovalDecision:
        rationale_parts = [heuristic.rationale] if heuristic.rationale else []
        rationale_parts.append("No persisted allowlist rule matched; first use requires approval.")
        return ApprovalDecision(
            action=ApprovalAction.ESCALATE,
            risk_level=heuristic.risk_level,
            rationale=" | ".join(rationale_parts),
            confidence=max(heuristic.confidence, 0.9),
            policy_source="approval_allowlist",
            metadata=heuristic.metadata,
        )

    def _build_allowlist_candidates(
        self,
        *,
        action_kind: str,
        action_name: str,
        metadata: dict[str, Any],
    ) -> list[str]:
        if action_kind == "tool":
            arguments = metadata.get("arguments", {})
            if action_name == "shell_exec" and isinstance(arguments, dict):
                command = str(arguments.get("command", "")).strip()
                commands, _ = self._shell_grant_targets(command)
                if commands:
                    return commands
                preview = self._command_preview(command)
                return [preview] if preview else ["*"]

            candidates: list[str] = []
            if isinstance(arguments, dict):
                for key in (
                    "path",
                    "target",
                    "working_directory",
                    "workdir",
                    "cwd",
                    "url",
                    "recipient",
                    "query",
                    "message",
                    "subject",
                ):
                    value = str(arguments.get(key, "")).strip()
                    if value:
                        candidates.append(value)
            if not candidates:
                candidates.append("*")
            return list(dict.fromkeys(candidates))

        if action_kind == "external_agent":
            preview = self._command_preview(metadata.get("command"))
            return [preview] if preview else ["*"]

        return ["*"]

    def _build_allowlist_patterns(
        self,
        *,
        action_kind: str,
        action_name: str,
        metadata: dict[str, Any],
    ) -> list[str]:
        if action_kind == "tool":
            arguments = metadata.get("arguments", {})
            if action_name == "shell_exec" and isinstance(arguments, dict):
                _, prefixes = self._shell_grant_targets(str(arguments.get("command", "")).strip())
                if prefixes:
                    return prefixes
                preview = self._command_preview(arguments.get("command"))
                return [preview] if preview else []
            return ["*"]

        return ["*"]

    def _shell_grant_targets(self, command: str) -> tuple[list[str], list[str]]:
        """Derive allowlist candidates (full per-segment commands) and grant
        patterns (word-boundary prefixes) for a shell command.

        Only the segments that actually need approval are returned: read-only
        safe segments (`ls` / `echo` / verification `cat`s chained after a
        granted command) pass on their own merit and must neither break the
        every-candidate-must-match rule nor be persisted as grants.

        Fail closed: a command containing substitution we cannot audit, or one
        that does not tokenize, is only ever grantable as its exact normalized
        string — never as a broad prefix.
        """
        raw = " ".join(str(command or "").split()).strip()
        if not raw:
            return [], []
        sanitized, expansions_safe = shell_safety.sanitize_expansions(raw)
        sanitized = shell_safety.strip_safe_redirections(sanitized)
        segments = shell_safety.split_shell_segments(sanitized) if expansions_safe else None
        if not segments:
            return [raw], [raw]
        safe_prefixes = [
            item for item in self.config.safe_command_prefixes
            if str(item or "").strip() not in _LOW_RISK_SHELL_PREFIXES
        ]
        commands: list[str] = []
        prefixes: list[str] = []
        all_commands: list[str] = []
        all_prefixes: list[str] = []
        for tokens in segments:
            full = " ".join(tokens).strip()
            if not full:
                continue
            prefix_tokens = self._shell_command_prefix(tokens)
            prefix = " ".join(prefix_tokens).strip()
            if prefix_tokens and prefix_tokens[0] in shell_safety.UNGRANTABLE_PREFIX_HEADS:
                # "always allow bash/eval/sudo ..." would be a blank check;
                # degrade to the exact command.
                prefix = full
            all_commands.append(full)
            all_prefixes.append(prefix or full)
            if not shell_safety.is_read_only_shell_command(full, safe_prefixes)[0]:
                commands.append(full)
                prefixes.append(prefix or full)
        if not commands:
            # Fully read-only command: grants are moot, but keep the raw
            # targets so callers still have a meaningful display candidate.
            commands, prefixes = all_commands, all_prefixes
        return list(dict.fromkeys(commands)), list(dict.fromkeys(prefixes))

    def _command_has_shell_substitution(self, command: str) -> bool:
        """True when a command contains dynamic constructs (unauditable
        ``$(...)``, backticks, process substitution, ``eval``/``source``) that
        must never ride through on a safe prefix or a persisted grant."""
        if shell_safety.has_blocked_substitution(command):
            return True
        segments = shell_safety.split_shell_segments(command)
        if segments is None:
            return True
        return any(tokens and tokens[0] in {"eval", "source", "."} for tokens in segments)

    def _command_matches_safe_prefix(self, command: str, prefixes: list[str]) -> bool:
        safe, _ = shell_safety.is_read_only_shell_command(command, prefixes)
        return safe

    def _is_low_risk_shell_first_use_exempt(self, action_name: str, metadata: dict[str, Any]) -> bool:
        if action_name != "shell_exec":
            return False
        arguments = metadata.get("arguments", {})
        if not isinstance(arguments, dict):
            return False
        command = str(arguments.get("command", "") or arguments.get("cmd", "")).strip()
        if not command:
            return False
        return is_projection_scoped_acquisition_shell_command(
            command=command,
            projection_id=work_item_projection_id_from_metadata(metadata, fallback=""),
            role_id=str(metadata.get("role_id", "") or "").strip(),
            working_directory=str(arguments.get("working_directory", "") or arguments.get("workdir", "") or "").strip(),
            target_output_dir=str(metadata.get("target_output_dir", "") or "").strip(),
        )

    def _shell_command_prefix(self, tokens: list[str]) -> list[str]:
        # Interpreter inline-code / module runs keep the flag in the prefix so
        # a grant reads `python3 -c` (all inline snippets) or `python -m pip`
        # (that module) instead of a blanket `python3`.
        if tokens and tokens[0] in {"python", "python3", "python2", "node", "bun", "deno", "ruby", "perl"}:
            for index in (1, 2):
                if index >= len(tokens):
                    break
                if tokens[index] in {"-c", "-e"}:
                    return [tokens[0], tokens[index]]
                if tokens[index] == "-m" and index + 1 < len(tokens):
                    return [tokens[0], "-m", tokens[index + 1]]
        semantic = self._shell_semantic_tokens(tokens)
        for length in range(len(semantic), 0, -1):
            prefix = " ".join(semantic[:length])
            arity = _SHELL_COMMAND_PREFIX_ARITY.get(prefix)
            if arity is not None:
                return semantic[:arity]
        if not semantic:
            return []
        if semantic[0] in {"python", "python3", "node", "bun", "deno"} and len(semantic) > 1:
            if semantic[0] in {"python", "python3"} and semantic[1].startswith("<"):
                return semantic[:1]
            return semantic[:2]
        return semantic[:1]

    def _shell_semantic_tokens(self, tokens: list[str]) -> list[str]:
        if not tokens:
            return []
        semantic = [tokens[0]]
        i = 1
        while i < len(tokens):
            token = tokens[i]
            if token.startswith("-"):
                if token in {
                    "-C",
                    "-c",
                    "-m",
                    "-n",
                    "-p",
                    "--context",
                    "--cwd",
                    "--directory",
                    "--git-dir",
                    "--namespace",
                    "--profile",
                    "--project",
                    "--work-tree",
                } and i + 1 < len(tokens):
                    i += 2
                    continue
                i += 1
                continue
            semantic.append(token)
            i += 1
        return semantic

    def _heuristic_decision(
        self,
        action_kind: str,
        action_name: str,
        summary: str,
        metadata: dict[str, Any],
        learned: dict[str, Any],
        allow_auto: bool,
    ) -> ApprovalDecision:
        approval_mode = str(metadata.get("approval_mode", "") or "").strip().lower()
        if (
            action_kind == "external_agent"
            and approval_mode in {"auto", "user-settings"}
            and str(metadata.get("command", "") or "").strip()
        ):
            if not allow_auto:
                return ApprovalDecision(
                    action=ApprovalAction.ESCALATE,
                    risk_level=RiskLevel.MEDIUM,
                    rationale=(
                        f"External agent launch uses OpenOPC approval mode `{approval_mode}`, "
                        "but external-agent auto-approval is disabled; direct user approval is required before start."
                    ),
                    confidence=0.95,
                    policy_source="external_agent_launch_policy",
                    metadata=metadata,
                )
            return ApprovalDecision(
                action=ApprovalAction.AUTO_APPROVE,
                risk_level=RiskLevel.LOW,
                rationale=(
                    f"External agent launch uses OpenOPC approval mode `{approval_mode}`; "
                    "startup is audited while runtime permission requests remain bridged to OpenOPC."
                ),
                confidence=0.95,
                policy_source="external_agent_launch_policy",
                metadata=metadata,
            )

        sensitive_text, destructive_text, command = self._build_risk_inputs(
            action_kind=action_kind,
            action_name=action_name,
            summary=summary,
            metadata=metadata,
        )
        reasons: list[str] = []
        risk = RiskLevel.LOW

        explicit_allow = bool(learned.get("explicit_allow"))
        explicit_deny = bool(learned.get("explicit_deny"))
        approvals = int(learned.get("approvals", 0))
        rejections = int(learned.get("rejections", 0))

        if explicit_deny:
            return ApprovalDecision(
                action=ApprovalAction.ESCALATE,
                risk_level=RiskLevel.HIGH,
                rationale="This action family was explicitly denied before.",
                confidence=0.95,
                policy_source="learned_policy",
                metadata=metadata,
            )

        for keyword in self.config.sensitive_keywords:
            if self._matches_sensitive_keyword(sensitive_text, keyword):
                risk = RiskLevel.HIGH
                reasons.append(f"Matched sensitive keyword: {keyword}")

        destructive_patterns = [
            r"\brm\s+-rf\b",
            r"\bdrop\s+table\b",
            r"\btruncate\b",
            r"\bdelete\s+from\b",
            r"\bterraform\s+destroy\b",
            r"\bgit\s+push\s+--force\b",
            r"\bchmod\s+777\b",
        ]
        for pattern in destructive_patterns:
            if re.search(pattern, destructive_text):
                risk = RiskLevel.CRITICAL
                reasons.append(f"Matched destructive pattern: {pattern}")

        if command:
            projection_scoped_low_risk = is_projection_scoped_acquisition_shell_command(
                command=command,
                projection_id=work_item_projection_id_from_metadata(metadata, fallback=""),
                role_id=str(metadata.get("role_id", "") or "").strip(),
                working_directory=str(
                    dict(metadata.get("arguments", {}) or {}).get("working_directory", "")
                    or dict(metadata.get("arguments", {}) or {}).get("workdir", "")
                    or ""
                ).strip(),
                target_output_dir=str(metadata.get("target_output_dir", "") or "").strip(),
            )
            safe_prefixes = [
                item for item in self.config.safe_command_prefixes
                if projection_scoped_low_risk or str(item or "").strip() not in _LOW_RISK_SHELL_PREFIXES
            ]
            # The read-only audit must see the ORIGINAL command text: the
            # preview used for keyword scans re-joins shlex tokens and drops
            # quotes, turning e.g. `echo "<EOF>"` into `echo <EOF>` where the
            # bare `<` reads as a redirection and misclassifies the command.
            arguments = metadata.get("arguments", {})
            raw_command = (
                str(arguments.get("command", "") or arguments.get("cmd", "") or "").strip()
                if isinstance(arguments, dict)
                else ""
            ) or command
            if projection_scoped_low_risk:
                reasons.append("Command matches a projection-scoped acquisition prefix inside the assigned workspace.")
            elif self._command_matches_safe_prefix(raw_command, safe_prefixes):
                reasons.append("Command matches known low-risk prefix.")
            elif risk == RiskLevel.LOW:
                risk = RiskLevel.MEDIUM
                reasons.append("Command is not in the low-risk allowlist.")

        if approvals >= 3 and rejections == 0 and explicit_allow and allow_auto and risk != RiskLevel.CRITICAL:
            return ApprovalDecision(
                action=ApprovalAction.AUTO_APPROVE,
                risk_level=RiskLevel.LOW if risk == RiskLevel.LOW else RiskLevel.MEDIUM,
                rationale="Learned project policy explicitly allows this action family.",
                confidence=0.95,
                policy_source="learned_policy",
                metadata=metadata,
            )

        if risk == RiskLevel.CRITICAL:
            action = ApprovalAction.ESCALATE
        elif risk == RiskLevel.HIGH:
            action = ApprovalAction.ESCALATE
        elif risk == RiskLevel.MEDIUM and not allow_auto:
            action = ApprovalAction.ESCALATE
        else:
            action = ApprovalAction.AUTO_APPROVE if allow_auto else ApprovalAction.ESCALATE

        if allow_auto and self._risk_exceeds_policy(risk):
            action = ApprovalAction.ESCALATE
            reasons.append("Risk exceeds configured auto-approval threshold.")

        if rejections > approvals:
            action = ApprovalAction.ESCALATE
            reasons.append("Historical rejection rate is higher than approvals.")

        confidence = 0.9 if action == ApprovalAction.ESCALATE and risk in {RiskLevel.HIGH, RiskLevel.CRITICAL} else 0.65
        rationale = "; ".join(reasons) if reasons else "No sensitive patterns detected."
        return ApprovalDecision(
            action=action,
            risk_level=risk,
            rationale=rationale,
            confidence=confidence,
            policy_source="heuristic",
            metadata=metadata,
        )

    def _build_risk_inputs(
        self,
        *,
        action_kind: str,
        action_name: str,
        summary: str,
        metadata: dict[str, Any],
    ) -> tuple[str, str, str]:
        sensitive_fragments: list[str] = [action_name]
        destructive_fragments: list[str] = [action_name]
        command = ""

        if action_kind == "external_agent":
            external_keys = {
                "agent",
                "binary",
                "model",
                "model_flag",
                "session_mode",
                "run_mode",
                "approval_mode",
                "workspace",
                "target_output_dir",
            }
            sensitive_fragments.extend(self._collect_named_values(metadata, external_keys))
            command = self._command_preview(str(metadata.get("command", "")), drop_last_token=True)
            if command:
                sensitive_fragments.append(command)
                destructive_fragments.append(command)
            prompt_text = str(metadata.get("prompt_text", "")).strip()
            if prompt_text:
                destructive_fragments.append(prompt_text)
        elif action_kind == "tool":
            tool_keys = {
                "tool",
                "tool_name",
                "command",
                "cmd",
                "argv",
                "binary",
                "subcommand",
                "action",
                "operation",
                "path",
                "paths",
                "target",
                "target_path",
                "destination",
                "workspace",
                "workdir",
                "cwd",
                "url",
                "urls",
                "recipient",
                "recipients",
                "to",
                "email",
                "subject",
                "sql",
                "query",
                "statement",
                "script",
            }
            sensitive_fragments.extend(self._collect_named_values(metadata, tool_keys))
            arguments = metadata.get("arguments", {})
            command = (
                self._command_preview(arguments.get("argv")) if isinstance(arguments, dict) else ""
            ) or (
                self._command_preview(arguments.get("command")) if isinstance(arguments, dict) else ""
            ) or (
                self._command_preview(arguments.get("cmd")) if isinstance(arguments, dict) else ""
            ) or self._command_preview(metadata.get("command"))
            if command:
                sensitive_fragments.append(command)
                destructive_fragments.append(command)
        else:
            generic_keys = {
                "role_id",
                "work_item_projection_title",
                "work_item_projection_id",
                "company_profile",
                "gate_type",
                "command",
                "cmd",
                "path",
                "target",
                "workspace",
            }
            summary_text = str(summary).strip()
            if summary_text:
                sensitive_fragments.append(summary_text)
                destructive_fragments.append(summary_text)
            sensitive_fragments.extend(self._collect_named_values(metadata, generic_keys))
            command = self._command_preview(metadata.get("command")) or self._command_preview(metadata.get("cmd"))
            if command:
                destructive_fragments.append(command)

        sensitive_text = "\n".join(dict.fromkeys(fragment for fragment in sensitive_fragments if fragment)).lower()
        destructive_text = "\n".join(
            dict.fromkeys(fragment for fragment in [*sensitive_fragments, *destructive_fragments] if fragment)
        ).lower()
        normalized_command = " ".join(command.split()).strip().lower()
        return sensitive_text, destructive_text, normalized_command

    def _collect_named_values(
        self,
        value: Any,
        allowed_keys: set[str],
        *,
        current_key: str = "",
    ) -> list[str]:
        if isinstance(value, dict):
            fragments: list[str] = []
            for key, item in value.items():
                fragments.extend(
                    self._collect_named_values(
                        item,
                        allowed_keys,
                        current_key=self._normalize_key(key),
                    )
                )
            return fragments
        if isinstance(value, (list, tuple, set)):
            fragments: list[str] = []
            for item in value:
                fragments.extend(self._collect_named_values(item, allowed_keys, current_key=current_key))
            return fragments
        if current_key and current_key in allowed_keys:
            text = str(value).strip()
            return [text] if text else []
        return []

    def _command_preview(self, raw: Any, *, drop_last_token: bool = False) -> str:
        if isinstance(raw, (list, tuple)):
            tokens = [str(item).strip() for item in raw if str(item).strip()]
        else:
            text = str(raw or "").strip()
            if not text:
                return ""
            try:
                tokens = shlex.split(text)
            except ValueError:
                tokens = text.split()

        if drop_last_token and len(tokens) > 1:
            tokens = tokens[:-1]

        preview: list[str] = []
        for token in tokens[:20]:
            if not token:
                continue
            if "\n" in token or len(token) > 200:
                break
            preview.append(token)
        return " ".join(preview)

    def _command_for_user(self, raw: Any, *, drop_last_token: bool = False) -> str:
        if isinstance(raw, (list, tuple)):
            tokens = [str(item).strip() for item in raw if str(item).strip()]
            if drop_last_token and len(tokens) > 1:
                tokens = tokens[:-1]
            return shlex.join(tokens) if tokens else ""

        text = str(raw or "").strip()
        if not text:
            return ""
        if not drop_last_token:
            return text
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()
        if len(tokens) > 1:
            return " ".join(tokens[:-1])
        return text

    def _matches_sensitive_keyword(self, text: str, keyword: str) -> bool:
        normalized = str(keyword or "").strip().lower()
        if not normalized:
            return False
        escaped = re.escape(normalized)
        escaped = re.sub(r"(?:\\ )+", r"\\s+", escaped)
        return re.search(rf"(?<!\w){escaped}(?!\w)", text) is not None

    def _normalize_key(self, key: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")

    def _summarize_metadata_for_user(self, action_kind: str, metadata: dict[str, Any]) -> str:
        if action_kind == "external_agent":
            parts = [
                f"agent={metadata.get('agent', '')}",
                f"binary={metadata.get('binary', '')}",
                f"command={self._command_preview(str(metadata.get('command', '')), drop_last_token=True)}",
                f"workspace={metadata.get('workspace', '')}",
                f"session_mode={metadata.get('session_mode', '')}",
                f"run_mode={metadata.get('run_mode', '')}",
                f"approval_mode={metadata.get('approval_mode', '')}",
            ]
            return "; ".join(part for part in parts if not part.endswith("="))[:1000]
        if action_kind == "tool":
            arguments = metadata.get("arguments", {})
            command = (
                self._command_for_user(arguments.get("argv")) if isinstance(arguments, dict) else ""
            ) or (
                self._command_for_user(arguments.get("command")) if isinstance(arguments, dict) else ""
            ) or (
                self._command_for_user(arguments.get("cmd")) if isinstance(arguments, dict) else ""
            ) or self._command_for_user(metadata.get("command"))
            parts = [
                f"tool={metadata.get('tool', '')}",
                f"command={command}",
                f"path={arguments.get('path', '') if isinstance(arguments, dict) else ''}",
                f"target={arguments.get('target', '') if isinstance(arguments, dict) else ''}",
            ]
            return "; ".join(part for part in parts if not part.endswith("="))
        return str(metadata)[:1000]

    def _risk_exceeds_policy(self, risk: RiskLevel) -> bool:
        order = {
            RiskLevel.LOW: 0,
            RiskLevel.MEDIUM: 1,
            RiskLevel.HIGH: 2,
            RiskLevel.CRITICAL: 3,
        }
        configured = self.config.max_auto_approve_risk.lower()
        configured_risk = {
            "low": RiskLevel.LOW,
            "medium": RiskLevel.MEDIUM,
            "high": RiskLevel.HIGH,
            "critical": RiskLevel.CRITICAL,
        }.get(configured, RiskLevel.MEDIUM)
        return order[risk] > order[configured_risk]

    async def _llm_review(
        self,
        task: Task | None,
        action_kind: str,
        action_name: str,
        summary: str,
        metadata: dict[str, Any],
        learned: dict[str, Any],
    ) -> ApprovalDecision | None:
        model = self.config.approval_model or self.llm.config.default_model
        prompt = {
            "task_title": task.title if task else "",
            "project_id": task.project_id if task else "default",
            "action_kind": action_kind,
            "action_name": action_name,
            "summary": summary,
            "metadata": metadata,
            "learned_policy": learned,
            "policy": {
                "max_auto_approve_risk": self.config.max_auto_approve_risk,
                "approval_confidence_threshold": self.config.approval_confidence_threshold,
            },
        }
        system = (
            "You are the autonomy approval reviewer for an AI execution system.\n"
            "Decide whether an action should be AUTO_APPROVE or ESCALATE.\n"
            "Return strict JSON with keys: action, risk_level, confidence, rationale.\n"
            "Use risk_level in [low, medium, high, critical].\n"
            "If the action touches credentials, irreversible destructive changes, or external communications, escalate."
        )
        valid_actions = {item.value for item in ApprovalAction}
        valid_risk_levels = {item.value for item in RiskLevel}

        def _validate_approval(parsed: Any) -> str | None:
            if not isinstance(parsed, dict):
                return "Top-level response must be a JSON object."
            action_str = str(parsed.get("action", "") or "").strip().lower()
            if action_str not in valid_actions:
                return (
                    f"Unknown action `{action_str}`. Choose one of: "
                    f"{', '.join(sorted(valid_actions))}."
                )
            risk_str = str(parsed.get("risk_level", "") or "").strip().lower()
            if risk_str not in valid_risk_levels:
                return (
                    f"Unknown risk_level `{risk_str}`. Choose one of: "
                    f"{', '.join(sorted(valid_risk_levels))}."
                )
            try:
                float(parsed.get("confidence", 0.5))
            except (TypeError, ValueError):
                return "`confidence` must be a number between 0 and 1."
            return None

        try:
            data = await call_llm_json_with_retry(
                self.llm,
                system=system,
                payload=prompt,
                task_type="quick_tasks",
                validator=_validate_approval,
                label="approval_llm_review",
            )
            return ApprovalDecision(
                action=ApprovalAction(str(data.get("action", "escalate")).lower()),
                risk_level=RiskLevel(str(data.get("risk_level", "medium")).lower()),
                rationale=data.get("rationale", ""),
                confidence=float(data.get("confidence", 0.5)),
                policy_source=f"llm:{model}",
                metadata=metadata,
            )
        except LLMRetryError as e:
            logger.debug(f"Approval LLM review failed after retries: {e}")
            return None
        except Exception as e:
            logger.debug(f"Approval LLM review construction failed: {e}")
            return None

    def _merge_decisions(self, heuristic: ApprovalDecision, llm_decision: ApprovalDecision) -> ApprovalDecision:
        if heuristic.risk_level == RiskLevel.CRITICAL:
            return heuristic
        threshold = self.config.approval_confidence_threshold
        if llm_decision.action == ApprovalAction.AUTO_APPROVE and llm_decision.confidence >= threshold:
            return llm_decision
        if llm_decision.risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL}:
            return llm_decision
        return heuristic

    def _format_allowlist_hint(self, action_kind: str, action_name: str, patterns: list[str]) -> str:
        if not patterns:
            return ""
        if action_kind == "tool" and action_name == "shell_exec":
            return ", ".join(patterns[:4])
        if patterns == ["*"]:
            return f"{action_kind}:{action_name}"
        return ", ".join(patterns[:4])

    async def _ask_user(
        self,
        task: Task,
        action_kind: str,
        action_name: str,
        decision: ApprovalDecision,
        metadata: dict[str, Any],
    ) -> tuple[bool, ApprovalDecision]:
        if not self.escalation:
            return False, decision
        allowlist_enabled = self._allowlist_enabled_for_action(action_kind, metadata)
        allowlist_patterns = (
            self._build_allowlist_patterns(
                action_kind=action_kind,
                action_name=action_name,
                metadata=metadata,
            )
            if allowlist_enabled
            else []
        )
        allowlist_hint = self._format_allowlist_hint(action_kind, action_name, allowlist_patterns)
        question = (
            f"Approve {action_kind} '{action_name}'?\n"
            f"Risk: {decision.risk_level.value}\n"
            f"Reason: {decision.rationale}\n"
            f"Summary: {self._summarize_metadata_for_user(action_kind, metadata)}"
        )
        if allowlist_hint:
            question += f"\nAllowlist target: {allowlist_hint}"
        options = [
            {"id": "approve_once", "label": "Approve once"},
            {"id": "deny", "label": "Deny"},
        ]
        if allowlist_enabled:
            options[1:1] = [{"id": "approve_session", "label": "Allow for this session"}]
            options.extend([
                {"id": "always_project", "label": "Always allow for this project"},
                {"id": "always_global", "label": "Always allow globally"},
            ])
        approval_context = {
            "action_kind": action_kind,
            "action_name": action_name,
            "project_id": str(task.project_id or "") if task else "",
            "session_scope_id": self._approval_session_scope_id(task),
            "allowlist_enabled": allowlist_enabled,
            "allowlist_patterns": list(allowlist_patterns),
            "candidates": self._build_allowlist_candidates(
                action_kind=action_kind,
                action_name=action_name,
                metadata=metadata,
            ),
        }
        reply = await self.escalation.escalate_decision(
            task,
            question,
            options,
            default_action=None,
            context=approval_context,
        )
        if reply is None:
            return False, ApprovalDecision(
                action=ApprovalAction.REQUIRE_INPUT,
                risk_level=decision.risk_level,
                rationale=f"{decision.rationale} | Awaiting user input.",
                confidence=1.0,
                requires_user_input=True,
                policy_source="human_escalation",
                metadata={**metadata, "human_reply": None},
            )
        if not allowlist_enabled and reply in {"approve_session", "always_project", "always_global"}:
            reply = "approve_once"
        approved = reply in {"approve_once", "approve_session", "always_project", "always_global"}
        explicit = reply in {"approve_session", "always_project", "always_global"}
        notes = "User approved via escalation." if approved else "User denied via escalation."
        saved_patterns: list[str] = []
        allowlist_scope: str | None = None
        if reply == "approve_session":
            saved_patterns = self._add_session_patterns(
                task=task,
                action_kind=action_kind,
                action_name=action_name,
                patterns=allowlist_patterns,
            )
            session_scope_id = self._approval_session_scope_id(task)
            if session_scope_id:
                allowlist_scope = f"session:{session_scope_id}"
        elif reply == "approve_once" and allowlist_enabled and action_kind == "tool":
            # "Approve once" still records the exact blocked candidates as a
            # session grant: repeating the identical action in this session
            # must not re-prompt, but nothing broader is granted.
            once_patterns = approval_context.get("candidates") or allowlist_patterns
            if once_patterns:
                saved_patterns = self._add_session_patterns(
                    task=task,
                    action_kind=action_kind,
                    action_name=action_name,
                    patterns=list(once_patterns),
                )
                session_scope_id = self._approval_session_scope_id(task)
                if saved_patterns and session_scope_id:
                    allowlist_scope = f"session:{session_scope_id}"
        elif reply == "always_project" and self.allowlist:
            saved_patterns = self.allowlist.add_patterns(
                action_kind=action_kind,
                action_name=action_name,
                patterns=allowlist_patterns,
                project_id=task.project_id,
            )
            allowlist_scope = f"project:{task.project_id}"
        elif reply == "always_global" and self.allowlist:
            saved_patterns = self.allowlist.add_patterns(
                action_kind=action_kind,
                action_name=action_name,
                patterns=allowlist_patterns,
                project_id=None,
            )
            allowlist_scope = "global"
        self.preferences.record_autonomy_feedback(
            action_name=action_name,
            approved=approved,
            project_id=task.project_id if reply == "always_project" else None,
            explicit=explicit,
            notes=notes,
        )
        result_metadata = {**metadata, "human_reply": reply}
        if saved_patterns:
            result_metadata["allowlist_patterns"] = saved_patterns
        if allowlist_scope:
            result_metadata["allowlist_scope"] = allowlist_scope
        return approved, ApprovalDecision(
            action=ApprovalAction.AUTO_APPROVE if approved else ApprovalAction.REJECT,
            risk_level=decision.risk_level,
            rationale=f"{decision.rationale} | User decision: {reply}",
            confidence=1.0,
            requires_user_input=False,
            policy_source="human_escalation",
            metadata=result_metadata,
        )

    def escalation_context_for_blocked_tool(
        self,
        task: Task | None,
        *,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Rebuild the ``approval_context`` an inline escalation would carry
        for a runtime-blocked tool.

        A block that outlived its inline wait survives only as checkpoint
        payload (``pause_request.permission_context``), which does not carry
        the allowlist context an approval card stores. This builder lets any
        reply surface (chat, CLI, headless API) route its decision through
        ``apply_deferred_escalation_decision`` — the same channel as the
        Office UI card click — instead of degrading to plain task input.
        """
        action_kind = "tool"
        action_name = str(tool_name or "").strip()
        metadata: dict[str, Any] = {"arguments": dict(arguments or {})}
        allowlist_enabled = self._allowlist_enabled_for_action(action_kind, metadata)
        patterns = (
            self._build_allowlist_patterns(
                action_kind=action_kind,
                action_name=action_name,
                metadata=metadata,
            )
            if allowlist_enabled
            else []
        )
        return {
            "action_kind": action_kind,
            "action_name": action_name,
            "project_id": str(getattr(task, "project_id", "") or "") if task else "",
            "session_scope_id": self._approval_session_scope_id(task),
            "allowlist_enabled": allowlist_enabled,
            "allowlist_patterns": list(patterns),
            "candidates": self._build_allowlist_candidates(
                action_kind=action_kind,
                action_name=action_name,
                metadata=metadata,
            ),
        }

    def apply_deferred_escalation_decision(
        self,
        reply: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Apply a decision clicked on an approval card after its inline wait
        expired (the blocked task has parked on AWAITING_HUMAN by then).

        Persists the same allowlist grant the live path would have applied, so
        the re-run of the blocked action passes automatically. ``context`` is
        the ``approval_context`` the card was created with. Returns a summary
        {approved, scope, patterns} for UI messaging.
        """
        normalized_reply = str(reply or "").strip()
        context = dict(context or {})
        action_kind = str(context.get("action_kind", "") or "").strip()
        action_name = str(context.get("action_name", "") or "").strip()
        project_id = str(context.get("project_id", "") or "").strip() or None
        session_scope_id = str(context.get("session_scope_id", "") or "").strip()
        allowlist_enabled = bool(context.get("allowlist_enabled", False))
        allowlist_patterns = [
            str(item).strip() for item in list(context.get("allowlist_patterns", []) or [])
            if str(item).strip()
        ]
        exact_candidates = [
            str(item).strip() for item in list(context.get("candidates", []) or [])
            if str(item).strip()
        ]
        if not allowlist_enabled and normalized_reply in {"approve_session", "always_project", "always_global"}:
            normalized_reply = "approve_once"
        approved = normalized_reply in {"approve_once", "approve_session", "always_project", "always_global"}

        saved_patterns: list[str] = []
        scope: str | None = None
        if normalized_reply == "approve_session" and session_scope_id and allowlist_patterns:
            saved_patterns = self._add_session_patterns_by_scope(
                session_scope_id=session_scope_id,
                action_kind=action_kind,
                action_name=action_name,
                patterns=allowlist_patterns,
            )
            scope = f"session:{session_scope_id}"
        elif normalized_reply == "approve_once" and session_scope_id and action_kind == "tool":
            # No one-shot grant store exists; the narrowest durable equivalent
            # is a session grant for the exact blocked command(s), so the
            # resumed run passes without widening approval to the whole family.
            once_patterns = exact_candidates or allowlist_patterns
            if once_patterns:
                saved_patterns = self._add_session_patterns_by_scope(
                    session_scope_id=session_scope_id,
                    action_kind=action_kind,
                    action_name=action_name,
                    patterns=once_patterns,
                )
                scope = f"session:{session_scope_id}"
        elif normalized_reply == "always_project" and self.allowlist and project_id and allowlist_patterns:
            saved_patterns = self.allowlist.add_patterns(
                action_kind=action_kind,
                action_name=action_name,
                patterns=allowlist_patterns,
                project_id=project_id,
            )
            scope = f"project:{project_id}"
        elif normalized_reply == "always_global" and self.allowlist and allowlist_patterns:
            saved_patterns = self.allowlist.add_patterns(
                action_kind=action_kind,
                action_name=action_name,
                patterns=allowlist_patterns,
                project_id=None,
            )
            scope = "global"

        if action_name:
            try:
                self.preferences.record_autonomy_feedback(
                    action_name=action_name,
                    approved=approved,
                    project_id=project_id if normalized_reply == "always_project" else None,
                    explicit=normalized_reply in {"approve_session", "always_project", "always_global"},
                    notes=(
                        "User approved via deferred escalation card."
                        if approved
                        else "User denied via deferred escalation card."
                    ),
                )
            except Exception:
                logger.opt(exception=True).debug(
                    "Failed to record autonomy feedback for deferred escalation decision"
                )

        return {
            "approved": approved,
            "reply": normalized_reply,
            "scope": scope,
            "patterns": saved_patterns,
            "action_name": action_name,
        }

    async def _record(
        self,
        task: Task | None,
        action_kind: str,
        action_name: str,
        target_agent: str,
        decision: ApprovalDecision,
    ) -> None:
        project_id = task.project_id if task else "default"
        await self.store.record_approval(
            decision=decision,
            task_id=task.id if task else None,
            project_id=project_id,
            action_kind=action_kind,
            action_name=action_name,
            target_agent=target_agent,
        )
        if self.config.learn_from_feedback and decision.action in {ApprovalAction.AUTO_APPROVE, ApprovalAction.REJECT}:
            approved = decision.action == ApprovalAction.AUTO_APPROVE
            self.preferences.record_autonomy_feedback(
                action_name=action_name,
                approved=approved,
                project_id=project_id if project_id != "default" else None,
                explicit=False,
                notes=decision.rationale,
            )
        self.memory.append_autonomy_event(
            {
                "action_kind": action_kind,
                "action_name": action_name,
                "decision": decision.action.value,
                "risk_level": decision.risk_level.value,
                "policy_source": decision.policy_source,
                "rationale": decision.rationale,
            },
            project=bool(task and task.project_id and task.project_id != "default"),
        )

    def _memory_path_decision(
        self,
        action_kind: str,
        action_name: str,
        metadata: dict[str, Any],
    ) -> ApprovalDecision | None:
        if action_kind not in {"tool", "external_agent"}:
            return None
        if action_kind == "tool" and action_name not in {"file_read", "file_write", "file_edit", "file_delete"}:
            return None
        candidates = self._memory_path_candidates(metadata)
        if not candidates:
            return None
        try:
            memory_root = (Path(get_opc_home()) / "memory").resolve()
        except Exception:
            return None
        for candidate in candidates:
            try:
                resolved = Path(candidate).expanduser().resolve()
            except Exception:
                return None
            if not (resolved == memory_root or memory_root in resolved.parents):
                return None
        return ApprovalDecision(
            action=ApprovalAction.AUTO_APPROVE,
            risk_level=RiskLevel.LOW,
            rationale="Allowed direct agent access to canonical OpenOPC memory files.",
            confidence=0.99,
            policy_source="memory_path_policy",
            metadata=metadata,
        )

    @staticmethod
    def _memory_path_candidates(metadata: dict[str, Any]) -> list[str]:
        candidates: list[str] = []

        def _collect(value: Any) -> None:
            if isinstance(value, str):
                text = value.strip()
                if text:
                    candidates.append(text)
            elif isinstance(value, list):
                for item in value:
                    _collect(item)

        arguments = metadata.get("arguments", {})
        if isinstance(arguments, dict):
            for key in ("path", "file_path", "target", "target_path", "directory", "workspace"):
                _collect(arguments.get(key))
        for key in ("path", "file_path", "target", "target_path"):
            _collect(metadata.get(key))
        _collect(metadata.get("permission_patterns"))
        return list(dict.fromkeys(candidates))
