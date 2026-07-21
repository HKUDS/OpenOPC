"""HumanAgentAdapter for OpenOPC Shadow Mode.

Bypasses LLM inference for human-assigned tasks, marks task state as
AWAITING_HUMAN_DELIVERABLE, emits event to Layer 0 MessageBus, and
reactively awaits human deliverable submission.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from opc.core.models import AgentStatus, Task, TaskResult, TaskStatus
from opc.layer0_interaction.message_bus import MessageBus
from opc.layer3_agent.adapters.base import ExternalAgentAdapter, ExternalAgentConfig


class HumanAgentAdapter(ExternalAgentAdapter):
    """Adapter for human contractor seats in Shadow Mode."""

    agent_type: str = "human"
    default_command: str = "human"

    def __init__(
        self,
        config: ExternalAgentConfig | None = None,
        message_bus: MessageBus | None = None,
        store: Any | None = None,
    ) -> None:
        super().__init__(config)
        self.message_bus = message_bus or MessageBus()
        self.store = store

    async def is_available(self) -> bool:
        """Human contractor seats are always available when configured."""
        return True

    async def get_status(self) -> AgentStatus:
        """Return status for human adapter."""
        return AgentStatus.IDLE

    def build_invocation(
        self,
        task: Task,
        workspace_path: str | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        """Return metadata for human task invocation."""
        return (["human"], {"agent": "human", "task_id": task.id})

    async def execute(self, task: Task, workspace_path: str) -> TaskResult:
        """Execute a human contractor task by marking state and awaiting deliverable."""
        return await self.execute_human_task(task=task, store=self.store, workspace_path=workspace_path)

    async def execute_human_task(
        self,
        task: Task,
        store: Any | None = None,
        workspace_path: str | None = None,
        message_bus: MessageBus | None = None,
        timeout: float | None = 86400.0 * 7,  # Default 7-day timeout for human deliverable
    ) -> TaskResult:
        """Execute a human contractor task by updating state and awaiting completion."""
        mbus = message_bus or self.message_bus
        target_store = store or self.store
        task_id = str(task.id or "").strip()
        owner_role = str(getattr(task, "assigned_to", "") or task.metadata.get("owner_role") or "").strip()

        logger.info(f"[Shadow Mode] HumanAgentAdapter executing task={task_id} for owner_role={owner_role}")

        # 1. Update task status and metadata in persistent store
        task.status = TaskStatus.AWAITING_HUMAN
        task.metadata["sub_state"] = "AWAITING_HUMAN_DELIVERABLE"
        task.metadata["owner_role"] = owner_role
        if target_store and hasattr(target_store, "save_task"):
            await target_store.save_task(task)

        # 2. Emit notification to Layer 0 MessageBus
        mbus.publish_event(
            "task_status_changed",
            {
                "task_id": task_id,
                "status": "AWAITING_HUMAN_DELIVERABLE",
                "owner_role": owner_role,
                "title": task.title,
            },
        )

        topic = f"deliverable_completed:{task_id}"
        logger.info(f"[Shadow Mode] Task {task_id} waiting reactively on MessageBus topic '{topic}' and store polling fallback")

        # 3. Hybrid Reactive Event + Cross-Process DB Polling Fallback
        payload: dict[str, Any] | None = None
        start_time = asyncio.get_event_loop().time()

        async def _subscribe_bus() -> dict[str, Any] | None:
            try:
                return await mbus.subscribe_once(topic, timeout=timeout)
            except Exception:
                return None

        async def _poll_db() -> dict[str, Any] | None:
            while True:
                await asyncio.sleep(2.0)
                if target_store and hasattr(target_store, "get_task"):
                    t = await target_store.get_task(task_id)
                    if t and t.result and t.result.get("submitted_by_human"):
                        return {
                            "summary": t.result.get("summary", ""),
                            "content": t.result.get("summary", ""),
                            "artifacts": t.result.get("artifacts", []),
                            "username": t.result.get("contractor_username", "human_contractor"),
                        }
                if timeout and (asyncio.get_event_loop().time() - start_time) > timeout:
                    return None

        bus_task = asyncio.create_task(_subscribe_bus())
        db_task = asyncio.create_task(_poll_db())

        done, pending = await asyncio.wait(
            [bus_task, db_task],
            return_when=asyncio.FIRST_COMPLETED,
            timeout=timeout,
        )

        for p in pending:
            p.cancel()

        for d in done:
            try:
                payload = d.result()
                if payload:
                    break
            except Exception:
                pass

        if not payload:
            logger.error(f"[Shadow Mode] Task {task_id} timed out awaiting human deliverable.")
            task.status = TaskStatus.FAILED
            task.metadata["failure_reason"] = "Human contractor deliverable timeout."
            if target_store and hasattr(target_store, "save_task"):
                await target_store.save_task(task)
            return TaskResult(
                status=TaskStatus.FAILED,
                content="Human contractor deliverable timeout.",
            )

        # 4. Process received deliverable payload
        deliverable_text = str(payload.get("content", "") or payload.get("summary", "")).strip()
        artifacts = payload.get("artifacts", [])

        task.status = TaskStatus.DONE
        task.result = {
            "summary": deliverable_text,
            "artifacts": artifacts,
            "submitted_by_human": True,
            "contractor_username": payload.get("username", "human_contractor"),
        }
        if target_store and hasattr(target_store, "save_task"):
            await target_store.save_task(task)

        logger.info(f"[Shadow Mode] Task {task_id} deliverable received and task completed.")
        return TaskResult(
            status=TaskStatus.DONE,
            content=deliverable_text,
            artifacts={"deliverable_files": artifacts, "submitted_by": payload.get("username", "human_contractor")},
        )
