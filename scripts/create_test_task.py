"""Script to seed test tasks for human contractor role testing."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from opc.core.models import Task, TaskStatus
from opc.database.store import OPCStore


async def create_test_tasks(db_path: str, role_id: str = "developer") -> None:
    store = OPCStore(db_path)
    await store.ensure_ready()

    task1 = Task(
        id="task_hero_ui",
        title="Build Responsive Hero Section Component",
        description="Implement modern glassmorphism landing page header component with React/Tailwind.",
        assigned_to=role_id,
        status=TaskStatus.RUNNING,
        priority=1,
        metadata={"sub_state": "AWAITING_HUMAN_DELIVERABLE", "owner_role": role_id},
    )

    task2 = Task(
        id="task_api_auth",
        title="Implement API Rate Limiter & Security Review",
        description="Review backend endpoints for JWT authentication and add token bucket rate limiting.",
        assigned_to=role_id,
        status=TaskStatus.AWAITING_HUMAN,
        priority=2,
        metadata={"sub_state": "AWAITING_HUMAN_DELIVERABLE", "owner_role": role_id},
    )

    await store.save_task(task1)
    await store.save_task(task2)

    print(f"[SUCCESS] Seeded test tasks for role '{role_id}' into '{db_path}':")
    print(f"   1. {task1.id} - {task1.title}")
    print(f"   2. {task2.id} - {task2.title}")


def main() -> None:
    db_path = os.getenv("OPC_DB_PATH", ".opc/tasks.db")
    asyncio.run(create_test_tasks(db_path, role_id="developer"))


if __name__ == "__main__":
    main()
