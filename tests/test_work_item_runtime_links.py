"""Regression tests for structured WorkItem <-> runtime Task links."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from opc.core.models import DelegationWorkItem, Phase, Task, TaskStatus
from opc.database.store import OPCStore
from opc.layer2_organization import phase_hooks  # noqa: F401
from opc.layer2_organization.work_item_identity import mark_work_item_projection
from opc.layer2_organization.work_item_runtime import mark_work_item_runtime
from opc.layer2_organization.work_item_transition import transition_work_item_from_task


def _work_item(
    work_item_id: str = "wi-1",
    *,
    projection_id: str = "",
    metadata: dict | None = None,
) -> DelegationWorkItem:
    return DelegationWorkItem(
        work_item_id=work_item_id,
        run_id="run-1",
        cell_id="cell-1",
        team_instance_id="team-instance-1",
        team_id="team-1",
        role_id="worker",
        seat_id="seat-1",
        title=f"Work {work_item_id}",
        phase=Phase.READY,
        projection_id=projection_id,
        metadata=dict(metadata or {}),
    )


def _task(
    task_id: str = "task-1",
    *,
    status: TaskStatus = TaskStatus.PENDING,
    metadata: dict | None = None,
    created_at: datetime | None = None,
    project_id: str = "default",
    session_id: str | None = None,
    parent_session_id: str | None = None,
) -> Task:
    return Task(
        id=task_id,
        title=f"Task {task_id}",
        assigned_to="worker",
        status=status,
        project_id=project_id,
        session_id=session_id,
        parent_session_id=parent_session_id,
        metadata=dict(metadata or {}),
        created_at=created_at or datetime.now(),
    )


def _runtime_task(
    task_id: str,
    *,
    projection_id: str,
    run_id: str = "run-1",
    status: TaskStatus = TaskStatus.PENDING,
    member_session_id: str = "",
    created_at: datetime | None = None,
    project_id: str = "default",
    session_id: str | None = None,
    parent_session_id: str | None = None,
) -> Task:
    metadata = mark_work_item_projection(
        mark_work_item_runtime(
            {
                "delegation_run_id": run_id,
                "work_item_role_id": "worker",
                "delegation_seat_id": "seat-1",
            }
        ),
        projection_id=projection_id,
        turn_type="execute",
    )
    if member_session_id:
        metadata["member_session_id"] = member_session_id
    return _task(
        task_id,
        status=status,
        metadata=metadata,
        created_at=created_at,
        project_id=project_id,
        session_id=session_id,
        parent_session_id=parent_session_id,
    )


class WorkItemRuntimeLinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    async def test_link_api_hydrates_tasks_and_resolves_both_directions(self) -> None:
        item = _work_item("wi-linked")
        task = _task("task-linked")
        await self.store.save_delegation_work_item(item)
        await self.store.save_task(task)

        linked = await self.store.link_work_item_runtime_task(item.work_item_id, task.id)

        self.assertTrue(linked)
        fetched_task = await self.store.get_task(task.id)
        self.assertEqual(fetched_task.linked_work_item_id, item.work_item_id)
        self.assertEqual(
            (await self.store.get_runtime_task_for_work_item(item.work_item_id)).id,
            task.id,
        )
        self.assertEqual(
            (await self.store.get_work_item_for_runtime_task(task.id)).work_item_id,
            item.work_item_id,
        )
        self.assertEqual(
            await self.store.get_runtime_links_for_work_items([item.work_item_id]),
            {item.work_item_id: task.id},
        )

    async def test_ensure_runtime_task_reuses_exact_existing_task_and_writes_link(self) -> None:
        projection_id = "corporate::execute::reuse"
        item = _work_item("wi-ensure-reuse", projection_id=projection_id)
        existing = _runtime_task(
            "task-existing-runtime",
            projection_id=projection_id,
            project_id="project-1",
            session_id="root-session:wi-ensure-reuse",
            parent_session_id="root-session",
        )
        candidate = _runtime_task(
            "task-new-candidate",
            projection_id=projection_id,
            project_id="project-1",
            session_id="root-session:wi-ensure-reuse",
            parent_session_id="root-session",
        )
        await self.store.save_delegation_work_item(item)
        await self.store.save_task(existing)

        ensured = await self.store.ensure_runtime_task_for_work_item(item, lambda: candidate)

        self.assertEqual(ensured.id, existing.id)
        self.assertEqual(
            await self.store.get_runtime_links_for_work_items([item.work_item_id]),
            {item.work_item_id: existing.id},
        )
        self.assertIsNone(await self.store.get_task(candidate.id))

        def fail_factory() -> Task:
            raise AssertionError("factory should not run once link exists")

        ensured_again = await self.store.ensure_runtime_task_for_work_item(item, fail_factory)
        self.assertEqual(ensured_again.id, existing.id)

    async def test_ensure_runtime_task_creates_task_and_writes_structured_link(self) -> None:
        projection_id = "corporate::execute::create"
        item = _work_item("wi-ensure-create", projection_id=projection_id)
        candidate = _runtime_task(
            "task-created-runtime",
            projection_id=projection_id,
            project_id="project-1",
            session_id="root-session:wi-ensure-create",
            parent_session_id="root-session",
        )
        await self.store.save_delegation_work_item(item)

        ensured = await self.store.ensure_runtime_task_for_work_item(item, lambda: candidate)
        fetched = await self.store.get_task(ensured.id)

        self.assertEqual(ensured.id, candidate.id)
        self.assertEqual(fetched.linked_work_item_id, item.work_item_id)

    async def test_ensure_runtime_task_accepts_canonical_delivery_turn_kind(self) -> None:
        projection_id = "corporate::delivery::create"
        metadata = mark_work_item_projection(
            mark_work_item_runtime({
                "runtime_model": "multi_team_org",
                "work_kind": "delivery",
                "delegation_turn_kind": "delivery",
                "seat_id": "seat-1",
            }),
            projection_id=projection_id,
            turn_type="deliver",
        )
        item = DelegationWorkItem(
            work_item_id="wi-delivery-create",
            run_id="run-1",
            cell_id="cell-1",
            team_instance_id="team-instance-1",
            team_id="team-1",
            role_id="worker",
            seat_id="seat-1",
            title="Deliver",
            kind="delivery",
            projection_id=projection_id,
            phase=Phase.READY,
            metadata=metadata,
        )
        candidate = _runtime_task(
            "task-delivery-runtime",
            projection_id=projection_id,
            project_id="project-1",
            session_id="root-session:wi-delivery-create",
            parent_session_id="root-session",
        )
        # Regression for new35: materialization must stamp the runtime turn as
        # deliver when the WorkItem business kind is delivery.
        candidate.metadata["work_item_turn_type"] = "deliver"
        candidate.metadata["work_kind"] = "delivery"
        candidate.metadata["delegation_turn_kind"] = "delivery"
        await self.store.save_delegation_work_item(item)

        ensured = await self.store.ensure_runtime_task_for_work_item(item, lambda: candidate)

        self.assertEqual(ensured.id, candidate.id)
        self.assertEqual(
            await self.store.get_runtime_links_for_work_items([item.work_item_id]),
            {item.work_item_id: candidate.id},
        )

    async def test_ensure_runtime_task_rejects_stale_execute_turn_for_delivery(self) -> None:
        projection_id = "corporate::delivery::stale"
        metadata = mark_work_item_projection(
            mark_work_item_runtime({
                "runtime_model": "multi_team_org",
                "work_kind": "delivery",
                "delegation_turn_kind": "delivery",
                "seat_id": "seat-1",
            }),
            projection_id=projection_id,
            turn_type="deliver",
        )
        item = DelegationWorkItem(
            work_item_id="wi-delivery-stale",
            run_id="run-1",
            cell_id="cell-1",
            team_instance_id="team-instance-1",
            team_id="team-1",
            role_id="worker",
            seat_id="seat-1",
            title="Deliver",
            kind="delivery",
            projection_id=projection_id,
            phase=Phase.READY,
            metadata=metadata,
        )
        candidate = _runtime_task(
            "task-delivery-stale",
            projection_id=projection_id,
            project_id="project-1",
            session_id="root-session:wi-delivery-stale",
            parent_session_id="root-session",
        )
        candidate.metadata["work_item_turn_type"] = "execute"
        candidate.metadata["work_kind"] = "delivery"
        candidate.metadata["delegation_turn_kind"] = "delivery"
        await self.store.save_delegation_work_item(item)

        with self.assertRaisesRegex(RuntimeError, "work_kind_mismatch"):
            await self.store.ensure_runtime_task_for_work_item(item, lambda: candidate)

    async def test_ensure_runtime_task_rejects_projection_owner_seat_mismatch(self) -> None:
        projection_id = "corporate::execute::seat-mismatch"
        item = _work_item("wi-seat-mismatch", projection_id=projection_id)
        candidate = _runtime_task(
            "task-seat-mismatch",
            projection_id=projection_id,
            project_id="project-1",
            session_id="root-session:wi-seat-mismatch",
            parent_session_id="root-session",
        )
        candidate.metadata["runtime_model"] = "multi_team_org"
        candidate.metadata["delegation_seat_id"] = "wrong-seat"
        await self.store.save_delegation_work_item(item)

        with self.assertRaisesRegex(RuntimeError, "owner_seat_mismatch"):
            await self.store.ensure_runtime_task_for_work_item(item, lambda: candidate)

        self.assertIsNone(await self.store.get_task(candidate.id))
        self.assertEqual(await self.store.get_runtime_links_for_work_items([item.work_item_id]), {})

    async def test_hydrate_task_work_item_links_does_not_repair_missing_link(self) -> None:
        projection_id = "corporate::execute::unlinked"
        item = _work_item("wi-unlinked", projection_id=projection_id)
        task = _runtime_task(
            "task-unlinked",
            projection_id=projection_id,
            project_id="project-1",
            session_id="root-session:wi-unlinked",
            parent_session_id="root-session",
        )
        task.metadata["runtime_model"] = "multi_team_org"
        await self.store.save_delegation_work_item(item)
        await self.store.save_task(task)

        fetched = await self.store.get_task(task.id)
        await self.store.hydrate_task_work_item_links([task])

        self.assertEqual(fetched.linked_work_item_id, "")
        self.assertEqual(task.linked_work_item_id, "")
        self.assertEqual(await self.store.get_runtime_links_for_work_items([item.work_item_id]), {})

    async def test_link_api_does_not_overwrite_active_link_with_duplicate_by_default(self) -> None:
        projection_id = "corporate::execute::stable"
        item = _work_item("wi-no-overwrite", projection_id=projection_id)
        canonical = _runtime_task("task-canonical", projection_id=projection_id, status=TaskStatus.RUNNING)
        duplicate = _runtime_task("task-duplicate-new", projection_id=projection_id, status=TaskStatus.PENDING)
        await self.store.save_delegation_work_item(item)
        await self.store.save_task(canonical)
        await self.store.save_task(duplicate)
        self.assertTrue(await self.store.link_work_item_runtime_task(item.work_item_id, canonical.id))

        self.assertFalse(await self.store.link_work_item_runtime_task(item.work_item_id, duplicate.id))
        self.assertEqual(
            await self.store.get_runtime_links_for_work_items([item.work_item_id]),
            {item.work_item_id: canonical.id},
        )

    async def test_task_delete_cascades_link_and_work_item_row(self) -> None:
        item = _work_item("wi-cascade")
        task = _task("task-cascade")
        await self.store.save_delegation_work_item(item)
        await self.store.save_task(task)
        await self.store.link_work_item_runtime_task(item.work_item_id, task.id)

        await self.store.hard_delete_task(task.id)

        # Hard delete removes the linked work item row together with the
        # runtime traces, so the identity cannot be rematerialized.
        self.assertEqual(await self.store.get_runtime_links_for_work_items([item.work_item_id]), {})
        self.assertIsNone(await self.store.get_delegation_work_item(item.work_item_id))
        replacement = _task("task-cascade-replacement")
        await self.store.save_task(replacement)
        self.assertFalse(await self.store.link_work_item_runtime_task(item.work_item_id, replacement.id))

    async def test_transition_from_task_uses_structured_link_without_legacy_metadata(self) -> None:
        item = _work_item("wi-transition")
        task = _task("task-transition")
        await self.store.save_delegation_work_item(item)
        await self.store.save_task(task)
        await self.store.link_work_item_runtime_task(item.work_item_id, task.id)
        linked_task = await self.store.get_task(task.id)

        ok = await transition_work_item_from_task(
            self.store,
            linked_task,
            target_status_or_phase=TaskStatus.RUNNING,
            reason="structured_link_test",
        )

        self.assertTrue(ok)
        self.assertEqual((await self.store.get_delegation_work_item(item.work_item_id)).phase, Phase.RUNNING)


if __name__ == "__main__":
    unittest.main()
