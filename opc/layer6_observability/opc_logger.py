"""Structured logging for OPC system."""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(log_dir: Path | None = None, level: str = "INFO") -> None:
    """Configure loguru for OPC with file + console output."""
    logger.remove()

    logger.add(
        sys.stderr,
        level=level,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan> - {message}",
        colorize=True,
    )

    if log_dir:
        from datetime import date
        day_dir = log_dir / date.today().isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(day_dir / "opc.log"),
            level="DEBUG",
            format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
            rotation="50 MB",
            retention="30 days",
        )


class OPCLogger:
    """Observability logger for trajectory recording and execution audit."""

    def __init__(self, store: Any = None) -> None:
        self.store = store

    async def log_trajectory_step(
        self,
        task_id: str,
        role_id: str,
        employee_id: str,
        action: str,
        content: str,
        artifacts: list[dict] | None = None,
        outcome_score: float = 1.0,
    ) -> None:
        """Record human or agent trajectory step into persistent store and org changelog."""
        logger.info(f"[OPCLogger] Trajectory step: task={task_id} role={role_id} employee={employee_id} action={action}")
        if self.store and hasattr(self.store, "save_trajectory"):
            await self.store.save_trajectory({
                "task_id": task_id,
                "role_id": role_id,
                "employee_id": employee_id,
                "action": action,
                "content": content,
                "artifacts": artifacts or [],
                "outcome_score": outcome_score,
            })
        
        # Silently write to ORG_CHANGELOGS table
        if self.store and hasattr(self.store, "log_org_changelog"):
            await self.store.log_org_changelog(
                event_type=f"task_{action.lower()}",
                actor_id=employee_id or role_id,
                description=f"[{role_id}] {action}: {content[:160]}",
                impact_score=outcome_score,
                metadata={"task_id": task_id, "artifacts_count": len(artifacts or [])},
            )

    async def log_org_changelog_event(
        self,
        event_type: str,
        actor_id: str,
        description: str,
        impact_score: float = 1.0,
        metadata: dict | None = None,
    ) -> str | None:
        """Directly record significant organizational event into ORG_CHANGELOGS."""
        logger.info(f"[OPCLogger] Org Changelog: event={event_type} actor={actor_id} impact={impact_score}")
        if self.store and hasattr(self.store, "log_org_changelog"):
            return await self.store.log_org_changelog(
                event_type=event_type,
                actor_id=actor_id,
                description=description,
                impact_score=impact_score,
                metadata=metadata,
            )
        return None

