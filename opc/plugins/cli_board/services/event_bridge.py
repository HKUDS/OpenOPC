"""Translate OPC engine callbacks into board-friendly runtime updates."""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable

from opc.presentation.kanban import STATUS_TO_COLUMN

BoardEventSink = Callable[[dict[str, Any]], Awaitable[None]]

_TOOL_RE = re.compile(r"^\[Tool:\s*([^\]]+)\]")


class CliBoardEventBridge:
    """Bridges in-process engine events to the board state layer."""

    def __init__(self, sink: BoardEventSink) -> None:
        self._sink = sink

    async def handle_event(self, event: Any) -> None:
        event_type = str(getattr(event, "event_type", "") or "")
        payload = getattr(event, "payload", {}) or {}

        if event_type == "task_status_changed":
            task_id = str(payload.get("task_id", "") or "")
            status = str(payload.get("status", "") or "")
            if task_id and status:
                await self._sink(
                    {
                        "kind": "task_status",
                        "task_id": task_id,
                        "status": status,
                        "column_id": STATUS_TO_COLUMN.get(status, "todo"),
                    }
                )
                if status in {"awaiting_review", "awaiting_peer", "blocked", "failed", "cancelled", "done"}:
                    await self._sink({"kind": "refresh", "reason": f"status:{status}"})
                return

        if event_type in {"task_created", "child_session_created"}:
            await self._sink({"kind": "refresh", "reason": event_type})
            return

        if event_type == "agent_status_changed":
            task_id = str(payload.get("task_id", "") or "")
            status = str(payload.get("status", "") or "")
            if task_id and status:
                await self._sink(
                    {
                        "kind": "runtime",
                        "task_id": task_id,
                        "status": status,
                        "current_tool": None,
                        "iteration": None,
                    }
                )
            return

        if event_type == "runtime_event":
            task_id = str(payload.get("task_id", "") or "")
            runtime_type = str(payload.get("type", "") or "").strip()
            if not task_id or not runtime_type:
                return
            if runtime_type == "status_snapshot":
                await self._sink(
                    {
                        "kind": "runtime",
                        "task_id": task_id,
                        "status": "tool_active" if payload.get("current_tool") else "reflecting",
                        "current_tool": payload.get("current_tool"),
                        "iteration": payload.get("iteration"),
                        "tool_elapsed_ms": payload.get("tool_elapsed_ms"),
                        "last_tool_summary": payload.get("last_tool_summary"),
                        "context_tokens": payload.get("context_tokens"),
                        "context_window": payload.get("context_window"),
                        "context_remaining_pct": payload.get("context_remaining_pct"),
                        "turn_cost_usd": payload.get("turn_cost_usd"),
                        "session_cost_usd": payload.get("session_cost_usd"),
                        "pending_permission_count": payload.get("pending_permission_count"),
                        "drain_mode": payload.get("drain_mode"),
                    }
                )
                return
            if runtime_type in {"tool_started", "tool_progress", "tool_completed", "tool_skipped", "permission_requested", "permission_resolved"}:
                await self._sink(
                    {
                        "kind": "runtime",
                        "task_id": task_id,
                        "status": "tool_active" if runtime_type in {"tool_started", "tool_progress"} else "reflecting",
                        "current_tool": payload.get("tool_name"),
                        "iteration": payload.get("iteration"),
                        "tool_elapsed_ms": payload.get("elapsed_ms"),
                        "last_tool_summary": payload.get("result_summary") or payload.get("message"),
                    }
                )
                return

        if event_type == "agent_log":
            task_id = str(payload.get("task_id", "") or "")
            if not task_id:
                return
            status = str(payload.get("status", "") or "")
            iteration = payload.get("iteration")
            current_tool = None
            runtime_status = status
            if status == "thinking":
                runtime_status = "reflecting"
                current_tool = "Reflect"
            elif status == "executing":
                current_tool = str(payload.get("tool", "") or "") or None
                runtime_status = "tool_active"
            await self._sink(
                {
                    "kind": "runtime",
                    "task_id": task_id,
                    "status": runtime_status,
                    "current_tool": current_tool,
                    "iteration": int(iteration) if isinstance(iteration, int) else None,
                }
            )

    async def handle_progress(self, text: str, *, task_id: str | None = None, **_: Any) -> None:
        if not task_id:
            return
        current_tool = None
        match = _TOOL_RE.match(str(text or "").strip())
        if match:
            current_tool = match.group(1).strip() or None
        await self._sink(
            {
                "kind": "progress",
                "task_id": task_id,
                "text": str(text or ""),
                "current_tool": current_tool,
            }
        )
