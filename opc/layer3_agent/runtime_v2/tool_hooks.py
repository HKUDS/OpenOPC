"""Runtime-managed tool hook bus for Native Runtime V2."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from opc.core.company_controller import CompanyRunControllerLeaseLost
from opc.layer2_organization.company_runtime_identity import is_company_runtime_task
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task
from opc.layer4_tools.file_ops import (
    FILE_MUTATION_TOOL_NAMES,
    PatchApplyError,
    file_mutation_paths,
)
from opc.layer4_tools.opaque_execution import (
    activate_approved_opaque_execution_plan,
    OpaqueExecutionEnvelopeError,
    OpaqueExecutionPlan,
    company_opaque_execution_plan_for_permit,
    company_opaque_execution_identity,
    exact_tool_call_fingerprint,
    install_opaque_execution_plan,
    reset_opaque_execution_plan,
)
from opc.layer4_tools.registry import (
    COMPANY_EFFECT_FORBIDDEN,
    COMPANY_EFFECT_NO_LOCAL_FS,
    COMPANY_EFFECT_OPAQUE_EXACT,
    COMPANY_EFFECT_RUNTIME_INTERNAL,
    COMPANY_EFFECT_STRUCTURED_PATHS,
    COMPANY_EFFECT_UNKNOWN,
)
from opc.layer4_tools.workspace_fs import (
    is_model_reserved_workspace_path,
    workspace_roots_for_task,
)


@dataclass(frozen=True)
class _CompanyToolEffectPolicy:
    """One centralized classification for effects lacking a safe capability."""

    allowed: bool
    outcome: str = ""
    reason: str = ""


def _company_tool_effect_policy(
    tool_name: str,
    arguments: dict[str, Any],
    tool_category: str,
    tool_effect_kind: str,
) -> _CompanyToolEffectPolicy:
    """Fail closed for registered effects outside an enforceable boundary.

    Structured file tools and opaque shell/Python have their own durable
    capability paths below.  ``forbidden`` marks known wrappers whose effects
    cannot yet be expressed
    through either capability.  Unknown declarations remain denied so adding
    a plugin or built-in cannot silently create a new company effect path.
    """

    name = str(tool_name or "").strip()
    _ = arguments, tool_category
    effect_kind = str(tool_effect_kind or "").strip().lower()
    if not effect_kind:
        if name in FILE_MUTATION_TOOL_NAMES:
            effect_kind = COMPANY_EFFECT_STRUCTURED_PATHS
        elif name in {"shell_exec", "python_exec"}:
            effect_kind = COMPANY_EFFECT_OPAQUE_EXACT
        elif name in {"file_read", "grep", "glob", "file_search", "list_dir"}:
            # Direct fence callers have no registry descriptor.  These are the
            # only built-in read capabilities accepted by name; the real
            # executor always supplies the ToolDefinition declaration.
            effect_kind = COMPANY_EFFECT_NO_LOCAL_FS
        else:
            effect_kind = COMPANY_EFFECT_UNKNOWN
    if effect_kind == COMPANY_EFFECT_FORBIDDEN:
        return _CompanyToolEffectPolicy(
            allowed=False,
            outcome="forbidden_company_effect",
            reason=(
                "Company tool blocked before execution because its registered "
                "effect is not covered by a durable company capability."
            ),
        )
    valid_kind = effect_kind in {
        COMPANY_EFFECT_NO_LOCAL_FS,
        COMPANY_EFFECT_STRUCTURED_PATHS,
        COMPANY_EFFECT_OPAQUE_EXACT,
        COMPANY_EFFECT_RUNTIME_INTERNAL,
    }
    kind_matches_tool = not (
        effect_kind == COMPANY_EFFECT_STRUCTURED_PATHS
        and name not in FILE_MUTATION_TOOL_NAMES
    ) and not (
        effect_kind == COMPANY_EFFECT_OPAQUE_EXACT
        and name not in {"shell_exec", "python_exec"}
    )
    if not valid_kind or not kind_matches_tool:
        return _CompanyToolEffectPolicy(
            allowed=False,
            outcome="unknown_company_effect_blocked",
            reason=(
                "Company tool blocked before execution because its registered "
                "effect has no enforceable company capability declaration."
            ),
        )
    return _CompanyToolEffectPolicy(allowed=True)


@dataclass(frozen=True)
class CompanyControllerToolCredential:
    """Immutable controller generation captured at one tool-effect boundary."""

    run_id: str
    project_id: str
    owner_token: str
    generation: int
    attempt_seq: int


class RuntimeCompanyControllerToolFence:
    """Fence every NativeRuntimeV2 tool effect with its durable run lease.

    This is deliberately separate from the permission hook bus: company
    collaboration tools can be auto-approved and runtime-managed tools bypass
    the registry approval callback.  The executor calls this single boundary
    immediately around whichever handler actually performs the effect.
    """

    def __init__(self, *, store: Any = None) -> None:
        self.store = store

    @staticmethod
    def _credential_for_task(task: Any) -> CompanyControllerToolCredential | None:
        if task is None:
            return None
        metadata = dict(getattr(task, "metadata", {}) or {})
        token_key = "company_run_controller_owner_token"
        generation_key = "company_run_controller_lease_generation"
        run_id = str(metadata.get("delegation_run_id", "") or "").strip()
        if not is_company_runtime_task(task):
            return None
        owner_token = str(metadata.get(token_key, "") or "").strip()
        project_id = (
            str(getattr(task, "project_id", "") or "default").strip() or "default"
        )
        try:
            generation = int(metadata.get(generation_key, 0) or 0)
        except (TypeError, ValueError):
            generation = 0
        try:
            attempt_seq = int(
                metadata.get("claimed_work_item_attempt_seq", 0) or 0
            )
        except (TypeError, ValueError):
            attempt_seq = 0
        if not run_id or not owner_token or generation <= 0 or attempt_seq <= 0:
            raise CompanyRunControllerLeaseLost(
                "company tool effect has an incomplete controller credential"
            )
        return CompanyControllerToolCredential(
            run_id=run_id,
            project_id=project_id,
            owner_token=owner_token,
            generation=generation,
            attempt_seq=attempt_seq,
        )

    async def _assert_current(
        self,
        credential: CompanyControllerToolCredential,
    ) -> None:
        validate = getattr(
            self.store,
            "delegation_run_controller_lease_is_current",
            None,
        )
        if not callable(validate):
            raise CompanyRunControllerLeaseLost(
                "company tool effect cannot validate its controller credential"
            )
        try:
            current = await validate(
                credential.run_id,
                project_id=credential.project_id,
                owner_token=credential.owner_token,
                generation=credential.generation,
            )
        except CompanyRunControllerLeaseLost:
            raise
        except Exception as exc:
            raise CompanyRunControllerLeaseLost(
                "company tool effect could not validate its controller credential"
            ) from exc
        if not current:
            raise CompanyRunControllerLeaseLost(
                "company tool effect lost its durable controller generation"
            )

    @staticmethod
    def _ownership_blocked_result(
        *,
        credential: CompanyControllerToolCredential,
        work_item_id: str,
        runtime_task_id: str,
        outcome: str,
        reason: str,
        path_keys: tuple[str, ...] = (),
        conflicting_path: str = "",
        conflicting_work_item_id: str = "",
        conflicting_role_id: str = "",
    ) -> dict[str, Any]:
        detail = str(reason or "artifact ownership could not be validated").strip()
        return {
            "success": False,
            "error": (
                "Company artifact mutation blocked before the filesystem "
                f"effect: {detail}. Read the artifact instead, or ask its "
                "owning WorkItem to make the change."
            ),
            "artifact_ownership": {
                "outcome": str(outcome or "denied").strip(),
                "project_id": credential.project_id,
                "run_id": credential.run_id,
                "work_item_id": work_item_id,
                "runtime_task_id": runtime_task_id,
                "path_keys": list(path_keys),
                "conflicting_path": conflicting_path,
                "conflicting_work_item_id": conflicting_work_item_id,
                "conflicting_role_id": conflicting_role_id,
            },
        }

    async def _claim_file_mutation(
        self,
        *,
        credential: CompanyControllerToolCredential,
        task: Any,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any] | None:
        if tool_name not in FILE_MUTATION_TOOL_NAMES:
            return None
        work_item_id = linked_work_item_id_for_task(task)
        runtime_task_id = str(getattr(task, "id", "") or "").strip()
        task_metadata = dict(getattr(task, "metadata", {}) or {})
        subagent_run_id = str(
            task_metadata.get("_comms_endpoint_id", "") or ""
        ).strip()
        subagent_parent_task_id = str(
            getattr(task, "parent_id", "") or ""
        ).strip()
        if subagent_run_id:
            # Native child Tasks are not themselves linked to a WorkItem.
            # The Store resolves their exact durable subagent -> parent Task ->
            # WorkItem chain, so an accidentally copied link can never select
            # the direct-Task branch.
            work_item_id = ""
        if not runtime_task_id or not (work_item_id or subagent_run_id):
            return self._ownership_blocked_result(
                credential=credential,
                work_item_id=work_item_id,
                runtime_task_id=runtime_task_id,
                outcome="invalid_identity",
                reason="company file mutation lacks a durable Task-to-WorkItem link",
            )
        try:
            raw_paths = file_mutation_paths(tool_name, arguments)
        except (PatchApplyError, TypeError, ValueError) as exc:
            return self._ownership_blocked_result(
                credential=credential,
                work_item_id=work_item_id,
                runtime_task_id=runtime_task_id,
                outcome="invalid_path",
                reason=str(exc),
            )
        if not raw_paths:
            return None
        if any(is_model_reserved_workspace_path(path) for path in raw_paths):
            return self._ownership_blocked_result(
                credential=credential,
                work_item_id=work_item_id,
                runtime_task_id=runtime_task_id,
                outcome="invalid_path",
                reason="path belongs to the runtime-internal workspace namespace",
            )
        actual_workspace_root, actual_output_root = workspace_roots_for_task(task)
        if not actual_workspace_root or not actual_output_root:
            return self._ownership_blocked_result(
                credential=credential,
                work_item_id=work_item_id,
                runtime_task_id=runtime_task_id,
                outcome="invalid_path",
                reason="filesystem effect has no exact workspace capability",
            )
        claim = getattr(
            self.store,
            "claim_company_artifact_paths_for_controller",
            None,
        )
        if not callable(claim):
            return self._ownership_blocked_result(
                credential=credential,
                work_item_id=work_item_id,
                runtime_task_id=runtime_task_id,
                outcome="unavailable",
                reason="durable artifact ownership Store capability is unavailable",
            )
        try:
            receipt = await claim(
                project_id=credential.project_id,
                run_id=credential.run_id,
                work_item_id=work_item_id,
                runtime_task_id=runtime_task_id,
                subagent_run_id=subagent_run_id,
                subagent_parent_task_id=subagent_parent_task_id,
                actual_workspace_root=actual_workspace_root,
                actual_output_root=actual_output_root,
                owner_token=credential.owner_token,
                generation=credential.generation,
                attempt_seq=credential.attempt_seq,
                raw_paths=list(raw_paths),
            )
        except CompanyRunControllerLeaseLost:
            raise
        except Exception:
            return self._ownership_blocked_result(
                credential=credential,
                work_item_id=work_item_id,
                runtime_task_id=runtime_task_id,
                outcome="unavailable",
                reason="durable artifact ownership validation failed",
            )
        if bool(getattr(receipt, "claimed", False)):
            return None
        durable_work_item_id = str(
            getattr(receipt, "work_item_id", "") or work_item_id
        ).strip()
        durable_runtime_task_id = str(
            getattr(receipt, "runtime_task_id", "") or runtime_task_id
        ).strip()
        return self._ownership_blocked_result(
            credential=credential,
            work_item_id=durable_work_item_id,
            runtime_task_id=durable_runtime_task_id,
            outcome=str(getattr(receipt, "outcome", "denied") or "denied"),
            reason=str(
                getattr(receipt, "reason", "")
                or "artifact path is not owned by this WorkItem"
            ),
            path_keys=tuple(getattr(receipt, "path_keys", ()) or ()),
            conflicting_path=str(
                getattr(receipt, "conflicting_path", "") or ""
            ),
            conflicting_work_item_id=str(
                getattr(receipt, "conflicting_work_item_id", "") or ""
            ),
            conflicting_role_id=str(
                getattr(receipt, "conflicting_role_id", "") or ""
            ),
        )

    async def _authorize_opaque_effect(
        self,
        *,
        credential: CompanyControllerToolCredential,
        task: Any,
        tool_name: str,
        arguments: dict[str, Any],
        tool_call: dict[str, Any],
        execution_plan: OpaqueExecutionPlan,
    ) -> dict[str, Any] | None:
        if tool_name not in {"shell_exec", "python_exec"}:
            return None

        work_item_id = linked_work_item_id_for_task(task)
        runtime_task_id = str(getattr(task, "id", "") or "").strip()
        task_metadata = dict(getattr(task, "metadata", {}) or {})
        subagent_run_id = str(
            task_metadata.get("_comms_endpoint_id", "") or ""
        ).strip()
        subagent_parent_task_id = str(
            getattr(task, "parent_id", "") or ""
        ).strip()
        if subagent_run_id:
            work_item_id = ""
        permit = tool_call.get("_approval_permit")
        auto_sign = dict(tool_call.get("_native_permission_auto_sign", {}) or {})
        auto_approved = bool(auto_sign)
        if auto_approved:
            runtime_session_id = str(
                tool_call.get("_runtime_session_id", "") or ""
            ).strip()
            fingerprint = exact_tool_call_fingerprint(
                tool_call_id=str(tool_call.get("id", "") or "").strip(),
                tool_name=tool_name,
                arguments=arguments,
                runtime_session_id=runtime_session_id,
                execution_envelope=execution_plan.envelope,
                execution_identity=company_opaque_execution_identity(task),
            )
            permit = {
                "id": str(tool_call.get("id", "") or "").strip(),
                "function": tool_name,
                "arguments": dict(arguments or {}),
                "execution_envelope": dict(execution_plan.envelope or {}),
                "execution_identity": company_opaque_execution_identity(task),
                "fingerprint": fingerprint,
                "runtime_session_id": runtime_session_id,
                "decision": "native_policy_allow",
                "approved": True,
                "state": "executing",
                "native_approval_level": str(auto_sign.get("level", "") or ""),
                "native_policy_source": str(
                    auto_sign.get("policy_source", "") or ""
                ),
                "native_permission_scope_id": str(auto_sign.get("scope_id", "") or ""),
            }
        claim = getattr(
            self.store,
            "authorize_company_opaque_tool_effect_for_controller",
            None,
        )
        if not callable(claim) or not isinstance(permit, dict):
            return {
                "success": False,
                "error": (
                    "Company opaque tool effect blocked before execution: "
                    "this exact ToolCall has no durable one-shot human permit."
                ),
                "opaque_tool_permission": {"outcome": "invalid_permit"},
            }
        try:
            receipt = await claim(
                project_id=credential.project_id,
                run_id=credential.run_id,
                work_item_id=work_item_id,
                runtime_task_id=runtime_task_id,
                subagent_run_id=subagent_run_id,
                subagent_parent_task_id=subagent_parent_task_id,
                owner_token=credential.owner_token,
                generation=credential.generation,
                attempt_seq=credential.attempt_seq,
                tool_call_id=str(tool_call.get("id", "") or "").strip(),
                tool_name=tool_name,
                arguments=arguments,
                execution_envelope=execution_plan.envelope,
                execution_identity=company_opaque_execution_identity(task),
                permit=dict(permit),
                auto_approved=auto_approved,
            )
        except CompanyRunControllerLeaseLost:
            raise
        except Exception:
            return {
                "success": False,
                "error": (
                    "Company opaque tool effect blocked because its durable "
                    "one-shot permit could not be validated."
                ),
                "opaque_tool_permission": {"outcome": "unavailable"},
            }
        if bool(getattr(receipt, "authorized", False)):
            return None
        return {
            "success": False,
            "error": (
                "Company opaque tool effect blocked before execution: "
                + str(
                    getattr(receipt, "reason", "")
                    or "exact one-shot human permit is not current"
                )
            ),
            "opaque_tool_permission": {
                "outcome": str(getattr(receipt, "outcome", "invalid_permit")),
                "work_item_id": str(getattr(receipt, "work_item_id", "") or ""),
                "runtime_task_id": str(
                    getattr(receipt, "runtime_task_id", "") or ""
                ),
                "fingerprint": str(getattr(receipt, "fingerprint", "") or ""),
            },
        }

    async def run(
        self,
        *,
        task: Any,
        tool_name: str = "",
        tool_category: str = "",
        tool_effect_kind: str = "",
        arguments: dict[str, Any] | None = None,
        tool_call: dict[str, Any] | None = None,
        effect: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Execute one handler effect only while its captured lease is live."""

        credential = self._credential_for_task(task)
        if credential is None:
            return await effect()
        await self._assert_current(credential)
        normalized_tool_name = str(tool_name or "").strip()
        normalized_arguments = dict(arguments or {})
        effect_policy = _company_tool_effect_policy(
            normalized_tool_name,
            normalized_arguments,
            tool_category,
            tool_effect_kind,
        )
        if not effect_policy.allowed:
            return {
                "success": False,
                "error": effect_policy.reason,
                "opaque_tool_permission": {
                    "outcome": effect_policy.outcome,
                },
            }
        execution_plan: OpaqueExecutionPlan | None = None
        normalized_tool_call = dict(tool_call or {})
        sandbox_override = dict(
            normalized_tool_call.get("_sandbox_override", {}) or {}
        )
        if normalized_tool_name in {"shell_exec", "python_exec"}:
            try:
                permit = dict(
                    normalized_tool_call.get("_approval_permit", {}) or {}
                )
                execution_plan = company_opaque_execution_plan_for_permit(
                    task,
                    normalized_tool_name,
                    normalized_arguments,
                    permit_envelope=dict(
                        permit.get("execution_envelope", {}) or {}
                    ),
                    sandbox_override=sandbox_override,
                )
            except (OpaqueExecutionEnvelopeError, TypeError, ValueError) as exc:
                return {
                    "success": False,
                    "error": (
                        "Company opaque tool effect blocked because its exact "
                        f"launch envelope could not be resolved: {exc}"
                    ),
                    "opaque_tool_permission": {"outcome": "invalid_envelope"},
                }
        opaque_block = await self._authorize_opaque_effect(
            credential=credential,
            task=task,
            tool_name=normalized_tool_name,
            arguments=normalized_arguments,
            tool_call=normalized_tool_call,
            execution_plan=execution_plan,
        )
        if opaque_block is not None:
            return opaque_block
        if execution_plan is not None and sandbox_override:
            execution_plan = activate_approved_opaque_execution_plan(
                execution_plan,
                sandbox_override=sandbox_override,
            )
        ownership_block = await self._claim_file_mutation(
            credential=credential,
            task=task,
            tool_name=normalized_tool_name,
            arguments=normalized_arguments,
        )
        if ownership_block is not None:
            return ownership_block
        plan_token = (
            install_opaque_execution_plan(execution_plan)
            if execution_plan is not None
            else None
        )
        try:
            try:
                result = await effect()
            except BaseException:
                # A handler may have crossed its external-effect boundary before
                # raising.  Prefer the lease-loss signal when a takeover occurred;
                # otherwise preserve the handler's original control flow.
                await self._assert_current(credential)
                raise
            await self._assert_current(credential)
            return result
        finally:
            if plan_token is not None:
                reset_opaque_execution_plan(plan_token)


