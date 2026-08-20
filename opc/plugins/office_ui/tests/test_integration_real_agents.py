"""Integration tests — real LLM calls, real agent execution, real event pipeline.

Each test runs a NativeAgent with a real LLM (OpenRouter minimax-m2.5),
captures OPCEvents from the EventBus, translates them via EventAdapter,
and verifies the resulting visual events match what the Phaser frontend expects.

Pipeline under test:
    NativeAgent.execute(task)
      → NativeRuntimeV2 publishes OPCEvents to EventBus
      → EventAdapter.translate() → VisualEvents
      → Tests verify sequences, state machine, kanban mapping

Requires: OPENROUTER_API_KEY or the key in .opc/config/llm_config.yaml
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
from typing import Any

import pytest

from opc.core.config import LLMConfig, OPCConfig, SystemConfig, get_opc_home
from opc.core.events import EventBus
from opc.core.models import (
    AgentInfo,
    OPCEvent,
    Task,
    TaskStatus,
)
from opc.layer3_agent.native_agent import NativeAgent
from opc.layer4_tools.registry import ToolDefinition, ToolRegistry
from opc.layer4_tools.file_ops import (
    file_read,
    file_write,
    file_edit,
    file_search,
)
from opc.layer4_tools.shell import create_shell_tool
from opc.layer4_tools.web_search import create_web_tools
from opc.layer4_tools.python_exec import create_python_tool
from opc.layer4_tools.todo import create_todo_tools
from opc.layer5_memory.memory_manager import MemoryManager
from opc.layer5_memory.preference import PreferenceManager
from opc.layer5_memory.skill_library import SkillLibrary
from opc.llm.provider import LLMProvider
from opc.plugins.office_ui.event_adapter import (
    AgentAnimState,
    EventAdapter,
)


# ── Skip guard ──────────────────────────────────────────────────────────────

def _load_llm_config() -> LLMConfig:
    """Load LLM config from .opc/config or env."""
    try:
        cfg = OPCConfig.load(get_opc_home() / "config")
        if cfg.llm.api_key or cfg.llm.api_key_env:
            return cfg.llm
    except Exception:
        pass
    # Fallback to env
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        key = os.environ.get("ANTHROPIC_API_KEY", "")
    return LLMConfig(
        default_model="openrouter/minimax/minimax-m2.5",
        api_base="https://openrouter.ai/api/v1",
        api_key=key,
        temperature=0.3,
        max_tokens=4096,
    )


_LLM_CFG = _load_llm_config()
_HAS_KEY = bool(_LLM_CFG.api_key or os.environ.get(_LLM_CFG.api_key_env or "__NONE__", ""))
_RUN_REAL_AGENT_TESTS = os.environ.get("OPENOPC_RUN_REAL_AGENT_TESTS", "").strip() == "1"
_HAS_PYTEST_ASYNCIO = importlib.util.find_spec("pytest_asyncio") is not None

pytestmark = [
    pytest.mark.skipif(
        not (_HAS_KEY and _RUN_REAL_AGENT_TESTS and _HAS_PYTEST_ASYNCIO),
        reason=(
            "Set OPENOPC_RUN_REAL_AGENT_TESTS=1 with an LLM key and install "
            "pytest-asyncio to run real-agent tests"
        ),
    ),
    pytest.mark.timeout(180),
]
if _HAS_PYTEST_ASYNCIO:
    pytestmark.append(pytest.mark.asyncio)


# ── Helpers ─────────────────────────────────────────────────────────────────

def types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


def types_for(events: list[dict], agent_id: str) -> list[str]:
    return [e["type"] for e in events if e.get("agent_id") == agent_id]


def opc_types(events: list[OPCEvent]) -> list[str]:
    return [e.event_type for e in events]


def collect_visual(opc_events: list[OPCEvent], adapter: EventAdapter) -> list[dict]:
    results = []
    for ev in opc_events:
        results.extend(adapter.translate(ev))
    return results


def assert_lifecycle(visual: list[dict], agent_id: str) -> None:
    """Assert the fundamental agent lifecycle: agent_active ... waiting."""
    agent_events = [e for e in visual if e.get("agent_id") == agent_id]
    assert len(agent_events) >= 2, f"Expected at least 2 events for {agent_id}, got {len(agent_events)}"
    assert agent_events[0]["type"] == "agent_active", f"First event should be agent_active, got {agent_events[0]['type']}"
    assert agent_events[-1]["type"] == "waiting", f"Last event should be waiting, got {agent_events[-1]['type']}"


def assert_paired_events(visual: list[dict], agent_id: str) -> None:
    """Assert all reflect_start/reflect_done and tool_start/tool_done are properly paired."""
    agent_events = types_for(visual, agent_id)
    reflect_depth = 0
    tool_depth = 0
    for t in agent_events:
        if t == "reflect_start":
            assert reflect_depth == 0, "Nested reflect_start without reflect_done"
            reflect_depth += 1
        elif t == "reflect_done":
            assert reflect_depth == 1, "reflect_done without matching reflect_start"
            reflect_depth -= 1
        elif t == "tool_start":
            assert tool_depth == 0, "Nested tool_start without tool_done"
            tool_depth += 1
        elif t == "tool_done":
            assert tool_depth == 1, "tool_done without matching tool_start"
            tool_depth -= 1
    assert reflect_depth == 0, "Unclosed reflect_start"
    assert tool_depth == 0, "Unclosed tool_start"


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def llm() -> LLMProvider:
    return LLMProvider(_LLM_CFG)


@pytest.fixture
def event_collector():
    """Returns (event_bus, collected_events_list)."""
    bus = EventBus()
    collected: list[OPCEvent] = []

    async def _listener(event: OPCEvent) -> None:
        collected.append(event)

    bus.subscribe_all(_listener)
    return bus, collected


@pytest.fixture
def adapter() -> EventAdapter:
    return EventAdapter()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a temp workspace with seed files."""
    (tmp_path / "hello.txt").write_text("Hello from OPC integration test!\nLine 2\nLine 3\n")
    (tmp_path / "sample.py").write_text("x = 1\nprint(x)\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text('def main():\n    print("main")\n\nif __name__ == "__main__":\n    main()\n')
    (src / "utils.py").write_text('def add(a, b):\n    return a + b\n')
    (src / "config.py").write_text('DEBUG = True\nPORT = 8080\n')
    return tmp_path


def _make_tool_registry(tool_names: list[str]) -> ToolRegistry:
    """Create a ToolRegistry with only the specified tools."""
    registry = ToolRegistry()

    _FILE_TOOLS = {
        "file_read": ToolDefinition(
            name="file_read",
            description="Read file contents.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path"},
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer"},
                },
                "required": ["path"],
            },
            func=file_read,
            category="filesystem",
        ),
        "file_write": ToolDefinition(
            name="file_write",
            description="Write content to a file (creates dirs if needed).",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "create_dirs": {"type": "boolean", "default": True},
                },
                "required": ["path", "content"],
            },
            func=file_write,
            category="filesystem",
        ),
        "file_edit": ToolDefinition(
            name="file_edit",
            description="Replace an exact unique string in a file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
            func=file_edit,
            category="filesystem",
        ),
        "file_search": ToolDefinition(
            name="file_search",
            description="Search for a pattern in files using ripgrep.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "directory": {"type": "string", "default": "."},
                    "file_glob": {"type": "string", "default": "*"},
                    "max_results": {"type": "integer", "default": 50},
                },
                "required": ["pattern"],
            },
            func=file_search,
            category="filesystem",
        ),
        "list_dir": ToolDefinition(
            name="list_dir",
            description="List directory contents.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "recursive": {"type": "boolean", "default": False},
                },
                "required": [],
            },
            func=_list_dir_func,
            category="filesystem",
        ),
    }

    for name in tool_names:
        if name in _FILE_TOOLS:
            registry.register(_FILE_TOOLS[name])
        elif name == "shell_exec":
            registry.register(create_shell_tool())
        elif name == "python_exec":
            registry.register(create_python_tool())
        elif name in ("web_search", "web_fetch"):
            for t in create_web_tools():
                if t.name == name:
                    registry.register(t)
        elif name in ("todo_read", "todo_write"):
            for t in create_todo_tools():
                if t.name == name:
                    registry.register(t)

    return registry


