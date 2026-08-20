"""Phase-transition hooks.

Each hook subscribes to `on_phase_transition` from `phase.py` and is
responsible for ONE downstream layer:

    sync_task_status_hook       → tasks.status follows work_item.phase
    signal_dispatcher_hook      → kick the dispatcher loop awake when a
                                   transition opens new dispatchable work
    refresh_dependents_hook     → propagate child terminal/escalation
                                   transitions up the dep graph so stuck
                                   leaders wake (Fix 3)

Phase B note: the old ``wake_parent_on_resume_hook`` /
``sync_member_session_hook`` / ``_REENQUEUE_WORK_ITEM_HOOKS`` /
``_RUNTIME_RECONCILER_HOOKS`` machinery has been removed. Parent wake
and rework dispatch are now handled by the dispatcher's per-tick
rehydrate pass (``CompanyMode._execute_multi_team_org`` unparks stale
member sessions + re-enqueues runnable work items from the DB every
iteration). Keeping the DB as the single source of truth and having
the dispatcher converge on each tick replaces three layers of
cross-layer sync hooks — much less to break.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from opc.core.models import Phase
from opc.layer2_organization.phase import (
    DONE_PHASES,
    RUNNABLE_PHASES,
    is_stale_claim_releasable,
    register_phase_transition_hook,
    task_status_for_phase,
)
from opc.layer2_organization.work_item_transition import refresh_dependents_for_run


# Module-level singleton for dispatcher signalling. CompanyMode populates
# this in its constructor with a callable that sets the wake event. We
# keep it as a list (rather than a single callable) so multiple engines
# can coexist (rare, but happens in tests).
_DISPATCHER_WAKE_HOOKS: list[Any] = []


def register_dispatcher_wake(callback: Any) -> None:
    """CompanyMode calls this to register its `_signal_dispatcher_wake`
    so the phase-transition hook can fire it without an import cycle."""
    if callback not in _DISPATCHER_WAKE_HOOKS:
        _DISPATCHER_WAKE_HOOKS.append(callback)


def unregister_dispatcher_wake(callback: Any) -> None:
    try:
        _DISPATCHER_WAKE_HOOKS.remove(callback)
    except ValueError:
        pass


# ── Hook 1: task.status follows work_item.phase ──────────────────────────

async def sync_task_status_hook(
    previous: Phase | None,
    target: Phase,
    item: Any,
    *,
    store: Any,
) -> None:
    """Whenever a work-item phase changes, project the new TaskStatus and
    persist it on the linked task. Without this, task.status drifts from
    work_item.phase (the bug behind the app04 deadlock — task remained
    BLOCKED even after work_item moved WAITING_FOR_CHILDREN → RUNNING).

    Company-mode boundary: this hook keeps the internal runtime Task aligned
    with the WorkItem; it does not make Task a second business-state owner.
    """
    # Even idempotent work-item saves should project the phase back onto
    # the linked task. This repairs task/work_item drift when an older
    # task row is still awaiting review after the work item already
    # reached a terminal phase.
    desired_status = task_status_for_phase(target)
    fenced_projection = getattr(
        store,
        "sync_runtime_task_status_for_work_item_projection",
        None,
    )
    if callable(fenced_projection):
        try:
            await fenced_projection(item, desired_status)
        except Exception:
            logger.opt(exception=True).debug(
                "sync_task_status_hook: fenced Task projection failed"
            )
        return
    if not hasattr(store, "get_task") or not hasattr(store, "save_task"):
        return
    try:
        task = None
        get_runtime_task = getattr(store, "get_runtime_task_for_work_item", None)
        if callable(get_runtime_task):
            task = await get_runtime_task(str(getattr(item, "work_item_id", "") or "").strip())
    except Exception:
        return
    if task is None:
        return
    if task.status == desired_status:
        return
    task.status = desired_status
    try:
        await store.save_task(task)
    except Exception:
        logger.opt(exception=True).debug("sync_task_status_hook: save_task failed")


# ── Hook 2: kick the dispatcher when a transition opens dispatchable work ─

_DISPATCHER_WAKE_TARGETS = RUNNABLE_PHASES | DONE_PHASES


async def signal_dispatcher_hook(
    previous: Phase | None,
    target: Phase,
    item: Any,
    *,
    store: Any,
) -> None:
    """Fire the dispatcher's wake event whenever a phase transition
    opens the door to new work or unblocks downstream dependents.

    Specifically: any transition to RUNNABLE_PHASES (READY /
    READY_FOR_REWORK) or DONE_PHASES (children-done propagation) should
    immediately ping the loop. Without this, the dispatcher only sees
    the change on its next periodic tick (slower UX).
    """
    if previous == target:
        return
    if target not in _DISPATCHER_WAKE_TARGETS and target != Phase.RUNNING:
        # Allow RUNNING because parent-unblock transitions to RUNNING and
        # the dispatcher needs to re-pick the parent.
        return
    for cb in list(_DISPATCHER_WAKE_HOOKS):
        try:
            cb()
        except Exception:
            logger.opt(exception=True).debug("dispatcher wake callback raised")


# Controller-owned phase transitions persist the WorkItem and Task together.
# After that commit only in-memory notifications may run without carrying the
# transaction's owner/generation fence; every durable convergence hook is
# deferred to the winning dispatcher's next DB-driven reconcile tick.
signal_dispatcher_hook._phase_notification_only = True  # type: ignore[attr-defined]


# ── Hook 3: propagate child terminal/escalation to parent dep frontier ──

# Which transitions can unblock or re-evaluate a parent.
# APPROVED — may make parent's all_approved true → WAITING_FOR_CHILDREN → RUNNING.
# FAILED / CANCELLED — parent should see the child exited; same refresh pass
# rewrites waiting_on_work_item_ids and lets higher-level policy decide next.
# AWAITING_HUMAN — a final delivery or explicit owner-input gate needs a human
# decision; the frontier refresh keeps parent metadata consistent (e.g.
# waiting_on_work_item_ids). Manager review never escalates to this phase: its
# only outcomes are APPROVED or READY_FOR_REWORK. When an actual owner gate is
# subsequently approved (AWAITING_HUMAN → APPROVED), that transition also
# fires this hook and finally unblocks the parent.
# READY_FOR_REWORK — child was sent back to worker by reviewer. Parent's
# waiting_on_work_item_ids is stale in the opposite direction (the child is
# no longer "done" from parent's perspective) and parent's claim may be
# holding the parent's session hostage. Refresh keeps the frontier accurate
# and triggers claim release on wake (see clear_claim_on_wake in
# work_item_transition.refresh_dependents_for_run).
_DEPENDENT_REFRESH_TARGETS: frozenset[Phase] = DONE_PHASES | frozenset({
    Phase.AWAITING_HUMAN,
    Phase.READY_FOR_REWORK,
})


async def refresh_dependents_hook(
    previous: Phase | None,
    target: Phase,
    item: Any,
    *,
    store: Any,
) -> None:
    """Fix 3 — single entry point for parent dep-frontier updates.

    Before this hook existed, ``_refresh_delegation_dependents`` was
    only called on the APPROVED-verdict branch of
    ``_finalize_review_work_item``. An explicit owner gate or CANCELLED child
    never triggered the refresh, so its parent's ``waiting_on_work_item_ids``
    and claim state drifted from reality. new16/app12 reproduced this: cto
    parent ``cdb248d8`` sat in WAITING_FOR_CHILDREN for 13+ minutes with the
    claim held by an idle session, neither runnable nor orphaned.

    Re-entrancy is handled inside ``refresh_dependents_for_run`` via a
    ContextVar, so transitive updates (parent → RUNNING → some ancestor)
    don't re-walk the same run.
    """
    if previous == target:
        return
    if target not in _DEPENDENT_REFRESH_TARGETS:
        return
    run_id = str(getattr(item, "run_id", "") or "").strip()
    if not run_id:
        return
    try:
        await refresh_dependents_for_run(
            store,
            run_id=run_id,
            source_work_item_id=str(getattr(item, "work_item_id", "") or "").strip() or None,
            source_role_id=str(getattr(item, "role_id", "") or "").strip() or None,
            source_cell_id=str(getattr(item, "cell_id", "") or "").strip() or None,
        )
    except Exception:
        logger.opt(exception=True).debug(
            "refresh_dependents_hook: refresh_dependents_for_run raised"
        )


# ── Hook 4: clear stale role-session focus when work items terminate ─────

# Firing scope — any transition into a terminal DB phase. We deliberately
# do NOT include AWAITING_HUMAN: a human still needs to act on those, and
# the session that escalated is legitimately still "focused" on the item
# until the human resolves it.
_FOCUS_CLEAR_TARGETS: frozenset[Phase] = DONE_PHASES


async def clear_session_focus_on_terminal_hook(
    previous: Phase | None,
    target: Phase,
    item: Any,
    *,
    store: Any,
) -> None:
    """Null out ``focused_work_item_id`` on any role_runtime_session still
    pointing at ``item`` once it reaches a terminal phase. Also demotes
    ``status='blocked'`` → ``'idle'`` for those sessions so the UI and
    dispatcher rehydrate pass no longer see a "CTO blocked on X" row
    after X has been approved / failed / cancelled.

    Why a hook and not a routine cleanup in ``complete_claim``:
    ``complete_claim`` only runs on the claim-release path. Work items
    transitioning via review verdict, migration, or direct store writes
    bypass it, leaving session.focused_work_item_id stale. new16/app13
    reproduced the symptom on two leader sessions (cto / coo) that kept
    ``status=blocked`` + focus on APPROVED work items for ~30 minutes.

    Not functionally blocking — the dispatcher keys off work_item.phase,
    not session focus — but the UI mislead users and the per-tick
    rehydrate pass did dead work on the stale rows.

    Fix 5 PR3: when the serial-queue flag is on, after clearing focus
    we reconcile the role's pending queue and clear the next valid
    ``queued_behind_session`` stamp so the dispatcher picks it up.
    """
    if previous == target:
        return
    if target not in _FOCUS_CLEAR_TARGETS:
        return
    wid = str(getattr(item, "work_item_id", "") or "").strip()
    if not wid:
        return
    run_id = str(getattr(item, "run_id", "") or "").strip()
    if not run_id:
        return
    if not hasattr(store, "list_role_runtime_sessions") or not hasattr(
        store, "update_delegation_role_session"
    ):
        return
    try:
        sessions = await store.list_role_runtime_sessions(run_id)
    except Exception:
        logger.opt(exception=True).debug(
            "clear_session_focus_on_terminal_hook: list_role_runtime_sessions failed "
            f"run_id={run_id}"
        )
        return
    queue_enabled = bool(getattr(store, "role_serial_queue_enabled", False))
    item_metadata = dict(getattr(item, "metadata", {}) or {})
    controller_owner_token = str(
        item_metadata.get("company_run_controller_owner_token", "") or ""
    ).strip()
    controller_lease_generation = int(
        item_metadata.get("company_run_controller_lease_generation", 0) or 0
    )
    project_id = "default"
    affected_role_session_ids: set[str] = set()
    item_role_session_id = str(
        getattr(item, "role_runtime_session_id", "") or ""
    ).strip()
    if item_role_session_id:
        affected_role_session_ids.add(item_role_session_id)
    for session in sessions:
        session_project_id = str(
            getattr(session, "project_id", "") or ""
        ).strip()
        if session_project_id:
            project_id = session_project_id
        if str(getattr(session, "focused_work_item_id", "") or "") != wid:
            continue
        sid = str(getattr(session, "role_session_id", "") or "").strip()
        if sid:
            affected_role_session_ids.add(sid)
        current_status = str(getattr(session, "status", "") or "").strip().lower()
        status_override = "idle" if current_status == "blocked" else None
        try:
            await store.update_delegation_role_session(
                session.role_session_id,
                focused_work_item_id="",
                status=status_override,
                controller_owner_token=controller_owner_token,
                controller_lease_generation=controller_lease_generation,
            )
        except Exception:
            logger.opt(exception=True).debug(
                "clear_session_focus_on_terminal_hook: update_delegation_role_session "
                f"failed session_id={session.role_session_id}"
            )
            continue
    if queue_enabled and affected_role_session_ids:
        # PR3 originally promoted only when the terminal item was still
        # focused by the session. In practice the focus may already have
        # been cleared by a different path while the queued marker remains
        # on the next card. Reconcile the assigned role session either way.
        await reconcile_role_serial_queues(
            store,
            run_id,
            role_session_ids=affected_role_session_ids,
            project_id=project_id,
            controller_owner_token=controller_owner_token,
            controller_lease_generation=controller_lease_generation,
        )


def _work_item_id(item: Any) -> str:
    return str(getattr(item, "work_item_id", "") or "").strip()


def _work_item_role_session_id(item: Any) -> str:
    return str(getattr(item, "role_runtime_session_id", "") or "").strip()


def _queued_behind_session(item: Any) -> str:
    metadata = getattr(item, "metadata", {}) or {}
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("queued_behind_session", "") or "").strip()


def _queue_candidate(item: Any) -> bool:
    if getattr(item, "phase", None) not in RUNNABLE_PHASES:
        return False
    claimed = str(
        getattr(item, "claimed_by_role_runtime_session_id", "") or ""
    ).strip()
    return not claimed


async def _clear_queued_marker(
    item: Any,
    store: Any,
    *,
    expected_session_id: str | None = None,
    project_id: str = "default",
    controller_owner_token: str = "",
    controller_lease_generation: int = 0,
) -> bool:
    metadata = dict(getattr(item, "metadata", {}) or {})
    marker = str(metadata.get("queued_behind_session", "") or "").strip()
    if not marker:
        return False
    if expected_session_id is not None and marker != str(expected_session_id or "").strip():
        return False
    fenced_update = getattr(
        store,
        "update_work_item_serial_queue_marker_for_controller",
        None,
    )
    try:
        if callable(fenced_update):
            changed = await fenced_update(
                _work_item_id(item),
                run_id=str(getattr(item, "run_id", "") or "").strip(),
                project_id=str(project_id or "default").strip() or "default",
                queued_behind_session=None,
                expected_queued_behind_session=marker,
                controller_owner_token=str(controller_owner_token or "").strip(),
                controller_lease_generation=int(
                    controller_lease_generation or 0
                ),
            )
            if not changed:
                return False
        else:
            metadata.pop("queued_behind_session", None)
            item.metadata = metadata
            await store.save_delegation_work_item(item)
    except Exception:
        logger.opt(exception=True).debug(
            f"clear queued_behind_session failed wid={_work_item_id(item)}"
        )
        return False
    metadata.pop("queued_behind_session", None)
    item.metadata = metadata
    return True


async def _save_role_session(
    store: Any,
    session: Any,
    *,
    controller_owner_token: str = "",
    controller_lease_generation: int = 0,
) -> bool:
    try:
        await store.save_delegation_role_session(
            session,
            controller_owner_token=str(controller_owner_token or "").strip(),
            controller_lease_generation=int(controller_lease_generation or 0),
        )
        return True
    except Exception:
        logger.opt(exception=True).debug(
            "save_delegation_role_session failed during serial queue reconcile "
            f"sid={getattr(session, 'role_session_id', '')}"
        )
        return False


def _active_focus_id(session: Any, work_item_by_id: dict[str, Any]) -> str:
    focused = str(getattr(session, "focused_work_item_id", "") or "").strip()
    if not focused:
        return ""
    focused_item = work_item_by_id.get(focused)
    if focused_item is None:
        return ""
    if getattr(focused_item, "phase", None) in DONE_PHASES:
        return ""
    phase = getattr(focused_item, "phase", None)
    if is_stale_claim_releasable(phase):
        claim = str(
            getattr(focused_item, "claimed_by_role_runtime_session_id", "") or ""
        ).strip()
        if not claim:
            # On a live transition the focused item retains its claim. After
            # restart the startup sweep removes that dead-process claim; the
            # persisted session focus must stop blocking the orphaned item (or
            # a passive parent's report/review helper) from being dispatched.
            return ""
    return focused


async def reconcile_role_serial_queues(
    store: Any,
    run_id: str,
    *,
    role_session_ids: set[str] | list[str] | tuple[str, ...] | None = None,
    project_id: str = "default",
    controller_owner_token: str = "",
    controller_lease_generation: int = 0,
) -> dict[str, Any]:
    """Repair derived serial-queue state for a run.

    ``pending_work_item_ids`` and ``focused_work_item_id`` are the source of
    truth. ``metadata.queued_behind_session`` is only a dispatcher filter, so
    it must be cleared when it no longer matches the role session's queue.
    """
    result: dict[str, Any] = {
        "run_id": str(run_id or "").strip(),
        "cleared_markers": [],
        "pruned_pending_ids": [],
        "promoted_work_item_ids": [],
        "cleared_focus_session_ids": [],
    }
    if not bool(getattr(store, "role_serial_queue_enabled", False)):
        return result
    rid = result["run_id"]
    if not rid:
        return result
    required = (
        "list_role_runtime_sessions",
        "list_delegation_work_items",
        "save_delegation_role_session",
        "save_delegation_work_item",
    )
    if any(not hasattr(store, name) for name in required):
        return result
    wanted_sessions = {
        str(item).strip()
        for item in list(role_session_ids or [])
        if str(item).strip()
    }
    try:
        all_sessions = await store.list_role_runtime_sessions(rid)
        work_items = await store.list_delegation_work_items(rid)
    except Exception:
        logger.opt(exception=True).debug(
            f"reconcile_role_serial_queues: load failed run_id={rid}"
        )
        return result
    sessions = list(all_sessions)
    if wanted_sessions:
        sessions = [
            session for session in sessions
            if str(getattr(session, "role_session_id", "") or "").strip()
            in wanted_sessions
        ]
    session_by_id = {
        str(getattr(session, "role_session_id", "") or "").strip(): session
        for session in sessions
        if str(getattr(session, "role_session_id", "") or "").strip()
    }
    all_session_ids = {
        str(getattr(session, "role_session_id", "") or "").strip()
        for session in all_sessions
        if str(getattr(session, "role_session_id", "") or "").strip()
    }
    work_item_by_id = {
        _work_item_id(item): item
        for item in work_items
        if _work_item_id(item)
    }
    pending_after_by_session: dict[str, list[str]] = {}
    blocker_by_session: dict[str, str] = {}
    effective_project_id = str(project_id or "default").strip() or "default"
    if effective_project_id == "default" and sessions:
        session_project_id = str(
            getattr(sessions[0], "project_id", "") or ""
        ).strip()
        if session_project_id:
            effective_project_id = session_project_id

    for session in sessions:
        sid = str(getattr(session, "role_session_id", "") or "").strip()
        if not sid:
            continue
        original_pending = [
            str(item).strip()
            for item in list(getattr(session, "pending_work_item_ids", []) or [])
            if str(item).strip()
        ]
        original_focus = str(getattr(session, "focused_work_item_id", "") or "").strip()
        active_focus = _active_focus_id(session, work_item_by_id)
        session_changed = False
        if original_focus and not active_focus:
            session.focused_work_item_id = ""
            session.status = "idle"
            session_changed = True
            result["cleared_focus_session_ids"].append(sid)
        elif active_focus and str(getattr(session, "status", "") or "").strip().lower() == "idle":
            session.status = "running"
            session_changed = True

        clean_pending: list[str] = []
        seen_pending: set[str] = set()
        for pending_id in original_pending:
            if pending_id in seen_pending:
                result["pruned_pending_ids"].append(pending_id)
                continue
            seen_pending.add(pending_id)
            pending_item = work_item_by_id.get(pending_id)
            if pending_item is None or not _queue_candidate(pending_item):
                result["pruned_pending_ids"].append(pending_id)
                if pending_item is not None and await _clear_queued_marker(
                    pending_item,
                    store,
                    expected_session_id=sid,
                    project_id=effective_project_id,
                    controller_owner_token=controller_owner_token,
                    controller_lease_generation=controller_lease_generation,
                ):
                    result["cleared_markers"].append(pending_id)
                continue
            clean_pending.append(pending_id)

        front_unqueued = ""
        if not active_focus:
            for candidate in work_items:
                candidate_id = _work_item_id(candidate)
                if not candidate_id or candidate_id in clean_pending:
                    continue
                if _work_item_role_session_id(candidate) != sid:
                    continue
                if not _queue_candidate(candidate):
                    continue
                if _queued_behind_session(candidate):
                    continue
                front_unqueued = candidate_id
                break

        promoted = ""
        if not active_focus and not front_unqueued and clean_pending:
            promoted = clean_pending.pop(0)
            promoted_item = work_item_by_id.get(promoted)
            if promoted_item is not None and await _clear_queued_marker(
                promoted_item,
                store,
                expected_session_id=sid,
                project_id=effective_project_id,
                controller_owner_token=controller_owner_token,
                controller_lease_generation=controller_lease_generation,
            ):
                result["cleared_markers"].append(promoted)
            result["promoted_work_item_ids"].append(promoted)

        if clean_pending != original_pending:
            session.pending_work_item_ids = clean_pending
            session_changed = True
        if not active_focus and not front_unqueued and not promoted:
            if str(getattr(session, "status", "") or "").strip().lower() != "idle":
                session.status = "idle"
                session.focused_work_item_id = ""
                session_changed = True
        if session_changed:
            await _save_role_session(
                store,
                session,
                controller_owner_token=controller_owner_token,
                controller_lease_generation=controller_lease_generation,
            )
        pending_after_by_session[sid] = list(clean_pending)
        blocker_by_session[sid] = active_focus or front_unqueued or promoted

    for item in work_items:
        item_id = _work_item_id(item)
        marker = _queued_behind_session(item)
        if not item_id or not marker:
            continue
        if wanted_sessions and marker not in wanted_sessions:
            continue
        valid = False
        if marker in all_session_ids:
            pending = pending_after_by_session.get(marker)
            if pending is None and not wanted_sessions:
                session = session_by_id.get(marker)
                pending = list(getattr(session, "pending_work_item_ids", []) or []) if session is not None else []
            blocker = blocker_by_session.get(marker, "")
            valid = item_id in list(pending or []) and bool(blocker) and blocker != item_id
        if valid:
            continue
        if await _clear_queued_marker(
            item,
            store,
            expected_session_id=marker,
            project_id=effective_project_id,
            controller_owner_token=controller_owner_token,
            controller_lease_generation=controller_lease_generation,
        ):
            result["cleared_markers"].append(item_id)

    if (
        result["cleared_markers"]
        or result["pruned_pending_ids"]
        or result["promoted_work_item_ids"]
        or result["cleared_focus_session_ids"]
    ):
        logger.debug(f"serial queue reconciled: {result}")
        if hasattr(store, "save_runtime_event"):
            try:
                await store.save_runtime_event(
                    rid,
                    "serial_queue_reconciled",
                    result,
                )
            except Exception:
                logger.opt(exception=True).debug(
                    f"serial_queue_reconciled event emit failed run_id={rid}"
                )
        if result["promoted_work_item_ids"]:
            for cb in list(_DISPATCHER_WAKE_HOOKS):
                try:
                    cb()
                except Exception:
                    logger.opt(exception=True).debug("dispatcher wake callback raised")
    return result


async def _promote_next_pending_for_session(session: Any, store: Any) -> None:
    """Compatibility helper that reconciles one session's serial queue.

    The reconciler prunes dead entries before promoting the next runnable
    item, so this wrapper is safer than blindly popping the FIFO head.
    """
    sid = str(getattr(session, "role_session_id", "") or "").strip()
    if not sid:
        return
    run_id = str(getattr(session, "run_id", "") or "").strip()
    if not run_id:
        return
    await reconcile_role_serial_queues(store, run_id, role_session_ids={sid})


# ── Hook 5 (Fix 5 PR3): enqueue runnable work for busy sessions ──────────

_RUNNABLE_ENQUEUE_TARGETS: frozenset[Phase] = RUNNABLE_PHASES


async def enqueue_session_work_on_runnable_hook(
    previous: Phase | None,
    target: Phase,
    item: Any,
    *,
    store: Any,
) -> None:
    """When a work item becomes runnable for a role whose session is
    already focused on something else, append it to the session's pending
    queue and stamp ``metadata.queued_behind_session`` so the dispatcher
    skips it until its turn.

    Gated by ``store.role_serial_queue_enabled``. The flag remains available
    for explicit compatibility tests, but company mode enables serial role
    queues by default.

    Design notes:
    * The enqueue is idempotent. If the work item is already in the
      queue, ``enqueue_pending_work_item`` returns False and this hook
      skips the metadata stamp. That handles the case where the hook
      fires twice on the same item (e.g. READY → RUNNING → READY_FOR_REWORK
      cycling) without double-enqueuing.
    * We deliberately leave the work item's phase alone — it stays
      READY/READY_FOR_REWORK so the dispatcher still considers it, and
      the claim filter (PR3.4) uses the ``queued_behind_session`` stamp
      to decide whether to skip.
    * No work is enqueued when the session's own focus IS this work item
      (transient race during a claim — the session just grabbed the item
      and its phase transitioned to RUNNING then back). That's handled by
      the focus check: if ``session.focused_work_item_id == wid`` the
      session is NOT busy with a different item, so we don't enqueue.
    """
    if previous == target:
        return
    if not bool(getattr(store, "role_serial_queue_enabled", False)):
        return
    if target not in _RUNNABLE_ENQUEUE_TARGETS:
        return
    wid = str(getattr(item, "work_item_id", "") or "").strip()
    if not wid:
        return
    sid = str(getattr(item, "role_runtime_session_id", "") or "").strip()
    if not sid:
        # No session stamped on the work item yet — the engine's bootstrap
        # will stamp it before dispatch. Skip; we'll see the next
        # transition once the session id is available.
        return
    # Is the session busy with a different work item?
    try:
        session = await store.get_delegation_role_session(sid)
    except Exception:
        logger.opt(exception=True).debug(
            f"enqueue_session_work_on_runnable_hook: get_delegation_role_session "
            f"failed sid={sid}"
        )
        return
    if session is None:
        return
    focused = str(getattr(session, "focused_work_item_id", "") or "").strip()
    if not focused or focused == wid:
        # Session is idle or already holding this exact work item — let
        # the normal claim path proceed without queueing.
        return
    # Append to queue, stamp metadata so the claim filter skips this wid.
    item_metadata = dict(getattr(item, "metadata", {}) or {})
    try:
        enqueued = await store.enqueue_pending_work_item(
            sid,
            wid,
            controller_owner_token=str(
                item_metadata.get("company_run_controller_owner_token", "") or ""
            ).strip(),
            controller_lease_generation=int(
                item_metadata.get(
                    "company_run_controller_lease_generation",
                    0,
                )
                or 0
            ),
        )
    except Exception:
        logger.opt(exception=True).debug(
            f"enqueue_session_work_on_runnable_hook: enqueue failed "
            f"sid={sid} wid={wid}"
        )
        return
    if not enqueued:
        return  # already in the queue — nothing more to do
    metadata = item_metadata
    if metadata.get("queued_behind_session") == sid:
        return
    marker_before = str(metadata.get("queued_behind_session", "") or "").strip()
    fenced_update = getattr(
        store,
        "update_work_item_serial_queue_marker_for_controller",
        None,
    )
    try:
        if callable(fenced_update):
            updated = await fenced_update(
                wid,
                run_id=str(getattr(item, "run_id", "") or "").strip(),
                project_id=str(getattr(session, "project_id", "") or "default"),
                queued_behind_session=sid,
                expected_queued_behind_session=marker_before,
                controller_owner_token=str(
                    item_metadata.get(
                        "company_run_controller_owner_token",
                        "",
                    )
                    or ""
                ).strip(),
                controller_lease_generation=int(
                    item_metadata.get(
                        "company_run_controller_lease_generation",
                        0,
                    )
                    or 0
                ),
            )
            if not updated:
                return
        else:
            await store.update_delegation_work_item(
                wid, metadata_updates={"queued_behind_session": sid}
            )
    except Exception:
        logger.opt(exception=True).debug(
            f"enqueue_session_work_on_runnable_hook: update_delegation_work_item "
            f"failed wid={wid}"
        )
        return
    metadata["queued_behind_session"] = sid
    item.metadata = metadata
    # Fix 5 PR7 observability: emit a runtime event when the queue
    # crosses the attention threshold. Fires at the *crossing* so we
    # don't spam on every subsequent enqueue — dedup handled by checking
    # that the queue length equals the threshold exactly after this
    # enqueue (previous state was threshold-1 or lower, now we're at
    # threshold). ``STUCK_QUEUE_DEPTH_THRESHOLD`` is conservative;
    # operators can watch for the event and investigate before a true
    # pile-up develops.
    try:
        refreshed = await store.get_delegation_role_session(sid)
    except Exception:
        refreshed = None
    if refreshed is not None:
        depth = len(list(getattr(refreshed, "pending_work_item_ids", []) or []))
        if depth == STUCK_QUEUE_DEPTH_THRESHOLD and hasattr(store, "save_runtime_event"):
            try:
                await store.save_runtime_event(
                    sid,
                    "stuck_session_queue_depth",
                    {
                        "role_session_id": sid,
                        "role_id": str(getattr(refreshed, "role_id", "") or ""),
                        "run_id": str(getattr(refreshed, "run_id", "") or ""),
                        "focused_work_item_id": str(
                            getattr(refreshed, "focused_work_item_id", "") or ""
                        ),
                        "queue_depth": depth,
                        "threshold": STUCK_QUEUE_DEPTH_THRESHOLD,
                        "triggered_by_work_item_id": wid,
                    },
                )
            except Exception:
                logger.opt(exception=True).debug(
                    f"PR7 stuck-queue event emit failed sid={sid}"
                )


# ── Fix 5 PR7 thresholds ─────────────────────────────────────────────────

# Queue-depth threshold: when a role's pending queue first reaches this,
# we emit ``stuck_session_queue_depth``. Chosen conservatively — with the
# serial queue on, a handful of pending items is normal during a delegation
# wave; 5 in flight to one role means upstream is producing faster than
# the role can drain and ops should look.
STUCK_QUEUE_DEPTH_THRESHOLD = 5

# Stuck-focus threshold (minutes): when a session has held the same
# ``focused_work_item_id`` for longer than this without a phase transition,
# ``check_stuck_focused_sessions`` emits a ``stuck_session_focused`` event.
# This is a poll-style check; call it periodically from a dispatcher tick
# or from an ops CLI (not auto-fired by the phase hooks).
STUCK_FOCUS_MINUTES = 10


async def check_stuck_focused_sessions(
    store: Any,
    *,
    run_id: str | None = None,
    threshold_minutes: int = STUCK_FOCUS_MINUTES,
) -> list[dict[str, Any]]:
    """Scan role_runtime_sessions whose ``focused_work_item_id`` has been
    set for longer than ``threshold_minutes`` and emit one
    ``stuck_session_focused`` runtime event per offender.

    Returns a list of the emitted event payloads for ops visibility.
    Safe to call on every tick — events are cheap inserts and the dedup
    is done by the consumer (downstream dashboards filter by last-seen-time).
    """
    from datetime import datetime, timedelta

    if not hasattr(store, "list_role_runtime_sessions"):
        return []
    if run_id:
        sessions = await store.list_role_runtime_sessions(run_id)
    else:
        # Walk every known run. The store exposes per-run listing only,
        # so gather run_ids from delegation_runs first.
        if not hasattr(store, "list_delegation_runs"):
            return []
        try:
            runs = await store.list_delegation_runs()
        except Exception:
            return []
        sessions = []
        for run in runs:
            try:
                sessions.extend(await store.list_role_runtime_sessions(run.run_id))
            except Exception:
                continue

    threshold = timedelta(minutes=max(0, int(threshold_minutes)))
    now = datetime.now()
    emitted: list[dict[str, Any]] = []
    for session in sessions:
        focused = str(getattr(session, "focused_work_item_id", "") or "").strip()
        if not focused:
            continue
        updated_at = getattr(session, "updated_at", None)
        if not isinstance(updated_at, datetime):
            continue
        if now - updated_at < threshold:
            continue
        payload = {
            "role_session_id": session.role_session_id,
            "role_id": getattr(session, "role_id", ""),
            "run_id": getattr(session, "run_id", ""),
            "focused_work_item_id": focused,
            "focused_for_minutes": int((now - updated_at).total_seconds() // 60),
            "threshold_minutes": int(threshold_minutes),
            "queue_depth": len(list(getattr(session, "pending_work_item_ids", []) or [])),
        }
        if hasattr(store, "save_runtime_event"):
            try:
                await store.save_runtime_event(
                    session.role_session_id,
                    "stuck_session_focused",
                    payload,
                )
            except Exception:
                logger.opt(exception=True).debug(
                    f"PR7 stuck-focus event emit failed "
                    f"sid={session.role_session_id}"
                )
        emitted.append(payload)
    return emitted


# ── Module-level registration ────────────────────────────────────────────

register_phase_transition_hook(sync_task_status_hook)
register_phase_transition_hook(signal_dispatcher_hook)
register_phase_transition_hook(refresh_dependents_hook)
register_phase_transition_hook(clear_session_focus_on_terminal_hook)
# Fix 5 PR3 — gated internally on ``store.role_serial_queue_enabled``,
# always-registered so flipping the flag at runtime takes effect without
# re-wiring the hook list.
register_phase_transition_hook(enqueue_session_work_on_runnable_hook)
