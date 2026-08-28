"""Stable backend protocol constants for durable owner interactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opc.core.models import ExecutionCheckpoint


# Runtime suspend/interrupt and peer/manager waits intentionally use their
# own actor/state machines and are not owner interaction replies.
COMPANY_ADMISSION_CHECKPOINT_TYPES: frozenset[str] = frozenset({
    "company_staffing_selection",
    "company_recruitment_confirmation",
})

OWNER_INTERACTION_CHECKPOINT_TYPES: frozenset[str] = frozenset({
    "tool_permission",
    "action_permission",
    "task_user_input",
    "company_work_item_gate",
    "company_delivery_feedback",
    *COMPANY_ADMISSION_CHECKPOINT_TYPES,
    "company_reorg_pending",
    "route_clarification",
    "company_runtime_selection",
    "company_run_failure_review",
})

OWNER_INTERACTION_ACTIVE_STATUSES: frozenset[str] = frozenset({
    "pending",
    "answered",
    "consuming",
})


@dataclass(frozen=True)
class PreparedOwnerInteractionPublication:
    """Canonical, validation-ready owner interaction publication.

    The Engine prepares ownership and domain/supersession identity once, then
    either the Coordinator or a wider authoritative Store transaction may
    publish this exact object.  Keeping preparation separate from publication
    lets a company delivery commit its business state and feedback card in one
    SQLite transaction without reimplementing the owner-interaction protocol.
    """

    checkpoint: ExecutionCheckpoint
    interaction_key: str
    supersession_key: str
    supersession_order: tuple[int, ...]


@dataclass(frozen=True)
class OriginOwnerInteractionLease:
    """Immutable owner lease carried by work started from an owner decision.

    Company staffing and recruitment decisions can synchronously drive a long
    company run.  The final delivery transaction uses this exact identity to
    close the originating decision before it publishes the next owner card.
    Persisting only this fencing identity avoids carrying a stale copy of the
    mutable checkpoint payload through the runtime.
    """

    checkpoint_id: str
    checkpoint_type: str
    project_id: str
    claim_id: str
    consumer_id: str

    @property
    def complete(self) -> bool:
        return bool(
            str(self.checkpoint_id or "").strip()
            and str(self.project_id or "").strip()
            and str(self.claim_id or "").strip()
            and str(self.consumer_id or "").strip()
            and str(self.checkpoint_type or "").strip()
            in COMPANY_ADMISSION_CHECKPOINT_TYPES
        )

    def to_payload(self) -> dict[str, str]:
        if not self.complete:
            raise ValueError("origin owner interaction lease is incomplete")
        return {
            "checkpoint_id": str(self.checkpoint_id).strip(),
            "checkpoint_type": str(self.checkpoint_type).strip(),
            "project_id": str(self.project_id).strip(),
            "claim_id": str(self.claim_id).strip(),
            "consumer_id": str(self.consumer_id).strip(),
        }

    @classmethod
    def from_payload(
        cls,
        value: Any,
    ) -> "OriginOwnerInteractionLease | None":
        if not isinstance(value, dict) or not value:
            return None
        lease = cls(
            checkpoint_id=str(value.get("checkpoint_id", "") or "").strip(),
            checkpoint_type=str(value.get("checkpoint_type", "") or "").strip(),
            project_id=str(value.get("project_id", "") or "").strip(),
            claim_id=str(value.get("claim_id", "") or "").strip(),
            consumer_id=str(value.get("consumer_id", "") or "").strip(),
        )
        if not lease.complete:
            raise ValueError("origin owner interaction lease is incomplete")
        return lease


@dataclass(frozen=True)
class CompanyWorkItemGateDecisionCommand:
    """Exact interaction/domain identity for one gate-decision commit.

    The mutable checkpoint itself is deliberately not carried into the Store.
    Every field below is compared with the durable row again after
    ``BEGIN IMMEDIATE`` together with the controller and attempt fences.
    """

    checkpoint_id: str
    project_id: str
    claim_id: str
    consumer_id: str
    run_id: str
    task_id: str
    work_item_id: str
    attempt_seq: int
    gate_attempt: int
    basis_hash: str
    action: str
    feedback: str = ""

    @property
    def complete(self) -> bool:
        try:
            attempt_seq = int(self.attempt_seq or 0)
            gate_attempt = int(self.gate_attempt or 0)
        except (TypeError, ValueError):
            return False
        return bool(
            str(self.checkpoint_id or "").strip()
            and str(self.project_id or "").strip()
            and str(self.claim_id or "").strip()
            and str(self.consumer_id or "").strip()
            and str(self.run_id or "").strip()
            and str(self.task_id or "").strip()
            and str(self.work_item_id or "").strip()
            and attempt_seq > 0
            and gate_attempt >= 0
            and str(self.basis_hash or "").strip()
            and str(self.action or "").strip().lower() in {"approve", "deny"}
        )


def canonical_company_work_item_gate_decision(value: Any) -> str:
    """Return the strict approve/deny action carried by a gate decision.

    Free-form text is deliberately ignored.  When both structured aliases are
    present they must agree, which prevents a transport or caller from
    selecting whichever field is more convenient after the owner answered.
    """

    if not isinstance(value, dict):
        return ""
    tokens = {
        str(value.get(key, "") or "").strip().lower()
        for key in ("option_id", "checkpoint_reply_kind")
        if str(value.get(key, "") or "").strip()
    }
    if len(tokens) != 1:
        return ""
    action = next(iter(tokens))
    return action if action in {"approve", "deny"} else ""


def company_work_item_gate_decision_feedback(value: Any) -> str:
    """Return the deterministic display/feedback text from a gate answer."""

    if not isinstance(value, dict):
        return ""
    text = str(value.get("text", "") or "").strip()
    if text:
        return text
    answers = value.get("user_input_answers")
    if isinstance(answers, dict) and answers:
        return "\n".join(
            f"{question_id}: {answer}"
            for question_id, answer in answers.items()
        )
    return str(
        value.get("option_id")
        or value.get("checkpoint_reply_kind")
        or ""
    ).strip()


def owner_interaction_actor_identity(
    checkpoint: Any | None,
) -> tuple[str, str]:
    """Return the durable owner actor without execution-identity fallbacks.

    A waiting Task is the native/external execution that is blocked by the
    checkpoint; it is not necessarily the user-facing owner.  An empty UI Task
    with a non-empty root session is intentional for rootless company runtimes.
    Every presentation adapter uses this helper so a child Task can never be
    promoted to an owner merely because ``ui_anchor_task_id`` is empty.
    """

    if checkpoint is None:
        return "", ""
    payload = dict(getattr(checkpoint, "payload", {}) or {})
    interaction = dict(payload.get("interaction", {}) or {})
    ownership = dict(interaction.get("ownership", {}) or {})
    return (
        str(ownership.get("ui_anchor_task_id", "") or "").strip(),
        str(
            ownership.get("ui_anchor_session_id")
            or ownership.get("root_session_id")
            or ""
        ).strip(),
    )