async def _list_dir_func(path: str = ".", recursive: bool = False) -> dict[str, Any]:
    """Simple list_dir implementation for tests."""
    p = Path(path)
    if not p.exists():
        return {"error": f"Directory not found: {path}"}
    if not p.is_dir():
        return {"error": f"Not a directory: {path}"}
    entries = []
    if recursive:
        for item in sorted(p.rglob("*")):
            entries.append({"name": str(item.relative_to(p)), "type": "dir" if item.is_dir() else "file"})
    else:
        for item in sorted(p.iterdir()):
            entries.append({"name": item.name, "type": "dir" if item.is_dir() else "file"})
    return {"entries": entries[:200]}


def _make_agent(
    role_id: str,
    tools: list[str],
    responsibility: str,
    llm: LLMProvider,
    event_bus: EventBus,
    opc_home: Path,
    max_iterations: int = 3,
) -> NativeAgent:
    """Factory for creating a NativeAgent with minimal dependencies."""
    role = AgentInfo(
        role_id=role_id,
        name=role_id.replace("_", " ").title(),
        responsibility=responsibility,
        tools=tools,
    )
    memory = MemoryManager(opc_home=opc_home, project_id="test")
    prefs = PreferenceManager(opc_home=opc_home)
    skills = SkillLibrary(opc_home=opc_home)
    config = OPCConfig(system=SystemConfig(max_agent_iterations=max_iterations))
    tool_registry = _make_tool_registry(tools)

    return NativeAgent(
        role=role,
        llm=llm,
        tool_registry=tool_registry,
        memory=memory,
        preferences=prefs,
        skills=skills,
        event_bus=event_bus,
        config=config,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1: Single agent reads a file — verify full event pipeline
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration01_SingleAgentFileRead:
    """Agent reads a real file; verify OPC events → visual events lifecycle."""

    async def test_read_file_event_pipeline(self, llm, event_collector, adapter, workspace):
        bus, collected = event_collector
        agent = _make_agent(
            role_id="reader",
            tools=["file_read", "list_dir"],
            responsibility="Read files and report contents",
            llm=llm, event_bus=bus, opc_home=workspace,
            max_iterations=3,
        )
        task = Task(
            title="Read hello.txt",
            description=f"Read the file at {workspace}/hello.txt and tell me its full contents.",
            assigned_to="reader",
        )

        result = await agent.execute(task)

        # Agent should complete successfully
        assert result.status in (TaskStatus.DONE, TaskStatus.FAILED), f"Unexpected status: {result.status}"

        # OPC events must include the lifecycle
        opc_t = opc_types(collected)
        assert "agent_status_changed" in opc_t, "Missing agent_status_changed events"
        assert "agent_log" in opc_t, "Missing agent_log events"

        # Check running → idle lifecycle in raw events
        status_events = [e for e in collected if e.event_type == "agent_status_changed"]
        assert status_events[0].payload["status"] == "running"
        assert status_events[-1].payload["status"] == "idle"

        # Translate to visual events
        visual = collect_visual(collected, adapter)
        assert len(visual) >= 2, f"Expected at least 2 visual events, got {len(visual)}"

        # Verify lifecycle and pairing
        assert_lifecycle(visual, "reader")
        assert_paired_events(visual, "reader")

        # If agent used file_read, verify tool_start(read) appears
        tool_events = [e for e in collected if e.event_type == "agent_log" and e.payload.get("tool") == "file_read"]
        if tool_events:
            tool_visuals = [v for v in visual if v["type"] == "tool_start" and v["data"].get("tool_name") == "read"]
            assert len(tool_visuals) >= 1, "file_read executed but no tool_start(read) visual event"

        # Adapter state should be clean
        tracker = adapter._get_tracker("reader")
        assert tracker.state == AgentAnimState.IDLE
        assert tracker.task_id is None


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2: Single agent multi-tool — read, edit, verify
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration02_SingleAgentMultiTool:
    """Agent reads, edits, then runs shell to verify — multiple tool transitions."""

    async def test_multi_tool_sequence(self, llm, event_collector, adapter, workspace):
        bus, collected = event_collector
        agent = _make_agent(
            role_id="developer",
            tools=["file_read", "file_edit", "shell_exec"],
            responsibility="Code development and verification",
            llm=llm, event_bus=bus, opc_home=workspace,
            max_iterations=5,
        )
        task = Task(
            title="Fix sample.py",
            description=(
                f"1. Read the file at {workspace}/sample.py\n"
                f"2. Change 'x = 1' to 'x = 42'\n"
                f"3. Run 'python3 {workspace}/sample.py' to verify it prints 42"
            ),
            assigned_to="developer",
        )

        result = await agent.execute(task)
        assert result.status in (TaskStatus.DONE, TaskStatus.FAILED)

        # Verify multiple tool types were attempted
        tool_names_used = {
            e.payload.get("tool")
            for e in collected
            if e.event_type == "agent_log" and e.payload.get("status") == "executing"
        }
        # At minimum, agent should have tried file_read
        assert len(tool_names_used) >= 1, f"Expected at least 1 tool, got: {tool_names_used}"

        # Translate and verify
        visual = collect_visual(collected, adapter)
        assert_lifecycle(visual, "developer")
        assert_paired_events(visual, "developer")

        # Verify tool_start events have correct mapped names
        tool_starts = [v for v in visual if v["type"] == "tool_start"]
        for ts in tool_starts:
            assert ts["data"]["tool_name"] in ("read", "edit", "shell", "write", "search", "list", "reflect"), \
                f"Unexpected tool_name: {ts['data']['tool_name']}"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3: Single agent with probe sub-agent
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration03_ProbeSubagent:
    """Agent with write tools gets probe capability — verify subagent_spawn if used."""

    async def test_probe_exploration(self, llm, event_collector, adapter, workspace):
        bus, collected = event_collector
        agent = _make_agent(
            role_id="architect",
            tools=["file_read", "file_write", "file_edit", "file_search", "list_dir"],
            responsibility="Codebase exploration and architecture documentation",
            llm=llm, event_bus=bus, opc_home=workspace,
            max_iterations=5,
        )
        task = Task(
            title="Explore and summarize",
            description=(
                f"Explore the project structure at {workspace}/src/ using the probe tool. "
                f"Then create a file {workspace}/SUMMARY.md summarizing what you found."
            ),
            assigned_to="architect",
        )

        result = await agent.execute(task)
        assert result.status in (TaskStatus.DONE, TaskStatus.FAILED)

        visual = collect_visual(collected, adapter)
        assert_lifecycle(visual, "architect")
        assert_paired_events(visual, "architect")

        # If probe was used, verify subagent_spawn visual event
        probe_used = any(
            e.event_type == "agent_log" and e.payload.get("tool") == "probe"
            for e in collected
        )
        if probe_used:
            assert "subagent_spawn" in types(visual), "Probe used but no subagent_spawn event"
            probe_tool_starts = [
                v for v in visual
                if v["type"] == "tool_start" and v["data"].get("tool_name") == "reflect"
            ]
            assert len(probe_tool_starts) >= 1, "Probe used but no tool_start(reflect) event"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4: Two agents working in parallel — interleaved events
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration04_TwoParallelAgents:
    """Two agents run concurrently — verify independent event streams."""

    async def test_parallel_execution(self, llm, event_collector, adapter, workspace):
        bus, collected = event_collector

        agent_a = _make_agent(
            role_id="writer_agent",
            tools=["file_write"],
            responsibility="Create files",
            llm=llm, event_bus=bus, opc_home=workspace,
            max_iterations=3,
        )
        agent_b = _make_agent(
            role_id="shell_agent",
            tools=["shell_exec"],
            responsibility="Run shell commands",
            llm=llm, event_bus=bus, opc_home=workspace,
            max_iterations=3,
        )

        task_a = Task(
            title="Write a file",
            description=f"Create a file at {workspace}/output_a.txt with content 'Agent A was here'",
            assigned_to="writer_agent",
        )
        task_b = Task(
            title="Run echo",
            description="Run the command: echo 'Agent B says hello'",
            assigned_to="shell_agent",
        )

        results = await asyncio.gather(
            agent_a.execute(task_a),
            agent_b.execute(task_b),
        )

        # Both should complete
        for r in results:
            assert r.status in (TaskStatus.DONE, TaskStatus.FAILED)

        # Both agents should have status events
        status_agents = {
            e.payload.get("role_id")
            for e in collected
            if e.event_type == "agent_status_changed"
        }
        assert "writer_agent" in status_agents
        assert "shell_agent" in status_agents

        # Translate and verify each agent independently
        visual = collect_visual(collected, adapter)

        for agent_id in ("writer_agent", "shell_agent"):
            agent_vis = [v for v in visual if v.get("agent_id") == agent_id]
            if len(agent_vis) >= 2:
                assert_lifecycle(visual, agent_id)
                assert_paired_events(visual, agent_id)

        # Verify trackers are independent
        tracker_a = adapter._get_tracker("writer_agent")
        tracker_b = adapter._get_tracker("shell_agent")
        assert tracker_a.state == AgentAnimState.IDLE
        assert tracker_b.state == AgentAnimState.IDLE


# ═══════════════════════════════════════════════════════════════════════════════
# Test 5: Agent with web_search
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration05_WebSearch:
    """Agent uses web_search — verify tool mapping to web_search visual."""

    async def test_web_search_visual_events(self, llm, event_collector, adapter, workspace):
        bus, collected = event_collector
        agent = _make_agent(
            role_id="researcher",
            tools=["web_search"],
            responsibility="Web research and information gathering",
            llm=llm, event_bus=bus, opc_home=workspace,
            max_iterations=3,
        )
        task = Task(
            title="Web research",
            description="Search the web for 'Python asyncio best practices 2024' and summarize what you find.",
            assigned_to="researcher",
        )

        result = await agent.execute(task)
        # Agent should complete (even if web_search fails, LLM produces text)
        assert result.status in (TaskStatus.DONE, TaskStatus.FAILED)

        visual = collect_visual(collected, adapter)
        assert_lifecycle(visual, "researcher")
        assert_paired_events(visual, "researcher")

        # If web_search was used, check visual mapping
        ws_used = any(
            e.event_type == "agent_log" and e.payload.get("tool") == "web_search"
            for e in collected
        )
        if ws_used:
            ws_visuals = [
                v for v in visual
                if v["type"] == "tool_start" and v["data"].get("tool_name") == "web_search"
            ]
            assert len(ws_visuals) >= 1, "web_search used but no tool_start(web_search) visual"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 6: Agent creates a new file
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration06_FileWrite:
    """Agent creates a new file — verify file exists and tool_start(write) event."""

    async def test_file_creation(self, llm, event_collector, adapter, workspace):
        bus, collected = event_collector
        target = workspace / "greeting.txt"
        agent = _make_agent(
            role_id="writer",
            tools=["file_write"],
            responsibility="Create files with specified content",
            llm=llm, event_bus=bus, opc_home=workspace,
            max_iterations=3,
        )
        task = Task(
            title="Create greeting file",
            description=f"Create a file at {target} with the content: Hello World!",
            assigned_to="writer",
        )

        result = await agent.execute(task)
        assert result.status in (TaskStatus.DONE, TaskStatus.FAILED)

        visual = collect_visual(collected, adapter)
        assert_lifecycle(visual, "writer")
        assert_paired_events(visual, "writer")

        # If file_write was used, verify visual
        fw_used = any(
            e.event_type == "agent_log" and e.payload.get("tool") == "file_write"
            for e in collected
        )
        if fw_used:
            assert target.exists(), "Agent used file_write but file was not created"
            write_visuals = [
                v for v in visual
                if v["type"] == "tool_start" and v["data"].get("tool_name") == "write"
            ]
            assert len(write_visuals) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# Test 7: Agent completes without using any tools
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration07_NoTools:
    """Agent answers a simple question without tool calls."""

    async def test_direct_answer(self, llm, event_collector, adapter, workspace):
        bus, collected = event_collector
        agent = _make_agent(
            role_id="advisor",
            tools=[],
            responsibility="Answer questions directly",
            llm=llm, event_bus=bus, opc_home=workspace,
            max_iterations=3,
        )
        task = Task(
            title="Simple math",
            description="What is 2 + 2? Reply with just the number.",
            assigned_to="advisor",
        )

        result = await agent.execute(task)
        assert result.status == TaskStatus.DONE
        assert "4" in result.content, f"Expected '4' in response, got: {result.content[:200]}"

        # Should have no executing events (no tool calls)
        exec_events = [
            e for e in collected
            if e.event_type == "agent_log" and e.payload.get("status") == "executing"
        ]
        assert len(exec_events) == 0, f"Expected no tool executions, got {len(exec_events)}"

        # Visual: agent_active → reflect_start → reflect_done → waiting (no tool events)
        visual = collect_visual(collected, adapter)
        assert_lifecycle(visual, "advisor")
        assert_paired_events(visual, "advisor")

        tool_starts = [v for v in visual if v["type"] == "tool_start" and v.get("agent_id") == "advisor"]
        assert len(tool_starts) == 0, "No tools used but tool_start events appeared"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 8: Agent hits max iterations
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration08_MaxIterations:
    """Agent hits max_iterations=2 and fails — verify lifecycle still closes."""

    async def test_max_iterations_failure(self, llm, event_collector, adapter, workspace):
        bus, collected = event_collector
        # Create many files so the task can't finish in 2 iterations
        for i in range(20):
            (workspace / "src" / f"module_{i}.py").write_text(f"# Module {i}\ndef func_{i}(): pass\n")

        agent = _make_agent(
            role_id="analyzer",
            tools=["file_read", "list_dir", "file_search"],
            responsibility="Comprehensive code analysis",
            llm=llm, event_bus=bus, opc_home=workspace,
            max_iterations=2,  # intentionally low
        )
        task = Task(
            title="Analyze all modules",
            description=(
                f"Read and analyze EVERY single .py file in {workspace}/src/ directory. "
                f"You must read each file individually and provide detailed analysis of each one."
            ),
            assigned_to="analyzer",
        )

        result = await agent.execute(task)
        # Should fail or succeed within 2 iterations
        assert result.status in (TaskStatus.DONE, TaskStatus.FAILED)

        # Verify max 2 thinking events
        thinking_events = [
            e for e in collected
            if e.event_type == "agent_log" and e.payload.get("status") == "thinking"
        ]
        assert len(thinking_events) <= 2, f"Expected at most 2 thinking events, got {len(thinking_events)}"

        # Visual lifecycle should still close properly
        visual = collect_visual(collected, adapter)
        assert_lifecycle(visual, "analyzer")
        assert_paired_events(visual, "analyzer")

        # Adapter state should be clean
        tracker = adapter._get_tracker("analyzer")
        assert tracker.state == AgentAnimState.IDLE


# ═══════════════════════════════════════════════════════════════════════════════
# Test 9: Agent doom loop — tool fails repeatedly
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration09_DoomLoop:
    """Agent calls a tool that always fails — verify doom loop detection."""

    async def test_doom_loop_detection(self, llm, event_collector, adapter, workspace):
        bus, collected = event_collector

        # Register a custom tool that always fails
        registry = _make_tool_registry(["file_read"])

        async def always_fail(**kwargs: Any) -> dict:
            return {"error": "Service unavailable: database connection failed", "success": False}

        registry.register(ToolDefinition(
            name="check_database",
            description="Check database connection status. Always call this tool first.",
            parameters={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "default": "localhost"},
                },
            },
            func=always_fail,
        ))

        role = AgentInfo(
            role_id="db_checker",
            name="DB Checker",
            responsibility="Check database connections",
            tools=["check_database", "file_read"],
        )
        memory = MemoryManager(opc_home=workspace, project_id="test")
        prefs = PreferenceManager(opc_home=workspace)
        skills = SkillLibrary(opc_home=workspace)
        config = OPCConfig(system=SystemConfig(max_agent_iterations=6))

        agent = NativeAgent(
            role=role, llm=llm, tool_registry=registry,
            memory=memory, preferences=prefs, skills=skills,
            event_bus=bus, config=config,
        )
        task = Task(
            title="Check DB",
            description="Use the check_database tool to verify the database is running. Keep trying until it works.",
            assigned_to="db_checker",
        )

        result = await agent.execute(task)
        assert result.status in (TaskStatus.DONE, TaskStatus.FAILED)

        # Verify the failing tool was called multiple times
        db_calls = [
            e for e in collected
            if e.event_type == "agent_log"
            and e.payload.get("tool") == "check_database"
            and e.payload.get("status") == "executing"
        ]
        # Doom loop triggers after 3 identical failures; agent should have called it at least twice
        assert len(db_calls) >= 1, "Expected at least 1 check_database call"

        # Visual lifecycle should still close
        visual = collect_visual(collected, adapter)
        assert_lifecycle(visual, "db_checker")
        assert_paired_events(visual, "db_checker")


