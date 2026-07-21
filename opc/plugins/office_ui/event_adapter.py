"""Stateful OPCEvent → VisualEvent translator.

Tracks per-agent animation state to synthesize paired start/done events
that the office-UI frontend expects.

OPC emits linear transitions: thinking → executing → thinking → idle
UI expects paired events:     reflect_start → reflect_done → tool_start → tool_done → waiting

The EventAdapter bridges this gap by maintaining a state machine per agent.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ── OPC tool name → UI tool name mapping ──────────────────────────────────

TOOL_MAP: dict[str, str] = {
    "file_read": "read",
    "file_search": "search",
    "list_dir": "list",
    "web_search": "web_search",
    "web_fetch": "fetch",
    "todo_read": "read",
    "file_write": "write",
    "file_edit": "edit",
    "apply_patch": "edit",
    "shell_exec": "shell",
    "python_exec": "shell",
    "git_status": "shell",
    "git_commit": "shell",
    "git_diff": "shell",
    "todo_write": "write",
    "agent_spawn": "reflect",
    "browser_navigate": "web_search",
    "browser_snapshot": "read",
    "browser_click": "shell",
    "browser_type": "write",
    "browser_take_screenshot": "read",
    "browser_close": "shell",
    "browser_wait_for": "shell",
    "browser_scroll": "shell",
    "browser_select_option": "write",
    "browser_navigate_back": "web_search",
}

# Collaboration tools whose visual effects are driven by downstream semantic
# events (meeting_started → collab_started, agent_message_sent → message_out).
# Emitting tool_start for these would conflict: the agent walks to the desk
# first, then immediately redirects to the meeting room when the real event
# arrives.  Skipping tool_start/tool_done avoids this visual stutter.
COLLAB_SKIP_TOOLS: frozenset[str] = frozenset({
    "start_meeting",
    "respond_meeting",
    "send_dm",
    "reply_message",
    "broadcast_issue",
})

# Collaboration tools that have NO downstream semantic event.
# We emit a lightweight visual event directly instead of tool_start.
COLLAB_DIRECT_MAP: dict[str, str] = {
    "inbox": "message_in",
    "read_inbox": "message_in",
}


# ── Per-agent animation state ─────────────────────────────────────────────

class AgentAnimState(Enum):
    IDLE = "idle"
    REFLECTING = "reflecting"
    TOOL_ACTIVE = "tool_active"


@dataclass
class AgentTracker:
    state: AgentAnimState = AgentAnimState.IDLE
    current_tool: str | None = None
    task_id: str | None = None


# ── Helper ─────────────────────────────────────────────────────────────────

def _ve(agent_id: str, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Build a VisualEvent dict."""
    return {
        "event_id": str(uuid.uuid4()),
        "type": event_type,
        "agent_id": agent_id,
        "data": data,
        "timestamp": time.time(),
    }


def _add_execution_turn_aliases(data: dict[str, Any], task_id: Any | None = None) -> dict[str, Any]:
    runtime_task_id = str(
        task_id
        or data.get("runtime_task_id")
        or data.get("execution_turn_id")
        or data.get("task_id")
        or ""
    ).strip()
    if runtime_task_id:
        data.setdefault("runtime_task_id", runtime_task_id)
        data.setdefault("execution_turn_id", runtime_task_id)
    return data


# ── EventAdapter ───────────────────────────────────────────────────────────

