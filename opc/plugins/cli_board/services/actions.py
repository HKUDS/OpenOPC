"""Interactive task actions for the CLI board."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

from opc.core.models import Task, TaskStatus
from opc.core.interaction_protocol import (
    OWNER_INTERACTION_CHECKPOINT_TYPES,
    owner_interaction_actor_identity,
)
from opc.layer2_organization.work_item_transition import apply_task_status_transition
from opc.presentation.kanban import column_to_task_status
from opc.plugins.office_ui.services.factory import OfficeServiceFactory

if TYPE_CHECKING:
    from .engine_facade import EngineFacade


class BoardActions:
    """Mutating operations exposed to the TUI."""

    def __init__(self, facade: "EngineFacade", project_id: str | None = None) -> None:
        self.facade = facade
        self.project_id = project_id
        self._task_locks: dict[str, asyncio.Lock] = {}
        self._task_bg_map: dict[str, asyncio.Task[Any]] = {}

    @property
    def _project_id(self) -> str:
        return self.project_id or self.facade.project_id or "default"

    async def _run_office_service(self, operation):
        engine = await self.facade.ensure_ready()
        async with OfficeServiceFactory(
            config=getattr(engine, "config", None),
            project_id=self._project_id,
            on_progress=getattr(self.facade, "_progress_callback", None),
            on_runtime_event=getattr(self.facade, "_event_callback", None),
        ) as services:
            return await operation(services)

    async def create_task(
        self,
        *,
        title: str,
        description: str = "",
        auto_run: bool = False,
        initial_message: str | None = None,
        mode: str = "task",
        company_profile: str | None = None,
    ) -> Task:
        engine = await self.facade.ensure_ready()
        if not engine.store:
            raise RuntimeError("OPC store is not available.")

        normalized_title = str(title or "").strip() or "New Chat"
        normalized_description = str(description or "").strip()
        result = await self._run_office_service(
            lambda svc: svc.session.create(
                project_id=self._project_id,
                title=normalized_title,
                description=normalized_description,
                exec_mode=mode,
                company_profile=company_profile,
                interface="cli_board",
            )
        )
        task_id = str(result.payload.get("task_id", "") or "")
        task = await self._get_task(task_id)

        if auto_run:
            prompt = (initial_message or f"{normalized_title}\n{normalized_description}").strip()
            if prompt:
                await self.send_session_message(
                    task.id,
                    prompt,
                    mode=mode,
                    company_profile=company_profile,
                    allow_terminal=True,
                )
        return task

    async def run_task(
        self,
        task_id: str,
        *,
        mode: str = "task",
        company_profile: str | None = None,
    ) -> str:
        task = await self._get_task(task_id)
        prompt = f"{task.title}\n{task.description}".strip()
        if not prompt:
            raise ValueError("Selected task has no title or description to run.")
        return await self.send_session_message(
            task_id,
            prompt,
            mode=mode,
            company_profile=company_profile,
            allow_terminal=True,
        )

    async def retry_task(
        self,
        task_id: str,
        *,
        mode: str = "task",
        company_profile: str | None = None,
    ) -> str:
        return await self.run_task(task_id, mode=mode, company_profile=company_profile)

    async def send_session_message(
        self,
        task_id: str,
        content: str,
        *,
        mode: str = "task",
        company_profile: str | None = None,
        allow_terminal: bool = False,
    ) -> str:
        engine = await self.facade.ensure_ready()
        if not engine.store:
            raise RuntimeError("OPC store is not available.")

        async with self._get_task_lock(task_id):
            if existing := self._task_bg_map.get(task_id):
                if not existing.done() and existing is not asyncio.current_task():
                    raise RuntimeError("This task is already running.")

            task = await self._get_task(task_id)
            if not allow_terminal and task.status in {TaskStatus.CANCELLED, TaskStatus.DONE}:
                raise ValueError("Cannot send messages to a completed or cancelled task.")

            if not task.session_id:
                task.session_id = str(uuid.uuid4())
                if engine.memory:
                    await engine.memory.ensure_session(
                        task.session_id,
                        project_id=self._project_id,
                        title=task.title,
                        mode="primary",
                        metadata={"source": "cli_board"},
                    )

            current_task = asyncio.current_task()
            if current_task is not None:
                self._task_bg_map[task_id] = current_task

            try:
                normalized_mode = str(mode or "task").strip().lower()
                send_mode = "company" if normalized_mode in {"company", "custom", "org"} else "task"
                send_profile = "custom" if normalized_mode in {"custom", "org"} else company_profile
                result = await self._run_office_service(
                    lambda svc: svc.session.send(
                        project_id=self._project_id,
                        task_id=task_id,
                        content=str(content or "").strip(),
                        mode=send_mode,
                        company_profile=send_profile,
                    )
                )
                return str(result.payload.get("response", "") or "")
            except asyncio.CancelledError:
                await self._mark_related_tasks(task_id, TaskStatus.CANCELLED)
                await self._resolve_related_checkpoints(task_id, status="cancelled")
                raise
            finally:
                if self._task_bg_map.get(task_id) is current_task:
                    self._task_bg_map.pop(task_id, None)

    async def move_task(self, task_id: str, column_id: str) -> Task:
        engine = await self.facade.ensure_ready()
        if not engine.store:
            raise RuntimeError("OPC store is not available.")

        task = await self._get_task(task_id)
        target_status = column_to_task_status(column_id)
        if target_status is None:
            raise ValueError(f"Unsupported target column: {column_id}")
        if task.status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED} and target_status not in {
            TaskStatus.DONE,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }:
            raise ValueError(f"Cannot move terminal task {task.status.value} back to {column_id}.")
        await self._run_office_service(lambda svc: svc.kanban.move_task(project_id=self._project_id, task_id=task_id, column_id=column_id))
        return await self._get_task(task_id)

    async def complete_task(self, task_id: str) -> Task:
        engine = await self.facade.ensure_ready()
        if not engine.store:
            raise RuntimeError("OPC store is not available.")
        await self._run_office_service(lambda svc: svc.session.complete(project_id=self._project_id, task_id=task_id))
        return await self._get_task(task_id)

    async def cancel_task(self, task_id: str) -> None:
        engine = await self.facade.ensure_ready()
        if not engine.store:
            raise RuntimeError("OPC store is not available.")

        background = self._task_bg_map.get(task_id)
        if background and not background.done() and background is not asyncio.current_task():
            background.cancel()
            await asyncio.sleep(0)

        await self._run_office_service(lambda svc: svc.session.stop(project_id=self._project_id, task_id=task_id))
        await self._resolve_related_checkpoints(task_id, status="cancelled")

    async def approve_checkpoint(self, task_id: str, *, approved: bool = True, reply: str | None = None) -> str:
        engine = await self.facade.ensure_ready()
        if not engine.store:
            raise RuntimeError("OPC store is not available.")

        if reply is not None:
            message = reply
        else:
            message = "approve" if approved else "deny"

        checkpoint, requester_task_id, requester_session_id, requester_actor = (
            await self._locate_owner_checkpoint(task_id)
        )
        reply_text = str(reply or "").strip().lower()
        denies = (not approved and reply is None) or reply_text.startswith("deny")
        if (
            checkpoint.checkpoint_type
            in {"tool_permission", "action_permission"}
            and not denies
        ):
            raise PermissionError(
                "The CLI board cannot approve exact tool/action permissions; "
                "review the complete payload in Office UI. Denial is allowed."
            )
        translate = getattr(engine, "build_compatibility_checkpoint_decision", None)
        submit = getattr(engine, "submit_checkpoint_decision", None)
        if not callable(translate) or not callable(submit):
            raise RuntimeError("The engine does not support durable checkpoint replies.")
        decision = translate(checkpoint, message, None)
        receipt = await submit(
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_type=checkpoint.checkpoint_type,
            decision=decision,
            client_request_id=f"cli-board:{checkpoint.checkpoint_id}:{uuid.uuid4().hex}",
            requester_task_id=requester_task_id,
            requester_session_id=requester_session_id or None,
            requester_actor=requester_actor,
        )
        if not bool(receipt.get("accepted")):
            reason = str(receipt.get("reason", "") or "invalid_decision").strip()
            raise RuntimeError(f"Checkpoint response was not accepted ({reason}).")
        return "Checkpoint response accepted."

    def is_running(self, task_id: str) -> bool:
        task = self._task_bg_map.get(task_id)
        return bool(task and not task.done())

    def _get_task_lock(self, task_id: str) -> asyncio.Lock:
        lock = self._task_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._task_locks[task_id] = lock
        return lock

    async def _get_task(self, task_id: str) -> Task:
        engine = await self.facade.ensure_ready()
        if not engine.store:
            raise RuntimeError("OPC store is not available.")
        task = await engine.store.get_task(task_id)
        if task is None:
            raise ValueError(f"Unknown task: {task_id}")
        return task

    async def _locate_owner_checkpoint(
        self,
        target_id: str,
    ) -> tuple[Any, str, str, dict[str, Any] | None]:
        """Resolve one visible owner card without entering the conversation lock."""

        engine = await self.facade.ensure_ready()
        if not engine.store:
            raise RuntimeError("OPC store is not available.")
        target_id = str(target_id or "").strip()
        if not target_id:
            raise ValueError("A task or work-item id is required.")

        target_task = await engine.store.get_task(target_id)
        related_tasks = (
            await self._collect_related_tasks(target_id)
            if target_task is not None
            else []
        )
        related_task_ids = {
            str(task.id or "").strip()
            for task in related_tasks
            if str(task.id or "").strip()
        }
        related_session_ids = {
            str(task.session_id or "").strip()
            for task in related_tasks
            if str(task.session_id or "").strip()
        }
        checkpoints = await engine.store.get_pending_checkpoints(
            project_id=self._project_id,
            checkpoint_types=sorted(OWNER_INTERACTION_CHECKPOINT_TYPES),
        )
        visibility_check = getattr(engine, "can_answer_checkpoint", None)
        if not callable(visibility_check):
            raise RuntimeError("The engine cannot authorize checkpoint replies.")

        visible: list[tuple[Any, str, str, dict[str, Any] | None]] = []
        for checkpoint in checkpoints:
            if (
                str(getattr(checkpoint, "checkpoint_type", "") or "").strip()
                not in OWNER_INTERACTION_CHECKPOINT_TYPES
            ):
                continue
            payload = dict(getattr(checkpoint, "payload", {}) or {})
            interaction = dict(payload.get("interaction", {}) or {})
            ownership = dict(interaction.get("ownership", {}) or {})
            checkpoint_task_id = str(getattr(checkpoint, "task_id", "") or "").strip()
            checkpoint_session_id = str(getattr(checkpoint, "session_id", "") or "").strip()
            reference_ids = {
                checkpoint_task_id,
                checkpoint_session_id,
                str(ownership.get("waiting_task_id", "") or "").strip(),
                str(ownership.get("waiting_session_id", "") or "").strip(),
                str(ownership.get("ui_anchor_task_id", "") or "").strip(),
                str(ownership.get("ui_anchor_session_id", "") or "").strip(),
                str(ownership.get("root_session_id", "") or "").strip(),
                str(ownership.get("work_item_id", "") or "").strip(),
            }
            reference_ids.discard("")
            if not (
                target_id in reference_ids
                or checkpoint_task_id in related_task_ids
                or checkpoint_session_id in related_session_ids
            ):
                continue

            requester_task_id, requester_session_id = (
                owner_interaction_actor_identity(checkpoint)
            )
            requester_actor: dict[str, Any] | None = None
            if not requester_task_id and not requester_session_id:
                issue_actor = getattr(engine, "issue_project_owner_actor", None)
                if not callable(issue_actor):
                    continue
                requester_actor = issue_actor(interface="cli_board")
            allowed = visibility_check(
                checkpoint,
                requester_task_id=requester_task_id,
                requester_session_id=requester_session_id or None,
                requester_actor=requester_actor,
            )
            if asyncio.iscoroutine(allowed):
                allowed = await allowed
            if allowed:
                visible.append(
                    (
                        checkpoint,
                        requester_task_id,
                        requester_session_id,
                        requester_actor,
                    )
                )

        if not visible:
            raise ValueError("No pending owner checkpoint is available for this item.")
        if len(visible) != 1:
            raise ValueError(
                "Multiple pending owner checkpoints are available; select an exact checkpoint."
            )
        return visible[0]

    async def _collect_related_tasks(self, task_id: str) -> list[Task]:
        engine = await self.facade.ensure_ready()
        if not engine.store:
            return []
        root_task = await self._get_task(task_id)
        tasks = await engine.store.get_tasks(project_id=self._project_id)
        related: list[Task] = []
        root_session_id = str(root_task.session_id or "").strip()
        for task in tasks:
            metadata = task.metadata if isinstance(task.metadata, dict) else {}
            origin_task_id = str(metadata.get("origin_task_id", "") or "").strip()
            if task.id == root_task.id:
                related.append(task)
                continue
            if origin_task_id and origin_task_id == root_task.id:
                related.append(task)
                continue
            parent_session_id = str(task.parent_session_id or "").strip()
            if root_session_id and parent_session_id and parent_session_id == root_session_id:
                related.append(task)
        return related

    async def _mark_related_tasks(self, task_id: str, status: TaskStatus) -> None:
        engine = await self.facade.ensure_ready()
        if not engine.store:
            return
        for task in await self._collect_related_tasks(task_id):
            await apply_task_status_transition(
                engine.store,
                task,
                target_status_or_phase=status,
                reason="cli_board_mark_related_tasks",
                release_claim=status == TaskStatus.CANCELLED,
            )

    async def _resolve_related_checkpoints(self, task_id: str, *, status: str) -> None:
        engine = await self.facade.ensure_ready()
        if not engine.store:
            return

        related = await self._collect_related_tasks(task_id)
        if not related:
            return

        task_ids = {task.id for task in related}
        session_ids = {task.session_id for task in related if task.session_id}
        checkpoints = await engine.store.get_pending_checkpoints(project_id=self._project_id)
        for checkpoint in checkpoints:
            if checkpoint.task_id in task_ids or checkpoint.session_id in session_ids:
                if checkpoint.checkpoint_type in OWNER_INTERACTION_CHECKPOINT_TYPES:
                    coordinator = getattr(engine, "interaction_coordinator", None)
                    if coordinator is None:
                        raise RuntimeError(
                            "owner interaction coordinator is unavailable"
                        )
                    await coordinator.close_pending_owner_checkpoint(
                        checkpoint.checkpoint_id,
                        checkpoint_type=checkpoint.checkpoint_type,
                        status=status,
                        payload_patch={
                            "administrative_reason": "cli_board_related_task_closed"
                        },
                    )
                else:
                    await engine.store.patch_execution_checkpoint_payload(
                        checkpoint.checkpoint_id,
                        project_id=self._project_id,
                        checkpoint_type=checkpoint.checkpoint_type,
                        expected_statuses=["pending"],
                        payload_patch={},
                        status=status,
                    )