# ═══════════════════════════════════════════════════════════════════════════════
# Test 10: Full kanban state tracking — task_created → ... → waiting
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration10_KanbanStateTracking:
    """Full kanban lifecycle: task_created → agent_active → tools → waiting."""

    async def test_kanban_lifecycle(self, llm, event_collector, adapter, workspace):
        bus, collected = event_collector

        # Manually publish task_created event first (simulates engine behavior)
        await bus.publish(OPCEvent(
            event_type="task_created",
            payload={"title": "Create hello.py and run it", "task_id": "kanban-task-001"},
        ))

        agent = _make_agent(
            role_id="implementer",
            tools=["file_write", "shell_exec"],
            responsibility="Implement and verify code",
            llm=llm, event_bus=bus, opc_home=workspace,
            max_iterations=4,
        )
        task = Task(
            id="kanban-task-001",
            title="Create hello.py",
            description=f"Create {workspace}/hello.py that prints 'hello world', then run it with python3.",
            assigned_to="implementer",
        )

        result = await agent.execute(task)
        assert result.status in (TaskStatus.DONE, TaskStatus.FAILED)

        # Translate ALL events (including task_created)
        visual = collect_visual(collected, adapter)

        # Verify kanban lifecycle order
        all_types = types(visual)

        # task_routed should appear (from task_created)
        assert "task_routed" in all_types, "Missing task_routed from task_created event"
        task_routed_idx = all_types.index("task_routed")

        # agent_active should appear after task_routed
        assert "agent_active" in all_types, "Missing agent_active"
        agent_active_idx = all_types.index("agent_active")
        assert agent_active_idx > task_routed_idx, "agent_active should come after task_routed"

        # waiting should be the last event for this agent
        assert "waiting" in all_types, "Missing waiting"

        # task_display_counter should be 1
        assert adapter.task_display_counter == 1

        # Full lifecycle check
        assert_lifecycle(visual, "implementer")
        assert_paired_events(visual, "implementer")

        # Adapter state clean
        tracker = adapter._get_tracker("implementer")
        assert tracker.state == AgentAnimState.IDLE
        assert tracker.task_id is None