class EventAdapter:
    """Stateful OPC → Visual event translator.

    Maintains per-agent state machines to emit correct transition events.
    """

    def __init__(self) -> None:
        self._trackers: dict[str, AgentTracker] = {}
        # Maps task_id → agent_id (populated from agent_status_changed events)
        self._task_agent_map: dict[str, str] = {}
        self._task_display_counter: int = 0
        # Maps task_id → display number (populated from task_created events)
        self._task_display_map: dict[str, int] = {}
        # Maps opc_role_id → UI agent_id (synced from agent_store)
        self._role_agent_map: dict[str, str] = {}

    def update_role_map(self, role_agent_map: dict[str, str]) -> None:
        """Sync the opc_role_id → UI agent_id mapping from agent_store."""
        self._role_agent_map = dict(role_agent_map)

    def _resolve_role_to_agent(self, role_id: str) -> str:
        """Map OPC role_id to UI agent_id. Falls back to role_id itself."""
        return self._role_agent_map.get(role_id, role_id)

    def _get_tracker(self, agent_id: str) -> AgentTracker:
        if agent_id not in self._trackers:
            self._trackers[agent_id] = AgentTracker()
        return self._trackers[agent_id]

    def _resolve_agent_from_task(self, task_id: str) -> str | None:
        """Resolve task_id → agent_id using the task→agent map."""
        return self._task_agent_map.get(task_id)

    def _close_previous_state(self, agent_id: str, tracker: AgentTracker) -> list[dict[str, Any]]:
        """Emit closing events for the agent's current animation state.

        REFLECTING → emit reflect_done
        TOOL_ACTIVE → emit tool_done(current_tool)
        IDLE → nothing
        """
        events: list[dict[str, Any]] = []
        if tracker.state == AgentAnimState.REFLECTING:
            events.append(_ve(agent_id, "reflect_done", {}))
            tracker.state = AgentAnimState.IDLE
        elif tracker.state == AgentAnimState.TOOL_ACTIVE:
            tool_name = TOOL_MAP.get(tracker.current_tool or "", tracker.current_tool or "unknown")
            events.append(_ve(agent_id, "tool_done", {"tool_name": tool_name}))
            tracker.current_tool = None
            tracker.state = AgentAnimState.IDLE
        return events

    def _emit_runtime_update(
        self,
        agent_id: str,
        tracker: AgentTracker,
        iteration: int | None = None,
        extras: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build an agent_runtime_update event for the current tracker state."""
        data = {
            "agent_id": agent_id,
            "status": tracker.state.value,
            "current_tool": tracker.current_tool,
            "task_id": tracker.task_id,
            "iteration": iteration,
            **dict(extras or {}),
        }
        return _ve(agent_id, "agent_runtime_update", _add_execution_turn_aliases(data))

    def translate(self, event: Any) -> list[dict[str, Any]]:
        """Translate a single OPCEvent into zero or more VisualEvents.

        Args:
            event: An OPCEvent (has .event_type, .payload, .timestamp, .event_id)

        Returns:
            List of VisualEvent dicts ready to send to the frontend.
        """
        results: list[dict[str, Any]] = []
        etype: str = event.event_type
        p: dict[str, Any] = event.payload or {}

        if etype == "runtime_event":
            runtime_type = str(p.get("type", "runtime_event") or "runtime_event")
            role_id = str(p.get("role_id", "") or "").strip()
            agent_id = (
                self._resolve_role_to_agent(role_id)
                if role_id
                else str(p.get("agent_id") or p.get("task_id") or "runtime")
            )
            results.append(_ve(agent_id, runtime_type, dict(p)))
            if runtime_type in {
                "turn_started",
                "turn_completed",
                "turn_failed",
                "tool_started",
                "tool_progress",
                "tool_completed",
                "status_snapshot",
                "permission_requested",
                "permission_resolved",
                "subagent_started",
                "subagent_updated",
                "subagent_completed",
                "verification_started",
                "verification_completed",
            }:
                tracker = self._get_tracker(agent_id)
                tracker.task_id = str(p.get("task_id", "") or tracker.task_id or "")
                if runtime_type == "tool_started":
                    tracker.current_tool = str(p.get("tool_name", "") or "")
                    tracker.state = AgentAnimState.TOOL_ACTIVE
                elif runtime_type == "tool_progress":
                    tracker.current_tool = str(p.get("tool_name", "") or tracker.current_tool or "")
                    tracker.state = AgentAnimState.TOOL_ACTIVE
                elif runtime_type == "status_snapshot":
                    tracker.current_tool = str(p.get("current_tool", "") or "") or None
                    tracker.state = AgentAnimState.TOOL_ACTIVE if tracker.current_tool else AgentAnimState.REFLECTING
                elif runtime_type in {"turn_started", "permission_requested", "permission_resolved", "subagent_started", "subagent_updated", "verification_started"}:
                    tracker.state = AgentAnimState.REFLECTING
                elif runtime_type in {"tool_completed", "subagent_completed", "verification_completed"}:
                    # The turn is still running between tools — the agent is
                    # back to reasoning, not idle. IDLE is owned by turn end.
                    tracker.current_tool = None
                    tracker.state = AgentAnimState.REFLECTING
                elif runtime_type in {"turn_completed", "turn_failed"}:
                    tracker.current_tool = None
                    tracker.state = AgentAnimState.IDLE
                extras: dict[str, Any] = {}
                for key in (
                    "tool_elapsed_ms",
                    "last_tool_summary",
                    "context_tokens",
                    "context_window",
                    "context_remaining_pct",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                    "turn_cost_usd",
                    "session_cost_usd",
                    "pending_permission_count",
                    "drain_mode",
                ):
                    if key in p:
                        extras[key] = p.get(key)
                if runtime_type in {"tool_started", "tool_progress", "tool_completed"} and "elapsed_ms" in p:
                    extras.setdefault("tool_elapsed_ms", p.get("elapsed_ms"))
                if runtime_type == "tool_completed":
                    extras.setdefault("last_tool_summary", p.get("result_summary"))
                results.append(self._emit_runtime_update(agent_id, tracker, p.get("iteration"), extras))
            return results

        # ── agent_log {status:"thinking"} ──────────────────────────
        if etype == "agent_log" and p.get("status") == "thinking":
            task_id = p.get("task_id", "")
            agent_id = self._resolve_agent_from_task(task_id)
            if not agent_id:
                return []
            tracker = self._get_tracker(agent_id)

            # Close previous state (emits tool_done or reflect_done)
            results.extend(self._close_previous_state(agent_id, tracker))

            iteration = p.get("iteration", 1)
            if iteration == 1:
                # First iteration = new task received
                results.append(_ve(agent_id, "message_in", {
                    "content_preview": f"Task started (iter {iteration})",
                }))

            # Enter reflecting state
            results.append(_ve(agent_id, "reflect_start", {}))
            tracker.state = AgentAnimState.REFLECTING
            results.append(self._emit_runtime_update(agent_id, tracker, iteration))

        # ── agent_log {tool:X, status:"executing"} ─────────────────
        elif etype == "agent_log" and p.get("status") == "executing":
            task_id = p.get("task_id", "")
            agent_id = self._resolve_agent_from_task(task_id)
            if not agent_id:
                return []
            tracker = self._get_tracker(agent_id)
            tool = p.get("tool", "unknown")

            # Close previous state (emits reflect_done if was reflecting)
            results.extend(self._close_previous_state(agent_id, tracker))

            # Special: agent_spawn is treated as subagent spawn visual.
            if tool == "agent_spawn":
                results.append(_ve(agent_id, "subagent_spawn", {}))

            # A-class collab tools: skip tool_start entirely.
            # Downstream semantic events (meeting_started, agent_message_sent)
            # will drive the correct frontend animations (collab_started,
            # message_out, etc.) without the conflicting desk-walk.
            if tool in COLLAB_SKIP_TOOLS:
                pass  # stay IDLE — no TOOL_ACTIVE, no tool_start

            # B-class collab tools: emit a direct visual event.
            # These have no downstream semantic event to rely on.
            elif tool in COLLAB_DIRECT_MAP:
                results.append(_ve(agent_id, COLLAB_DIRECT_MAP[tool], {
                    "content_preview": tool.replace("_", " ").title(),
                }))
                # stay IDLE — these are instant operations

            # Regular tools: normal tool_start / TOOL_ACTIVE path
            else:
                mapped = TOOL_MAP.get(tool, tool)
                results.append(_ve(agent_id, "tool_start", {"tool_name": mapped}))
                tracker.state = AgentAnimState.TOOL_ACTIVE
                tracker.current_tool = tool

            # Emit runtime update for kanban
            results.append(self._emit_runtime_update(agent_id, tracker))

        # ── agent_status_changed {status:"running"} ────────────────
        elif etype == "agent_status_changed" and p.get("status") == "running":
            role_id = p.get("role_id", "")
            if not role_id:
                return []
            agent_id = self._resolve_role_to_agent(role_id)
            tracker = self._get_tracker(agent_id)
            task_id = p.get("task_id", "")
            if task_id:
                tracker.task_id = task_id
                self._task_agent_map[task_id] = agent_id
            results.append(_ve(agent_id, "agent_active", {}))
            results.append(self._emit_runtime_update(agent_id, tracker))

        # ── agent_status_changed {status:"idle"} ───────────────────
        elif etype == "agent_status_changed" and p.get("status") == "idle":
            role_id = p.get("role_id", "")
            if not role_id:
                return []
            agent_id = self._resolve_role_to_agent(role_id)
            tracker = self._get_tracker(agent_id)

            # Close previous state (emits tool_done or reflect_done)
            results.extend(self._close_previous_state(agent_id, tracker))

            # Save task_id before clearing so the runtime_update
            # reaches the correct frontend session.
            old_task_id = tracker.task_id

            # Clean up task mapping
            if tracker.task_id and tracker.task_id in self._task_agent_map:
                del self._task_agent_map[tracker.task_id]
            tracker.task_id = None
            results.append(_ve(agent_id, "waiting", {}))
            update_evt = self._emit_runtime_update(agent_id, tracker)
            update_evt["data"]["task_id"] = old_task_id
            results.append(update_evt)

        # ── task_created ───────────────────────────────────────────
        elif etype == "task_created":
            self._task_display_counter += 1
            task_id = p.get("task_id", "")
            if task_id:
                self._task_display_map[task_id] = self._task_display_counter
            results.append(_ve("system", "task_routed", {
                "method": "auto",
                "task_preview": p.get("title", ""),
            }))

        # ── child_session_created ─────────────────────────────────
        elif etype == "child_session_created":
            raw_agent = p.get("agent_id", "system")
            ui_agent = self._resolve_role_to_agent(raw_agent) if raw_agent != "system" else "system"
            results.append({
                "event_id": str(uuid.uuid4()),
                "type": "child_session_created",
                "agent_id": ui_agent,
                "data": {
                    "session_id": p.get("session_id"),
                    "parent_session_id": p.get("parent_session_id"),
                    "task_id": p.get("task_id"),
                    **_add_execution_turn_aliases({}, p.get("task_id")),
                    "title": p.get("title", ""),
                    "agent_id": ui_agent,
                },
                "timestamp": time.time(),
            })

        # ── task_status_changed ────────────────────────────────────
        elif etype == "task_status_changed":
            from opc.plugins.office_ui.snapshot_builder import STATUS_TO_COLUMN
            task_id = p.get("task_id")
            new_status = p.get("status")
            if task_id and new_status:
                column = STATUS_TO_COLUMN.get(new_status)
                if column:
                    results.append({
                        "event_id": str(uuid.uuid4()),
                        "type": "board_task_status_changed",
                        "agent_id": "system",
                        "data": {
                            "task_id": task_id,
                            **_add_execution_turn_aliases({}, task_id),
                            "column_id": column,
                            "status": new_status,
                        },
                        "timestamp": time.time(),
                    })

        # ── meeting_started ────────────────────────────────────────
        elif etype == "meeting_started":
            for participant in p.get("participants", []):
                ui_id = self._resolve_role_to_agent(participant)
                results.append(_ve(ui_id, "collab_started", {}))

        # ── meeting_ended ──────────────────────────────────────────
        elif etype == "meeting_ended":
            # Emit collab_ended for all agents currently in reflecting state
            for aid, tracker in self._trackers.items():
                if tracker.state == AgentAnimState.REFLECTING:
                    results.append(_ve(aid, "collab_ended", {}))

        # ── agent_message_sent ─────────────────────────────────────
        elif etype == "agent_message_sent":
            from_role = p.get("from", "")
            preview = (p.get("subject", "") or p.get("body", "") or "Message")[:30]
            if from_role:
                from_agent = self._resolve_role_to_agent(from_role)
                results.append(_ve(from_agent, "message_out", {
                    "content_preview": preview,
                }))
                # Notify each recipient so they show a receive-bubble
                for to_role in p.get("to", []):
                    to_agent = self._resolve_role_to_agent(to_role)
                    results.append(_ve(to_agent, "message_in", {
                        "content_preview": preview,
                    }))

        # ── agent_message_replied ──────────────────────────────────
        elif etype == "agent_message_replied":
            task_id = p.get("task_id", "")
            agent_id = self._resolve_agent_from_task(task_id) if task_id else None
            if agent_id:
                results.append(_ve(agent_id, "message_out", {
                    "content_preview": "Reply sent",
                }))

        # ── escalation_created ─────────────────────────────────────
        elif etype == "escalation_created":
            msg = p.get("message", "Escalation")
            results.append(_ve("system", "message_out", {
                "content_preview": msg[:30],
            }))

        # ── execution_mode_resolved ──────────────────────────────
        elif etype == "execution_mode_resolved":
            results.append(_ve("system", "execution_mode_resolved", p))

        # ── escalation_resolved / escalation_timeout ───────────────
        elif etype in ("escalation_resolved", "escalation_timeout"):
            pass  # No visual effect needed

        return results

    def _resolve_progress_agent(
        self,
        *,
        task_id: str | None = None,
        agent_role_id: str | None = None,
    ) -> str:
        """Resolve progress updates to the most likely UI agent."""
        role_id = str(agent_role_id or "").strip()
        if role_id:
            return self._resolve_role_to_agent(role_id)

        resolved_task_id = str(task_id or "").strip()
        if resolved_task_id:
            agent_id = self._resolve_agent_from_task(resolved_task_id)
            if agent_id:
                return agent_id

        for aid, tracker in self._trackers.items():
            if tracker.state != AgentAnimState.IDLE:
                return aid
        return "system"

    def parse_progress(
        self,
        text: str,
        *,
        task_id: str | None = None,
        agent_role_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Parse on_progress callback strings into VisualEvents.

        Progress updates can originate from native or external execution.
        Prefer the explicitly scoped task/role identity so company-mode child
        work items render on the correct role card instead of the last active one.
        """
        active_agent = self._resolve_progress_agent(
            task_id=task_id,
            agent_role_id=agent_role_id,
        )

        # P1: "[Tool: X] ..." — SKIP (redundant with agent_log events)
        if text.startswith("[Tool:"):
            return []

        # External agent monitoring status — show a lightweight role-scoped
        # bubble so company-mode work items remain visibly active.
        if text.startswith("[External status]"):
            detail = text[text.find("]") + 1:].strip() if "]" in text else text
            return [_ve(active_agent, "message_out", {
                "content_preview": detail[:30] if detail else "External status",
            })]

        # P3: "[Company:projection] starting title"
        if text.startswith("[Company:") and "] starting" in text:
            bracket_end = text.find("]")
            target = text[9:bracket_end] if bracket_end > 9 else "projection"
            return [_ve(active_agent, "task_delegated", {"target": target})]

        # P4: "[Company:projection] gate passed" / "completed"
        if text.startswith("[Company:") and ("gate passed" in text or " completed" in text):
            bracket_end = text.find("]")
            target = text[9:bracket_end] if bracket_end > 9 else "projection"
            return [_ve(active_agent, "delegation_done", {"target": target})]

        # P5: "[Company:projection] rejected; reworking"
        if text.startswith("[Company:") and "reworking" in text:
            bracket_end = text.find("]")
            preview = text[bracket_end + 2:bracket_end + 52] if bracket_end >= 0 else text[:50]
            return [_ve(active_agent, "message_out", {
                "content_preview": "Rework: " + preview,
            })]

        # P6-P9: Other [Company:...] messages
        if text.startswith("[Company:"):
            bracket_end = text.find("]")
            preview = text[bracket_end + 2:bracket_end + 32] if bracket_end >= 0 else text[:30]
            return [_ve(active_agent, "message_out", {"content_preview": preview})]

        # P10: "[Delegating to agent]"
        if text.startswith("[Delegating to"):
            bracket_end = text.find("]")
            target = text[15:bracket_end] if bracket_end > 15 else "agent"
            return [_ve(active_agent, "task_delegated", {"target": target})]

        # External broker stdout/stderr lines. Keep the office preview short;
        # the detailed content remains available in the session progress log.
        if text.startswith("[External:"):
            bracket_end = text.find("]")
            header = text[1:bracket_end] if bracket_end > 1 else "External"
            detail = text[bracket_end + 1:].strip() if bracket_end > 0 else ""
            parts = header.split(":")
            stream = parts[2] if len(parts) > 2 else ""
            preview = detail or stream or header
            if stream and detail:
                preview = f"{stream}: {detail}"
            return [_ve(active_agent, "message_out", {"content_preview": preview[:30]})]

        if text.startswith("[External approval]"):
            detail = text[text.find("]") + 1:].strip() if "]" in text else text
            return [_ve(active_agent, "message_out", {
                "content_preview": detail[:30] if detail else "Approval requested",
            })]

        # P11-P12: "[External agent ...]"
        if text.startswith("[External agent") or text.startswith("[External agents"):
            return [_ve(active_agent, "message_out", {"content_preview": text[:30]})]

        # P13-P14: "[CapabilityRecovery]"
        if text.startswith("[CapabilityRecovery]"):
            skill = text.split(":")[-1].strip()[:20] if ":" in text else "recovery"
            return [_ve(active_agent, "skill_adopted", {"skill_name": skill})]

        # P2: Raw LLM final response (long text without bracket prefix)
        if len(text) > 20 and not text.startswith("["):
            return [_ve(active_agent, "message_out", {"content_preview": text[:30]})]

        return []

    def reset(self) -> None:
        """Clear all tracker state (used on mode switch)."""
        self._trackers.clear()
        self._task_agent_map.clear()
        self._task_display_map.clear()
        self._task_display_counter = 0

    def get_tracker(self, agent_id: str) -> AgentTracker | None:
        """Return a copy of the tracker for the given agent, or None."""
        tracker = self._trackers.get(agent_id)
        if tracker is None:
            return None
        # Return a shallow copy to avoid external mutation
        return AgentTracker(
            state=tracker.state,
            current_tool=tracker.current_tool,
            task_id=tracker.task_id,
        )

    @property
    def task_display_counter(self) -> int:
        return self._task_display_counter

    def get_task_display_num(self, task_id: str) -> int:
        """Return the display number for a task_id, or current counter as fallback."""
        return self._task_display_map.get(task_id, self._task_display_counter)
