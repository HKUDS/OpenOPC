"""Typed control-flow signals for durable company-run controller fencing."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CompanyControllerAttemptContext:
    """Immutable identity for one controller-owned WorkItem attempt.

    This value is only a *request* to perform an authoritative command.  The
    Store revalidates every field against ``delegation_runs``, the durable
    Task/WorkItem link, and the WorkItem's monotonically increasing
    ``attempt_seq`` inside the same SQLite write transaction as the effects.
    Consequently copying an old Task can never extend an expired generation's
    authority.
    """

    run_id: str
    project_id: str
    owner_token: str
    generation: int
    task_id: str
    work_item_id: str
    attempt_seq: int

    @classmethod
    def from_task(
        cls,
        task: Any,
        *,
        work_item_id: str,
    ) -> "CompanyControllerAttemptContext":
        metadata = dict(getattr(task, "metadata", {}) or {})
        try:
            generation = int(
                metadata.get("company_run_controller_lease_generation", 0) or 0
            )
        except (TypeError, ValueError):
            generation = 0
        try:
            attempt_seq = int(
                metadata.get("claimed_work_item_attempt_seq", 0) or 0
            )
        except (TypeError, ValueError):
            attempt_seq = 0
        return cls(
            run_id=str(metadata.get("delegation_run_id", "") or "").strip(),
            project_id=str(getattr(task, "project_id", "") or "default").strip()
            or "default",
            owner_token=str(
                metadata.get("company_run_controller_owner_token", "") or ""
            ).strip(),
            generation=generation,
            task_id=str(getattr(task, "id", "") or "").strip(),
            work_item_id=str(work_item_id or "").strip(),
            attempt_seq=attempt_seq,
        )

    @property
    def complete(self) -> bool:
        return bool(
            self.run_id
            and self.project_id
            and self.owner_token
            and self.generation > 0
            and self.task_id
            and self.work_item_id
            and self.attempt_seq > 0
        )


class CompanyRunControllerBusy(RuntimeError):
    """Another unexpired controller owns the requested company run.

    Busy admission is a domain outcome, not task cancellation.  In
    particular, interaction consumers must leave a suspended checkpoint
    pending rather than misclassifying a healthy remote owner as shutdown.
    """


class CompanyControllerAttemptSuperseded(RuntimeError):
    """A live controller rejected a tail write from an older WorkItem attempt.

    This is a local attempt outcome, not evidence that the caller lost the
    run-scoped controller lease.  Dispatchers may therefore harvest the stale
    coroutine without cancelling unrelated work owned by the same generation.
    """


class CompanyRunControllerLeaseLost(asyncio.CancelledError):
    """The caller no longer owns the fenced company-run generation."""