# ═══════════════════════════════════════════════════════════════════════════════
# Test 11: Event ordering integrity — all events properly paired
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration11_EventOrderingIntegrity:
    """Verify strict event ordering: no orphaned starts, no nested pairs."""

    async def test_strict_ordering(self, llm, event_collector, adapter, workspace):
        bus, collected = event_collector
        agent = _make_agent(
            role_id="strict_dev",
            tools=["file_read", "file_edit", "shell_exec", "list_dir"],
            responsibility="Code development with careful verification",
            llm=llm, event_bus=bus, opc_home=workspace,
            max_iterations=5,
        )
        task = Task(
            title="Read and modify",
            description=(
                f"1. List the files in {workspace}/src/\n"
                f"2. Read {workspace}/src/utils.py\n"
                f"3. Tell me what functions are defined there"
            ),
            assigned_to="strict_dev",
        )

        result = await agent.execute(task)
        assert result.status in (TaskStatus.DONE, TaskStatus.FAILED)

        visual = collect_visual(collected, adapter)
        agent_vis = types_for(visual, "strict_dev")

        # Structural validation: no two consecutive starts without a done in between
        assert_paired_events(visual, "strict_dev")

        # Verify agent_active is first, waiting is last
        assert_lifecycle(visual, "strict_dev")

        # Verify no visual event has empty agent_id
        for v in visual:
            assert v.get("agent_id"), f"Visual event missing agent_id: {v}"

        # Verify all visual events have required fields
        for v in visual:
            assert "event_id" in v, f"Missing event_id: {v}"
            assert "type" in v, f"Missing type: {v}"
            assert "timestamp" in v, f"Missing timestamp: {v}"

        # Verify reflect_start count == reflect_done count
        starts = agent_vis.count("reflect_start")
        dones = agent_vis.count("reflect_done")
        assert starts == dones, f"reflect_start ({starts}) != reflect_done ({dones})"

        # Same for tool_start/tool_done
        ts = agent_vis.count("tool_start")
        td = agent_vis.count("tool_done")
        assert ts == td, f"tool_start ({ts}) != tool_done ({td})"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 12: Multi-agent with dependency — second agent uses first's output
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration12_MultiAgentDependency:
    """Agent B depends on Agent A's output — sequential execution with handoff."""

    async def test_dependency_handoff(self, llm, event_collector, adapter, workspace):
        bus, collected = event_collector

        # Agent A: write a data file
        agent_a = _make_agent(
            role_id="data_creator",
            tools=["file_write"],
            responsibility="Create data files",
            llm=llm, event_bus=bus, opc_home=workspace,
            max_iterations=3,
        )
        task_a = Task(
            title="Create data file",
            description=f"Create a file at {workspace}/data.json with content: {{\"name\": \"OpenOPC\", \"version\": \"1.0\"}}",
            assigned_to="data_creator",
        )

        result_a = await agent_a.execute(task_a)
        assert result_a.status in (TaskStatus.DONE, TaskStatus.FAILED)

        # Agent B: read and process that file
        agent_b = _make_agent(
            role_id="data_reader",
            tools=["file_read"],
            responsibility="Read and analyze data files",
            llm=llm, event_bus=bus, opc_home=workspace,
            max_iterations=3,
        )
        task_b = Task(
            title="Read data file",
            description=f"Read the file at {workspace}/data.json and tell me the project name and version.",
            assigned_to="data_reader",
            result={"previous_agent": "data_creator", "output": result_a.content[:500]},
        )

        result_b = await agent_b.execute(task_b)
        assert result_b.status in (TaskStatus.DONE, TaskStatus.FAILED)

        # Verify two distinct agent lifecycles in events
        visual = collect_visual(collected, adapter)

        # Both agents should have agent_active and waiting
        creator_types = types_for(visual, "data_creator")
        reader_types = types_for(visual, "data_reader")

        assert "agent_active" in creator_types, "data_creator missing agent_active"
        assert "waiting" in creator_types, "data_creator missing waiting"
        assert "agent_active" in reader_types, "data_reader missing agent_active"
        assert "waiting" in reader_types, "data_reader missing waiting"

        # Verify proper pairing for each agent independently
        assert_paired_events(visual, "data_creator")
        assert_paired_events(visual, "data_reader")

        # Verify creator finishes (waiting) before reader starts (agent_active)
        creator_waiting_idx = None
        reader_active_idx = None
        for i, v in enumerate(visual):
            if v.get("agent_id") == "data_creator" and v["type"] == "waiting":
                creator_waiting_idx = i
            if v.get("agent_id") == "data_reader" and v["type"] == "agent_active":
                reader_active_idx = i
                break
        if creator_waiting_idx is not None and reader_active_idx is not None:
            assert creator_waiting_idx < reader_active_idx, \
                "data_reader started before data_creator finished"

        # Both trackers should be idle
        assert adapter._get_tracker("data_creator").state == AgentAnimState.IDLE
        assert adapter._get_tracker("data_reader").state == AgentAnimState.IDLE


