"""Controller-local ownership of active task execution attempts.

Persisted task rows describe durable workflow state; they cannot prove that
the controller which owns an execution coroutine is still alive.  This
registry intentionally stays in memory and is shared by all engines owned by
one controller.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import uuid
from collections.abc import Iterator


_CURRENT_HANDOFF: ContextVar[tuple[object, str] | None] = ContextVar(
    "opc_active_task_run_handoff",
    default=None,
)
_CURRENT_DRIVER_ATTEMPT: ContextVar[tuple[object, str] | None] = ContextVar(
    "opc_active_task_run_driver_attempt",
    default=None,
)


class ActiveTaskRunAdmissionClosed(RuntimeError):
    """Raised when execution registration starts after shutdown admission closes."""


class ActiveTaskRunRegistry:
    """Track active execution attempts by ``(project_id, task_id)``.

    A task can briefly have overlapping attempts while cancellation and a new
    dispatch cross.  Each registration therefore receives its own token and a
    task remains active until its last token is removed.
    """

    def __init__(self) -> None:
        self._attempts: dict[tuple[str, str], set[str]] = {}
        # Durable task ids are the liveness index used by recovery.  Shutdown
        # additionally needs the process-local asyncio owner so it can join
        # the coroutine *before* the shared Store is closed.  Keep that
        # relationship keyed by the opaque attempt token; callers which use
        # the registry only as a test/coordination ledger remain untracked.
        self._attempt_owner_tasks: dict[str, asyncio.Task[object]] = {}
        self._shutdown_owner_failures: list[BaseException] = []
        self._recorded_failure_task_ids: set[int] = set()
        self._scope_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._handoff_refs: dict[str, int] = {}
        self._handoffs_drained = asyncio.Event()
        self._handoffs_drained.set()
        self._admission_closed = False

    @staticmethod
    def _key(project_id: str | None, task_id: str | None) -> tuple[str, str]:
        project = str(project_id or "default").strip() or "default"
        task = str(task_id or "").strip()
        if not task:
            raise ValueError("task_id is required")
        return project, task

    def register(
        self,
        project_id: str | None,
        task_id: str | None,
        *,
        owner_task: asyncio.Task[object] | None = None,
    ) -> str:
        handoff_token = self._current_pending_handoff_token()
        driver_attempt_token = self._current_driver_attempt_token()
        if (
            self._admission_closed
            and handoff_token is None
            and driver_attempt_token is None
        ):
            raise ActiveTaskRunAdmissionClosed(
                "task execution admission is closed for controller shutdown"
            )
        key = self._key(project_id, task_id)
        attempt_token = uuid.uuid4().hex
        self._attempts.setdefault(key, set()).add(attempt_token)
        if owner_task is not None:
            self._attempt_owner_tasks[attempt_token] = owner_task
            owner_task.add_done_callback(self._attempt_owner_finished)
        # A pre-shutdown WS request is handed off once its first real execution
        # coroutine is registered.  The reservation itself is deliberately not
        # reported by is_active()/active_task_ids(); only this attempt is.
        if handoff_token is not None:
            self._settle_handoff(handoff_token)
        return attempt_token

    def bind_attempt_task(
        self,
        attempt_token: str,
        owner_task: asyncio.Task[object],
    ) -> None:
        """Attach the coroutine which owns an already-registered attempt.

        Work-item claims must register before ``asyncio.create_task`` so a
        shutdown scope lock can never observe a durable claim without a live
        attempt.  This second, synchronous step records the newly-created task
        without weakening that ordering invariant.
        """

        if not self._attempt_token_is_active(attempt_token):
            raise ValueError("attempt token is not active")
        self._attempt_owner_tasks[attempt_token] = owner_task
        owner_task.add_done_callback(self._attempt_owner_finished)

    def _attempt_owner_finished(self, owner_task: asyncio.Task[object]) -> None:
        """Retain shutdown-race failures after an attempt unregisters itself."""

        if not self._admission_closed or owner_task.cancelled():
            return
        task_identity = id(owner_task)
        if task_identity in self._recorded_failure_task_ids:
            return
        try:
            failure = owner_task.exception()
        except asyncio.CancelledError:
            return
        if failure is not None:
            self._recorded_failure_task_ids.add(task_identity)
            self._shutdown_owner_failures.append(failure)

    def active_owner_tasks(
        self,
        project_id: str | None = None,
    ) -> set[asyncio.Task[object]]:
        """Return live coroutine owners for registered execution attempts."""

        project = (
            str(project_id or "default").strip() or "default"
            if project_id is not None
            else None
        )
        live_tokens = {
            token
            for (candidate_project, _task_id), tokens in self._attempts.items()
            if project is None or candidate_project == project
            for token in tokens
        }
        return {
            task
            for token, task in self._attempt_owner_tasks.items()
            if token in live_tokens and not task.done()
        }

    async def cancel_and_wait_for_owner_tasks(
        self,
        project_id: str | None = None,
    ) -> None:
        """Cancel and join every process-local owner known to the registry.

        This is a shutdown primitive, not a durable state transition.  The
        controller must atomically suspend its company scopes first; only then
        may it call this method to unwind provider/runtime coroutines while the
        Store and exact controller leases are still available.
        """

        current = asyncio.current_task()
        while True:
            if self._shutdown_owner_failures:
                failure = self._shutdown_owner_failures.pop(0)
                self._recorded_failure_task_ids.clear()
                raise failure
            owners = {
                task
                for task in self.active_owner_tasks(project_id)
                if task is not current
            }
            if not owners:
                if self._shutdown_owner_failures:
                    failure = self._shutdown_owner_failures.pop(0)
                    self._recorded_failure_task_ids.clear()
                    raise failure
                return
            for task in owners:
                task.cancel()
            results = await asyncio.gather(*owners, return_exceptions=True)
            failures = [
                result
                for result in results
                if isinstance(result, BaseException)
                and not isinstance(result, asyncio.CancelledError)
            ]
            if failures:
                self._shutdown_owner_failures.clear()
                self._recorded_failure_task_ids.clear()
                # A cancellation owner may discover a lost controller fence or
                # fail while persisting its final cleanup.  Shutdown must stay
                # fail-closed with the Store open; treating that as an ordinary
                # cancelled coroutine recreates the closed-Store tail race.
                raise failures[0]

    @contextmanager
    def bind_driver_attempt(self, attempt_token: str) -> Iterator[None]:
        """Allow nested attempts while their live scheduler owns the scope.

        Closing controller admission rejects new ingress, but a scheduler that
        was already running may be between its atomic WorkItem claim and child
        coroutine creation.  Its nested registrations remain admissible until
        that scheduler attempt ends, so shutdown can snapshot the still-live
        scope instead of creating an orphan RUNNING claim.
        """

        if not self._attempt_token_is_active(attempt_token):
            raise ValueError("driver attempt is not active")
        context_token = _CURRENT_DRIVER_ATTEMPT.set((self, attempt_token))
        try:
            yield
        finally:
            _CURRENT_DRIVER_ATTEMPT.reset(context_token)

    def reserve_handoff(self) -> str:
        """Reserve one accepted ingress request until execution is registered.

        Reservations bridge the short scheduling gap between the WS router and
        ``register``.  They are controller-local synchronization only and never
        become a second liveness source.
        """

        if self._admission_closed:
            raise ActiveTaskRunAdmissionClosed(
                "task execution admission is closed for controller shutdown"
            )
        token = uuid.uuid4().hex
        self._handoff_refs[token] = 1
        self._handoffs_drained.clear()
        return token

    @contextmanager
    def bind_handoff(self, handoff_token: str) -> Iterator[None]:
        """Propagate a reservation through tasks spawned by an ingress handler."""

        if handoff_token not in self._handoff_refs:
            raise ValueError("handoff reservation is not pending")
        context_token = _CURRENT_HANDOFF.set((self, handoff_token))
        try:
            yield
        finally:
            _CURRENT_HANDOFF.reset(context_token)

    def retain_current_handoff(self) -> str | None:
        """Retain the bound reservation for a newly scheduled coroutine."""

        handoff_token = self._current_pending_handoff_token()
        if handoff_token is None:
            return None
        self._handoff_refs[handoff_token] += 1
        return handoff_token

    def release_current_handoff(self) -> bool:
        """Release an accepted request that will not start an execution."""

        handoff_token = self._current_pending_handoff_token()
        if handoff_token is None:
            return False
        return self.release_handoff(handoff_token)

    def release_handoff(self, handoff_token: str) -> bool:
        """Release one owner, draining a request that exited before execution."""

        refs = self._handoff_refs.get(handoff_token)
        if refs is None:
            return False
        if refs > 1:
            self._handoff_refs[handoff_token] = refs - 1
            return True
        self._settle_handoff(handoff_token)
        return True

    def revoke_handoff(self, handoff_token: str) -> bool:
        """Invalidate every retained owner of a queued ingress handoff.

        Controller shutdown uses this after synchronously cancelling a request
        which has not registered its first execution attempt.  Revocation is
        intentionally stronger than ``release_handoff``: callbacks may be
        delayed by cancellation cleanup, but the revoked request must neither
        keep the shutdown barrier open nor register work after admission has
        closed.
        """

        if handoff_token not in self._handoff_refs:
            return False
        self._settle_handoff(handoff_token)
        return True

    def _current_pending_handoff_token(self) -> str | None:
        binding = _CURRENT_HANDOFF.get()
        if binding is None or binding[0] is not self:
            return None
        token = binding[1]
        return token if token in self._handoff_refs else None

    def _current_driver_attempt_token(self) -> str | None:
        binding = _CURRENT_DRIVER_ATTEMPT.get()
        if binding is None or binding[0] is not self:
            return None
        token = binding[1]
        return token if self._attempt_token_is_active(token) else None

    def _attempt_token_is_active(self, attempt_token: str) -> bool:
        return any(
            attempt_token in attempts
            for attempts in self._attempts.values()
        )

    def _settle_handoff(self, handoff_token: str) -> None:
        self._handoff_refs.pop(handoff_token, None)
        if not self._handoff_refs:
            self._handoffs_drained.set()

    @property
    def admission_closed(self) -> bool:
        return self._admission_closed

    def close_admission(self) -> None:
        """Reject future attempts without dropping attempts already in flight."""

        self._admission_closed = True

    async def close_admission_and_wait_for_handoffs(self) -> None:
        """Close ingress and wait until every already-accepted request hands off.

        A bound pending reservation may still call ``register`` after admission
        closes.  That registration atomically drains the reservation and turns
        the real coroutine into the sole active fact.  This wait therefore ends
        at handoff, never at completion of the potentially long execution.
        """

        self.close_admission()
        while self._handoff_refs:
            await self._handoffs_drained.wait()

    @property
    def pending_handoff_count(self) -> int:
        return len(self._handoff_refs)

    def is_handoff_pending(self, handoff_token: str | None) -> bool:
        return bool(handoff_token and handoff_token in self._handoff_refs)

    def scope_lock(
        self,
        project_id: str | None,
        runtime_session_id: str | None,
    ) -> asyncio.Lock:
        """Return the controller-shared lock for one durable runtime scope."""

        project = str(project_id or "default").strip() or "default"
        session = str(runtime_session_id or "").strip()
        if not session:
            raise ValueError("runtime_session_id is required")
        key = (project, session)
        lock = self._scope_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._scope_locks[key] = lock
        return lock

    def unregister(
        self,
        project_id: str | None,
        task_id: str | None,
        attempt_token: str,
    ) -> bool:
        key = self._key(project_id, task_id)
        attempts = self._attempts.get(key)
        if not attempts or attempt_token not in attempts:
            return False
        attempts.remove(attempt_token)
        self._attempt_owner_tasks.pop(attempt_token, None)
        if not attempts:
            self._attempts.pop(key, None)
        return True

    def is_active(self, project_id: str | None, task_id: str | None) -> bool:
        return bool(self._attempts.get(self._key(project_id, task_id)))

    def active_task_ids(self, project_id: str | None) -> set[str]:
        project = str(project_id or "default").strip() or "default"
        return {
            task_id
            for (candidate_project, task_id), attempts in self._attempts.items()
            if candidate_project == project and attempts
        }

    def attempt_count(self, project_id: str | None, task_id: str | None) -> int:
        return len(self._attempts.get(self._key(project_id, task_id), ()))
