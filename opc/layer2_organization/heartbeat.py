"""Heartbeat scheduler for company-mode agent autonomy.

Periodically checks heartbeat-enabled agents and wakes them to process
pending tasks.  Runs as a background ``asyncio.Task`` within the same
process — no separate service needed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine

from loguru import logger

from opc.layer2_organization.company_runtime_identity import (
    is_runtime_auxiliary_task,
)


class HeartbeatScheduler:
    """In-process heartbeat loop that periodically activates company-mode agents."""

    def __init__(
        self,
        store: Any,
        org_engine: Any,
        execute_task_fn: Callable[..., Coroutine[Any, Any, Any]],
        checkout_and_run_fn: Callable[..., Coroutine[Any, Any, Any]] | None = None,
        interval_sec: int = 30,
        max_concurrent_runs: int = 1,
        communication: Any | None = None,
    ) -> None:
        self.store = store
        self.org_engine = org_engine
        self.execute_task_fn = execute_task_fn
        self.checkout_and_run_fn = checkout_and_run_fn
        self.interval_sec = interval_sec
        self.max_concurrent_runs = max_concurrent_runs
        self.communication = communication
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._active_runs: dict[str, asyncio.Task[Any]] = {}
        self._wakeup_event = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._tick_loop())
        logger.info("HeartbeatScheduler started (interval={}s)", self.interval_sec)

    async def stop(self) -> None:
        self._running = False
        self._wakeup_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for task in list(self._active_runs.values()):
            task.cancel()
        self._active_runs.clear()
        logger.info("HeartbeatScheduler stopped")

    # -- on-demand wakeup --------------------------------------------------

    async def wakeup(self, agent_id: str, reason: str = "on_demand") -> None:
        """Immediately wake a specific agent outside the normal tick cycle."""
        logger.info("Wakeup requested for agent={} reason={}", agent_id, reason)
        if agent_id in self._active_runs and not self._active_runs[agent_id].done():
            logger.debug("Agent {} already has an active run, skipping wakeup", agent_id)
            return
        self._active_runs[agent_id] = asyncio.create_task(
            self._run_agent_heartbeat(agent_id)
        )

    # -- main loop ---------------------------------------------------------

    async def _tick_loop(self) -> None:
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("HeartbeatScheduler tick error")
            try:
                await asyncio.wait_for(
                    self._wakeup_event.wait(),
                    timeout=self.interval_sec,
                )
                self._wakeup_event.clear()
            except asyncio.TimeoutError:
                pass

    async def _resolve_stale_waits(self) -> None:
        """Resolve stale peer waits and auto-simulate meetings when all work is stalled."""
        if not self.communication:
            return
        from opc.core.models import TaskStatus
        try:
            waiting_tasks = await self.store.get_tasks(status=TaskStatus.AWAITING_PEER)
            if not waiting_tasks:
                return
            resumed = await self.communication.refresh_waiting_tasks(waiting_tasks)
            for task in resumed:
                logger.info("Heartbeat resolved peer wait for task={}", task.id)
            still_waiting = [t for t in waiting_tasks if t.status == TaskStatus.AWAITING_PEER]
            if not still_waiting:
                return
            project_ids = {t.project_id for t in still_waiting}
            for pid in project_ids:
                all_project_tasks = await self.store.get_tasks(project_id=pid)
                has_runnable = any(
                    t.status in {TaskStatus.PENDING, TaskStatus.RUNNING}
                    for t in all_project_tasks
                    if not is_runtime_auxiliary_task(t)
                )
                if has_runnable:
                    continue
                project_waiting = [t for t in still_waiting if t.project_id == pid]
                resolved = await self.communication.auto_resolve_stale_meetings(project_waiting)
                for room_id in resolved:
                    logger.info("Heartbeat auto-simulated meeting={} (project {} fully stalled)", room_id, pid)
        except Exception:
            logger.exception("Heartbeat _resolve_stale_waits error")

    async def _tick(self) -> None:
        self._cleanup_done_runs()
        await self._resolve_stale_waits()

        agents = self.org_engine.list_agents()
        now = datetime.now()

        for agent in agents:
            if not getattr(agent, "heartbeat_enabled", False):
                continue
            if agent.role_id in self._active_runs and not self._active_runs[agent.role_id].done():
                continue
            if len(self._active_runs) >= self.max_concurrent_runs:
                break

            interval = getattr(agent, "heartbeat_interval_sec", 300)
            last_hb = getattr(agent, "last_heartbeat_at", None)
            if last_hb and (now - last_hb) < timedelta(seconds=interval):
                continue

            logger.debug("Heartbeat tick: scheduling agent={}", agent.role_id)
            self._active_runs[agent.role_id] = asyncio.create_task(
                self._run_agent_heartbeat(agent.role_id)
            )

    async def _run_agent_heartbeat(self, agent_id: str) -> None:
        """Single heartbeat cycle: find a pending task, check it out, execute."""
        from opc.core.models import TaskStatus
        try:
            tasks = await self.store.get_tasks(status=TaskStatus.PENDING)
            candidate = None
            for task in tasks:
                if is_runtime_auxiliary_task(task):
                    continue
                if task.assigned_to == agent_id or not task.assigned_to:
                    candidate = task
                    break
            if not candidate:
                return

            claimed = await self.store.checkout_task(candidate.id, agent_id)
            if not claimed:
                logger.debug("Agent {} failed to checkout task {}", agent_id, candidate.id)
                return

            logger.info("Agent {} executing task {} via heartbeat", agent_id, candidate.title)
            if self.checkout_and_run_fn:
                await self.checkout_and_run_fn(candidate, agent_id)
            else:
                await self.execute_task_fn(candidate)
        except Exception:
            logger.exception("Heartbeat run failed for agent={}", agent_id)

    def _cleanup_done_runs(self) -> None:
        done = [k for k, v in self._active_runs.items() if v.done()]
        for k in done:
            task = self._active_runs.pop(k)
            if task.exception():
                logger.warning("Heartbeat run for {} ended with error: {}", k, task.exception())