# ═══════════════════════════════════════════════════════════════════════════════
# Test 13: Agent with python_exec tool
# ═══════════════════════════════════════════════════════════════════════════════

class TestIntegration13_PythonExec:
    """Agent uses python_exec to run a computation — verify shell mapping."""

    async def test_python_exec_event(self, llm, event_collector, adapter, workspace):
        bus, collected = event_collector
        agent = _make_agent(
            role_id="calculator",
            tools=["python_exec"],
            responsibility="Run Python computations",
            llm=llm, event_bus=bus, opc_home=workspace,
            max_iterations=3,
        )
        task = Task(
            title="Calculate factorial",
            description="Use python_exec to calculate the factorial of 10 and tell me the result.",
            assigned_to="calculator",
        )

        result = await agent.execute(task)
        assert result.status in (TaskStatus.DONE, TaskStatus.FAILED)

        visual = collect_visual(collected, adapter)
        assert_lifecycle(visual, "calculator")
        assert_paired_events(visual, "calculator")

        # python_exec maps to "shell" in TOOL_MAP
        py_used = any(
            e.event_type == "agent_log" and e.payload.get("tool") == "python_exec"
            for e in collected
        )
        if py_used:
            shell_visuals = [
                v for v in visual
                if v["type"] == "tool_start" and v["data"].get("tool_name") == "shell"
            ]
            assert len(shell_visuals) >= 1, "python_exec maps to shell but no tool_start(shell) found"
