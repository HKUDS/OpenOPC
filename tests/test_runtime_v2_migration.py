from __future__ import annotations

import contextlib
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

from opc.core.models import ExecutionCheckpoint, SessionCompactionRecord, Task, TaskStatus
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer0_interaction.coordinator import InteractionCoordinator


@contextlib.contextmanager
def _workspace_tempdir() -> Path:
    base = Path.cwd() / ".tmp-test" / f"runtime-migration-{uuid.uuid4().hex}"
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


class RuntimeV2MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def _publish_legacy_owner_checkpoint(
        self,
        store: OPCStore,
        checkpoint: ExecutionCheckpoint,
    ) -> tuple[InteractionCoordinator, ExecutionCheckpoint]:
        coordinator = InteractionCoordinator(
            store=store,
            project_id=checkpoint.project_id,
        )
        domain_key = f"runtime-v2-migration:{checkpoint.checkpoint_id}"
        checkpoint.payload = {
            **dict(checkpoint.payload or {}),
            "interaction": {
                "kind": checkpoint.checkpoint_type,
                "prompt": str(
                    dict(checkpoint.payload or {})
                    .get("pause_request", {})
                    .get("reason", "")
                ),
                "domain_key": domain_key,
                "supersession_key": (
                    f"runtime-v2-migration:{checkpoint.project_id}:"
                    f"{checkpoint.session_id}:{checkpoint.checkpoint_type}"
                ),
                "supersession_order": [0, 0],
                "ownership": {},
            },
        }
        persisted, created = await coordinator.publish_owner_checkpoint(
            checkpoint,
            interaction_key=domain_key,
            supersession_key=str(
                checkpoint.payload["interaction"]["supersession_key"]
            ),
            supersession_order=[0, 0],
        )
        self.assertTrue(created)
        return coordinator, persisted

    async def test_checkpoint_lookup_migrates_legacy_payload_to_runtime_v2(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(tmpdir / "tasks.db")
            await store.initialize()
            task = Task(
                id="task-1",
                title="Need approval",
                session_id="sess-1",
                project_id="proj1",
                status=TaskStatus.AWAITING_REVIEW,
                metadata={},
            )
            await store.save_task(task)
            checkpoint = ExecutionCheckpoint(
                project_id="proj1",
                session_id="sess-1",
                checkpoint_type="task_user_input",
                task_id=task.id,
                payload={
                    "task_id": task.id,
                    "pause_request": {"reason": "Need confirmation"},
                    "tool_name": "shell_exec",
                },
            )
            coordinator, checkpoint = await self._publish_legacy_owner_checkpoint(
                store,
                checkpoint,
            )

            engine = OPCEngine()
            engine.project_id = "proj1"
            engine.store = store
            engine.interaction_coordinator = coordinator

            migrated = await engine.get_latest_pending_checkpoint_for_session("sess-1")
            assert migrated is not None
            self.assertIn("runtime_v2", migrated.payload)
            runtime_state = migrated.payload["runtime_v2"]
            self.assertTrue(runtime_state["runtime_session_id"].startswith("rtmig_"))
            self.assertTrue(runtime_state["migrated_from_legacy"])
            refreshed_task = await store.get_task(task.id)
            self.assertEqual(refreshed_task.metadata["migration_status"], "runtime_v2_migrated")
            self.assertEqual(
                refreshed_task.metadata["runtime_v2"]["runtime_session_id"],
                runtime_state["runtime_session_id"],
            )
            await coordinator.shutdown()
            await store.close()

    async def test_read_only_checkpoint_lookup_enriches_without_persisting(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(tmpdir / "tasks.db")
            await store.initialize()
            task = Task(
                id="task-read-only",
                title="Need approval",
                session_id="sess-read-only",
                project_id="proj1",
                status=TaskStatus.AWAITING_REVIEW,
                metadata={},
            )
            await store.save_task(task)
            checkpoint = ExecutionCheckpoint(
                project_id="proj1",
                session_id="sess-read-only",
                checkpoint_type="task_user_input",
                task_id=task.id,
                payload={
                    "task_id": task.id,
                    "pause_request": {"reason": "Need confirmation"},
                },
            )
            coordinator, _checkpoint = await self._publish_legacy_owner_checkpoint(
                store,
                checkpoint,
            )

            engine = OPCEngine()
            engine.project_id = "proj1"
            engine.store = store
            engine.interaction_coordinator = coordinator

            enriched = await engine.get_latest_pending_checkpoint_for_session(
                "sess-read-only",
                persist_runtime_v2_migration=False,
            )

            assert enriched is not None
            self.assertIn("runtime_v2", enriched.payload)
            refreshed_task = await store.get_task(task.id)
            assert refreshed_task is not None
            self.assertNotIn("runtime_v2", refreshed_task.metadata)
            persisted_checkpoint = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="proj1",
                checkpoint_type="task_user_input",
            )
            assert persisted_checkpoint is not None
            self.assertNotIn("runtime_v2", persisted_checkpoint.payload)
            await coordinator.shutdown()
            await store.close()

    async def test_migrated_runtime_state_carries_legacy_compaction_boundary(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(tmpdir / "tasks.db")
            await store.initialize()
            task = Task(
                id="task-legacy",
                title="Legacy compacted session",
                session_id="sess-legacy",
                project_id="proj1",
                status=TaskStatus.AWAITING_REVIEW,
            )
            await store.save_task(task)
            await store.save_session_compaction(
                SessionCompactionRecord(
                    session_id="sess-legacy",
                    compaction_message_id="msg-compact",
                    source_boundary_message_id="msg-boundary",
                )
            )
            engine = OPCEngine()
            engine.project_id = "proj1"
            engine.store = store

            runtime_state = await engine._build_migrated_runtime_state(
                task,
                checkpoint_type="task_user_input",
                payload={},
            )

            self.assertEqual(runtime_state["compaction_boundaries"][0]["source_boundary_message_id"], "msg-boundary")
            await store.close()

    async def test_resume_task_checkpoint_restores_migrated_runtime_state(self) -> None:
        with _workspace_tempdir() as tmpdir:
            store = OPCStore(tmpdir / "tasks.db")
            await store.initialize()
            task = Task(
                id="task-2",
                title="Resume task",
                session_id="sess-2",
                project_id="proj1",
                status=TaskStatus.AWAITING_REVIEW,
                metadata={},
            )
            await store.save_task(task)
            checkpoint = ExecutionCheckpoint(
                project_id="proj1",
                session_id="sess-2",
                checkpoint_type="task_user_input",
                task_id=task.id,
                payload={
                    "task_id": task.id,
                    "task_ids": [task.id],
                    "execution_mode": "task_mode",
                    "pause_request": {"reason": "Need confirmation"},
                },
            )
            coordinator, checkpoint = await self._publish_legacy_owner_checkpoint(
                store,
                checkpoint,
            )

            engine = OPCEngine()
            engine.project_id = "proj1"
            engine.store = store
            engine.interaction_coordinator = coordinator
            coordinator.checkpoint_changed_callback = (
                engine._interaction_checkpoint_changed
            )
            coordinator.orphaned_answer_callback = (
                engine._schedule_interaction_consumption
            )
            engine._execute_single_agent = AsyncMock(return_value="resumed")  # type: ignore[method-assign]

            response = await engine._maybe_resume_checkpoint(
                "continue",
                "sess-2",
                reply_metadata={
                    "response_to_checkpoint_id": checkpoint.checkpoint_id,
                    "response_to_checkpoint_type": checkpoint.checkpoint_type,
                },
            )

            self.assertEqual(response, "resumed")
            persisted_checkpoint = await store.get_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="proj1",
                checkpoint_type="task_user_input",
            )
            assert persisted_checkpoint is not None
            self.assertEqual(persisted_checkpoint.status, "resolved")
            resumed_task = await store.get_task(task.id)
            self.assertIn("runtime_resume", resumed_task.context_snapshot)
            self.assertTrue(
                resumed_task.context_snapshot["runtime_resume"]["runtime_session_id"].startswith("rtmig_")
            )
            await coordinator.shutdown()
            await store.close()


if __name__ == "__main__":
    unittest.main()
