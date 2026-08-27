"""Durable coordination for user decisions.

The database checkpoint is the source of truth.  Process-local futures only
avoid polling while the runtime that opened a checkpoint is still alive; a
missed notification or process restart never loses the accepted decision.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

from opc.core.models import (
    ExecutionCheckpoint,
    ExecutionCheckpointClaimReceipt,
    ExecutionCheckpointConsumptionReceipt,
    ExecutionCheckpointDecisionReceipt,
    ReorgDecisionReceipt,
    ReorgProposal,
)
from opc.core.interaction_protocol import (
    OWNER_INTERACTION_CHECKPOINT_TYPES,
    canonical_company_work_item_gate_decision,
)
from opc.database.store import OPCStore


PresentationCallback = Callable[
    [str, list[dict[str, str]]],
    Awaitable[str | None],
]
CheckpointChangedCallback = Callable[[ExecutionCheckpoint], Awaitable[None]]
OrphanedAnswerCallback = Callable[[str, str], Awaitable[None] | None]


logger = logging.getLogger(__name__)
T = TypeVar("T")


class InteractionLeaseLost(RuntimeError):
    """The durable interaction claim can no longer be kept by this consumer."""


class InteractionEffectFailed(RuntimeError):
    """A claimed domain effect failed after crossing its durable start fence."""


@dataclass(frozen=True)
class InteractionDecisionLease:
    """One claimed, durable decision ready for domain consumption."""

    checkpoint: ExecutionCheckpoint
    decision: dict[str, Any]
    consumer_id: str
    claim_id: str


class InteractionCoordinator:
    """One durable ingress and wake-up point for every user decision."""

    def __init__(
        self,
        *,
        store: OPCStore,
        project_id: str,
        presentation_callback: PresentationCallback | None = None,
        checkpoint_changed_callback: CheckpointChangedCallback | None = None,
        orphaned_answer_callback: OrphanedAnswerCallback | None = None,
    ) -> None:
        self.store = store
        self.project_id = str(project_id or "default").strip() or "default"
        self.presentation_callback = presentation_callback
        self.checkpoint_changed_callback = checkpoint_changed_callback
        self.orphaned_answer_callback = orphaned_answer_callback
        self._waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._lease_heartbeat_tasks: dict[
            tuple[str, str, str], asyncio.Task[None]
        ] = {}
        self._lease_loss_events: dict[tuple[str, str, str], asyncio.Event] = {}
        self._lease_loss_reasons: dict[tuple[str, str, str], str] = {}

    @staticmethod
    def decision_hash(decision: dict[str, Any]) -> str:
        encoded = json.dumps(
            dict(decision or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def validate_decision(
        checkpoint: ExecutionCheckpoint,
        decision: dict[str, Any],
    ) -> str:
        """Return a stable rejection reason, or ``""`` for a valid answer."""

        if not isinstance(decision, dict) or not any(
            value not in (None, "", [], {}) for value in decision.values()
        ):
            return "empty_decision"
        checkpoint_type = str(checkpoint.checkpoint_type or "").strip()
        if checkpoint_type not in OWNER_INTERACTION_CHECKPOINT_TYPES:
            return "unsupported_checkpoint_type"
        text = str(decision.get("text", "") or "").strip()

        def structured_answers_are_valid() -> bool:
            answers = decision.get("user_input_answers")
            if answers is None:
                return False
            if not isinstance(answers, dict) or not answers:
                return False
            for question_id, raw_answer in answers.items():
                if not str(question_id or "").strip() or not isinstance(raw_answer, dict):
                    return False
                if not any(
                    str(raw_answer.get(key, "") or "").strip()
                    for key in ("answer_text", "freeform_text", "selected_option_id")
                ):
                    return False
            return True

        def staffing_selections_are_valid(*, required: bool) -> bool:
            selections = decision.get("staffing_selections")
            if selections is None:
                return not required
            if not isinstance(selections, dict) or (required and not selections):
                return False
            for role_id, raw_selection in selections.items():
                if not str(role_id or "").strip() or not isinstance(raw_selection, dict):
                    return False
                kind = str(raw_selection.get("kind", "") or "").strip()
                selected_id = str(
                    raw_selection.get("id")
                    or raw_selection.get("employee_id")
                    or raw_selection.get("template_id")
                    or ""
                ).strip()
                if kind not in {"employee", "template", "fallback"}:
                    return False
                if kind in {"employee", "template"} and not selected_id:
                    return False
            return True

        if checkpoint_type in {"tool_permission", "action_permission"}:
            option_id = str(decision.get("option_id", "") or "").strip()
            interaction = dict((checkpoint.payload or {}).get("interaction", {}) or {})
            allowed_options = {
                str(option.get("id", "") or "").strip()
                for option in list(interaction.get("options", []) or [])
                if isinstance(option, dict) and str(option.get("id", "") or "").strip()
            }
            if not option_id:
                return "option_id_required"
            if not allowed_options or option_id not in allowed_options:
                return "invalid_option_id"
            return ""
        if checkpoint_type in {
            "task_user_input",
            "route_clarification",
            "company_runtime_selection",
        }:
            answers_present = "user_input_answers" in decision
            if answers_present and not structured_answers_are_valid():
                return "invalid_user_input_answers"
            if not text and not structured_answers_are_valid():
                return "input_required"
            return ""
        if checkpoint_type == "company_work_item_gate":
            if not canonical_company_work_item_gate_decision(decision):
                return "invalid_gate_action"
            return ""
        if checkpoint_type == "company_delivery_feedback":
            action = str(
                decision.get("checkpoint_reply_kind")
                or decision.get("option_id")
                or ""
            ).strip().lower()
            if action not in {"approve", "feedback", "ignore"}:
                return "invalid_delivery_action"
            if action == "feedback" and not str(
                decision.get("human_feedback_text")
                or decision.get("text")
                or ""
            ).strip():
                return "feedback_text_required"
            return ""
        if checkpoint_type == "company_staffing_selection":
            action = str(decision.get("staffing_action", "") or "").strip().lower()
            if action not in {"manual_approve", "auto_recruit", "deny"}:
                return "invalid_staffing_action"
            if not staffing_selections_are_valid(required=action == "manual_approve"):
                return "invalid_staffing_selections"
            role_agents = decision.get("recruitment_role_agents")
            if role_agents is not None and (
                not isinstance(role_agents, dict)
                or any(
                    not str(role_id or "").strip() or not str(agent or "").strip()
                    for role_id, agent in role_agents.items()
                )
            ):
                return "invalid_recruitment_role_agents"
            return ""
        if checkpoint_type == "company_recruitment_confirmation":
            action = str(decision.get("checkpoint_reply_kind", "") or "").strip().lower()
            if action not in {"approve", "deny", "feedback"}:
                return "invalid_recruitment_action"
            if action == "feedback" and not text:
                return "feedback_text_required"
            if not staffing_selections_are_valid(required=False):
                return "invalid_staffing_selections"
            role_agents = decision.get("recruitment_role_agents")
            if role_agents is not None and (
                not isinstance(role_agents, dict)
                or any(
                    not str(role_id or "").strip() or not str(agent or "").strip()
                    for role_id, agent in role_agents.items()
                )
            ):
                return "invalid_recruitment_role_agents"
            return ""
        if checkpoint_type == "company_reorg_pending":
            action = str(
                decision.get("option_id")
                or decision.get("checkpoint_reply_kind")
                or text
            ).strip().lower()
            approved = {"y", "yes", "ok", "okay", "approve", "approved", "confirm", "continue", "proceed", "go"}
            denied = {"n", "no", "deny", "denied", "reject", "rejected", "stop", "cancel", "abort"}
            if action not in approved | denied:
                return "invalid_reorg_action"
            return ""
        if checkpoint_type == "company_run_failure_review":
            action = str(
                decision.get("checkpoint_reply_kind")
                or decision.get("option_id")
                or text
            ).strip().lower()
            if action not in {
                "acknowledge",
                "dismiss",
                "ignore",
                "close",
                "ok",
                "okay",
                "知道了",
                "关闭",
            }:
                return "invalid_failure_review_action"
            return ""
        return "unsupported_checkpoint_type"

    async def _notify_changed(self, checkpoint: ExecutionCheckpoint | None) -> None:
        """Best-effort refresh hint; durable transitions never depend on it."""

        if checkpoint is None or self.checkpoint_changed_callback is None:
            return
        try:
            await self.checkpoint_changed_callback(checkpoint)
        except Exception:
            logger.exception(
                "interaction checkpoint refresh callback failed for %s",
                checkpoint.checkpoint_id,
            )

    async def open_and_wait(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        prompt: str,
        options: list[dict[str, str]],
        consumer_id: str,
        lease_seconds: float = 300.0,
    ) -> InteractionDecisionLease:
        """Persist *checkpoint*, wait for its answer, then claim it.

        The waiter is installed before the row is published so an immediate
        reply cannot race past the live runtime.  A CLI presentation callback
        is an adapter only: its result is submitted through the same durable
        Store transition as an Office/HTTP reply.
        """

        checkpoint.project_id = (
            str(checkpoint.project_id or self.project_id).strip() or self.project_id
        )
        checkpoint_id = str(checkpoint.checkpoint_id or "").strip()
        if not checkpoint_id:
            raise ValueError("checkpoint_id is required")
        if checkpoint.checkpoint_type not in OWNER_INTERACTION_CHECKPOINT_TYPES:
            raise ValueError("open_and_wait only accepts owner interaction checkpoints")
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[dict[str, Any]] = loop.create_future()
        if checkpoint_id in self._waiters:
            raise RuntimeError(f"interaction waiter already exists for {checkpoint_id}")
        self._waiters[checkpoint_id] = waiter
        lease_acquired = False
        try:
            interaction = dict((checkpoint.payload or {}).get("interaction", {}) or {})
            interaction_key = str(interaction.get("domain_key", "") or "").strip()
            if not interaction_key:
                raise ValueError(
                    "owner interaction requires a durable interaction.domain_key"
                )
            persisted, created = (
                await self.store.create_owner_interaction_checkpoint(
                    checkpoint,
                    interaction_key=interaction_key,
                )
            )
            persisted_id = str(persisted.checkpoint_id or "").strip()
            if persisted_id and persisted_id != checkpoint_id:
                self._waiters.pop(checkpoint_id, None)
                if persisted_id in self._waiters:
                    raise RuntimeError(
                        f"interaction waiter already exists for {persisted_id}"
                    )
                checkpoint_id = persisted_id
                self._waiters[checkpoint_id] = waiter
            if not created:
                if str(persisted.status or "").strip() != "pending":
                    raise RuntimeError(
                        f"interaction checkpoint {checkpoint_id} already exists in "
                        f"state {persisted.status}"
                    )
                if not self._same_immutable_identity(persisted, checkpoint):
                    raise RuntimeError(
                        f"interaction checkpoint {checkpoint_id} identity conflict"
                    )
            checkpoint = persisted
            await self._notify_changed(checkpoint)

            if self.presentation_callback is not None:
                reply = await self.presentation_callback(prompt, list(options or []))
                if reply is not None:
                    presentation_decision = {
                        "option_id": str(reply),
                        "text": str(reply),
                    }
                    presentation_receipt = await self.submit(
                        checkpoint_id=checkpoint_id,
                        checkpoint_type=checkpoint.checkpoint_type,
                        decision=presentation_decision,
                        client_request_id=f"presentation:{uuid.uuid4().hex}",
                        checkpoint=checkpoint,
                    )
                    if not presentation_receipt.acknowledged:
                        raise ValueError(
                            "presentation callback returned an invalid decision: "
                            f"{presentation_receipt.outcome}"
                        )

            # Polling is the correctness path for replies accepted by another
            # controller/process; the local Future only removes latency when
            # submit and waiter happen to share this coordinator.
            while True:
                current = await self.store.get_execution_checkpoint(
                    checkpoint_id,
                    project_id=checkpoint.project_id,
                    checkpoint_type=checkpoint.checkpoint_type,
                )
                current_decision = self._decision_value(current)
                if current_decision is not None:
                    if not waiter.done():
                        waiter.set_result(current_decision)
                    break
                if current is None:
                    raise RuntimeError(
                        f"interaction checkpoint {checkpoint_id} disappeared while waiting"
                    )
                if str(current.status or "").strip() != "pending":
                    raise RuntimeError(
                        f"interaction checkpoint {checkpoint_id} entered "
                        f"{current.status} without a decision"
                    )
                done, _ = await asyncio.wait({waiter}, timeout=0.2)
                if done:
                    break
            decision = waiter.result()
            claim = await self.store.claim_answered_execution_checkpoint(
                checkpoint_id,
                project_id=checkpoint.project_id,
                checkpoint_type=checkpoint.checkpoint_type,
                consumer_id=consumer_id,
                lease_seconds=lease_seconds,
            )
            if not claim.acquired or claim.checkpoint is None:
                raise RuntimeError(
                    f"could not claim answered checkpoint {checkpoint_id}: {claim.outcome}"
                )
            lease_acquired = True
            await self._notify_changed(claim.checkpoint)
            lease = InteractionDecisionLease(
                checkpoint=claim.checkpoint,
                decision=dict(decision or {}),
                consumer_id=consumer_id,
                claim_id=claim.claim_id,
            )
            self.start_lease_heartbeat(lease, lease_seconds=lease_seconds)
            return lease
        finally:
            current_waiter = self._waiters.get(checkpoint_id)
            if current_waiter is waiter:
                self._waiters.pop(checkpoint_id, None)
            if not lease_acquired and self.orphaned_answer_callback is not None:
                # Submit deliberately suppresses the generic consumer while a
                # live waiter is registered.  If that waiter is cancelled (or
                # loses the claim race) after pending→answered, hand the durable
                # row to recovery immediately instead of waiting for restart.
                current = await self.store.get_execution_checkpoint(
                    checkpoint_id,
                    project_id=checkpoint.project_id,
                    checkpoint_type=checkpoint.checkpoint_type,
                )
                if current is not None and str(current.status or "") == "answered":
                    result = self.orphaned_answer_callback(
                        checkpoint_id,
                        checkpoint.checkpoint_type,
                    )
                    if asyncio.iscoroutine(result):
                        await result

    async def submit(
        self,
        *,
        checkpoint_id: str,
        checkpoint_type: str,
        decision: dict[str, Any],
        client_request_id: str,
        checkpoint: ExecutionCheckpoint | None = None,
    ) -> ExecutionCheckpointDecisionReceipt:
        """Durably accept one answer and wake its live consumer, if any."""

        normalized = dict(decision or {})
        if checkpoint is None:
            checkpoint = await self.store.get_execution_checkpoint(
                checkpoint_id,
                project_id=self.project_id,
                checkpoint_type=checkpoint_type,
            )
        if checkpoint is None:
            return ExecutionCheckpointDecisionReceipt(outcome="not_found")
        validation_error = self.validate_decision(checkpoint, normalized)
        if validation_error:
            return ExecutionCheckpointDecisionReceipt(
                outcome=validation_error,
                checkpoint=checkpoint,
            )
        receipt = await self.store.accept_execution_checkpoint_decision(
            checkpoint_id,
            project_id=self.project_id,
            checkpoint_type=checkpoint_type,
            request_id=client_request_id,
            decision_hash=self.decision_hash(normalized),
            decision=normalized,
        )
        live_waiter_notified = False
        if receipt.acknowledged:
            value = self._decision_value(receipt.checkpoint)
            waiter = self._waiters.get(str(checkpoint_id or "").strip())
            live_waiter_notified = bool(
                waiter is not None and not waiter.cancelled()
            )
            if value is not None and waiter is not None and not waiter.done():
                waiter.set_result(value)
            await self._notify_changed(receipt.checkpoint)
        if live_waiter_notified:
            return ExecutionCheckpointDecisionReceipt(
                outcome=receipt.outcome,
                checkpoint=receipt.checkpoint,
                live_waiter_notified=True,
            )
        return receipt

    async def publish_owner_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
        *,
        interaction_key: str,
        supersede_pending_scope: bool = True,
        supersession_key: str | None = None,
        supersession_order: list[int] | tuple[int, ...] | None = None,
    ) -> tuple[ExecutionCheckpoint, bool]:
        """Publish one owner interaction and emit the unified refresh hint."""

        persisted, created = await self.store.publish_owner_interaction_checkpoint(
            checkpoint,
            interaction_key=interaction_key,
            supersede_pending_scope=supersede_pending_scope,
            supersession_key=supersession_key,
            supersession_order=supersession_order,
        )
        await self._notify_changed(persisted)
        return persisted, created

    async def notify_persisted_owner_checkpoint(
        self,
        checkpoint: ExecutionCheckpoint,
    ) -> None:
        """Emit the normal refresh hint for a row committed by a wider tx.

        Some domain transitions, notably final company delivery, publish the
        owner checkpoint inside the same Store transaction as their business
        state.  They call this only after that transaction commits; consumers
        still reload the canonical row and no second publication is attempted.
        """

        if checkpoint.checkpoint_type not in OWNER_INTERACTION_CHECKPOINT_TYPES:
            raise ValueError("owner checkpoint notification requires an owner type")
        if (str(checkpoint.project_id or "default").strip() or "default") != self.project_id:
            raise ValueError("owner checkpoint notification crossed project scope")
        await self._notify_changed(checkpoint)

    async def publish_reorg_proposal(
        self,
        proposal: ReorgProposal,
        checkpoint: ExecutionCheckpoint,
    ) -> tuple[ReorgProposal, bool, ExecutionCheckpoint, bool]:
        """Atomically publish a confirmation-required proposal and owner card."""

        persisted, created, persisted_checkpoint, checkpoint_created = (
            await self.store.create_reorg_proposal_with_interaction(
                proposal,
                owner_checkpoint=checkpoint,
            )
        )
        if persisted_checkpoint is None:
            raise RuntimeError("confirmation-required reorg has no owner checkpoint")
        await self._notify_changed(persisted_checkpoint)
        return persisted, created, persisted_checkpoint, checkpoint_created

    async def decide_reorg_proposal(
        self,
        proposal_id: str,
        *,
        approved: bool,
        notes: str = "",
        lease: InteractionDecisionLease | None = None,
    ) -> ReorgDecisionReceipt:
        """Bind a required reorg decision to its claimed owner interaction."""

        return await self.store.decide_reorg_proposal(
            proposal_id,
            approved=approved,
            notes=notes,
            checkpoint_id=(lease.checkpoint.checkpoint_id if lease else ""),
            checkpoint_claim_id=(lease.claim_id if lease else ""),
            checkpoint_consumer_id=(lease.consumer_id if lease else ""),
        )

    async def enrich_owner_checkpoint(
        self,
        checkpoint_id: str,
        *,
        checkpoint_type: str,
        expected_statuses: list[str] | tuple[str, ...] | set[str],
        payload_patch: dict[str, Any],
    ) -> tuple[ExecutionCheckpoint | None, bool]:
        """Add whitelisted execution/migration projections without touching decisions."""

        persisted, applied = await self.store.enrich_owner_interaction_checkpoint(
            checkpoint_id,
            project_id=self.project_id,
            checkpoint_type=checkpoint_type,
            expected_statuses=expected_statuses,
            payload_patch=payload_patch,
        )
        if applied:
            await self._notify_changed(persisted)
        return persisted, applied

    async def backfill_owner_checkpoint_ownership(
        self,
        checkpoint_id: str,
        *,
        checkpoint_type: str,
        expected_statuses: list[str] | tuple[str, ...] | set[str],
        ownership: dict[str, Any],
    ) -> tuple[ExecutionCheckpoint | None, bool]:
        """Fill a missing legacy actor through the typed owner CAS path."""

        persisted, applied = await self.store.backfill_owner_interaction_ownership(
            checkpoint_id,
            project_id=self.project_id,
            checkpoint_type=checkpoint_type,
            expected_statuses=expected_statuses,
            ownership=ownership,
        )
        if applied:
            await self._notify_changed(persisted)
        return persisted, applied

    async def close_pending_owner_checkpoint(
        self,
        checkpoint_id: str,
        *,
        checkpoint_type: str,
        status: str,
        payload_patch: dict[str, Any] | None = None,
    ) -> tuple[ExecutionCheckpoint | None, bool]:
        """Administratively close only an unanswered owner interaction."""

        persisted, applied = (
            await self.store.close_pending_owner_interaction_checkpoint(
                checkpoint_id,
                project_id=self.project_id,
                checkpoint_type=checkpoint_type,
                status=status,
                payload_patch=payload_patch,
            )
        )
        if applied:
            await self._notify_changed(persisted)
        return persisted, applied

    async def claim_answered(
        self,
        *,
        checkpoint_id: str,
        checkpoint_type: str,
        consumer_id: str,
        lease_seconds: float = 300.0,
        enforce_company_controller_eligibility: bool = False,
        controller_run_id: str | None = None,
        controller_root_session_id: str | None = None,
        controller_owner_token: str | None = None,
        controller_lease_generation: int | None = None,
    ) -> ExecutionCheckpointClaimReceipt:
        receipt = await self.store.claim_answered_execution_checkpoint(
            checkpoint_id,
            project_id=self.project_id,
            checkpoint_type=checkpoint_type,
            consumer_id=consumer_id,
            lease_seconds=lease_seconds,
            enforce_company_controller_eligibility=(
                enforce_company_controller_eligibility
            ),
            controller_run_id=controller_run_id,
            controller_root_session_id=controller_root_session_id,
            controller_owner_token=controller_owner_token,
            controller_lease_generation=controller_lease_generation,
        )
        if receipt.acquired:
            await self._notify_changed(receipt.checkpoint)
            if receipt.checkpoint is not None:
                interaction = dict(
                    (receipt.checkpoint.payload or {}).get("interaction", {}) or {}
                )
                decision_record = dict(interaction.get("decision", {}) or {})
                raw_decision = decision_record.get("value")
                decision = (
                    dict(raw_decision)
                    if isinstance(raw_decision, dict)
                    else {"text": str(raw_decision or "")}
                )
                self.start_lease_heartbeat(
                    InteractionDecisionLease(
                        checkpoint=receipt.checkpoint,
                        decision=decision,
                        consumer_id=consumer_id,
                        claim_id=receipt.claim_id,
                    ),
                    lease_seconds=lease_seconds,
                )
        return receipt

    async def finish(
        self,
        lease: InteractionDecisionLease,
        *,
        final_status: str = "resolved",
        payload_patch: dict[str, Any] | None = None,
    ) -> ExecutionCheckpointConsumptionReceipt:
        receipt = await self.store.finish_execution_checkpoint_consumption(
            lease.checkpoint.checkpoint_id,
            project_id=lease.checkpoint.project_id,
            checkpoint_type=lease.checkpoint.checkpoint_type,
            claim_id=lease.claim_id,
            consumer_id=lease.consumer_id,
            final_status=final_status,
            payload_patch=payload_patch,
        )
        if receipt.applied:
            await self._notify_changed(receipt.checkpoint)
        await self.stop_lease_heartbeat(lease)
        return receipt

    async def renew(
        self,
        lease: InteractionDecisionLease,
        *,
        lease_seconds: float,
    ) -> ExecutionCheckpointClaimReceipt:
        """Renew a live domain consumer lease without changing its owner."""

        receipt = await self.store.renew_execution_checkpoint_claim(
            lease.checkpoint.checkpoint_id,
            project_id=lease.checkpoint.project_id,
            checkpoint_type=lease.checkpoint.checkpoint_type,
            claim_id=lease.claim_id,
            consumer_id=lease.consumer_id,
            lease_seconds=lease_seconds,
        )
        # Renewal does not alter the user-visible state; publishing a refresh
        # event for every heartbeat would only create UI churn.
        return receipt

    async def begin_effect(
        self,
        lease: InteractionDecisionLease,
    ) -> ExecutionCheckpointClaimReceipt:
        """Persist the non-idempotent execution boundary for this claim."""

        receipt = await self.store.begin_execution_checkpoint_effect(
            lease.checkpoint.checkpoint_id,
            project_id=lease.checkpoint.project_id,
            checkpoint_type=lease.checkpoint.checkpoint_type,
            claim_id=lease.claim_id,
            consumer_id=lease.consumer_id,
        )
        return receipt

    async def exact_tool_lease(
        self,
        permit: dict[str, Any],
    ) -> InteractionDecisionLease:
        """Resolve and verify the owner lease referenced by an exact ToolCall permit.

        Native runtimes carry only the immutable permit reference.  Loading the
        checkpoint here keeps all owner-checkpoint mutation and heartbeat
        ownership behind the coordinator boundary.
        """

        permit = dict(permit or {})
        checkpoint_id = str(permit.get("checkpoint_id", "") or "").strip()
        checkpoint_type = str(
            permit.get("checkpoint_type", "") or "tool_permission"
        ).strip()
        project_id = str(
            permit.get("checkpoint_project_id", "") or self.project_id
        ).strip() or self.project_id
        claim_id = str(permit.get("claim_id", "") or "").strip()
        consumer_id = str(permit.get("consumer_id", "") or "").strip()
        if (
            not checkpoint_id
            or checkpoint_type != "tool_permission"
            or not claim_id
            or not consumer_id
        ):
            raise ValueError("exact ToolCall permit has incomplete owner identity")
        if project_id != self.project_id:
            raise ValueError("exact ToolCall permit project does not match coordinator")
        checkpoint = await self.store.get_execution_checkpoint(
            checkpoint_id,
            project_id=project_id,
            checkpoint_type=checkpoint_type,
        )
        if checkpoint is None:
            raise InteractionLeaseLost(
                f"exact ToolCall checkpoint {checkpoint_id} no longer exists"
            )
        interaction = dict((checkpoint.payload or {}).get("interaction", {}) or {})
        recorded_claim = dict(interaction.get("claim", {}) or {})
        if (
            str(recorded_claim.get("claim_id", "") or "") != claim_id
            or str(recorded_claim.get("consumer_id", "") or "") != consumer_id
        ):
            raise InteractionLeaseLost(
                f"exact ToolCall checkpoint {checkpoint_id} claim ownership changed"
            )
        return InteractionDecisionLease(
            checkpoint=checkpoint,
            decision=self._decision_value(checkpoint) or {},
            consumer_id=consumer_id,
            claim_id=claim_id,
        )

    async def begin_exact_tool_effect(
        self,
        permit: dict[str, Any],
    ) -> ExecutionCheckpointClaimReceipt:
        """Fence an exact ToolCall immediately before the external effect."""

        return await self.store.begin_exact_tool_permission_effect(dict(permit))

    async def persist_exact_tool_result(
        self,
        permit: dict[str, Any],
        *,
        runtime_session_id: str,
        tool_name: str,
        payload: dict[str, Any],
        tool_call_id: str,
        task_id: str,
        session_id: str | None,
        message_id: str,
        metadata: dict[str, Any] | None,
        checkpoint_payload_patch: dict[str, Any] | None = None,
    ) -> ExecutionCheckpointConsumptionReceipt:
        """Atomically persist ToolResult, consume its permit, and finish its lease."""

        lease = await self.exact_tool_lease(permit)
        receipt = await self.store.persist_runtime_tool_result_and_finish_permission(
            runtime_session_id=runtime_session_id,
            tool_name=tool_name,
            payload=dict(payload or {}),
            tool_call_id=tool_call_id,
            task_id=task_id,
            session_id=session_id,
            message_id=message_id,
            metadata=dict(metadata or {}),
            fingerprint=str(permit.get("fingerprint", "") or ""),
            checkpoint_id=lease.checkpoint.checkpoint_id,
            project_id=lease.checkpoint.project_id,
            checkpoint_type=lease.checkpoint.checkpoint_type,
            claim_id=lease.claim_id,
            consumer_id=lease.consumer_id,
            checkpoint_payload_patch=checkpoint_payload_patch,
        )
        if receipt.applied:
            await self._notify_changed(receipt.checkpoint)
        await self.stop_lease_heartbeat(lease)
        return receipt

    async def settle_interrupted_exact_tool(
        self,
        permit: dict[str, Any],
        *,
        state: str,
        error_kind: str,
        payload_patch: dict[str, Any] | None = None,
    ) -> ExecutionCheckpointConsumptionReceipt:
        """Settle an interrupted exact ToolCall with fail-closed effect semantics."""

        lease = await self.exact_tool_lease(permit)
        if state in {"executing", "result_persisted"}:
            return await self.finish(
                lease,
                final_status=(
                    "resolved" if state == "result_persisted" else "outcome_unknown"
                ),
                payload_patch=payload_patch,
            )
        receipt = await self.release(
            lease,
            reason=f"runtime_interrupted:{error_kind}",
            payload_patch=payload_patch,
        )
        if receipt.applied and self.orphaned_answer_callback is not None:
            result = self.orphaned_answer_callback(
                lease.checkpoint.checkpoint_id,
                lease.checkpoint.checkpoint_type,
            )
            if asyncio.iscoroutine(result):
                await result
        return receipt

    async def finish_exact_tool_permission(
        self,
        permit: dict[str, Any],
        *,
        payload_patch: dict[str, Any] | None = None,
    ) -> ExecutionCheckpointConsumptionReceipt:
        """Compatibility finish for stores without the atomic ToolResult primitive."""

        return await self.finish(
            await self.exact_tool_lease(permit),
            final_status="resolved",
            payload_patch=payload_patch,
        )

    @staticmethod
    def _lease_key(lease: InteractionDecisionLease) -> tuple[str, str, str]:
        return (
            str(lease.checkpoint.checkpoint_id or "").strip(),
            str(lease.claim_id or "").strip(),
            str(lease.consumer_id or "").strip(),
        )

    def start_lease_heartbeat(
        self,
        lease: InteractionDecisionLease,
        *,
        lease_seconds: float,
    ) -> None:
        """Keep a claim alive until its checkpoint becomes terminal/released."""

        key = self._lease_key(lease)
        current = self._lease_heartbeat_tasks.get(key)
        if current is not None and not current.done():
            return
        interval = max(0.05, min(30.0, float(lease_seconds) / 3.0))
        loss_event = self._lease_loss_events.setdefault(key, asyncio.Event())
        self._lease_loss_reasons.pop(key, None)

        async def heartbeat() -> None:
            loop = asyncio.get_running_loop()
            ownership_deadline = loop.time() + float(lease_seconds)
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        receipt = await self.renew(
                            lease,
                            lease_seconds=lease_seconds,
                        )
                    except Exception as exc:
                        if loop.time() < ownership_deadline:
                            logger.warning(
                                "transient interaction lease renewal failure for %s: %s",
                                lease.checkpoint.checkpoint_id,
                                type(exc).__name__,
                            )
                            continue
                        self._lease_loss_reasons[key] = (
                            f"renewal_error:{type(exc).__name__}"
                        )
                        loss_event.set()
                        return
                    if not receipt.acquired:
                        checkpoint_status = str(
                            getattr(receipt.checkpoint, "status", "") or ""
                        ).strip()
                        if (
                            receipt.outcome == "invalid_state"
                            and checkpoint_status
                            not in {"pending", "answered", "consuming", "resuming"}
                        ):
                            terminal_interaction = dict(
                                (getattr(receipt.checkpoint, "payload", {}) or {}).get(
                                    "interaction", {}
                                )
                                or {}
                            )
                            completion = dict(
                                terminal_interaction.get("completion", {}) or {}
                            )
                            if (
                                str(completion.get("claim_id", "") or "")
                                == lease.claim_id
                                and str(completion.get("consumer_id", "") or "")
                                == lease.consumer_id
                            ):
                                # Runtime-owned exact completion may finish
                                # through the Store after the dispatcher
                                # returned.  Its own terminal transition is
                                # success, not lease loss.
                                return
                        self._lease_loss_reasons[key] = str(
                            receipt.outcome or "ownership_lost"
                        )
                        loss_event.set()
                        return
                    ownership_deadline = loop.time() + float(lease_seconds)
            except asyncio.CancelledError:
                raise
            finally:
                current_task = self._lease_heartbeat_tasks.get(key)
                if current_task is asyncio.current_task():
                    self._lease_heartbeat_tasks.pop(key, None)
                if not loss_event.is_set():
                    self._lease_loss_events.pop(key, None)
                    self._lease_loss_reasons.pop(key, None)

        task = asyncio.create_task(
            heartbeat(),
            name=f"interaction-lease:{key[0]}:{key[1]}",
        )
        self._lease_heartbeat_tasks[key] = task

    async def run_while_claimed(
        self,
        lease: InteractionDecisionLease,
        operation: Awaitable[T],
        *,
        begin_effect: bool = True,
        lease_seconds: float = 300.0,
    ) -> T:
        """Run a domain effect between a durable start fence and owner fence."""

        key = self._lease_key(lease)
        loss_event = self._lease_loss_events.setdefault(key, asyncio.Event())
        if begin_effect:
            started = await self.begin_effect(lease)
            if not started.acquired:
                close = getattr(operation, "close", None)
                if callable(close):
                    close()
                raise InteractionLeaseLost(
                    f"could not fence interaction effect: {started.outcome}"
                )
        operation_task = asyncio.ensure_future(operation)
        lost_task = asyncio.create_task(
            loss_event.wait(),
            name=f"interaction-lease-watch:{key[0]}:{key[1]}",
        )
        try:
            done, _ = await asyncio.wait(
                {operation_task, lost_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Loss wins a simultaneous race.  Returning an effect result after
            # another controller reclaimed the row would falsely report that
            # the stale owner can still settle it.
            if lost_task in done and loss_event.is_set():
                if not operation_task.done():
                    operation_task.cancel()
                    await asyncio.gather(operation_task, return_exceptions=True)
                raise InteractionLeaseLost(
                    self._lease_loss_reasons.get(key, "interaction lease lost")
                )
            if operation_task in done:
                try:
                    result = await operation_task
                except BaseException as exc:
                    if begin_effect:
                        raise InteractionEffectFailed(
                            "interaction effect failed after its execution fence"
                        ) from exc
                    raise
                # Fence the return against a reclaim that happened while a
                # synchronous effect blocked this event loop.
                fence = await self.renew(lease, lease_seconds=lease_seconds)
                if not fence.acquired:
                    fenced_interaction = dict(
                        (getattr(fence.checkpoint, "payload", {}) or {}).get(
                            "interaction", {}
                        )
                        or {}
                    )
                    completion = dict(
                        fenced_interaction.get("completion", {}) or {}
                    )
                    if (
                        str(completion.get("claim_id", "") or "")
                        == lease.claim_id
                        and str(completion.get("consumer_id", "") or "")
                        == lease.consumer_id
                    ):
                        return result
                    raise InteractionLeaseLost(
                        f"interaction completion fence failed: {fence.outcome}"
                    )
                return result
            if not operation_task.done():
                operation_task.cancel()
                await asyncio.gather(operation_task, return_exceptions=True)
            raise InteractionLeaseLost(
                self._lease_loss_reasons.get(key, "interaction lease lost")
            )
        finally:
            if not lost_task.done():
                lost_task.cancel()
            await asyncio.gather(lost_task, return_exceptions=True)

    async def stop_lease_heartbeat(
        self,
        lease: InteractionDecisionLease,
    ) -> None:
        key = self._lease_key(lease)
        task = self._lease_heartbeat_tasks.pop(key, None)
        self._lease_loss_events.pop(key, None)
        self._lease_loss_reasons.pop(key, None)
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def shutdown(self) -> None:
        """Stop process-local optimizers; durable rows remain recoverable."""

        tasks = list(self._lease_heartbeat_tasks.values())
        self._lease_heartbeat_tasks.clear()
        self._lease_loss_events.clear()
        self._lease_loss_reasons.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def release(
        self,
        lease: InteractionDecisionLease,
        *,
        reason: str,
        payload_patch: dict[str, Any] | None = None,
    ) -> ExecutionCheckpointConsumptionReceipt:
        receipt = await self.store.release_execution_checkpoint_claim(
            lease.checkpoint.checkpoint_id,
            project_id=lease.checkpoint.project_id,
            checkpoint_type=lease.checkpoint.checkpoint_type,
            claim_id=lease.claim_id,
            consumer_id=lease.consumer_id,
            reason=reason,
            payload_patch=payload_patch,
        )
        if receipt.applied:
            await self._notify_changed(receipt.checkpoint)
        await self.stop_lease_heartbeat(lease)
        return receipt

    @staticmethod
    def _decision_value(
        checkpoint: ExecutionCheckpoint | None,
    ) -> dict[str, Any] | None:
        if checkpoint is None:
            return None
        interaction = dict((checkpoint.payload or {}).get("interaction") or {})
        decision = interaction.get("decision")
        if not isinstance(decision, dict):
            return None
        value = decision.get("value")
        return dict(value) if isinstance(value, dict) else {"text": str(value or "")}

    @staticmethod
    def _same_immutable_identity(
        persisted: ExecutionCheckpoint,
        candidate: ExecutionCheckpoint,
    ) -> bool:
        if (
            persisted.project_id != candidate.project_id
            or persisted.checkpoint_type != candidate.checkpoint_type
            or str(persisted.task_id or "") != str(candidate.task_id or "")
            or str(persisted.session_id or "") != str(candidate.session_id or "")
        ):
            return False
        persisted_payload = dict(persisted.payload or {})
        candidate_payload = dict(candidate.payload or {})
        persisted_interaction = dict(persisted_payload.get("interaction") or {})
        candidate_interaction = dict(candidate_payload.get("interaction") or {})
        persisted_tool_call = dict(persisted_payload.get("tool_call") or {})
        candidate_tool_call = dict(candidate_payload.get("tool_call") or {})
        return (
            str(persisted_interaction.get("kind", "") or "")
            == str(candidate_interaction.get("kind", "") or "")
            and str(persisted_tool_call.get("fingerprint", "") or "")
            == str(candidate_tool_call.get("fingerprint", "") or "")
        )
