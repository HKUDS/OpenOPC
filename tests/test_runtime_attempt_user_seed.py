from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opc.core.company_controller import CompanyRunControllerLeaseLost
from opc.core.config import OPCConfig
from opc.core.models import (
    DelegationRun,
    DelegationWorkItem,
    Phase,
    SessionRecord,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer1_perception.context_assembler import ContextAssembler
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer3_agent.native_agent import NativeAgent
from opc.layer3_agent.runtime_v2.runtime import NativeRuntimeV2
from opc.layer4_tools.registry import ToolDefinition, ToolRegistry
from opc.layer5_memory.memory_manager import MemoryManager
from opc.llm.provider import LLMProvider


class _ResumeLLM:
    def sanitize_tool_call_history(self, messages):
        return LLMProvider.sanitize_tool_call_history(messages)


class _CaptureLLM(_ResumeLLM):
    def __init__(self) -> None:
        self.config = SimpleNamespace(max_tokens=2048)
        self.message_batches: list[list[dict[str, object]]] = []
        self.tool_batches: list[list[dict[str, object]]] = []

    def prepare_user_message_content(self, content: str, attachment_refs=None):
        _ = attachment_refs
        return content

    def get_tool_definitions(self, tools):
        return list(tools)

    def is_context_overflow_error(self, error: Exception) -> bool:
        _ = error
        return False

    def is_tool_protocol_error(self, error: Exception) -> bool:
        _ = error
        return False

    async def chat_stream(self, messages, tools=None):
        self.message_batches.append([dict(item) for item in messages])
        self.tool_batches.append(list(tools or []))
        yield SimpleNamespace(event_type="message_start", payload={}, model="stub")
        yield SimpleNamespace(
            event_type="assistant_delta",
            payload={"text": "Correction applied."},
            model="stub",
        )
        yield SimpleNamespace(
            event_type="message_stop",
            payload={"finish_reason": "stop"},
            model="stub",
        )


class _CorrectionAssembler:
    def build_task_brief(self, task: Task) -> str:
        return f"ORIGINAL ASSIGNMENT: {task.title}"

    async def build_rework_feedback_context(
        self,
        task: Task,
        *,
        include_previous_submission: bool = True,
    ) -> str:
        _ = task
        _ = include_previous_submission
        return (
            "## Deterministic Gate Feedback (Rework Required)\n"
            "- Blocker: report.md must use company_analysis.json exactly.\n"
            "- Required action: edit and read back report.md."
        )

    async def build_turn_mode_context(self, task: Task) -> str:
        _ = task
        return "## Turn Mode\nREWORK"


class RuntimeAttemptUserSeedTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = OPCStore(Path(self.tmp.name) / "tasks.db")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self.tmp.cleanup()

    async def _scalar(self, query: str, params: tuple[object, ...] = ()) -> int:
        assert self.store._db is not None
        async with self.store._db.execute(query, params) as cursor:
            row = await cursor.fetchone()
        return int(row[0] if row else 0)

    async def _seed_non_company_runtime(self) -> tuple[Task, NativeRuntimeV2]:
        task = Task(
            id="task-attempt-seed",
            session_id="session-attempt-seed",
            project_id="project-attempt-seed",
            title="Fix the final report",
            metadata={
                "claimed_work_item_attempt_seq": 2,
                "_runtime_v2_attempt_user_seed_required": True,
                "_runtime_v2_attempt_user_seed_revision": "revision-round-1",
            },
            context_snapshot={
                "runtime_resume": {"runtime_session_id": "runtime-attempt-seed"}
            },
        )
        set_linked_work_item_id(task, "work-item-attempt-seed")
        await self.store.save_task(task)
        await self.store.save_session(
            SessionRecord(
                session_id=str(task.session_id),
                project_id=task.project_id,
                title="Attempt seed test",
            )
        )
        runtime = NativeRuntimeV2(
            llm=_ResumeLLM(),
            tool_registry=ToolRegistry(),
            memory_manager=SimpleNamespace(store=self.store),
            config=OPCConfig(),
        )
        return task, runtime

    async def test_native_agent_builds_correction_only_attempt_message(self) -> None:
        task = Task(
            id="task-correction",
            session_id="session-correction",
            title="Deliver the investment report",
            metadata={
                "claimed_work_item_attempt_seq": 2,
                "gate_harness_rework_count": 1,
                "gate_harness_rework_request": {
                    "rework_round": 1,
                    "blockers": ["wrong child artifact name"],
                },
            },
        )
        set_linked_work_item_id(task, "work-item-correction")
        agent = SimpleNamespace(context_assembler=_CorrectionAssembler())

        message = await NativeAgent._build_user_message(agent, task)

        self.assertIn("Current WorkItem Attempt", message)
        self.assertIn("REWORK", message)
        self.assertIn("company_analysis.json", message)
        self.assertIn("edit and read back report.md", message)
        self.assertIn("work-item-correction", message)
        self.assertIn("Attempt sequence: `2`", message)
        self.assertIn("rework round: `1`", message)
        self.assertNotIn("ORIGINAL ASSIGNMENT", message)
        self.assertTrue(task.metadata["_runtime_v2_attempt_user_seed_required"])
        self.assertEqual(
            len(task.metadata["_runtime_v2_attempt_user_seed_revision"]),
            64,
        )

    async def test_attempt_seed_is_atomic_idempotent_and_round_scoped(self) -> None:
        task, runtime = await self._seed_non_company_runtime()
        round_one = "ROUND 1: edit report.md and use company_analysis.json."

        first = await runtime._ensure_attempt_user_turn_seed(
            task,
            round_one,
            runtime_session_id="runtime-attempt-seed",
            conversation_turn_id="turn-round-1",
        )
        retry = await runtime._ensure_attempt_user_turn_seed(
            task,
            round_one,
            runtime_session_id="runtime-attempt-seed",
            conversation_turn_id="turn-restarted-but-same-attempt",
        )

        self.assertTrue(first)
        self.assertTrue(retry)
        for table in (
            "runtime_user_turn_seeds",
            "session_messages",
            "session_parts",
            "runtime_transcript_entries",
        ):
            self.assertEqual(await self._scalar(f"SELECT COUNT(*) FROM {table}"), 1)

        task.metadata["_runtime_v2_attempt_user_seed_revision"] = (
            "same-attempt-but-drifted-revision"
        )
        with self.assertRaisesRegex(RuntimeError, "identity/content invariant"):
            await runtime._ensure_attempt_user_turn_seed(
                task,
                "DRIFTED CONTENT IN THE SAME ATTEMPT",
                runtime_session_id="runtime-attempt-seed",
                conversation_turn_id="turn-round-1-drift",
            )
        for table in (
            "runtime_user_turn_seeds",
            "session_messages",
            "session_parts",
            "runtime_transcript_entries",
        ):
            self.assertEqual(await self._scalar(f"SELECT COUNT(*) FROM {table}"), 1)

        restored, prefix_len = await runtime._bootstrap_messages(
            system_prompt="system",
            user_content=round_one,
            user_message=round_one,
            context_messages=[{"role": "system", "content": "dynamic"}],
            task=task,
            suppress_resume_user_append=True,
        )
        self.assertEqual(prefix_len, 2)
        self.assertEqual(restored[-1], {"role": "user", "content": round_one})
        self.assertEqual(
            sum(1 for item in restored if item.get("role") == "user"),
            1,
        )

        task.metadata["claimed_work_item_attempt_seq"] = 3
        task.metadata["_runtime_v2_attempt_user_seed_revision"] = "revision-round-2"
        round_two = "ROUND 2: replace risk_analysis with risk_analysis.json."
        await runtime._ensure_attempt_user_turn_seed(
            task,
            round_two,
            runtime_session_id="runtime-attempt-seed",
            conversation_turn_id="turn-round-2",
        )
        restarted_runtime = NativeRuntimeV2(
            llm=_ResumeLLM(),
            tool_registry=ToolRegistry(),
            memory_manager=SimpleNamespace(store=self.store),
            config=OPCConfig(),
        )
        await restarted_runtime._ensure_attempt_user_turn_seed(
            task,
            round_two,
            runtime_session_id="runtime-attempt-seed",
            conversation_turn_id="turn-round-2-after-process-restart",
        )

        self.assertEqual(await self._scalar("SELECT COUNT(*) FROM runtime_user_turn_seeds"), 2)
        self.assertEqual(await self._scalar("SELECT COUNT(*) FROM session_messages"), 2)
        restored_round_two, _ = await restarted_runtime._bootstrap_messages(
            system_prompt="system",
            user_content=round_two,
            user_message=round_two,
            context_messages=[],
            task=task,
            suppress_resume_user_append=True,
        )
        self.assertEqual(restored_round_two[-1]["content"], round_two)
        self.assertEqual(
            [
                item["content"]
                for item in restored_round_two
                if item.get("role") == "user"
            ],
            [round_one, round_two],
        )

    async def test_provider_retry_rebuilds_byte_identical_attempt_correction(self) -> None:
        task = Task(
            id="task-provider-retry-correction",
            session_id="session-provider-retry-correction",
            project_id="project-attempt-seed",
            title="Correct a deterministic validation failure",
            result={"content": "Previous business submission summary"},
            metadata={
                "claimed_work_item_attempt_seq": 2,
                "gate_harness_rework_count": 1,
                "gate_harness_rework_request": {
                    "rework_round": 1,
                    "feedback": "Fix the unsupported URL in report.md.",
                    "blockers": ["unsupported evidence URL: 'https://'"],
                },
            },
        )
        set_linked_work_item_id(task, "work-item-provider-retry-correction")
        memory = AsyncMock()
        assembler = ContextAssembler(memory=memory, store=self.store)
        agent = SimpleNamespace(context_assembler=assembler)

        original = await NativeAgent._build_user_message(agent, task)
        original_revision = task.metadata[
            "_runtime_v2_attempt_user_seed_revision"
        ]
        # This is the run27 failure shape: Engine records the provider error as
        # Task.result and a rebuilt feedback block gains a mutable excerpt while
        # the durable WorkItem attempt remains unchanged.
        task.result = {
            "content": "LLM stream failed: provider request id changed on retry"
        }
        task.retry_count = 1
        rebuilt = await NativeAgent._build_user_message(agent, task)

        self.assertEqual(rebuilt, original)
        self.assertEqual(
            task.metadata["_runtime_v2_attempt_user_seed_revision"],
            original_revision,
        )
        self.assertNotIn("provider request id changed", rebuilt)
        self.assertNotIn("Previous business submission summary", rebuilt)

    async def test_engine_provider_failure_retry_keeps_one_canonical_user_seed(
        self,
    ) -> None:
        task, runtime = await self._seed_non_company_runtime()
        correction = "Stable correction across the Engine provider retry boundary."
        await runtime._ensure_attempt_user_turn_seed(
            task,
            correction,
            runtime_session_id="runtime-attempt-seed",
            conversation_turn_id="turn-engine-first-attempt",
        )
        engine = OPCEngine()
        engine.store = self.store
        engine.memory = None
        attempts = 0
        seen_results: list[object] = []

        async def _run_task_once(retry_task: Task) -> TaskResult:
            nonlocal attempts
            attempts += 1
            seen_results.append(deepcopy(retry_task.result))
            retry_task.metadata["_runtime_v2_attempt_user_seed_revision"] = (
                "revision-round-1"
            )
            receipt = await runtime._ensure_attempt_user_turn_seed(
                retry_task,
                correction,
                runtime_session_id="runtime-attempt-seed",
                conversation_turn_id=f"turn-engine-attempt-{attempts}",
            )
            if attempts == 1:
                return TaskResult(
                    status=TaskStatus.FAILED,
                    content="LLM stream failed: transient provider request id",
                )
            self.assertFalse(receipt["created"])
            return TaskResult(status=TaskStatus.DONE, content="corrected")

        engine._run_task_once = _run_task_once  # type: ignore[method-assign]
        engine._attempt_capability_recovery = AsyncMock()  # type: ignore[method-assign]

        result = await engine._execute_registered_task_attempt(task)

        self.assertEqual(result.status, TaskStatus.DONE)
        self.assertEqual(attempts, 2)
        self.assertIsNone(seen_results[0])
        self.assertEqual(
            dict(seen_results[1] or {}).get("content"),
            "LLM stream failed: transient provider request id",
        )
        self.assertEqual(await self._scalar("SELECT COUNT(*) FROM runtime_user_turn_seeds"), 1)
        self.assertEqual(
            await self._scalar("SELECT COUNT(*) FROM session_messages WHERE role = 'user'"),
            1,
        )

    async def test_existing_seed_with_corrupted_atomic_graph_fails_closed(self) -> None:
        task, runtime = await self._seed_non_company_runtime()
        original = "Canonical correction whose graph must stay immutable."
        await runtime._ensure_attempt_user_turn_seed(
            task,
            original,
            runtime_session_id="runtime-attempt-seed",
            conversation_turn_id="turn-before-corruption",
        )
        assert self.store._db is not None
        await self.store._db.execute(
            "UPDATE runtime_transcript_entries SET content = 'tampered'"
        )
        await self.store._db.commit()

        with self.assertRaisesRegex(RuntimeError, "atomic graph invariant"):
            await runtime._ensure_attempt_user_turn_seed(
                task,
                "different proposed retry content",
                runtime_session_id="runtime-attempt-seed",
                conversation_turn_id="turn-after-corruption",
            )

    async def test_session_data_cleanup_deletes_complete_attempt_seed_graph(
        self,
    ) -> None:
        task, runtime = await self._seed_non_company_runtime()
        await runtime._ensure_attempt_user_turn_seed(
            task,
            "Correction that will be deleted with its session data.",
            runtime_session_id="runtime-attempt-seed",
            conversation_turn_id="turn-before-session-delete",
        )

        await self.store.delete_session_data(task.id, task.session_id)

        for table in (
            "runtime_user_turn_seeds",
            "session_messages",
            "session_parts",
            "runtime_transcript_entries",
            "runtime_sessions",
        ):
            self.assertEqual(await self._scalar(f"SELECT COUNT(*) FROM {table}"), 0)

    async def test_session_data_cleanup_rolls_back_complete_seed_graph_on_failure(
        self,
    ) -> None:
        task, runtime = await self._seed_non_company_runtime()
        await runtime._ensure_attempt_user_turn_seed(
            task,
            "Correction whose cleanup transaction will be interrupted.",
            runtime_session_id="runtime-attempt-seed",
            conversation_turn_id="turn-before-cleanup-failure",
        )
        assert self.store._db is not None
        original_execute = self.store._db.execute

        def _fail_after_part_delete(sql: str, parameters=()):
            if "DELETE FROM session_messages" in sql and "message_id" in sql:
                raise RuntimeError("injected cleanup failure")
            return original_execute(sql, parameters)

        self.store._db.execute = _fail_after_part_delete  # type: ignore[method-assign]
        try:
            with self.assertRaisesRegex(RuntimeError, "injected cleanup failure"):
                await self.store.delete_session_data(task.id, task.session_id)
        finally:
            self.store._db.execute = original_execute  # type: ignore[method-assign]

        for table in (
            "runtime_user_turn_seeds",
            "session_messages",
            "session_parts",
            "runtime_transcript_entries",
            "runtime_sessions",
        ):
            self.assertEqual(await self._scalar(f"SELECT COUNT(*) FROM {table}"), 1)

    async def test_shared_session_hard_delete_removes_only_deleted_task_seed_graph(
        self,
    ) -> None:
        task, runtime = await self._seed_non_company_runtime()
        await runtime._ensure_attempt_user_turn_seed(
            task,
            "Correction owned by the Task that will be hard-deleted.",
            runtime_session_id="runtime-attempt-seed",
            conversation_turn_id="turn-before-shared-hard-delete",
        )
        sibling = Task(
            id="task-shared-session-sibling",
            session_id=task.session_id,
            project_id=task.project_id,
            title="Keep this sibling Task and shared session",
        )
        await self.store.save_task(sibling)

        await self.store.hard_delete_task(task.id, task.session_id)

        self.assertIsNone(await self.store.get_task(task.id))
        self.assertIsNotNone(await self.store.get_task(sibling.id))
        self.assertIsNotNone(await self.store.get_session(str(task.session_id)))
        for table in (
            "runtime_user_turn_seeds",
            "session_messages",
            "session_parts",
            "runtime_transcript_entries",
            "runtime_sessions",
        ):
            self.assertEqual(await self._scalar(f"SELECT COUNT(*) FROM {table}"), 0)

    async def test_cross_project_purge_deletes_complete_attempt_seed_graph(
        self,
    ) -> None:
        scoped_db = (
            Path(self.tmp.name)
            / ".opc"
            / "projects"
            / "project-good"
            / "tasks.db"
        )
        scoped_db.parent.mkdir(parents=True, exist_ok=True)
        scoped_store = OPCStore(scoped_db)
        await scoped_store.initialize()
        try:
            assert scoped_store._db is not None
            now = datetime.now().isoformat()
            await scoped_store._db.execute(
                """INSERT INTO runtime_sessions
                (runtime_session_id, project_id, session_id, task_id, status,
                 metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'running', '{}', ?, ?)""",
                (
                    "runtime-cross-project",
                    "project-wrong",
                    "session-cross-project",
                    "task-cross-project",
                    now,
                    now,
                ),
            )
            await scoped_store._db.execute(
                """INSERT INTO runtime_user_turn_seeds
                (seed_key, runtime_session_id, task_id, session_id, project_id,
                 prompt_revision, content_hash, message_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "seed-cross-project",
                    "runtime-cross-project",
                    "task-cross-project",
                    "session-cross-project",
                    "project-wrong",
                    "revision-cross-project",
                    "hash-cross-project",
                    "message-cross-project",
                    now,
                ),
            )
            await scoped_store._db.execute(
                """INSERT INTO session_messages
                (message_id, session_id, role, task_id, metadata, created_at)
                VALUES (?, ?, 'user', ?, '{}', ?)""",
                (
                    "message-cross-project",
                    "session-cross-project",
                    "task-cross-project",
                    now,
                ),
            )
            await scoped_store._db.execute(
                """INSERT INTO session_parts
                (part_id, message_id, session_id, part_type, payload, created_at)
                VALUES (?, ?, ?, 'text', '{}', ?)""",
                (
                    "part-cross-project",
                    "message-cross-project",
                    "session-cross-project",
                    now,
                ),
            )
            await scoped_store._db.execute(
                """INSERT INTO runtime_transcript_entries
                (entry_id, runtime_session_id, task_id, session_id, message_id,
                 role, entry_type, content, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, 'user', 'runtime_v2_user_turn',
                        'correction', '{}', ?)""",
                (
                    "entry-cross-project",
                    "runtime-cross-project",
                    "task-cross-project",
                    "session-cross-project",
                    "message-cross-project",
                    now,
                ),
            )
            await scoped_store._db.commit()

            deleted = await scoped_store._purge_cross_project_runtime_rows()

            self.assertEqual(deleted["runtime_user_turn_seeds"], 1)
            self.assertEqual(deleted["runtime_sessions"], 1)
            for table in (
                "runtime_user_turn_seeds",
                "session_messages",
                "session_parts",
                "runtime_transcript_entries",
                "runtime_sessions",
            ):
                async with scoped_store._db.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ) as cursor:
                    row = await cursor.fetchone()
                self.assertEqual(int(row[0] if row else -1), 0)
        finally:
            await scoped_store.close()

    async def test_runtime_model_sees_one_fresh_correction_user_per_attempt(
        self,
    ) -> None:
        task, _unit_runtime = await self._seed_non_company_runtime()
        llm = _CaptureLLM()
        registry = ToolRegistry()

        async def _file_edit(**kwargs):
            return {"success": True, **kwargs}

        registry.register(
            ToolDefinition(
                name="file_edit",
                description="Edit an existing file",
                parameters={
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                },
                func=_file_edit,
            )
        )
        memory = MemoryManager(
            Path(self.tmp.name) / "opc-home",
            project_id=task.project_id,
            store=self.store,
        )
        runtime = NativeRuntimeV2(
            llm=llm,
            tool_registry=registry,
            memory_manager=memory,
            config=OPCConfig(),
            max_iterations=2,
        )
        round_one = (
            "## Current WorkItem Attempt — Correction Required\n"
            "WorkItem work-item-attempt-seed attempt 2, REWORK round 1.\n"
            "Edit report.md; use company_analysis.json exactly."
        )

        result_one = await runtime.run(
            system_prompt="system",
            user_message=round_one,
            context_messages=[{"role": "system", "content": "dynamic"}],
            task=task,
        )

        self.assertEqual(result_one.status, TaskStatus.DONE)
        first_model_messages = llm.message_batches[-1]
        self.assertEqual(first_model_messages[-1]["role"], "user")
        self.assertEqual(first_model_messages[-1]["content"], round_one)
        self.assertEqual(
            sum(
                1
                for item in first_model_messages
                if item.get("role") == "user" and item.get("content") == round_one
            ),
            1,
        )
        self.assertIn("file_edit", {item["name"] for item in llm.tool_batches[-1]})

        task.metadata["claimed_work_item_attempt_seq"] = 3
        task.metadata["_runtime_v2_attempt_user_seed_revision"] = "revision-round-2"
        round_two = (
            "## Current WorkItem Attempt — Correction Required\n"
            "WorkItem work-item-attempt-seed attempt 3, REWORK round 2.\n"
            "Edit report.md; use risk_analysis.json exactly."
        )
        result_two = await runtime.run(
            system_prompt="system",
            user_message=round_two,
            context_messages=[{"role": "system", "content": "dynamic"}],
            task=task,
        )

        self.assertEqual(result_two.status, TaskStatus.DONE)
        second_model_messages = llm.message_batches[-1]
        self.assertEqual(second_model_messages[-1]["role"], "user")
        self.assertEqual(second_model_messages[-1]["content"], round_two)
        self.assertEqual(
            [
                item["content"]
                for item in second_model_messages
                if item.get("role") == "user"
            ],
            [round_one, round_two],
        )
        self.assertEqual(await self._scalar("SELECT COUNT(*) FROM runtime_user_turn_seeds"), 2)

    async def test_approved_tool_resume_preserves_assistant_tool_call_adjacency(
        self,
    ) -> None:
        runtime = NativeRuntimeV2(
            llm=_ResumeLLM(),
            tool_registry=ToolRegistry(),
            memory_manager=SimpleNamespace(store=SimpleNamespace()),
            config=OPCConfig(),
        )
        task = Task(
            id="task-permission-adjacency",
            session_id="session-permission-adjacency",
            metadata={
                "runtime_v2": {},
                "_runtime_v2_attempt_user_seed_required": True,
            },
            context_snapshot={
                "runtime_resume": {
                    "runtime_session_id": "runtime-permission-adjacency",
                    "approved_tool_calls": {
                        "fingerprint-1": {
                            "id": "tool-call-1",
                            "function": "file_edit",
                            "arguments": {"path": "report.md"},
                            "fingerprint": "fingerprint-1",
                            "state": "ready",
                            "approved": True,
                            "checkpoint_id": "checkpoint-1",
                            "claim_id": "claim-1",
                            "consumer_id": "consumer-1",
                            "runtime_session_id": "runtime-permission-adjacency",
                            "task_id": "task-permission-adjacency",
                        }
                    },
                }
            },
        )
        restored = [
            {"role": "user", "content": "Make the correction."},
            {
                "role": "assistant",
                "content": "I will edit it now.",
                "tool_calls": [
                    {
                        "id": "tool-call-1",
                        "type": "function",
                        "function": {
                            "name": "file_edit",
                            "arguments": '{"path":"report.md"}',
                        },
                    }
                ],
            },
        ]
        runtime._restore_transcript_messages = AsyncMock(return_value=restored)

        messages, prefix_len = await runtime._bootstrap_messages(
            system_prompt="system",
            user_content="NEW CORRECTION MUST WAIT",
            user_message="NEW CORRECTION MUST WAIT",
            context_messages=[],
            task=task,
            suppress_resume_user_append=True,
        )

        self.assertEqual(prefix_len, 1)
        self.assertEqual(messages[-1]["role"], "assistant")
        self.assertEqual(messages[-1]["tool_calls"][0]["id"], "tool-call-1")
        self.assertNotIn(
            "NEW CORRECTION MUST WAIT",
            [item.get("content") for item in messages],
        )
        # The production sanitizer alone would drop the pending block; the
        # runtime boundary deliberately preserves it for its exact approved
        # ToolResult.
        self.assertEqual(
            LLMProvider.sanitize_tool_call_history(restored),
            [{"role": "user", "content": "Make the correction."}],
        )

        runtime._restore_transcript_messages = AsyncMock(return_value=[])
        with self.assertRaisesRegex(RuntimeError, "no durable transcript"):
            await runtime._bootstrap_messages(
                system_prompt="system",
                user_content="NEW CORRECTION MUST WAIT",
                user_message="NEW CORRECTION MUST WAIT",
                context_messages=[],
                task=task,
                suppress_resume_user_append=True,
            )

        # The exact resume validation is a runtime invariant, not an optional
        # provider feature.  It also runs when a test/provider has no generic
        # history sanitizer.
        runtime.llm = SimpleNamespace()
        runtime._restore_transcript_messages = AsyncMock(return_value=restored)
        no_sanitizer_messages, _ = await runtime._bootstrap_messages(
            system_prompt="system",
            user_content="NEW CORRECTION MUST WAIT",
            user_message="NEW CORRECTION MUST WAIT",
            context_messages=[],
            task=task,
            suppress_resume_user_append=True,
        )
        self.assertEqual(no_sanitizer_messages[-1]["role"], "assistant")
        self.assertEqual(
            no_sanitizer_messages[-1]["tool_calls"][0]["id"],
            "tool-call-1",
        )

        mismatched = [dict(item) for item in restored]
        mismatched[-1] = {
            **dict(mismatched[-1]),
            "tool_calls": [
                {
                    **dict(mismatched[-1]["tool_calls"][0]),
                    "function": {
                        "name": "file_edit",
                        "arguments": '{"path":"different.md"}',
                    },
                }
            ],
        }
        runtime._restore_transcript_messages = AsyncMock(return_value=mismatched)
        with self.assertRaisesRegex(RuntimeError, "arguments do not match"):
            await runtime._bootstrap_messages(
                system_prompt="system",
                user_content="NEW CORRECTION MUST WAIT",
                user_message="NEW CORRECTION MUST WAIT",
                context_messages=[],
                task=task,
                suppress_resume_user_append=True,
            )

        completed_but_still_ready = [
            *restored,
            {
                "role": "tool",
                "tool_call_id": "tool-call-1",
                "content": "already completed",
            },
        ]
        runtime._restore_transcript_messages = AsyncMock(
            return_value=completed_but_still_ready
        )
        with self.assertRaisesRegex(RuntimeError, "already has"):
            await runtime._bootstrap_messages(
                system_prompt="system",
                user_content="NEW CORRECTION MUST WAIT",
                user_message="NEW CORRECTION MUST WAIT",
                context_messages=[],
                task=task,
                suppress_resume_user_append=True,
            )

    async def test_settled_company_attempt_cannot_seed_old_runtime_tail(self) -> None:
        project_id = "project-company-seed"
        root_session_id = "session-company-seed"
        run_id = "run-company-seed"
        task = Task(
            id="task-company-seed",
            session_id=root_session_id,
            project_id=project_id,
            title="Company delivery",
            status=TaskStatus.PENDING,
            metadata={
                "delegation_run_id": run_id,
                "delegation_role_session_id": "role-session-company-seed",
                "delegation_seat_id": "seat-company-seed",
                "execution_mode": "company_mode",
                "runtime_model": "multi_team_org",
                "work_item_projection_id": "delivery-projection",
                "work_item_role_id": "lead",
                "work_item_turn_type": "deliver",
            },
        )
        work_item = DelegationWorkItem(
            work_item_id="work-item-company-seed",
            run_id=run_id,
            role_id="lead",
            seat_id="seat-company-seed",
            title=task.title,
            projection_id="delivery-projection",
            phase=Phase.READY,
            metadata={"task_id": task.id},
        )
        await self.store.save_session(
            SessionRecord(
                session_id=root_session_id,
                project_id=project_id,
                title="Company seed",
            )
        )
        await self.store.save_delegation_run(
            DelegationRun(
                run_id=run_id,
                project_id=project_id,
                session_id=root_session_id,
                execution_model="multi_team_org",
                status="running",
                lifecycle_status="active",
            )
        )
        await self.store.save_delegation_work_item(work_item)
        await self.store.save_task(task)
        self.assertTrue(
            await self.store.link_work_item_runtime_task(work_item.work_item_id, task.id)
        )
        set_linked_work_item_id(task, work_item.work_item_id)
        lease = await self.store.acquire_delegation_run_controller_lease(
            run_id,
            project_id=project_id,
            root_session_id=root_session_id,
            owner_token="company-seed-owner",
            lease_seconds=60,
        )
        self.assertTrue(lease.acquired)
        claimed = await self.store.claim_delegation_work_item_if_dispatchable(
            work_item.work_item_id,
            expected_phase=Phase.READY,
            role_runtime_session_id="role-session-company-seed",
            seat_id="seat-company-seed",
            task_id=task.id,
            controller_owner_token="company-seed-owner",
            controller_lease_generation=lease.generation,
        )
        assert claimed is not None
        task.metadata.update(
            {
                "company_run_controller_owner_token": "company-seed-owner",
                "company_run_controller_lease_generation": lease.generation,
                "claimed_work_item_attempt_seq": int(
                    claimed.metadata.get("attempt_seq", 0) or 0
                ),
            }
        )
        await self.store.save_task(task)
        await self.store.save_runtime_session(
            runtime_session_id="runtime-company-seed",
            project_id=project_id,
            session_id=root_session_id,
            task_id=task.id,
            status="running",
            controller_run_id=run_id,
            controller_owner_token="company-seed-owner",
            controller_lease_generation=lease.generation,
        )

        active = await self.store.ensure_runtime_user_turn_seed(
            task=task,
            runtime_session_id="runtime-company-seed",
            seed_key="company-active-seed",
            prompt_revision="active-revision",
            content="Active correction",
        )
        self.assertTrue(active["created"])

        # A legitimate takeover of the same still-running attempt reuses the
        # business seed identity; the old controller loses write authority.
        self.assertTrue(
            await self.store.renew_delegation_run_controller_lease(
                run_id,
                project_id=project_id,
                root_session_id=root_session_id,
                owner_token="company-seed-owner",
                generation=lease.generation,
                lease_seconds=1,
                heartbeat_at=datetime.now() - timedelta(seconds=2),
            )
        )
        takeover_store = OPCStore(Path(self.tmp.name) / "tasks.db")
        await takeover_store.initialize()
        try:
            takeover = await takeover_store.acquire_delegation_run_controller_lease(
                run_id,
                project_id=project_id,
                root_session_id=root_session_id,
                owner_token="company-seed-owner-b",
                lease_seconds=60,
            )
            self.assertTrue(takeover.acquired)
        finally:
            await takeover_store.close()
        takeover_task = deepcopy(task)
        takeover_task.metadata.update(
            {
                "company_run_controller_owner_token": "company-seed-owner-b",
                "company_run_controller_lease_generation": takeover.generation,
            }
        )
        duplicate = await self.store.ensure_runtime_user_turn_seed(
            task=takeover_task,
            runtime_session_id="runtime-company-seed",
            seed_key="company-active-seed",
            prompt_revision="active-revision",
            content="Active correction",
        )
        self.assertFalse(duplicate["created"])
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store.ensure_runtime_user_turn_seed(
                task=task,
                runtime_session_id="runtime-company-seed",
                seed_key="company-old-owner-tail",
                prompt_revision="old-owner-revision",
                content="Old controller must not append",
            )

        # Even a caller which strips its in-memory company markers cannot opt
        # out of the fence while the durable Task↔WorkItem link is company
        # scoped.
        stripped_task = deepcopy(takeover_task)
        for key in (
            "delegation_run_id",
            "execution_mode",
            "runtime_model",
            "company_run_controller_owner_token",
            "company_run_controller_lease_generation",
        ):
            stripped_task.metadata.pop(key, None)
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store.ensure_runtime_user_turn_seed(
                task=stripped_task,
                runtime_session_id="runtime-company-seed",
                seed_key="company-stripped-caller",
                prompt_revision="stripped-revision",
                content="Stripped caller must not bypass the fence",
            )

        # Model the durable shape after an authoritative rework/reset: the
        # attempt sequence remains for audit, while phase and exact claims are
        # released.  A stale coroutine from that attempt must no longer write
        # conversation state.
        assert self.store._db is not None
        await self.store._db.execute(
            """UPDATE delegation_work_items
               SET phase = 'ready_for_rework',
                   claimed_by_role_runtime_session_id = '',
                   claimed_by_seat_id = '',
                   metadata = json_set(
                       metadata,
                       '$.claimed_by_role_session_id', '',
                       '$.claimed_task_id', '',
                       '$.attempt_settled', json('true'),
                       '$.attempt_outcome', 'rework'
                   )
               WHERE work_item_id = ?""",
            (work_item.work_item_id,),
        )
        await self.store._db.commit()
        released = await self.store.get_delegation_work_item(work_item.work_item_id)
        assert released is not None
        self.assertEqual(released.claimed_by_role_runtime_session_id, "")
        self.assertEqual(released.metadata.get("claimed_task_id"), "")
        with self.assertRaises(CompanyRunControllerLeaseLost):
            await self.store.ensure_runtime_user_turn_seed(
                task=takeover_task,
                runtime_session_id="runtime-company-seed",
                seed_key="company-released-old-tail",
                prompt_revision="released-revision",
                content="This stale tail must not be persisted",
            )
        self.assertEqual(await self._scalar("SELECT COUNT(*) FROM runtime_user_turn_seeds"), 1)
        self.assertEqual(await self._scalar("SELECT COUNT(*) FROM session_messages"), 1)
        self.assertEqual(await self._scalar("SELECT COUNT(*) FROM session_parts"), 1)
        self.assertEqual(await self._scalar("SELECT COUNT(*) FROM runtime_transcript_entries"), 1)


if __name__ == "__main__":
    unittest.main()
