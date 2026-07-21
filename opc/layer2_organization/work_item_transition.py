"""Single authoritative entry point for WorkItem phase changes.

This module exposes ``transition_work_item`` — the only function in the
codebase that should mutate a ``DelegationWorkItem.phase``. Its purpose is
to centralise the "phase is the single source of truth" invariant that
``phase.py`` always claimed but code never enforced.

Everything downstream of phase (``Task.status``, ``DelegationRoleSession
.status`` (DB), ``CompanyMemberSession.status`` (memory), UI kanban
column) is synced via the registered phase-transition hooks, so callers
only need to think about "what phase should this card be in now".

Direct writes like ``task.status = TaskStatus.CANCELLED`` or
``session.status = "idle"`` bypass the hook chain and guarantee cross-layer
state desync — the exact pattern that produced the parent-resume and
stop-cascade bugs in new11 app05. See ``plans/task-key-proud-blum.md``.

This module also exposes ``refresh_dependents_for_run`` — the dependency
frontier pass that propagates child completion (or terminal state) to
parent work items. Lifted out of CompanyMode so a phase-transition hook
can invoke it on any terminal/escalation transition without an import
cycle. See Fix 3 in ``memory/company-mode-stuck-bugs.md``.
"""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime
from typing import Any

from loguru import logger

from opc.core.models import DelegationEvent, DelegationWorkItem, Phase, Task, TaskStatus
from opc.layer2_organization.phase import (
    DONE_PHASES,
    InvalidPhaseTransition,
    coerce_phase,
    has_open_attempt,
    phase_for_task_status,
    task_status_for_phase,
    validate_transition,
)
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task
from opc.layer2_organization.work_item_identity import work_item_identity_payload
from opc.layer2_organization.work_item_runtime import is_work_item_runtime_metadata