@dataclass
class RuntimeToolHookContext:
    phase: str
    tool_name: str
    call: dict[str, Any]
    task: Any = None
    tool: Any = None
    arguments: dict[str, Any] = field(default_factory=dict)
    predicted_permission: Any = None
    result: dict[str, Any] | None = None
    state: dict[str, Any] = field(default_factory=dict)


RuntimeToolHook = Callable[[RuntimeToolHookContext], Awaitable[Optional[dict[str, Any]]]]
RuntimeHookEmitter = Callable[[str, dict[str, Any]], Awaitable[None]]


class RuntimeToolHookBus:
    """Composable pre/post/failure hook bus for runtime-managed tool execution."""

    def __init__(self, *, emit_event: RuntimeHookEmitter | None = None) -> None:
        self.emit_event = emit_event
        self._pre_hooks: list[tuple[str, RuntimeToolHook]] = []
        self._post_hooks: list[tuple[str, RuntimeToolHook]] = []
        self._failure_hooks: list[tuple[str, RuntimeToolHook]] = []

    def register_pre_hook(self, name: str, hook: RuntimeToolHook) -> None:
        self._pre_hooks.append((name, hook))

    def register_post_hook(self, name: str, hook: RuntimeToolHook) -> None:
        self._post_hooks.append((name, hook))

    def register_failure_hook(self, name: str, hook: RuntimeToolHook) -> None:
        self._failure_hooks.append((name, hook))

    async def run_pre_hooks(self, context: RuntimeToolHookContext) -> RuntimeToolHookContext:
        return await self._run_hooks(self._pre_hooks, context)

    async def run_post_hooks(self, context: RuntimeToolHookContext) -> RuntimeToolHookContext:
        return await self._run_hooks(self._post_hooks, context)

    async def run_failure_hooks(self, context: RuntimeToolHookContext) -> RuntimeToolHookContext:
        return await self._run_hooks(self._failure_hooks, context)

    async def _run_hooks(
        self,
        hooks: list[tuple[str, RuntimeToolHook]],
        context: RuntimeToolHookContext,
    ) -> RuntimeToolHookContext:
        for hook_name, hook in hooks:
            patch = await hook(context) or {}
            self._apply_patch(context, patch)
            if self.emit_event:
                await self.emit_event(
                    "tool_hook",
                    {
                        "phase": context.phase,
                        "tool_name": context.tool_name,
                        "tool_call_id": context.call.get("id", ""),
                        "hook_name": hook_name,
                        "stopped": bool(context.state.get("stop_execution")),
                        "result_overridden": context.result is not None,
                    },
                )
            if context.state.get("stop_execution"):
                break
        return context

    @staticmethod
    def _apply_patch(context: RuntimeToolHookContext, patch: dict[str, Any]) -> None:
        if not patch:
            return
        if isinstance(patch.get("arguments"), dict):
            context.arguments = dict(patch["arguments"])
        if isinstance(patch.get("result"), dict):
            context.result = dict(patch["result"])
        if isinstance(patch.get("metadata"), dict):
            context.state.setdefault("metadata", {}).update(dict(patch["metadata"]))
        if isinstance(patch.get("approval"), dict):
            context.state.setdefault("approval", {}).update(dict(patch["approval"]))
        if "stop_execution" in patch:
            context.state["stop_execution"] = bool(patch["stop_execution"])
        if "stop_batch_on_failure" in patch:
            context.state["stop_batch_on_failure"] = bool(patch["stop_batch_on_failure"])
        if "prevent_continuation" in patch:
            context.state["prevent_continuation"] = bool(patch["prevent_continuation"])
        if patch.get("stop_reason"):
            context.state["stop_reason"] = str(patch["stop_reason"])
