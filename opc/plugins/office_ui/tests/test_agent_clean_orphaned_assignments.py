from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from opc.core.models import Task
from opc.database.store import OPCStore
from opc.plugins.office_ui.services.agent import AgentService


class TestCleanOrphanedAssignments(unittest.IsolatedAsyncioTestCase):
    """Regression test for a silent ``except Exception: pass`` on a
    dispatch/write path.

    ``AgentService._clean_orphaned_assignments`` clears ``assigned_to`` on
    every Task still pointing at a role that was just deleted. The whole
    loop used to be wrapped in one ``try/except Exception: pass``: if
    ``store.save_task`` failed on any one Task, the loop stopped there and
    every *later* Task in the list was silently left still pointing at the
    deleted role -- exactly the orphaned-assignment state this method
    exists to prevent, with no error surfaced anywhere.
    """

    def _make_context(self, store: OPCStore) -> SimpleNamespace:
        engine = SimpleNamespace(store=store, project_id="default")
        return SimpleNamespace(engine=engine)

    async def test_one_failing_save_does_not_block_cleanup_of_other_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            try:
                tasks = [
                    Task(id=f"task-{i}", session_id=f"session-{i}", title=f"T{i}", project_id="default", assigned_to="stale-role")
                    for i in range(3)
                ]
                for task in tasks:
                    await store.save_task(task)

                service = AgentService(self._make_context(store))

                real_save_task = store.save_task
                failing_task_id = "task-1"

                async def flaky_save_task(task):
                    if task.id == failing_task_id:
                        raise RuntimeError("simulated transient save failure")
                    return await real_save_task(task)

                with patch.object(store, "save_task", side_effect=flaky_save_task):
                    await service._clean_orphaned_assignments("stale-role")

                persisted = {t.id: t for t in await store.get_tasks(project_id="default")}

                # task-0 and task-2 come after/around the failing task in
                # iteration order; both must still be cleaned up even though
                # task-1's save failed.
                self.assertEqual(persisted["task-0"].assigned_to, "")
                self.assertEqual(persisted["task-2"].assigned_to, "")
                # The failing task keeps its stale assignment (its own save
                # genuinely failed) but must not have silently corrupted the
                # cleanup of its siblings.
                self.assertEqual(persisted["task-1"].assigned_to, "stale-role")
            finally:
                await store.close()


if __name__ == "__main__":
    unittest.main()