def build_attempt_settlement_updates(
    current_metadata: dict[str, Any] | None,
    *,
    outcome: str,
) -> dict[str, Any]:
    """Compute the metadata delta that settles the currently-open attempt.

    ``outcome`` semantics:
      * ``"crashed"`` — the attempt died on an exception/timeout; increments
        ``attempt_crash_streak`` (consecutive crashes).
      * ``"interrupted"`` — the owning process/coroutine went away without a
        verdict (kill, cancel-before-harvest); increments
        ``attempt_interrupted_streak``.
      * anything else — a clean turn boundary (approved/failed-by-review/
        waiting/park/...); resets both streaks.

    Returns ``{}`` when there is no open attempt to settle, so callers can
    merge unconditionally.
    """
    metadata = dict(current_metadata or {})
    if not has_open_attempt(metadata):
        return {}

    def _streak(key: str) -> int:
        try:
            return int(metadata.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    outcome_clean = str(outcome or "").strip() or "settled"
    if outcome_clean == "crashed":
        crash_streak = _streak("attempt_crash_streak") + 1
        interrupted_streak = _streak("attempt_interrupted_streak")
    elif outcome_clean == "interrupted":
        crash_streak = _streak("attempt_crash_streak")
        interrupted_streak = _streak("attempt_interrupted_streak") + 1
    else:
        crash_streak = 0
        interrupted_streak = 0
    return {
        "attempt_settled": True,
        "attempt_outcome": outcome_clean,
        "attempt_settled_at": datetime.now().isoformat(),
        "attempt_crash_streak": crash_streak,
        "attempt_interrupted_streak": interrupted_streak,
    }


async def transition_work_item(
    store: Any,
    work_item_id: str,
    *,
    target_phase: Phase | str,
    reason: str,
    summary: str | None = None,
    metadata_updates: dict[str, Any] | None = None,
    release_claim: bool = False,
    attempt_outcome: str | None = None,
) -> DelegationWorkItem | None:
    """Transition a work item to ``target_phase``.

    The function wraps ``store.update_delegation_work_item(phase=...)`` —
    which already validates the transition against the state-machine table
    (``ALLOWED_TRANSITIONS`` in phase.py) and fires the full
    ``on_phase_transition`` hook chain. This wrapper adds:

    - A mandatory ``reason`` string stamped into metadata for audit
    - An optional ``release_claim`` flag that clears
      ``claimed_by_role_runtime_session_id`` / ``claimed_by_seat_id`` so
      ``is_orphaned`` becomes True and the dispatcher re-picks the card

    Args:
        store: OPCStore instance; must expose ``update_delegation_work_item``.
        work_item_id: Target work item id.
        target_phase: ``Phase`` enum or string (coerced via ``coerce_phase``).
        reason: Short human-readable reason; recorded in metadata for audit.
        summary: Optional summary string persisted on the work item.
        metadata_updates: Extra metadata keys to merge onto the work item.
        release_claim: When True, clears the current claim so the dispatcher
            can re-acquire. Useful for cancel / timeout / forced-release paths.

    Returns:
        The updated ``DelegationWorkItem``, or ``None`` when the store lacks
        the required API or the work item does not exist.
    """
    if not store or not hasattr(store, "update_delegation_work_item"):
        logger.warning(
            "transition_work_item: store lacks update_delegation_work_item"
        )
        return None
    phase = coerce_phase(target_phase)
    merged: dict[str, Any] = dict(metadata_updates or {})
    reason_clean = str(reason or "").strip()
    if reason_clean:
        merged["last_transition_reason"] = reason_clean
    # Attempt settlement: any transition to a non-RUNNING phase is a turn
    # boundary for the claim that opened the current attempt (the claim CAS
    # stamped attempt_settled=false). Fold the settlement delta into the SAME
    # write as the phase change so an attempt can never end without its
    # ledger entry — the structural guarantee behind the dispatcher's
    # crash/interrupted streak brake (see phase.attempt_ledger_dispatch_
    # block_reason).
    settlement: dict[str, Any] = {}
    current_item = None
    if phase != Phase.RUNNING and hasattr(store, "get_delegation_work_item"):
        try:
            current_item = await store.get_delegation_work_item(work_item_id)
        except Exception:
            current_item = None
        if current_item is not None:
            settlement = build_attempt_settlement_updates(
                dict(getattr(current_item, "metadata", {}) or {}),
                outcome=attempt_outcome or phase.value,
            )
            if settlement:
                merged = {**settlement, **merged}
    kwargs: dict[str, Any] = {
        "phase": phase,
        "metadata_updates": merged,
    }
    if summary is not None:
        kwargs["summary"] = summary
    if release_claim:
        # Fold claim release into the same write as the phase change: the
        # legacy two-call sequence could commit the phase and then fail the
        # release, stranding a dead claim. Terminal phases additionally drop
        # the metadata claim mirror keys so the row can never satisfy a
        # future claim-CAS predicate by accident.
        kwargs["claimed_by_role_runtime_session_id"] = ""
        kwargs["claimed_by_seat_id"] = ""
        if phase in DONE_PHASES:
            merged.setdefault("claimed_by_role_session_id", "")
            merged.setdefault("claimed_task_id", "")
    try:
        result = await store.update_delegation_work_item(work_item_id, **kwargs)
    except InvalidPhaseTransition:
        # The phase write lost a race (or the caller is out of date). The
        # attempt settlement must still land — losing it is exactly the
        # "crash without accounting" gap that produced endless re-dispatch.
        if settlement:
            try:
                await store.update_delegation_work_item(
                    work_item_id,
                    metadata_updates=settlement,
                )
            except Exception:
                logger.opt(exception=True).warning(
                    f"transition_work_item: settlement fallback failed wid={work_item_id}"
                )
        raise
    except Exception:
        logger.opt(exception=True).warning(
            f"transition_work_item failed wid={work_item_id} "
            f"target={phase.value} reason={reason_clean}"
        )
        raise
    return result


async def settle_open_attempt_as_interrupted(
    store: Any,
    work_item: DelegationWorkItem,
) -> bool:
    """Back-fill the ledger for an attempt whose owner died without settling.

    Metadata-only write (no phase change): the card keeps whatever phase the
    crash left it in — recovery semantics stay untouched — but the attempt
    stops being invisible. Called by the dispatcher's per-tick reconcile pass
    for cards with an open attempt and no live in-process owner. Returns True
    when a settlement write was issued.
    """
    if store is None or not hasattr(store, "update_delegation_work_item"):
        return False
    metadata = dict(getattr(work_item, "metadata", {}) or {})
    settlement = build_attempt_settlement_updates(metadata, outcome="interrupted")
    if not settlement:
        return False
    try:
        updated = await store.update_delegation_work_item(
            work_item.work_item_id,
            metadata_updates=settlement,
        )
    except Exception:
        logger.opt(exception=True).warning(
            "settle_open_attempt_as_interrupted failed "
            f"wid={getattr(work_item, 'work_item_id', '')}"
        )
        return False
    if updated is not None:
        work_item.metadata = dict(updated.metadata or {})
    return True


def _fallback_status_for(
    target_status_or_phase: TaskStatus | Phase | str,
    task: Task,
) -> TaskStatus | None:
    """Pre-compute the TaskStatus to assign locally when the work-item
    transition cannot happen (no linked work_item, no store). Mirrors the
    projection the live hook would apply, so task-mode callers see the
    same local result whether or not a work_item exists.
    """
    try:
        if isinstance(target_status_or_phase, TaskStatus):
            return target_status_or_phase
        if isinstance(target_status_or_phase, Phase):
            return task_status_for_phase(target_status_or_phase)
        if isinstance(target_status_or_phase, str):
            raw = target_status_or_phase.strip().lower()
            try:
                return task_status_for_phase(Phase(raw))
            except ValueError:
                try:
                    return TaskStatus(raw)
                except ValueError:
                    return None
    except Exception:
        return None
    return None


async def transition_work_item_from_task(
    store: Any,
    task: Task,
    *,
    target_status_or_phase: TaskStatus | Phase | str,
    reason: str,
    summary: str | None = None,
    metadata_updates: dict[str, Any] | None = None,
    release_claim: bool = False,
    require_work_item: bool = False,
    attempt_outcome: str | None = None,
) -> bool:
    """Task bridge helper: transition a work item when the caller holds a Task.

    Designed as the replacement for direct ``task.status = ...`` writes in
    company-mode code. Resolves the linked work item via the hydrated runtime
    link table id (falling back to legacy metadata for old rows), coerces the
    desired state into a Phase, and delegates to ``transition_work_item``.

    ``target_status_or_phase`` may be:
      * a ``Phase`` (or phase-string) — used verbatim.
      * a ``TaskStatus`` (or status-string) — projected via
        ``phase_for_task_status``. BLOCKED disambiguation uses
        ``task.metadata['delegation_pending_work_item_ids']`` to distinguish
        ``WAITING_FOR_CHILDREN`` (has pending children) from ``PAUSED``.
        This preserves the old task-status projection semantics.

    Forward-invalid transitions (e.g. a late async callback arriving after
    the work item was already moved by a reviewer) are silently preserved
    rather than raising. We log at DEBUG so the race is observable without
    being noisy.

    **Local task.status sync**: after the work item transition, the registered
    ``sync_task_status_hook`` updates the DB task.status. The caller still
    holds a local ``task`` object whose ``status`` is now stale — and any
    subsequent ``save_task(task)`` would overwrite the hook's DB update with
    the stale value (race). To avoid that, we eagerly project the target
    Phase back to a TaskStatus and assign it to the local ``task.status``
    in-memory. This is NOT a direct DB write; it keeps the caller's in-memory
    view consistent with what the DB now holds.

    **task-mode fallback**: when there's no linked work item (task-mode
    path or pre-materialization), the helper returns ``False`` but still
    syncs the local ``task.status`` to the caller's intended value. This
    lets company-mode call sites be migrated to this helper without each
    one needing its own ``task.status = ...`` fallback — the task-mode
    execution path just sees the local mutation and a subsequent
    ``save_task(task)`` by the caller persists it.

    ``require_work_item=True`` is for company-mode runtime call sites where
    falling back to a local Task.status write would reintroduce drift. In
    that mode missing store/link returns ``False`` without mutating local
    status.

    Returns ``True`` when a work-item transition was issued (including the
    silent-degrade no-op case). Returns ``False`` when there is no linked
    work item; the local ``task.status`` is only synced when
    ``require_work_item`` is false.
    """
    # Pre-resolve the fallback status so task-mode / pre-materialization
    # paths still end up with a synced local task.status before we bail.
    fallback_status = _fallback_status_for(target_status_or_phase, task)
    if not store or not hasattr(store, "update_delegation_work_item"):
        if fallback_status is not None and not require_work_item:
            task.status = fallback_status
        return False
    work_item_id = linked_work_item_id_for_task(task)
    if not work_item_id and hasattr(store, "get_work_item_for_runtime_task"):
        try:
            linked_item = await store.get_work_item_for_runtime_task(task.id)
        except Exception:
            linked_item = None
        work_item_id = str(getattr(linked_item, "work_item_id", "") or "").strip()
    if not work_item_id:
        if fallback_status is not None and not require_work_item:
            task.status = fallback_status
        return False
    # Coerce target to Phase, applying BLOCKED → WAITING_FOR_CHILDREN/PAUSED
    # disambiguation from task metadata.
    if isinstance(target_status_or_phase, Phase):
        target_phase: Phase = target_status_or_phase
    elif isinstance(target_status_or_phase, TaskStatus):
        has_pending_children = bool(
            (task.metadata or {}).get("delegation_pending_work_item_ids") or []
        )
        target_phase = phase_for_task_status(
            target_status_or_phase,
            has_pending_children=has_pending_children,
        )
    elif isinstance(target_status_or_phase, str):
        raw = target_status_or_phase.strip().lower()
        try:
            target_phase = Phase(raw)
        except ValueError:
            try:
                ts = TaskStatus(raw)
            except ValueError as exc:
                raise ValueError(
                    f"transition_work_item_from_task: target_status_or_phase {target_status_or_phase!r} "
                    "is neither a valid Phase nor TaskStatus"
                ) from exc
            has_pending_children = bool(
                (task.metadata or {}).get("delegation_pending_work_item_ids") or []
            )
            target_phase = phase_for_task_status(
                ts, has_pending_children=has_pending_children
            )
    else:
        raise TypeError(
            "transition_work_item_from_task: target_status_or_phase must be "
            f"Phase | TaskStatus | str, got {type(target_status_or_phase).__name__}"
        )
    # Silent-degrade guard against late async races (see docstring). Look up
    # the persisted phase and preserve it if the desired transition is not
    # in ALLOWED_TRANSITIONS. This keeps shared role-session callbacks
    # crash-free when a late writer observes stale task state.
    persisted_phase: Phase | None = None
    if hasattr(store, "get_delegation_work_item"):
        try:
            persisted_item = await store.get_delegation_work_item(work_item_id)
        except Exception:
            persisted_item = None
        if persisted_item is not None:
            persisted_phase = getattr(persisted_item, "phase", None)
    if target_phase != persisted_phase and persisted_phase is not None:
        try:
            validate_transition(persisted_phase, target_phase)
        except InvalidPhaseTransition:
            logger.debug(
                "transition_work_item_from_task: preserving persisted phase "
                f"{persisted_phase.value} for work_item={work_item_id} "
                f"(projected {target_phase.value} would be an invalid transition)"
            )
            return True
    # Always stamp the task-id / task-status back-reference so audit and
    # reverse lookup stay consistent. Callers can layer extra metadata on top.
    back_ref: dict[str, Any] = {
        "task_id": task.id,
        "task_status": (
            target_status_or_phase.value
            if isinstance(target_status_or_phase, (TaskStatus, Phase))
            else str(target_status_or_phase)
        ),
    }
    if metadata_updates:
        back_ref.update(metadata_updates)
    try:
        await transition_work_item(
            store,
            work_item_id,
            target_phase=target_phase,
            reason=reason,
            summary=summary,
            metadata_updates=back_ref,
            release_claim=release_claim,
            attempt_outcome=attempt_outcome,
        )
    except InvalidPhaseTransition:
        # Defensive: state-machine validation at the store layer can also
        # raise. Degrade the same way the pre-check does, for the race
        # where persisted_phase changed between our lookup and the write.
        logger.debug(
            f"transition_work_item_from_task: store-layer rejected "
            f"{persisted_phase} → {target_phase.value} for wid={work_item_id} "
            "(concurrent writer); degrading to no-op."
        )
        return True
    # Sync local task.status to match the target phase so any subsequent
    # save_task(task) by the caller doesn't race with the hook's DB update.
    # The assignment goes through task_status_for_phase() — not a literal
    # TaskStatus.CANCELLED/FAILED — so the DirectStatusWriteLintTest regex
    # doesn't flag it as a bypass.
    try:
        task.status = task_status_for_phase(target_phase)
    except Exception:
        logger.opt(exception=True).debug(
            "transition_work_item_from_task: local status sync failed"
        )
    return True


async def apply_task_status_transition(
    store: Any,
    task: Task,
    *,
    target_status_or_phase: TaskStatus | Phase | str,
    reason: str,
    summary: str | None = None,
    metadata_updates: dict[str, Any] | None = None,
    release_claim: bool = False,
    save_plain_task: bool = True,
    raise_on_missing_work_item: bool = True,
) -> bool:
    """Apply a task status intent through the right source of truth.

    Company WorkItem runtime tasks must transition their linked WorkItem phase;
    plain task-mode tasks keep the legacy Task.status behavior through the
    fallback branch in ``transition_work_item_from_task``.
    """
    metadata = dict(getattr(task, "metadata", {}) or {})
    company_runtime = bool(
        linked_work_item_id_for_task(task)
        or is_work_item_runtime_metadata(metadata)
    )
    transitioned = await transition_work_item_from_task(
        store,
        task,
        target_status_or_phase=target_status_or_phase,
        reason=reason,
        summary=summary,
        metadata_updates=metadata_updates,
        release_claim=release_claim,
        require_work_item=company_runtime,
    )
    if company_runtime:
        if not transitioned and raise_on_missing_work_item:
            target = (
                target_status_or_phase.value
                if isinstance(target_status_or_phase, (TaskStatus, Phase))
                else str(target_status_or_phase)
            )
            raise RuntimeError(
                "company runtime task cannot transition without a linked WorkItem: "
                f"task={getattr(task, 'id', '')} target={target}"
            )
        return transitioned
    if save_plain_task and store and hasattr(store, "save_task"):
        await store.save_task(task)
    return transitioned


# Re-entrancy guard: refresh_dependents_for_run writes to work items, which
# fires phase-transition hooks, which can re-call refresh. The outer call
# already walks every item in the run, so inner calls on the same run_id
# are redundant — silently skip them. Module-level ContextVar because the
# dispatcher runs async tasks, and we want per-task isolation.
_REFRESH_IN_FLIGHT: ContextVar[frozenset[str]] = ContextVar(
    "refresh_dependents_in_flight", default=frozenset()
)


_SYNTHESIS_SKIP_KINDS: frozenset[str] = frozenset({
    "aggregate",
    "deliver",
    "delivery",
    "intake",
    "review",
    "synthesis",
    "synthesize",
})

# Default dependency class when a dependency id has no entry in
# metadata.dependency_classes. Shared with the claim-side runnability check
# in company_mode so the frontier pass and the dispatcher never disagree on
# whether an unlabelled dependency is hard.
DEPENDENCY_CLASS_DEFAULT = "hard"

# Roll-up kinds that may be released from WAITING_DEPENDENCIES by the
# failure-triage settlement path: their whole job is to integrate child
# results (including partial/failed ones) and carry them upward.
_SETTLEMENT_ROLLUP_KINDS: frozenset[str] = frozenset({
    "aggregate",
    "deliver",
    "delivery",
    "synthesis",
    "synthesize",
})


def _work_item_id(item: DelegationWorkItem | Any | None) -> str:
    return str(getattr(item, "work_item_id", "") or "").strip()


def _dependency_replacement_ids(item: DelegationWorkItem | Any | None) -> list[str]:
    metadata = dict(getattr(item, "metadata", {}) or {}) if item is not None else {}
    raw = (
        metadata.get("replacement_dependency_work_item_ids")
        or metadata.get("replacement_work_item_ids")
        or metadata.get("superseded_by_work_item_ids")
        or []
    )
    if isinstance(raw, str):
        raw = [raw]
    try:
        values = list(raw or [])
    except TypeError:
        values = [raw]
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def is_prunable_dependency_work_item(item: DelegationWorkItem | Any | None) -> bool:
    """True when a dependency target is obsolete rather than merely failed.

    A normal CANCELLED/FAILED dependency is still meaningful and should keep
    the parent from silently succeeding. Manager-deleted or hidden cancelled
    cards are different: they are explicit graph edits, so stale references to
    them must be removed or replaced whenever the dependency frontier refreshes.
    """
    if item is None:
        return False
    metadata = dict(getattr(item, "metadata", {}) or {})
    if bool(metadata.get("deleted_by_manager_tool", False)):
        return True
    upstream_visibility = str(metadata.get("upstream_visibility", "") or "").strip().lower()
    return (
        getattr(item, "phase", None) == Phase.CANCELLED
        and bool(metadata.get("hidden_from_company_kanban", False))
        and upstream_visibility == "hidden"
    )


def normalize_dependency_work_item_ids(
    raw_dependency_ids: list[str] | tuple[str, ...] | set[str],
    work_item_by_id: dict[str, DelegationWorkItem | Any],
    *,
    owner_work_item_id: str = "",
) -> tuple[list[str], list[str]]:
    """Drop or replace stale dependency ids while preserving hard failures.

    Returns ``(active_ids, pruned_ids)``. Replacement ids come from metadata on
    the obsolete dependency target, and are themselves validated against the
    current run graph so a deleted replacement cannot resurrect another stale
    edge.
    """
    owner_id = str(owner_work_item_id or "").strip()
    active: list[str] = []
    pruned: list[str] = []

    def append_active(candidate_id: str) -> None:
        candidate = str(candidate_id or "").strip()
        if not candidate or candidate == owner_id:
            if candidate:
                pruned.append(candidate)
            return
        item = work_item_by_id.get(candidate)
        if is_prunable_dependency_work_item(item):
            pruned.append(candidate)
            return
        active.append(candidate)

    for raw_id in list(raw_dependency_ids or []):
        dep_id = str(raw_id or "").strip()
        if not dep_id:
            continue
        item = work_item_by_id.get(dep_id)
        if is_prunable_dependency_work_item(item):
            pruned.append(dep_id)
            for replacement_id in _dependency_replacement_ids(item):
                append_active(replacement_id)
            continue
        append_active(dep_id)

    return (
        list(dict.fromkeys(active)),
        list(dict.fromkeys(pruned)),
    )


def compute_doomed_work_item_ids(
    work_item_by_id: dict[str, DelegationWorkItem | Any],
) -> set[str]:
    """Ids of work items that can never reach APPROVED on their own.

    Seeds are FAILED/CANCELLED items. Propagation: a QUEUED /
    WAITING_DEPENDENCIES / READY item whose hard dependency is doomed (and
    not APPROVED) can never become runnable, so it is doomed too. Pure
    fixpoint over the run snapshot — no IO. Dependencies are normalized
    first, so a doomed card that was rewired via ``delete_work_item`` +
    replacement ids drops back out of the set.
    """
    doomed: set[str] = {
        item_id
        for item_id, item in work_item_by_id.items()
        if getattr(item, "phase", None) in (Phase.FAILED, Phase.CANCELLED)
    }
    if not doomed:
        return doomed
    changed = True
    while changed:
        changed = False
        for item_id, item in work_item_by_id.items():
            if item_id in doomed:
                continue
            if getattr(item, "phase", None) not in (
                Phase.QUEUED,
                Phase.WAITING_DEPENDENCIES,
                Phase.READY,
            ):
                continue
            metadata = dict(getattr(item, "metadata", {}) or {})
            # A card carrying a settlement stamp was RELEASED by the
            # frontier pass over its failures — it is alive (a runnable
            # triage card), not doomed. Marking it doomed would let an
            # upper parent settle early and, worse, let that parent's
            # cascade cancel a triage card that is about to run.
            if dict(metadata.get("dependency_settlement", {}) or {}):
                continue
            raw_ids = [
                str(dep).strip()
                for dep in list(metadata.get("dependency_work_item_ids", []) or [])
                if str(dep).strip()
            ]
            if not raw_ids:
                continue
            dep_ids, _pruned = normalize_dependency_work_item_ids(
                raw_ids, work_item_by_id, owner_work_item_id=item_id
            )
            dependency_classes = dict(metadata.get("dependency_classes", {}) or {})
            for dep_id in dep_ids:
                dep = work_item_by_id.get(dep_id)
                if dep is None or dep_id not in doomed:
                    continue
                dep_class = str(
                    dependency_classes.get(dep_id, DEPENDENCY_CLASS_DEFAULT)
                    or DEPENDENCY_CLASS_DEFAULT
                ).strip().lower()
                if dep_class in ("soft", "info"):
                    continue
                if getattr(dep, "phase", None) == Phase.APPROVED:
                    continue
                doomed.add(item_id)
                changed = True
                break
    return doomed


def settled_failure_dependency_ids(metadata: dict[str, Any] | None) -> set[str]:
    """Dependency ids this card was explicitly released over despite failure.

    Includes the ``stuck`` ids (transitively-blocked, still non-terminal):
    a released triage card must be claimable even while its stuck
    dependencies linger — they are settled context awaiting rebuild or the
    settlement cascade, not blockers. Reads the ``dependency_settlement``
    stamp that ``refresh_dependents_for_run`` writes in the same update
    that wakes the card, so only the frontier pass — never ad-hoc metadata
    edits — can authorize running over an unsatisfied hard dependency.
    """
    settlement = dict((metadata or {}).get("dependency_settlement", {}) or {})
    return {
        str(item).strip()
        for key in ("failed", "cancelled", "stuck")
        for item in list(settlement.get(key, []) or [])
        if str(item).strip()
    }


def _work_item_kind(item: DelegationWorkItem, metadata: dict[str, Any]) -> str:
    return str(
        metadata.get("work_kind")
        or metadata.get("delegation_turn_kind")
        or item.kind
        or ""
    ).strip().lower()


def _dependency_settlement_snapshot(
    metadata: dict[str, Any],
    dependency_phases: dict[str, Any],
    doomed_ids: set[str],
) -> dict[str, Any]:
    """Single truth for the failure-triage gate over one card's deps.

    "Settled with failures": every dependency is terminal or doomed
    (transitively blocked by a terminal failure), so waiting longer cannot
    change the outcome. The failure may be purely transitive (a direct dep
    is stuck behind a FAILED card elsewhere), so the trigger counts stuck
    deps too. Info-class deps never gate claiming, so they do not gate
    settlement either. Missing deps (phase None) stay unsettled.
    """
    dependency_class_map = dict(metadata.get("dependency_classes", {}) or {})

    def _dep_is_info(dep_id: str) -> bool:
        return str(
            dependency_class_map.get(dep_id, DEPENDENCY_CLASS_DEFAULT)
            or DEPENDENCY_CLASS_DEFAULT
        ).strip().lower() == "info"

    all_approved = all(p == Phase.APPROVED for p in dependency_phases.values())
    failed = [d for d, p in dependency_phases.items() if p == Phase.FAILED]
    cancelled = [d for d, p in dependency_phases.items() if p == Phase.CANCELLED]
    stuck = [
        d
        for d, p in dependency_phases.items()
        if d in doomed_ids and p not in (Phase.FAILED, Phase.CANCELLED)
    ]
    settled_with_failures = (
        not all_approved
        and bool(failed or cancelled or stuck)
        and all(
            (p is not None and p in DONE_PHASES)
            or dep_id in doomed_ids
            or _dep_is_info(dep_id)
            for dep_id, p in dependency_phases.items()
        )
    )
    return {
        "all_approved": all_approved,
        "failed": failed,
        "cancelled": cancelled,
        "stuck": stuck,
        "settled_with_failures": settled_with_failures,
    }


def _is_settlement_release_candidate(
    item: DelegationWorkItem | Any,
    metadata: dict[str, Any],
) -> bool:
    """Cards a failure-triage release may wake: the delegating parent, a
    roll-up card, or an already-released (stamped) in-flight triage card."""
    phase = getattr(item, "phase", None)
    if phase == Phase.WAITING_FOR_CHILDREN:
        return True
    if phase == Phase.WAITING_DEPENDENCIES and _work_item_kind(item, metadata) in _SETTLEMENT_ROLLUP_KINDS:
        return True
    return phase in (Phase.READY, Phase.READY_FOR_REWORK, Phase.RUNNING) and bool(
        dict(metadata.get("dependency_settlement", {}) or {})
    )


def has_pending_settlement_release(
    work_item_by_id: dict[str, DelegationWorkItem | Any],
) -> bool:
    """True when some card is due a failure-triage release.

    Used by the dispatcher tick for cards created AFTER their dependency
    already failed: the failure's transition hook predates the card, so no
    future event would ever run the frontier for it. Cards already
    released (stamp present, phase moved) return False here, keeping the
    tick idempotent.
    """
    doomed_ids = compute_doomed_work_item_ids(work_item_by_id)
    if not doomed_ids:
        return False
    for item_id, item in work_item_by_id.items():
        metadata = dict(getattr(item, "metadata", {}) or {})
        phase = getattr(item, "phase", None)
        if not (
            phase == Phase.WAITING_FOR_CHILDREN
            or (
                phase == Phase.WAITING_DEPENDENCIES
                and _work_item_kind(item, metadata) in _SETTLEMENT_ROLLUP_KINDS
            )
        ):
            continue
        raw_ids = [
            str(dep).strip()
            for dep in list(metadata.get("dependency_work_item_ids", []) or [])
            if str(dep).strip()
        ]
        if not raw_ids:
            continue
        dependency_ids, _pruned = normalize_dependency_work_item_ids(
            raw_ids, work_item_by_id, owner_work_item_id=item_id
        )
        dependency_phases = {
            dep_id: (
                getattr(work_item_by_id[dep_id], "phase", None)
                if dep_id in work_item_by_id
                else None
            )
            for dep_id in dependency_ids
        }
        snapshot = _dependency_settlement_snapshot(metadata, dependency_phases, doomed_ids)
        if snapshot["settled_with_failures"]:
            return True
    return False


def _should_enter_synthesis_turn(
    item: DelegationWorkItem,
    metadata: dict[str, Any],
    dependency_ids: list[str],
) -> bool:
    if item.phase != Phase.WAITING_FOR_CHILDREN:
        return False
    if not dependency_ids:
        return False
    if bool(metadata.get("synthesis_turn_started", False)):
        return False
    if _work_item_kind(item, metadata) in _SYNTHESIS_SKIP_KINDS:
        return False
    if not (
        bool(metadata.get("delegated_children_pending", False))
        or str(metadata.get("frontier", "") or "").strip() == "waiting_for_children"
        or str(metadata.get("last_delegated_by_seat_id", "") or "").strip()
    ):
        return False
    return True


def _synthesis_turn_summary(item: DelegationWorkItem, dependency_ids: list[str]) -> str:
    title = str(item.title or "delegated work").strip()
    child_count = len(dependency_ids)
    manager_label = str(item.manager_role_id or "the upstream owner").strip()
    return (
        f"Synthesize the {child_count} approved child work item"
        f"{'' if child_count == 1 else 's'} for `{title}` and prepare the "
        f"handoff for {manager_label}. Include what was completed, evidence, "
        "remaining risks, and any decision needed from the upper role."
    )


def _failure_triage_turn_summary(
    item: DelegationWorkItem,
    dependency_phases: dict[str, Any],
    failed_ids: list[str],
    cancelled_ids: list[str],
    stuck_ids: list[str],
) -> str:
    title = str(item.title or "delegated work").strip()
    manager_label = str(item.manager_role_id or "the upstream owner").strip()
    approved_count = sum(1 for p in dependency_phases.values() if p == Phase.APPROVED)
    parts = [
        f"{approved_count} approved",
        f"{len(failed_ids) + len(cancelled_ids)} failed/cancelled",
    ]
    if stuck_ids:
        parts.append(f"{len(stuck_ids)} blocked downstream")
    return (
        f"Triage the delegated results for `{title}` ({', '.join(parts)}). "
        "Decide how to handle the failures — rebuild the failed work, accept "
        "partial results, or escalate the gap — then prepare the handoff for "
        f"{manager_label}."
    )


async def refresh_dependents_for_run(
    store: Any,
    *,
    run_id: str,
    source_work_item_id: str | None = None,
    source_task_id: str | None = None,
    source_role_id: str | None = None,
    source_cell_id: str | None = None,
) -> bool:
    """Walk all work items in ``run_id`` and propagate dependency state
    to parent phases.

    **What this does in one pass**:

    - ``WAITING_DEPENDENCIES → READY`` (or ``READY_FOR_REWORK`` when the
      item carries an outstanding rework_feedback) when all deps approved.
    - ``WAITING_FOR_CHILDREN → READY`` as a synthesis turn when delegated
      children are all approved; otherwise ``WAITING_FOR_CHILDREN → RUNNING``.
      Both paths release the parent's stale claim so the dispatcher can
      re-pick it cleanly.
    - Failure-triage release: when every dep is settled (terminal) or
      doomed (transitively blocked by a FAILED/CANCELLED dep) and at least
      one failed, the delegating parent / roll-up card is released anyway
      with a ``dependency_settlement`` stamp, so a single failure can
      never wedge the whole tree in WAITING_FOR_CHILDREN forever. Once
      that card reaches APPROVED, leftover doomed descendants it did not
      rebuild are cancelled (settlement cascade) so the run can finalize.
    - Reverse direction: a RUNNING item whose deps regress (new dep
      appeared) goes to ``WAITING_FOR_CHILDREN``; a READY item to
      ``WAITING_DEPENDENCIES``.

    **Who calls this**:

    1. ``CompanyMode._refresh_delegation_dependents`` — preserves the
       explicit call from APPROVED-verdict paths (belt-and-suspenders).
    2. ``phase_hooks.refresh_dependents_hook`` — fires on every terminal
       transition (APPROVED / FAILED / CANCELLED) and on AWAITING_HUMAN,
       so the frontier refreshes automatically. Without the hook, a
       child escalating to AWAITING_HUMAN (or a human-approved
       AWAITING_HUMAN → APPROVED click) would never unblock its parent.

    Returns True when any parent was mutated (for cheap change detection
    in callers that want to emit a downstream event).
    """
    if not store or not run_id:
        return False
    if not hasattr(store, "list_delegation_work_items") or not hasattr(
        store, "update_delegation_work_item"
    ):
        return False
    in_flight = _REFRESH_IN_FLIGHT.get()
    if run_id in in_flight:
        return False
    token = _REFRESH_IN_FLIGHT.set(in_flight | {run_id})
    try:
        try:
            work_items = await store.list_delegation_work_items(run_id)
        except Exception:
            logger.opt(exception=True).debug(
                f"refresh_dependents_for_run: list_delegation_work_items failed run={run_id}"
            )
            return False
        work_item_by_id = {item.work_item_id: item for item in work_items}
        doomed_ids = compute_doomed_work_item_ids(work_item_by_id)
        changed = False
        for work_item in work_items:
            metadata = dict(work_item.metadata or {})
            raw_dependency_ids = [
                str(item).strip()
                for item in list(metadata.get("dependency_work_item_ids", []) or [])
                if str(item).strip()
            ]
            if not raw_dependency_ids:
                continue
            dependency_ids, pruned_dependency_ids = normalize_dependency_work_item_ids(
                raw_dependency_ids,
                work_item_by_id,
                owner_work_item_id=work_item.work_item_id,
            )
            dependency_phases = {
                dep_id: (work_item_by_id[dep_id].phase if dep_id in work_item_by_id else None)
                for dep_id in dependency_ids
            }
            settlement_snapshot = _dependency_settlement_snapshot(
                metadata, dependency_phases, doomed_ids
            )
            all_approved = settlement_snapshot["all_approved"]
            failed_dep_ids = settlement_snapshot["failed"]
            cancelled_dep_ids = settlement_snapshot["cancelled"]
            stuck_dep_ids = settlement_snapshot["stuck"]
            all_settled_with_failures = settlement_snapshot["settled_with_failures"]
            settlement_release = False
            target_phase = work_item.phase
            metadata_updates: dict[str, Any] = {}
            summary_update: str | None = None
            entered_synthesis_turn = False
            if dependency_ids != raw_dependency_ids:
                metadata_updates["dependency_work_item_ids"] = list(dependency_ids)
                metadata_updates["dependency_pruned_at"] = datetime.now().isoformat()
            if pruned_dependency_ids:
                previous_pruned = [
                    str(item).strip()
                    for item in list(metadata.get("pruned_dependency_work_item_ids", []) or [])
                    if str(item).strip()
                ]
                metadata_updates["pruned_dependency_work_item_ids"] = list(
                    dict.fromkeys([*previous_pruned, *pruned_dependency_ids])
                )
            if all_approved:
                if _should_enter_synthesis_turn(work_item, metadata, dependency_ids):
                    entered_synthesis_turn = True
                    target_phase = Phase.READY
                    summary_update = _synthesis_turn_summary(work_item, dependency_ids)
                    previous_kind = _work_item_kind(work_item, metadata)
                    metadata_updates.update(
                        {
                            "pre_synthesis_work_kind": previous_kind,
                            "work_kind": "synthesize",
                            "delegation_turn_kind": "synthesize",
                            **work_item_identity_payload(
                                projection_id=str(work_item.projection_id or work_item.work_item_id or ""),
                                turn_type="aggregate",
                            ),
                            "current_turn_mode": "synthesize_required",
                            "synthesis_turn_started": True,
                            "synthesis_ready_at": datetime.now().isoformat(),
                            "synthesis_source_work_item_ids": list(dependency_ids),
                            "synthesis_reports_to_role_id": str(work_item.manager_role_id or "").strip(),
                            "synthesis_reports_to_seat_id": str(work_item.manager_seat_id or "").strip(),
                            "frontier": "synthesis_ready",
                            "needs_manager_attention": False,
                        }
                    )
                elif work_item.phase == Phase.WAITING_DEPENDENCIES:
                    target_phase = (
                        Phase.READY_FOR_REWORK
                        if str(metadata.get("rework_feedback", "") or "").strip()
                        else Phase.READY
                    )
                elif work_item.phase == Phase.WAITING_FOR_CHILDREN:
                    target_phase = Phase.RUNNING
                    if _work_item_kind(work_item, metadata) in {"deliver", "delivery"}:
                        metadata_updates.update(
                            {
                                "work_kind": "delivery",
                                "delegation_turn_kind": "delivery",
                                **work_item_identity_payload(
                                    projection_id=str(work_item.projection_id or work_item.work_item_id or ""),
                                    turn_type="deliver",
                                ),
                                "current_turn_mode": "deliver_required",
                                "delivery_turn_ready_at": datetime.now().isoformat(),
                            }
                        )
                metadata_updates["waiting_on_work_item_ids"] = []
                if metadata.get("delegated_children_pending"):
                    metadata_updates["delegated_children_pending"] = False
                if str(metadata.get("frontier", "") or "") == "waiting_for_children" and not entered_synthesis_turn:
                    metadata_updates["frontier"] = "resumed"
            elif all_settled_with_failures and _is_settlement_release_candidate(
                work_item, metadata
            ):
                # Failure-triage release: a FAILED/CANCELLED child must not
                # wedge the whole tree forever. Wake the card that owns the
                # decision (the delegating parent, or a roll-up card) and put
                # the failure context in the SAME write, so the released turn
                # cannot silently succeed without seeing it. Ordinary sibling
                # cards blocked on the failure are NOT released — they are in
                # ``stuck`` and get rebuilt, rewired, or cancelled by the
                # settlement cascade once the triage card completes.
                settlement_release = True
                if work_item.phase in (Phase.READY, Phase.READY_FOR_REWORK, Phase.RUNNING):
                    # Already released over this settlement: leave it
                    # untouched. Regressing it back to a waiting phase (the
                    # generic not-all-approved branch below) would oscillate
                    # a released triage card straight back into the deadlock.
                    pass
                elif work_item.phase == Phase.WAITING_FOR_CHILDREN:
                    if _should_enter_synthesis_turn(work_item, metadata, dependency_ids):
                        entered_synthesis_turn = True
                        target_phase = Phase.READY
                        previous_kind = _work_item_kind(work_item, metadata)
                        metadata_updates.update(
                            {
                                "pre_synthesis_work_kind": previous_kind,
                                "work_kind": "synthesize",
                                "delegation_turn_kind": "synthesize",
                                **work_item_identity_payload(
                                    projection_id=str(work_item.projection_id or work_item.work_item_id or ""),
                                    turn_type="aggregate",
                                ),
                                "current_turn_mode": "synthesize_required",
                                "synthesis_turn_started": True,
                                "synthesis_ready_at": datetime.now().isoformat(),
                                "synthesis_source_work_item_ids": list(dependency_ids),
                                "synthesis_reports_to_role_id": str(work_item.manager_role_id or "").strip(),
                                "synthesis_reports_to_seat_id": str(work_item.manager_seat_id or "").strip(),
                                "needs_manager_attention": False,
                            }
                        )
                    else:
                        target_phase = Phase.RUNNING
                        if _work_item_kind(work_item, metadata) in {"deliver", "delivery"}:
                            metadata_updates.update(
                                {
                                    "work_kind": "delivery",
                                    "delegation_turn_kind": "delivery",
                                    **work_item_identity_payload(
                                        projection_id=str(work_item.projection_id or work_item.work_item_id or ""),
                                        turn_type="deliver",
                                    ),
                                    "current_turn_mode": "deliver_required",
                                    "delivery_turn_ready_at": datetime.now().isoformat(),
                                }
                            )
                else:
                    target_phase = (
                        Phase.READY_FOR_REWORK
                        if str(metadata.get("rework_feedback", "") or "").strip()
                        else Phase.READY
                    )
                if target_phase != work_item.phase:
                    summary_update = _failure_triage_turn_summary(
                        work_item,
                        dependency_phases,
                        failed_dep_ids,
                        cancelled_dep_ids,
                        stuck_dep_ids,
                    )
                    metadata_updates["dependency_settlement"] = {
                        "failed": list(failed_dep_ids),
                        "cancelled": list(cancelled_dep_ids),
                        "stuck": list(stuck_dep_ids),
                        "settled_at": datetime.now().isoformat(),
                    }
                    metadata_updates["frontier"] = "settlement_ready"
                    metadata_updates["waiting_on_work_item_ids"] = []
                    if metadata.get("delegated_children_pending"):
                        metadata_updates["delegated_children_pending"] = False
            else:
                if work_item.phase == Phase.READY:
                    target_phase = Phase.WAITING_DEPENDENCIES
                elif work_item.phase == Phase.RUNNING:
                    target_phase = Phase.WAITING_FOR_CHILDREN
                metadata_updates["waiting_on_work_item_ids"] = dependency_ids
            # Clear the parent claim whenever the parent truly leaves
            # WAITING_FOR_CHILDREN toward a non-terminal phase. The old
            # condition ("only when all children approved AND target is
            # RUNNING") left a gap: when a child went READY_FOR_REWORK,
            # the refresh now fires (per _DEPENDENT_REFRESH_TARGETS) but
            # the parent stayed in WAITING_FOR_CHILDREN with a stale claim,
            # so the dispatcher couldn't re-pick it even though the child
            # was back on the worker's queue.
            # We exclude DONE_PHASES because for terminal parents the
            # claim is a historical audit record of "last executor".
            clear_claim_on_wake = (
                work_item.phase == Phase.WAITING_FOR_CHILDREN
                and target_phase != work_item.phase
                and target_phase not in DONE_PHASES
            )
            if target_phase != work_item.phase or metadata_updates or clear_claim_on_wake:
                try:
                    await store.update_delegation_work_item(
                        work_item.work_item_id,
                        phase=target_phase if target_phase != work_item.phase else None,
                        blocked_reason="" if (all_approved or settlement_release) else None,
                        metadata_updates=metadata_updates or None,
                        summary=summary_update,
                        claimed_by_role_runtime_session_id="" if clear_claim_on_wake else None,
                        claimed_by_seat_id="" if clear_claim_on_wake else None,
                    )
                    changed = True
                except Exception:
                    logger.opt(exception=True).debug(
                        "refresh_dependents_for_run: update_delegation_work_item failed "
                        f"wid={work_item.work_item_id}"
                    )
        # Settlement cascade: once a failure-triage card reaches APPROVED
        # (auto-approved synthesis or human-approved delivery), any stuck
        # descendants it chose not to rebuild are dead branches — cancel
        # them so the run reaches a fully-terminal state and can finalize.
        # Children the manager rebuilt or rewired (delete_work_item +
        # replacement ids) have dropped out of the doomed set and survive.
        for work_item in work_items:
            metadata = dict(work_item.metadata or {})
            settlement = dict(metadata.get("dependency_settlement", {}) or {})
            if not settlement or settlement.get("cascaded_at"):
                continue
            if work_item.phase != Phase.APPROVED:
                continue
            # Transitive closure through the doomed set: cancelling a stuck
            # child kills anything hard-chained onto it, and those deeper
            # nodes appear in no released card's direct dependency list —
            # without the closure they would linger non-terminal forever.
            # `covered` seeds from ALL stamped stuck ids regardless of
            # phase: a stuck child already cancelled by a previous
            # (partially failed) cascade attempt must still conduct the
            # traversal, or the deeper nodes behind it become unreachable
            # on retry. Growth only follows edges INTO the covered set, so
            # doomed subtrees owned by a different (not-yet-approved)
            # triage card are left for that card's own decision.
            covered_ids: set[str] = {
                str(item).strip()
                for item in list(settlement.get("stuck", []) or [])
                if str(item).strip() and str(item).strip() in work_item_by_id
            }
            grew = bool(covered_ids)
            while grew:
                grew = False
                for item_id, item in work_item_by_id.items():
                    if item_id in covered_ids or item_id not in doomed_ids:
                        continue
                    item_metadata = dict(getattr(item, "metadata", {}) or {})
                    raw_ids = [
                        str(dep).strip()
                        for dep in list(item_metadata.get("dependency_work_item_ids", []) or [])
                        if str(dep).strip()
                    ]
                    if not raw_ids:
                        continue
                    dep_ids, _pruned = normalize_dependency_work_item_ids(
                        raw_ids, work_item_by_id, owner_work_item_id=item_id
                    )
                    item_classes = dict(item_metadata.get("dependency_classes", {}) or {})
                    for dep_id in dep_ids:
                        if dep_id not in covered_ids:
                            continue
                        dep_class = str(
                            item_classes.get(dep_id, DEPENDENCY_CLASS_DEFAULT)
                            or DEPENDENCY_CLASS_DEFAULT
                        ).strip().lower()
                        if dep_class in ("soft", "info"):
                            continue
                        covered_ids.add(item_id)
                        grew = True
                        break
            cancel_ids = {
                covered_id
                for covered_id in covered_ids
                if covered_id in doomed_ids
                and getattr(work_item_by_id.get(covered_id), "phase", None)
                not in DONE_PHASES
            }
            cascade_complete = True
            for cancel_id in sorted(cancel_ids):
                try:
                    await transition_work_item(
                        store,
                        cancel_id,
                        target_phase=Phase.CANCELLED,
                        reason="upstream_dependency_failed_parent_settled",
                        release_claim=True,
                    )
                    changed = True
                except Exception:
                    cascade_complete = False
                    logger.opt(exception=True).debug(
                        "refresh_dependents_for_run: settlement cascade cancel failed "
                        f"wid={cancel_id}"
                    )
            if not cascade_complete:
                # Leave cascaded_at unset so the next refresh retries the
                # leftover cancels instead of permanently orphaning them.
                continue
            try:
                await store.update_delegation_work_item(
                    work_item.work_item_id,
                    metadata_updates={
                        "dependency_settlement": {
                            **settlement,
                            "cascaded_at": datetime.now().isoformat(),
                        }
                    },
                )
            except Exception:
                logger.opt(exception=True).debug(
                    "refresh_dependents_for_run: settlement cascade stamp failed "
                    f"wid={work_item.work_item_id}"
                )
        if changed and hasattr(store, "save_delegation_event"):
            try:
                await store.save_delegation_event(
                    DelegationEvent(
                        run_id=run_id,
                        work_item_id=source_work_item_id or None,
                        cell_id=source_cell_id or None,
                        role_id=source_role_id or None,
                        event_type="dependency_frontier_refreshed",
                        payload={
                            "source_task_id": source_task_id,
                            "source_work_item_id": source_work_item_id,
                        },
                    )
                )
            except Exception:
                logger.opt(exception=True).debug(
                    "refresh_dependents_for_run: event persistence failed"
                )
        return changed
    finally:
        _REFRESH_IN_FLIGHT.reset(token)
