"""Background sweeper that re-opens DONE tasks when new actionable mail arrives.

The company-mode end-of-turn hook (``_reactivate_for_unread_mail``) already
catches the common case where mail arrived *before* a task finished, but it
only runs at task-completion boundaries. When a DONE task's role receives a
blocking/actionable DM afterwards, nothing spontaneously wakes the role — in
previous versions of OPC the gap was hidden by a main-LLM "impersonation
reply" fallback that let the sender unblock but never involved the role's own
agent.

This sweeper closes the gap without introducing new abstractions: every few
seconds it re-scans DONE tasks for the active project and calls the existing
``reactivate_fn`` (which reuses all the guard logic in
``CompanyWorkItemExecutor._reactivate_for_unread_mail`` — fingerprint check,
depth cap, cross-role ping-pong detection). The scheduler then picks the task
up naturally and the external_broker's session-resume path hands the agent
back a fully contextualized codex/claude_code session.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from loguru import logger

from opc.core.models import Task, TaskStatus
from opc.layer2_organization.company_runtime_identity import (
    is_runtime_auxiliary_task,
)


class CommsReactivationSweeper:
    """Periodic scan that re-opens DONE tasks whose role received new mail."""

    def __init__(
        self,
        *,
        store: Any,
        project_id_getter: Callable[[], str | None],
        reactivate_fn: Callable[[Task], Awaitable[bool]],
        interval_sec: float = 10.0,
    ) -> None:
        self.store = store
        self.project_id_getter = project_id_getter
        self.reactivate_fn = reactivate_fn
        self.interval_sec = max(1.0, float(interval_sec))
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._running:
            return
        if self.store is None:
            logger.debug("CommsReactivationSweeper: no store, skipping start")
            return
        self._running = True
        self._task = asyncio.create_task(self._tick_loop())
        logger.info(
            "CommsReactivationSweeper started (interval={}s)", self.interval_sec
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("CommsReactivationSweeper stop error")
        self._task = None
        logger.info("CommsReactivationSweeper stopped")

    async def _tick_loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("CommsReactivationSweeper tick error")
            try:
                await asyncio.sleep(self.interval_sec)
            except asyncio.CancelledError:
                break

    async def _tick(self) -> None:
        project_id = (self.project_id_getter() or "").strip()
        if not project_id:
            return
        # Guard against a store that has been closed (e.g. during project
        # switch, where engine rebinds ``self.store`` on the sweeper but a
        # tick already in flight may still reference the previous handle)
        # or a store that is reattaching its sqlite connection.
        if getattr(self.store, "_db", None) is None:
            return
        try:
            done_tasks = await self.store.get_tasks(
                project_id=project_id,
                status=TaskStatus.DONE,
            )
        except AssertionError:
            # OPCStore raises AssertionError when queried while ``_db`` is
            # None (closed). Treat as a transient no-op; the next tick will
            # see the refreshed store.
            return
        except Exception:
            logger.exception(
                "CommsReactivationSweeper: failed to list DONE tasks for project={}",
                project_id,
            )
            return
        if not done_tasks:
            return
        reactivated_count = 0
        for task in done_tasks:
            # RuntimeV2 uses durable one-shot Tasks for internal role prompts
            # and meeting turns.  They are deliberately not schedulable work
            # items and must never be reopened by role-wide unread mail.
            if is_runtime_auxiliary_task(task):
                continue
            try:
                reactivated = await self.reactivate_fn(task)
            except Exception:
                logger.exception(
                    "CommsReactivationSweeper: reactivate_fn raised for task={}",
                    getattr(task, "id", ""),
                )
                continue
            if reactivated:
                reactivated_count += 1
                logger.info(
                    "[comms_reactivation] sweep reactivated task={} role={}",
                    getattr(task, "id", ""),
                    str(getattr(task, "assigned_to", "") or "").strip(),
                )
        if reactivated_count:
            logger.debug(
                "CommsReactivationSweeper: tick reactivated {} task(s) for project={}",
                reactivated_count,
                project_id,
            )
