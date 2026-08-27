from __future__ import annotations

import contextlib
import json
import shutil
import unittest
import uuid
from pathlib import Path

from opc.core.config import OPCConfig
from opc.core.models import Task, TaskStatus
from opc.database.store import OPCStore
from opc.layer3_agent.runtime_v2.runtime import NativeRuntimeV2
from opc.layer4_tools.registry import ToolRegistry
from opc.layer5_memory.memory_manager import MemoryManager


@contextlib.contextmanager
def _workspace_tempdir() -> Path:
    base = Path.cwd() / ".tmp-test" / f"durable-memory-{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


class _StubExtractionLLM:
    def __init__(self) -> None:
        self.config = type("Cfg", (), {"max_tokens": 4096})()

    def prepare_user_message_content(self, content: str, attachment_refs=None):
        _ = attachment_refs
        return content

    def get_tool_definitions(self, tools):
        return tools

    def is_context_overflow_error(self, error: Exception) -> bool:
        _ = error
        return False

    async def chat_stream(self, messages, tools=None):
        _ = (messages, tools)
        yield type("Evt", (), {"event_type": "message_start", "payload": {}, "model": "stub"})()
        yield type("Evt", (), {"event_type": "assistant_delta", "payload": {"text": "Repository convention captured."}, "model": "stub"})()
        yield type("Evt", (), {"event_type": "message_stop", "payload": {"finish_reason": "stop"}, "model": "stub"})()

    async def simple_chat(self, prompt: str, system: str | None = None, task_type: str | None = None) -> str:
        _ = (prompt, task_type)
        if system and "extracting durable reusable memory" in system:
            return json.dumps(
                {
                    "project_memory": [
                        {
                            "title": "Search Conventions",
                            "content": "- Prefer rg for repo-wide search before editing files.",
                        }
                    ],
                    "global_memory": [],
                    "reason": "Stable repo runtime preference.",
                },
                ensure_ascii=False,
            )
        raise AssertionError(f"Unexpected system prompt: {system}")


class RuntimeDurableMemoryExtractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_does_not_auto_extract_durable_memory_on_completed_turn(self) -> None:
        with _workspace_tempdir() as root:
            store = OPCStore(root / "tasks.db")
            await store.initialize()
            memory = MemoryManager(root, "proj1", store=store)
            config = OPCConfig()
            config.system.native_runtime.durable_memory_extract_min_messages = 2
            runtime = NativeRuntimeV2(
                llm=_StubExtractionLLM(),
                tool_registry=ToolRegistry(),
                memory_manager=memory,
                config=config,
            )

            task = Task(
                id="task-durable",
                title="Capture convention",
                description="Inspect the repository and remember reusable conventions.",
                session_id="sess-durable",
                project_id="proj1",
                assigned_to="executor",
            )
            # NativeRuntimeV2 refreshes the Store-owned ToolCall ledger before
            # each model boundary.  Production tasks are always persisted by
            # the Engine before runtime admission; keep this focused fixture on
            # the same durable contract.
            await store.save_task(task)

            result = await runtime.run(
                system_prompt="You are a test runtime.",
                user_message="Please remember that we prefer rg for search.",
                task=task,
            )

            self.assertEqual(result.status, TaskStatus.DONE)
            self.assertNotIn("durable_memory_extraction", result.artifacts)
            project_memory = memory.load_project_memory_markdown("proj1")
            self.assertEqual(project_memory, "")
            session = await store.get_session("sess-durable")
            self.assertFalse(str(session.metadata.get("durable_memory_cursor_message_id", "")).strip())
            await store.close()


if __name__ == "__main__":
    unittest.main()
