"""HumanAgentAdapter for OpenOPC Shadow Mode.

Bypasses LLM inference for human-assigned tasks, marks task state as
AWAITING_HUMAN_DELIVERABLE, emits event to Layer 0 MessageBus, and
reactively awaits human deliverable submission via Streamlit.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from opc.core.models import Task, TaskResult, TaskStatus
from opc.layer0_interaction.message_bus import MessageBus
from opc.layer3_agent.adapters.base import ExternalAgentAdapter, ExternalAgentConfig


class HumanAgentAdapter(ExternalAgentAdapter):
    """Adapter for human contractor seats in Shadow Mode."""

    agent_type: str = "human"
    default_command: str = "streamlit"

    def __init__(self, config: ExternalAgentConfig | None = None, message_bus: MessageBus | None = None) -> None:
        super().__init__(config)
        self.message_bus = message_bus or MessageBus()

    async def execute_human_task(
        self,
        task: Task,
        store: Any,
        message_bus: MessageBus | None = None,
        timeout: float | None = 86400.0 * 7,  # Default 7-day timeout for human deliverable
    ) -> TaskResult:
        """Execute a human contractor task by marking state and reactively awaiting deliverable."""
        mbus = message_bus or self.message_bus
        task_id = str(task.id or "").strip()
        owner_role = str(getattr(task, "assigned_to", "") or task.metadata.get("owner_role") or "").strip()

        logger.info(f"[Shadow Mode] HumanAgentAdapter executing task={task_id} for owner_role={owner_role}")

        # 1. Update task status and metadata in persistent store
        task.status = TaskStatus.AWAITING_HUMAN
        task.metadata["sub_state"] = "AWAITING_HUMAN_DELIVERABLE"
        task.metadata["owner_role"] = owner_role
        if hasattr(store, "save_task"):
            await store.save_task(task)

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
        logger.info(f"[Shadow Mode] Task {task_id} waiting reactively on MessageBus topic '{topic}'")

        # 3. Reactive Event Handling via Layer 0 (No Polling)
        try:
            payload = await mbus.subscribe_once(topic, timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"[Shadow Mode] Task {task_id} timed out awaiting human deliverable.")
            task.status = TaskStatus.FAILED
            task.metadata["failure_reason"] = "Human contractor deliverable timeout."
            if hasattr(store, "save_task"):
                await store.save_task(task)
            return TaskResult(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error="Human contractor deliverable timeout.",
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
        if hasattr(store, "save_task"):
            await store.save_task(task)

        logger.info(f"[Shadow Mode] Task {task_id} deliverable received and task completed.")
        return TaskResult(
            task_id=task_id,
            status=TaskStatus.DONE,
            output=deliverable_text,
            artifacts=artifacts,
        )
