"""Realistic end-to-end test suite for EventAdapter.

Each test scenario simulates a real agent↔LLM interaction trajectory,
replaying the exact OPC event sequence and verifying the resulting
visual events match what the Phaser frontend expects.

Event sources traced from:
  - runtime_v2/runtime.py: runtime status/progress events
  - native_agent.py: agent_status_changed(running/idle)
  - communication.py: agent_message_sent, agent_message_replied,
                       meeting_started, meeting_ended
  - task_graph.py: task_created, task_status_changed
  - company_mode.py: progress_callback strings
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opc.plugins.office_ui.event_adapter import (
    EventAdapter,
    AgentAnimState,
)


# ── Fake OPCEvent ────────────────────────────────────────────────────────────

@dataclass
class FakeEvent:
    event_type: str
    payload: dict[str, Any]
    timestamp: float = 0.0
    event_id: str = "test"


# ── Helpers ──────────────────────────────────────────────────────────────────

def types(events: list[dict]) -> list[str]:
    """Extract event types from visual events list."""
    return [e["type"] for e in events]


def types_for(events: list[dict], agent_id: str) -> list[str]:
    """Extract event types for a specific agent."""
    return [e["type"] for e in events if e["agent_id"] == agent_id]


def collect(adapter: EventAdapter, opc_events: list[FakeEvent]) -> list[dict]:
    """Feed a sequence of OPC events and collect all visual events."""
    results = []
    for ev in opc_events:
        results.extend(adapter.translate(ev))
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 1: SINGLE_AGENT — "Fix a Python bug"
#
# Real trajectory: User submits "fix the TypeError in utils.py".
# Agent reads file, thinks, edits file, runs test, reports result.
#
# Backend event sequence (from native runtime + native_agent.py):
#   1. agent_status_changed(running)     — NativeAgent.execute_task()
#   2. agent_log(thinking, iter=1)       — first LLM call
#   3. agent_log(executing, tool=file_read)  — LLM chose file_read
#   4. agent_log(thinking, iter=2)       — LLM sees file content, plans edit
#   5. agent_log(executing, tool=file_edit)  — LLM edits the file
#   6. agent_log(thinking, iter=3)       — LLM decides to verify
#   7. agent_log(executing, tool=shell_exec) — runs pytest
#   8. agent_log(thinking, iter=4)       — LLM sees test passed, done
#   9. agent_status_changed(idle)        — task complete
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario1_SingleAgentBugFix:
    """Single agent fixing a Python bug with file_read → file_edit → shell_exec."""

    def setup_method(self):
        self.adapter = EventAdapter()
        self.task_id = "task-bugfix-001"
        self.agent_id = "backend_dev"

    def _bootstrap(self) -> list[dict]:
        """Register agent via agent_status_changed(running)."""
        return self.adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": self.agent_id, "status": "running", "task_id": self.task_id},
        ))

    def test_full_trajectory(self):
        events = []

        # 1. Agent becomes active
        ve = self._bootstrap()
        events.extend(ve)
        assert types(ve) == ["agent_active", "agent_runtime_update"]
        assert ve[0]["agent_id"] == self.agent_id

        # 2. First thinking iteration (iter=1) → message_in + reflect_start
        ve = self.adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": self.task_id, "status": "thinking", "iteration": 1},
        ))
        events.extend(ve)
        assert types(ve) == ["message_in", "reflect_start", "agent_runtime_update"]

        # 3. Executing file_read → reflect_done + tool_start(read)
        ve = self.adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": self.task_id, "status": "executing", "tool": "file_read"},
        ))
        events.extend(ve)
        assert types(ve) == ["reflect_done", "tool_start", "agent_runtime_update"]
        assert ve[1]["data"]["tool_name"] == "read"

        # 4. Thinking iter=2 → tool_done(read) + reflect_start
        ve = self.adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": self.task_id, "status": "thinking", "iteration": 2},
        ))
        events.extend(ve)
        assert types(ve) == ["tool_done", "reflect_start", "agent_runtime_update"]
        assert ve[0]["data"]["tool_name"] == "read"

        # 5. Executing file_edit → reflect_done + tool_start(edit)
        ve = self.adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": self.task_id, "status": "executing", "tool": "file_edit"},
        ))
        events.extend(ve)
        assert types(ve) == ["reflect_done", "tool_start", "agent_runtime_update"]
        assert ve[1]["data"]["tool_name"] == "edit"

        # 6. Thinking iter=3 → tool_done(edit) + reflect_start
        ve = self.adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": self.task_id, "status": "thinking", "iteration": 3},
        ))
        events.extend(ve)
        assert types(ve) == ["tool_done", "reflect_start", "agent_runtime_update"]

        # 7. Executing shell_exec (pytest) → reflect_done + tool_start(shell)
        ve = self.adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": self.task_id, "status": "executing", "tool": "shell_exec"},
        ))
        events.extend(ve)
        assert types(ve) == ["reflect_done", "tool_start", "agent_runtime_update"]
        assert ve[1]["data"]["tool_name"] == "shell"

        # 8. Thinking iter=4 (final) → tool_done(shell) + reflect_start
        ve = self.adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": self.task_id, "status": "thinking", "iteration": 4},
        ))
        events.extend(ve)
        assert types(ve) == ["tool_done", "reflect_start", "agent_runtime_update"]

        # 9. Agent goes idle → reflect_done + waiting
        ve = self.adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": self.agent_id, "status": "idle"},
        ))
        events.extend(ve)
        assert types(ve) == ["reflect_done", "waiting", "agent_runtime_update"]

        # Verify final tracker state
        tracker = self.adapter._get_tracker(self.agent_id)
        assert tracker.state == AgentAnimState.IDLE
        assert tracker.task_id is None

    def test_state_machine_integrity(self):
        """Verify tracker never enters invalid state transitions."""
        self._bootstrap()
        # After running, tracker should be IDLE (running doesn't change anim state)
        tracker = self.adapter._get_tracker(self.agent_id)
        assert tracker.state == AgentAnimState.IDLE

        # thinking → REFLECTING
        self.adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": self.task_id, "status": "thinking", "iteration": 1},
        ))
        assert tracker.state == AgentAnimState.REFLECTING

        # executing → TOOL_ACTIVE (after closing REFLECTING)
        self.adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": self.task_id, "status": "executing", "tool": "file_read"},
        ))
        assert tracker.state == AgentAnimState.TOOL_ACTIVE
        assert tracker.current_tool == "file_read"


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 2: SINGLE_AGENT with probe (sub-agent spawn)
#
# Real trajectory: "Research and summarize the codebase architecture"
# Agent thinks, spawns a probe sub-agent, then writes summary.
#
# Backend:
#   1. agent_status_changed(running)
#   2. agent_log(thinking, iter=1)
#   3. agent_log(executing, tool=probe)    — spawns sub-agent
#   4. agent_log(thinking, iter=2)
#   5. agent_log(executing, tool=file_write)
#   6. agent_status_changed(idle)
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario2_ProbeTool:
    """The read-only probe tool is distinct from native subagent spawning."""

    def test_probe_renders_as_a_regular_tool(self):
        adapter = EventAdapter()
        task_id = "task-research-001"
        agent_id = "architect"

        # Bootstrap
        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": agent_id, "status": "running", "task_id": task_id},
        ))

        # Think
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": task_id, "status": "thinking", "iteration": 1},
        ))

        # Native subagents have explicit agent_spawn/subagent runtime events;
        # a read-only probe remains an ordinary tool animation.
        ve = adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": task_id, "status": "executing", "tool": "probe"},
        ))
        assert "subagent_spawn" not in types(ve)
        assert "tool_start" in types(ve)
        tool_start = [e for e in ve if e["type"] == "tool_start"][0]
        assert tool_start["data"]["tool_name"] == "probe"


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 3: MULTI_AGENT — Two agents working in parallel
#
# Real trajectory: DAG with 2 parallel tasks:
#   - frontend_dev: "Build the login page" (file_write → shell_exec)
#   - backend_dev: "Create the auth API" (file_write → shell_exec)
#
# Events interleave because agents run concurrently.
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario3_MultiAgentParallel:
    """Two agents executing tasks concurrently — events interleave correctly."""

    def test_interleaved_execution(self):
        adapter = EventAdapter()
        fe_task = "task-frontend-001"
        be_task = "task-backend-001"

        # Both agents start
        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "frontend_dev", "status": "running", "task_id": fe_task},
        ))
        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "backend_dev", "status": "running", "task_id": be_task},
        ))

        # Frontend thinks first
        ve = adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": fe_task, "status": "thinking", "iteration": 1},
        ))
        assert ve[0]["agent_id"] == "frontend_dev"
        assert "reflect_start" in types(ve)

        # Backend thinks (interleaved)
        ve = adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": be_task, "status": "thinking", "iteration": 1},
        ))
        assert ve[0]["agent_id"] == "backend_dev"

        # Frontend executes file_write
        ve = adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": fe_task, "status": "executing", "tool": "file_write"},
        ))
        assert all(e["agent_id"] == "frontend_dev" for e in ve)
        assert "tool_start" in types(ve)

        # Backend executes file_write (independent)
        ve = adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": be_task, "status": "executing", "tool": "file_write"},
        ))
        assert all(e["agent_id"] == "backend_dev" for e in ve)

        # Verify both trackers are in TOOL_ACTIVE independently
        fe_tracker = adapter._get_tracker("frontend_dev")
        be_tracker = adapter._get_tracker("backend_dev")
        assert fe_tracker.state == AgentAnimState.TOOL_ACTIVE
        assert be_tracker.state == AgentAnimState.TOOL_ACTIVE
        assert fe_tracker.current_tool == "file_write"
        assert be_tracker.current_tool == "file_write"


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 4: COMPANY_MODE — send_dm (non-blocking)
#
# Real trajectory: backend_dev sends a question to frontend_dev.
# The send_dm tool is A-class (COLLAB_SKIP_TOOLS) — tool_start is suppressed.
# CommunicationManager publishes agent_message_sent which drives the visuals.
#
# Backend:
#   1. agent_status_changed(running, backend_dev)
#   2. agent_log(thinking, iter=1)
#   3. agent_log(executing, tool=send_dm)     — NO tool_start emitted
#   4. agent_message_sent(from=backend_dev, to=[frontend_dev])
#   5. agent_log(thinking, iter=2)            — continues work
#   6. agent_status_changed(idle)
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario4_SendDmNonBlocking:
    """Non-blocking send_dm: tool_start suppressed, message_out/in from semantic event."""

    def test_send_dm_flow(self):
        adapter = EventAdapter()
        task_id = "task-collab-001"

        # Bootstrap
        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "backend_dev", "status": "running", "task_id": task_id},
        ))

        # Think
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": task_id, "status": "thinking", "iteration": 1},
        ))

        # Execute send_dm — A-class: NO tool_start, just reflect_done
        ve = adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": task_id, "status": "executing", "tool": "send_dm"},
        ))
        # Should only have reflect_done (closing previous REFLECTING state)
        assert "tool_start" not in types(ve)
        assert "reflect_done" in types(ve)

        # Tracker stays IDLE (not TOOL_ACTIVE)
        tracker = adapter._get_tracker("backend_dev")
        assert tracker.state == AgentAnimState.IDLE

        # CommunicationManager fires agent_message_sent
        ve = adapter.translate(FakeEvent(
            "agent_message_sent",
            {
                "from": "backend_dev",
                "to": ["frontend_dev"],
                "type": "question",
                "subject": "API contract question",
                "urgency": "normal",
                "task_id": task_id,
            },
        ))
        # Frontend: backend_dev shows message_out bubble, frontend_dev shows message_in
        assert len(ve) == 2
        assert ve[0]["type"] == "message_out"
        assert ve[0]["agent_id"] == "backend_dev"
        assert ve[0]["data"]["content_preview"] == "API contract question"
        assert ve[1]["type"] == "message_in"
        assert ve[1]["agent_id"] == "frontend_dev"


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 5: COMPANY_MODE — blocking send_dm (AWAITING_PEER)
#
# Real trajectory: backend_dev sends blocking DM, gets paused, frontend_dev
# replies, backend_dev resumes.
#
# Backend:
#   1. agent_status_changed(running, backend_dev)
#   2. agent_log(thinking, iter=1)
#   3. agent_log(executing, tool=send_dm)          — blocking=True
#   4. agent_message_sent(from=backend_dev, to=[frontend_dev])
#   5. agent_status_changed(idle, backend_dev)     — AWAITING_PEER pause
#   ... frontend_dev replies ...
#   6. agent_message_replied(task_id=...)
#   7. agent_status_changed(running, backend_dev)  — resumed
#   8. agent_log(thinking, iter=2)
#   9. agent_status_changed(idle, backend_dev)     — done
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario5_BlockingSendDm:
    """Blocking send_dm: agent pauses at AWAITING_PEER, resumes after reply."""

    def test_blocking_dm_pause_resume(self):
        adapter = EventAdapter()
        task_id = "task-blocking-001"
        fe_task = "task-frontend-001"

        # Bootstrap both agents
        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "backend_dev", "status": "running", "task_id": task_id},
        ))
        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "frontend_dev", "status": "running", "task_id": fe_task},
        ))

        # backend_dev thinks, then executes send_dm
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": task_id, "status": "thinking", "iteration": 1},
        ))
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": task_id, "status": "executing", "tool": "send_dm"},
        ))

        # Message sent
        adapter.translate(FakeEvent(
            "agent_message_sent",
            {"from": "backend_dev", "to": ["frontend_dev"],
             "subject": "Need schema", "task_id": task_id},
        ))

        # backend_dev goes idle (AWAITING_PEER)
        ve = adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "backend_dev", "status": "idle"},
        ))
        assert "waiting" in types(ve)

        # frontend_dev replies → agent_message_replied
        ve = adapter.translate(FakeEvent(
            "agent_message_replied",
            {"msg_id": "msg-001", "reply_msg_id": "reply-001", "task_id": fe_task},
        ))
        assert ve[0]["type"] == "message_out"
        assert ve[0]["agent_id"] == "frontend_dev"

        # backend_dev resumes
        ve = adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "backend_dev", "status": "running", "task_id": task_id},
        ))
        assert "agent_active" in types(ve)


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 6: COMPANY_MODE — start_meeting
#
# Real trajectory: architect calls start_meeting with backend_dev + frontend_dev.
# All 3 agents walk to meeting room (collab_started).
#
# Backend:
#   1. agent_status_changed(running, architect)
#   2. agent_log(thinking, iter=1)
#   3. agent_log(executing, tool=start_meeting)   — A-class, NO tool_start
#   4. agent_message_sent × 2 (invites to participants)
#   5. meeting_started(participants=[architect, backend_dev, frontend_dev])
#   6. agent_status_changed(idle, architect)      — AWAITING_PEER
#   ... meeting progresses ...
#   7. meeting_ended(outcome="Agreed on REST API")
#   8. agent_status_changed(running, architect)   — resumed
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario6_StartMeeting:
    """Meeting flow: collab_started for all participants, collab_ended on finish."""

    def test_meeting_lifecycle(self):
        adapter = EventAdapter()
        task_id = "task-meeting-001"
        participants = ["architect", "backend_dev", "frontend_dev"]

        # Bootstrap architect
        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "architect", "status": "running", "task_id": task_id},
        ))

        # Think
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": task_id, "status": "thinking", "iteration": 1},
        ))

        # Execute start_meeting — A-class: NO tool_start
        ve = adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": task_id, "status": "executing", "tool": "start_meeting"},
        ))
        assert "tool_start" not in types(ve)

        # meeting_started → collab_started for all participants
        ve = adapter.translate(FakeEvent(
            "meeting_started",
            {
                "room_id": "room-001",
                "task_id": task_id,
                "topic": "API Design Review",
                "participants": participants,
            },
        ))
        assert len(ve) == 3
        assert all(e["type"] == "collab_started" for e in ve)
        assert {e["agent_id"] for e in ve} == set(participants)

        # Architect goes idle (meeting pauses initiator)
        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "architect", "status": "idle"},
        ))

        # Set all participants to REFLECTING to test meeting_ended
        for p in participants:
            tracker = adapter._get_tracker(p)
            tracker.state = AgentAnimState.REFLECTING

        # meeting_ended → collab_ended for reflecting agents
        ve = adapter.translate(FakeEvent(
            "meeting_ended",
            {"room_id": "room-001", "task_id": task_id,
             "outcome": "Agreed on REST API"},
        ))
        assert all(e["type"] == "collab_ended" for e in ve)
        reflecting_agents = {e["agent_id"] for e in ve}
        assert reflecting_agents == set(participants)


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 7: B-class collaboration tools (read_inbox)
#
# This tool has no downstream semantic event, so EventAdapter emits a direct
# message_in visual event.
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario7_DirectMapCollabTools:
    """B-class tools: read_inbox → message_in."""

    def _setup_agent(self, adapter, agent_id, task_id):
        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": agent_id, "status": "running", "task_id": task_id},
        ))
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": task_id, "status": "thinking", "iteration": 1},
        ))

    def test_read_inbox(self):
        adapter = EventAdapter()
        self._setup_agent(adapter, "backend_dev", "task-inbox-001")

        ve = adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "task-inbox-001", "status": "executing", "tool": "read_inbox"},
        ))
        # reflect_done (closing REFLECTING) + message_in (direct map)
        assert "reflect_done" in types(ve)
        assert "message_in" in types(ve)
        msg = [e for e in ve if e["type"] == "message_in"][0]
        assert msg["data"]["content_preview"] == "Read Inbox"
        # Stays IDLE (not TOOL_ACTIVE)
        assert adapter._get_tracker("backend_dev").state == AgentAnimState.IDLE

# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 8: COMPANY_MODE — Work-item runtime with progress callbacks
#
# Real trajectory: Company mode runs work items with gates.
# progress_callback strings from company_mode.py are parsed by parse_progress.
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario8_CompanyModeProgress:
    """Company mode progress callbacks produce correct visual events."""

    def setup_method(self):
        self.adapter = EventAdapter()
        # Set up an active agent so parse_progress can find it
        self.adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "pm", "status": "running", "task_id": "task-co-001"},
        ))
        self.adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "task-co-001", "status": "thinking", "iteration": 1},
        ))

    def test_work_item_starting(self):
        ve = self.adapter.parse_progress("[Company:design] starting UI wireframes")
        assert len(ve) == 1
        assert ve[0]["type"] == "task_delegated"
        assert ve[0]["data"]["target"] == "design"

    def test_gate_passed(self):
        ve = self.adapter.parse_progress("[Company:design] gate passed")
        assert ve[0]["type"] == "delegation_done"
        assert ve[0]["data"]["target"] == "design"

    def test_work_item_completed(self):
        ve = self.adapter.parse_progress("[Company:design] completed")
        assert ve[0]["type"] == "delegation_done"
        assert ve[0]["data"]["target"] == "design"

    def test_gate_rejected_reworking(self):
        ve = self.adapter.parse_progress("[Company:impl] rejected; reworking: missing error handling")
        assert ve[0]["type"] == "message_out"
        assert "Rework:" in ve[0]["data"]["content_preview"]

    def test_delegating_to_agent(self):
        ve = self.adapter.parse_progress("[Delegating to backend_dev]")
        assert ve[0]["type"] == "task_delegated"
        assert ve[0]["data"]["target"] == "backend_dev"

    def test_external_agent_message(self):
        ve = self.adapter.parse_progress("[External agent heartbeat] backend_dev alive")
        assert ve[0]["type"] == "message_out"

    def test_external_status_is_visible(self):
        ve = self.adapter.parse_progress("[External status] checking agent health")
        assert ve[0]["type"] == "message_out"
        assert ve[0]["data"]["content_preview"] == "checking agent health"

    def test_tool_prefix_skipped(self):
        ve = self.adapter.parse_progress("[Tool: file_read] reading config.py")
        assert ve == []

    def test_capability_recovery(self):
        ve = self.adapter.parse_progress("[CapabilityRecovery] adopted: web_search")
        assert ve[0]["type"] == "skill_adopted"
        assert ve[0]["data"]["skill_name"] == "web_search"

    def test_raw_llm_response(self):
        ve = self.adapter.parse_progress(
            "I've completed the implementation of the login page with all required fields."
        )
        assert ve[0]["type"] == "message_out"
        assert len(ve[0]["data"]["content_preview"]) <= 30

    def test_short_text_ignored(self):
        ve = self.adapter.parse_progress("OK")
        assert ve == []


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 9: Task creation
#
# task_created → task_routed (system)
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario9_TaskCreation:
    """Task creation lifecycle events."""

    def test_task_created(self):
        adapter = EventAdapter()
        ve = adapter.translate(FakeEvent(
            "task_created",
            {"title": "Implement user authentication", "task_id": "task-auth-001"},
        ))
        assert len(ve) == 1
        assert ve[0]["type"] == "task_routed"
        assert ve[0]["agent_id"] == "system"
        assert ve[0]["data"]["task_preview"] == "Implement user authentication"
        assert adapter.task_display_counter == 1

    def test_multiple_tasks_increment_counter(self):
        adapter = EventAdapter()
        adapter.translate(FakeEvent("task_created", {"title": "Task A"}))
        adapter.translate(FakeEvent("task_created", {"title": "Task B"}))
        assert adapter.task_display_counter == 2

    def test_task_status_changed_no_visual(self):
        adapter = EventAdapter()
        ve = adapter.translate(FakeEvent(
            "task_status_changed",
            {"task_id": "task-001", "old_status": "pending", "new_status": "running"},
        ))
        assert ve == []


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 10: reply_message + broadcast_issue (A-class)
#
# reply_message: tool_start suppressed, agent_message_sent + agent_message_replied
#                drive message_out/in bubbles.
# broadcast_issue: same pattern but to multiple recipients.
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario10_ReplyAndBroadcast:
    """reply_message and broadcast_issue: A-class tools with semantic events."""

    def test_reply_message_flow(self):
        adapter = EventAdapter()
        task_id = "task-reply-001"

        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "frontend_dev", "status": "running", "task_id": task_id},
        ))
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": task_id, "status": "thinking", "iteration": 1},
        ))

        # Execute reply_message — A-class: NO tool_start
        ve = adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": task_id, "status": "executing", "tool": "reply_message"},
        ))
        assert "tool_start" not in types(ve)

        # Semantic events from CommunicationManager
        # 1) agent_message_sent (the reply itself)
        ve = adapter.translate(FakeEvent(
            "agent_message_sent",
            {"from": "frontend_dev", "to": ["backend_dev"],
             "subject": "Schema looks good"},
        ))
        assert ve[0]["type"] == "message_out"
        assert ve[1]["type"] == "message_in"
        assert ve[1]["agent_id"] == "backend_dev"

        # 2) agent_message_replied
        ve = adapter.translate(FakeEvent(
            "agent_message_replied",
            {"msg_id": "orig-001", "reply_msg_id": "reply-001", "task_id": task_id},
        ))
        assert ve[0]["type"] == "message_out"
        assert ve[0]["agent_id"] == "frontend_dev"

    def test_broadcast_to_multiple(self):
        adapter = EventAdapter()
        # broadcast_issue sends to multiple agents
        ve = adapter.translate(FakeEvent(
            "agent_message_sent",
            {
                "from": "pm",
                "to": ["backend_dev", "frontend_dev", "qa_dev"],
                "subject": "Critical: API breaking change",
            },
        ))
        # 1 message_out (pm) + 3 message_in (recipients)
        assert len(ve) == 4
        assert ve[0]["type"] == "message_out"
        assert ve[0]["agent_id"] == "pm"
        recipients = [e["agent_id"] for e in ve[1:]]
        assert set(recipients) == {"backend_dev", "frontend_dev", "qa_dev"}
        assert all(e["type"] == "message_in" for e in ve[1:])


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 11: Edge cases
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario11_EdgeCases:
    """Edge cases and boundary conditions."""

    def test_unknown_task_id_returns_empty(self):
        """agent_log for unregistered task_id returns no events."""
        adapter = EventAdapter()
        ve = adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "nonexistent", "status": "thinking", "iteration": 1},
        ))
        assert ve == []

    def test_empty_role_id_returns_empty(self):
        adapter = EventAdapter()
        ve = adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "", "status": "running", "task_id": "t1"},
        ))
        assert ve == []

    def test_idle_from_tool_active(self):
        """Going idle while TOOL_ACTIVE emits tool_done before waiting."""
        adapter = EventAdapter()
        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "dev", "status": "running", "task_id": "t1"},
        ))
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "t1", "status": "thinking", "iteration": 1},
        ))
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "t1", "status": "executing", "tool": "shell_exec"},
        ))
        # Now TOOL_ACTIVE; go idle directly
        ve = adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "dev", "status": "idle"},
        ))
        assert types(ve) == ["tool_done", "waiting", "agent_runtime_update"]
        assert ve[0]["data"]["tool_name"] == "shell"

    def test_consecutive_thinking_closes_previous(self):
        """Two consecutive thinking events properly close the first."""
        adapter = EventAdapter()
        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "dev", "status": "running", "task_id": "t1"},
        ))
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "t1", "status": "thinking", "iteration": 1},
        ))
        # Second thinking without executing in between
        ve = adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "t1", "status": "thinking", "iteration": 2},
        ))
        # Should emit reflect_done (closing first) then reflect_start (opening second)
        assert types(ve) == ["reflect_done", "reflect_start", "agent_runtime_update"]

    def test_unknown_tool_passes_through(self):
        """Tool not in TOOL_MAP uses raw name."""
        adapter = EventAdapter()
        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "dev", "status": "running", "task_id": "t1"},
        ))
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "t1", "status": "thinking", "iteration": 1},
        ))
        ve = adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "t1", "status": "executing", "tool": "custom_tool_xyz"},
        ))
        tool_start = [e for e in ve if e["type"] == "tool_start"][0]
        assert tool_start["data"]["tool_name"] == "custom_tool_xyz"

    def test_agent_message_sent_body_fallback(self):
        """When subject is empty, falls back to body."""
        adapter = EventAdapter()
        ve = adapter.translate(FakeEvent(
            "agent_message_sent",
            {"from": "dev", "to": ["pm"], "subject": "", "body": "Here is my update"},
        ))
        assert ve[0]["data"]["content_preview"] == "Here is my update"

    def test_agent_message_sent_default_fallback(self):
        """When both subject and body are empty, uses 'Message'."""
        adapter = EventAdapter()
        ve = adapter.translate(FakeEvent(
            "agent_message_sent",
            {"from": "dev", "to": [], "subject": "", "body": ""},
        ))
        assert ve[0]["data"]["content_preview"] == "Message"

    def test_task_agent_map_cleanup_on_idle(self):
        """task→agent mapping is cleaned up when agent goes idle."""
        adapter = EventAdapter()
        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "dev", "status": "running", "task_id": "t1"},
        ))
        assert "t1" in adapter._task_agent_map

        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "dev", "status": "idle"},
        ))
        assert "t1" not in adapter._task_agent_map


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario 12: Full COMPANY_MODE runtime end-to-end
#
# Simulates a complete company runtime:
#   PM creates task → architect designs → backend_dev implements →
#   backend_dev sends DM to frontend_dev → meeting → final idle
# ═══════════════════════════════════════════════════════════════════════════════

class TestScenario12_CompanyModeE2E:
    """Full company-mode runtime across multiple agents and work items."""

    def test_full_company_runtime(self):
        adapter = EventAdapter()
        all_events = []

        # ── Step 1: Task created ──
        ve = adapter.translate(FakeEvent(
            "task_created",
            {"title": "Build REST API for user management"},
        ))
        all_events.extend(ve)
        assert ve[0]["type"] == "task_routed"

        # ── Step 2: Architect designs ──
        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "architect", "status": "running", "task_id": "t-arch"},
        ))
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "t-arch", "status": "thinking", "iteration": 1},
        ))
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "t-arch", "status": "executing", "tool": "file_write"},
        ))
        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "architect", "status": "idle"},
        ))

        # ── Step 3: backend_dev implements ──
        adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "backend_dev", "status": "running", "task_id": "t-impl"},
        ))
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "t-impl", "status": "thinking", "iteration": 1},
        ))
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "t-impl", "status": "executing", "tool": "file_write"},
        ))

        # ── Step 4: backend_dev sends DM to frontend_dev ──
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "t-impl", "status": "thinking", "iteration": 2},
        ))
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "t-impl", "status": "executing", "tool": "send_dm"},
        ))
        ve = adapter.translate(FakeEvent(
            "agent_message_sent",
            {"from": "backend_dev", "to": ["frontend_dev"],
             "subject": "Need component specs"},
        ))
        assert len(ve) == 2  # message_out + message_in

        # ── Step 5: Meeting ──
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "t-impl", "status": "thinking", "iteration": 3},
        ))
        adapter.translate(FakeEvent(
            "agent_log",
            {"task_id": "t-impl", "status": "executing", "tool": "start_meeting"},
        ))
        ve = adapter.translate(FakeEvent(
            "meeting_started",
            {"room_id": "r1", "participants": ["backend_dev", "frontend_dev"]},
        ))
        assert len(ve) == 2
        assert all(e["type"] == "collab_started" for e in ve)

        # ── Step 6: Wrap up ──
        ve = adapter.translate(FakeEvent(
            "agent_status_changed",
            {"role_id": "backend_dev", "status": "idle"},
        ))
        assert "waiting" in types(ve)

        # Final state check
        be_tracker = adapter._get_tracker("backend_dev")
        assert be_tracker.state == AgentAnimState.IDLE
        assert be_tracker.task_id is None
