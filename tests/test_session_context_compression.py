from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from opc.core.models import AgentMemorySnapshotRecord, SessionMemorySnapshotRecord, Task, TaskStatus
from opc.database.store import OPCStore
from opc.layer1_perception.context_assembler import ContextAssembler
from opc.layer5_memory.history_compactor import HistoryCompactor
from opc.layer5_memory.memory_manager import MemoryManager


class _StubLLM:
    class _Config:
        max_tokens = 1024

    config = _Config()

    def get_context_window(self, task_type: str | None = None, model: str | None = None) -> int:
        _ = (task_type, model)
        return 100

    def count_input_tokens(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, object]] | None = None,
        task_type: str | None = None,
        model: str | None = None,
    ) -> int:
        _ = (tools, task_type, model)
        total = 0
        for message in messages:
            total += max(1, len(str(message.get("content", "")).strip()) // 20)
        return total

    async def simple_chat(self, prompt: str, system: str | None = None, task_type: str | None = None) -> str:
        _ = (prompt, task_type)
        if system and "persisted session history" in system:
            return json.dumps(
                {
                    "history_summary": "Session history summary before restart.",
                    "memory_summary": (
                        "## Primary Goal\n"
                        "- Continue the project.\n\n"
                        "## Active Rules\n"
                        "- Keep memory concise.\n\n"
                        "## Key Progress\n"
                        "- Session summary stored.\n\n"
                        "## Current State\n"
                        "- Waiting for the latest tail.\n\n"
                        "## Open Risks\n"
                        "- Re-check raw history if needed."
                    ),
                },
                ensure_ascii=False,
            )
        if system and "employee-level process history" in system:
            return json.dumps(
                {
                    "history_summary": "Agent history summary before restart.",
                    "memory_summary": (
                        "## Effective Patterns\n"
                        "- Leave concise handoffs.\n\n"
                        "## Watchouts\n"
                        "- Avoid duplicate edits.\n\n"
                        "## Current Progress\n"
                        "- Task completed.\n\n"
                        "## Current State\n"
                        "- Waiting for final reflection."
                    ),
                },
                ensure_ascii=False,
            )
        if system and "finalizing employee memory" in system:
            return json.dumps(
                {
                    "summary_text": "Final employee memory created.",
                    "memory_text": (
                        "## Effective Patterns\n"
                        "- Leave concise handoffs.\n\n"
                        "## Watchouts\n"
                        "- Avoid duplicate edits.\n\n"
                        "## Preferred Tools\n"
                        "- Prefer targeted validation.\n\n"
                        "## Reviewer Preferences\n"
                        "- Surface exact artifacts.\n\n"
                        "## Reusable Checklist\n"
                        "- Include validation evidence."
                    ),
                    "metadata": {
                        "effective_patterns": ["Leave concise handoffs."],
                        "watchouts": ["Avoid duplicate edits."],
                        "preferred_tools": ["Prefer targeted validation."],
                        "reviewer_preferences": ["Surface exact artifacts."],
                        "reusable_checklist": ["Include validation evidence."],
                    },
                },
                ensure_ascii=False,
            )
        raise AssertionError(f"Unexpected prompt type: {system}")


class SessionContextCompressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_session_prompt_context_returns_full_visible_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            memory = MemoryManager(root, "proj1", store=store)
            session_id = "session-1"

            for idx in range(6):
                role = "user" if idx % 2 == 0 else "assistant"
                payload = (
                    f"message {idx} " +
                    "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
                )
                await memory.append_session_message(session_id=session_id, role=role, text=payload)

            context = await memory.build_session_prompt_context(session_id)
            history_messages = await memory.build_session_history_messages(session_id)

            self.assertIn("Current Session History", context)
            self.assertIn("message 0", context)
            self.assertIn("message 5", context)
            self.assertEqual(len(history_messages), 6)
            self.assertEqual(history_messages[0]["role"], "user")
            self.assertIn("message 0", history_messages[0]["content"])
            self.assertEqual(history_messages[-1]["role"], "assistant")
            self.assertIn("message 5", history_messages[-1]["content"])

    async def test_build_memory_context_includes_global_project_and_session_layers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            memory = MemoryManager(root, "proj1", store=store)
            session_id = "session-layers"

            memory.save_memory("# Global rules\n- Use Chinese when appropriate.\n", project=False)
            memory.save_memory("# Project rules\n- Follow proj1 conventions.\n", project=True)
            await store.save_session_memory_snapshot(
                SessionMemorySnapshotRecord(
                    project_id="proj1",
                    session_id=session_id,
                    summary_message_id="summary-1",
                    source_boundary_message_id="msg-1",
                    summary_text="session summary",
                    memory_text="## Primary Goal\n- Finish the current ticket.",
                )
            )

            context = await memory.build_memory_context(project_id="proj1", session_id=session_id)

            self.assertIn("## Global Memory", context)
            self.assertIn("Use Chinese when appropriate", context)
            self.assertIn("## Project Memory (proj1)", context)
            self.assertIn("Follow proj1 conventions", context)
            self.assertIn("## Session Memory", context)
            self.assertIn("Finish the current ticket", context)

    async def test_memory_manager_preserves_long_content_without_numeric_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            memory = MemoryManager(root, "proj1", store=store)
            session_id = "session-long"

            msg = await memory.append_session_message(
                session_id=session_id,
                role="assistant",
                text="",
                project_id="proj1",
            )
            assert msg is not None
            long_output = "very-long-output " * 300
            await memory.append_session_part(
                session_id,
                msg.message_id,
                "tool_output",
                {"tool_name": "test_tool", "output": long_output},
            )

            context = await memory.build_session_prompt_context(session_id)
            history_messages = await memory.build_session_history_messages(session_id)
            compression_prompt = memory.get_compression_prompt(
                [{"role": "assistant", "content": long_output}],
                existing_memory="existing",
            )

            self.assertIn(long_output.strip(), context)
            self.assertIn(long_output.strip(), history_messages[0]["content"])
            self.assertIn(long_output.strip(), compression_prompt)

    async def test_restart_uses_summary_and_tail_after_compaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "tasks.db"
            store = OPCStore(db_path)
            await store.initialize()
            memory = MemoryManager(root, "proj1", store=store)
            compactor = HistoryCompactor(
                llm=_StubLLM(),
                store=store,
                memory_manager=memory,
                compression_threshold=0.85,
            )
            memory.set_history_compactor(compactor)
            session_id = "session-restart"

            for idx in range(6):
                role = "user" if idx % 2 == 0 else "assistant"
                payload = f"message {idx} " + ("alpha beta gamma delta " * 40)
                await memory.append_session_message(session_id=session_id, role=role, text=payload)

            # Append no longer auto-compacts; compaction is an explicit call.
            # Forced compaction consumes everything up to the latest message,
            # so the raw tail comes from messages appended afterwards.
            compacted = await compactor.maybe_compact_session(
                project_id="proj1",
                session_id=session_id,
                force=True,
            )
            self.assertTrue(compacted)
            for idx in range(6, 8):
                role = "user" if idx % 2 == 0 else "assistant"
                payload = f"message {idx} " + ("alpha beta gamma delta " * 40)
                await memory.append_session_message(session_id=session_id, role=role, text=payload)

            snapshot = await store.get_latest_session_memory_snapshot(session_id)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertIn("## Primary Goal", snapshot.memory_text)

            context = await memory.build_session_prompt_context(session_id)
            self.assertIn("## Session Memory", context)
            self.assertIn("Session history summary before restart", context)
            self.assertIn("message 7", context)

            await store.close()

            restarted_store = OPCStore(db_path)
            await restarted_store.initialize()
            restarted_memory = MemoryManager(root, "proj1", store=restarted_store)
            restarted_history = await restarted_memory.build_session_history_messages(session_id)
            restarted_context = await restarted_memory.build_session_prompt_context(session_id)

            self.assertTrue(any("Session history summary before restart" in item["content"] for item in restarted_history))
            self.assertTrue(any("message 7" in item["content"] for item in restarted_history))
            self.assertFalse(any("message 0" in item["content"] for item in restarted_history))
            self.assertNotIn("message 0", restarted_context)

    async def test_employee_history_tail_isolated_by_employee_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            memory = MemoryManager(root, "proj1", store=store)
            session_id = "shared-session"

            await memory.ensure_session(session_id, project_id="proj1", metadata={"role_id": "developer"})
            await memory.record_user_turn(
                session_id,
                "employee a asks a question",
                project_id="proj1",
                metadata={"employee_id": "emp-a", "role_id": "developer"},
            )
            await memory.record_assistant_turn(
                session_id,
                "employee a gets an answer",
                project_id="proj1",
                metadata={"employee_id": "emp-a", "role_id": "developer"},
            )
            await memory.record_user_turn(
                session_id,
                "employee b asks a different question",
                project_id="proj1",
                metadata={"employee_id": "emp-b", "role_id": "developer"},
            )

            history_a = await memory.build_employee_history_tail_messages(
                project_id="proj1",
                session_id=session_id,
                employee_id="emp-a",
            )
            history_b = await memory.build_employee_history_tail_messages(
                project_id="proj1",
                session_id=session_id,
                employee_id="emp-b",
            )

            self.assertEqual(len(history_a), 2)
            self.assertEqual(len(history_b), 1)
            self.assertTrue(all("employee a" in item["content"] for item in history_a))
            self.assertTrue(all("employee b" in item["content"] for item in history_b))

    async def test_final_agent_memory_replaces_process_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            memory = MemoryManager(root, "proj1", store=store)
            memory.set_history_compactor(
                HistoryCompactor(
                    llm=_StubLLM(),
                    store=store,
                    memory_manager=memory,
                    compression_threshold=0.85,
                )
            )

            await memory.ensure_session("session-a", project_id="proj1", metadata={"employee_id": "emp-a", "role_id": "developer"})
            await memory.ensure_session("session-b", project_id="proj1", metadata={"employee_id": "emp-b", "role_id": "developer"})

            await store.save_agent_memory_snapshot(
                AgentMemorySnapshotRecord(
                    project_id="proj1",
                    session_id="session-a",
                    employee_id="emp-a",
                    role_id="developer",
                    memory_kind="process",
                    summary_message_id="summary-a",
                    source_boundary_message_id="boundary-a",
                    summary_text="process summary a",
                    memory_text="## Effective Patterns\n- Temporary pattern A",
                )
            )
            await store.save_agent_memory_snapshot(
                AgentMemorySnapshotRecord(
                    project_id="proj1",
                    session_id="session-b",
                    employee_id="emp-b",
                    role_id="developer",
                    memory_kind="process",
                    summary_message_id="summary-b",
                    source_boundary_message_id="boundary-b",
                    summary_text="process summary b",
                    memory_text="## Effective Patterns\n- Temporary pattern B",
                )
            )

            task_a = Task(
                id="task-a",
                title="Implement feature A",
                project_id="proj1",
                session_id="session-a",
                assigned_to="developer",
                status=TaskStatus.DONE,
                result={"content": "done A"},
                metadata={
                    "employee_assignment": {
                        "employee_id": "emp-a",
                        "role_id": "developer",
                        "name": "Employee A",
                        "domains": ["coding"],
                    },
                    "work_item_projection_id": "projection-a",
                    "artifacts": ["file: a.py"],
                    "decisions": ["Use API A"],
                },
            )
            task_b = Task(
                id="task-b",
                title="Implement feature B",
                project_id="proj1",
                session_id="session-b",
                assigned_to="developer",
                status=TaskStatus.DONE,
                result={"content": "done B"},
                metadata={
                    "employee_assignment": {
                        "employee_id": "emp-b",
                        "role_id": "developer",
                        "name": "Employee B",
                        "domains": ["coding"],
                    },
                    "work_item_projection_id": "projection-b",
                    "artifacts": ["file: b.py"],
                    "decisions": ["Use API B"],
                },
            )
            delivery_task = Task(
                id="delivery-1",
                title="Project delivery",
                project_id="proj1",
                status=TaskStatus.DONE,
                metadata={"work_item_projection_id": "delivery"},
            )

            await memory._record_project_reflections_and_finalize(
                delivery_task,
                [task_a, task_b],
                partial=False,
            )

            final_a = await store.get_agent_memory_snapshot(
                project_id="proj1",
                employee_id="emp-a",
                memory_kind="final",
                memory_scope="project",
            )
            final_b = await store.get_agent_memory_snapshot(
                project_id="proj1",
                employee_id="emp-b",
                memory_kind="final",
                memory_scope="project",
            )
            process_a = await store.get_agent_memory_snapshot(
                project_id="proj1",
                session_id="session-a",
                employee_id="emp-a",
                memory_kind="process",
                memory_scope="session",
            )

            self.assertIsNotNone(final_a)
            self.assertIsNotNone(final_b)
            self.assertIsNone(process_a)
            assert final_a is not None
            self.assertEqual(final_a.memory_scope, "project")
            self.assertEqual(final_a.session_id, "")
            self.assertIn("## Effective Patterns", final_a.memory_text)

            context_a = await memory.build_employee_memory_context(
                project_id="proj1",
                session_id="session-a",
                employee_id="emp-a",
                role_id="developer",
            )
            context_b = await memory.build_employee_memory_context(
                project_id="proj1",
                session_id="session-b",
                employee_id="emp-b",
                role_id="developer",
            )
            self.assertIn("Employee Project Memory", context_a)
            self.assertIn("emp-a", context_a)
            self.assertNotIn("emp-b", context_a)
            self.assertIn("emp-b", context_b)

    async def test_durable_compactor_persists_session_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            memory = MemoryManager(root, "proj1", store=store)
            compactor = HistoryCompactor(
                llm=_StubLLM(),
                store=store,
                memory_manager=memory,
                compression_threshold=0.85,
            )
            memory.set_history_compactor(compactor)

            session_id = "session-loop"
            await memory.ensure_session(
                session_id,
                project_id="proj1",
                metadata={"employee_id": "emp-loop", "role_id": "developer"},
            )
            for idx in range(8):
                role = "user" if idx % 2 == 0 else "assistant"
                payload = (
                    f"loop message {idx} "
                    + "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu"
                )
                await memory.append_session_message(
                    session_id=session_id,
                    role=role,
                    text=payload,
                    project_id="proj1",
                    metadata={"employee_id": "emp-loop", "role_id": "developer"},
                )

            compacted = await compactor.maybe_compact_session(
                project_id="proj1",
                session_id=session_id,
                force=True,
            )

            session_snapshot = await store.get_latest_session_memory_snapshot(session_id)
            rebuilt = await memory.build_session_history_tail_messages(session_id)
            self.assertTrue(compacted)
            self.assertIsNotNone(session_snapshot)
            assert session_snapshot is not None
            self.assertIn("Session history summary before restart", session_snapshot.summary_text)
            self.assertTrue(any("Session history summary before restart" in str(item.get("content", "")) for item in rebuilt))

    async def test_legacy_session_final_memory_migrates_to_project_scope_on_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            memory = MemoryManager(root, "proj1", store=store)

            await store.save_agent_memory_snapshot(
                AgentMemorySnapshotRecord(
                    project_id="proj1",
                    session_id="legacy-session",
                    employee_id="emp-legacy",
                    role_id="developer",
                    memory_scope="session",
                    memory_kind="final",
                    summary_message_id="legacy-summary",
                    source_boundary_message_id="legacy-boundary",
                    summary_text="legacy final summary",
                    memory_text="## Effective Patterns\n- Legacy final memory",
                )
            )

            context = await memory.build_employee_memory_context(
                project_id="proj1",
                session_id=None,
                employee_id="emp-legacy",
                role_id="developer",
            )
            migrated = await store.get_agent_memory_snapshot(
                project_id="proj1",
                employee_id="emp-legacy",
                memory_kind="final",
                memory_scope="project",
            )

            self.assertIn("Employee Project Memory", context)
            self.assertIsNotNone(migrated)
            assert migrated is not None
            self.assertEqual(migrated.metadata.get("migrated_from_session_id"), "legacy-session")

    async def test_focused_memory_context_prefers_relevant_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            memory = MemoryManager(root, "proj1", store=store)
            memory.save_memory(
                "# Global\n\n"
                "## Rust Build\n"
                "- Use cargo build for Rust projects.\n\n"
                "## Database Migrations\n"
                "- Always use migrations for schema changes.\n",
                project=False,
            )
            memory.save_memory(
                "# Project\n\n"
                "## Search Tooling\n"
                "- Prefer rg for code search.\n\n"
                "## Browser Checks\n"
                "- Use Playwright for browser verification.\n",
                project=True,
            )
            session_id = "session-focus"
            await memory.ensure_session(session_id, project_id="proj1")
            await memory.update_session_summary(session_id, "Remember the current runtime summary.")

            focused = await memory.build_focused_memory_context(
                query="improve code search with rg",
                project_id="proj1",
                session_id=session_id,
            )
            self.assertIn("Focused Project Memory", focused)
            self.assertIn("Search Tooling", focused)
            self.assertNotIn("Browser Checks", focused)
            self.assertIn("## Session Memory", focused)

            assembler = ContextAssembler(memory)
            core = await assembler.build_core_context(
                Task(
                    title="Search fix",
                    description="Use rg to inspect the repository",
                    project_id="proj1",
                    session_id=session_id,
                )
            )
            self.assertIn("Search Tooling", core)
            self.assertNotIn("Browser Checks", core)


if __name__ == "__main__":
    unittest.main()
