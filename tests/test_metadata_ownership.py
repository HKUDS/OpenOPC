from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from opc.core.models import DelegationWorkItem, Phase, Task, TaskResult, TaskStatus
from opc.database.store import OPCStore
from opc.layer2_organization.metadata_ownership import (
    EXECUTION_COPY_KEYS,
    MetadataOwner,
    append_work_item_progress,
    build_work_item_owner_execution_copy,
    copy_work_item_execution_metadata,
    is_runtime_task_owned_key,
    is_work_item_owned_key,
    metadata_owner_for_key,
    migrate_work_item_owned_metadata_from_linked_tasks,
    strip_disallowed_work_item_metadata_from_runtime_task,
    sync_work_item_current_turn_mode,
    update_runtime_task_owned_metadata,
    validate_metadata_ownership,
)
from opc.layer2_organization.work_item_links import set_linked_work_item_id


def _work_item(metadata: dict | None = None) -> DelegationWorkItem:
    return DelegationWorkItem(
        work_item_id="wi-1",
        run_id="run-1",
        cell_id="cell-1",
        role_id="engineer",
        seat_id="seat-1",
        kind="execute",
        phase=Phase.READY,
        metadata=dict(metadata or {}),
    )


class MetadataOwnershipMatrixTests(unittest.TestCase):
    def test_matrix_covers_key_company_fields(self) -> None:
        expected_work_item = {
            "current_turn_mode",
            "progress_log",
            "review_owner_role_id",
            "report_target_work_item_id",
            "employee_assignment",
            "employee_prompt_context",
            "work_item_summary_for_downstream",
            "structured_review_verdict",
            "verification_status",
        }
        for key in expected_work_item:
            self.assertTrue(is_work_item_owned_key(key), key)
            self.assertEqual(metadata_owner_for_key(key), MetadataOwner.WORK_ITEM)

        for key in {
            "runtime_v2",
            "external_resume_session_id",
            "external_resume_agent_type",
            "working_memory",
            "interrupted_recovery",
        }:
            self.assertTrue(is_runtime_task_owned_key(key), key)
            self.assertEqual(metadata_owner_for_key(key), MetadataOwner.RUNTIME_TASK)

    def test_copy_work_item_execution_metadata_uses_matrix_not_ad_hoc_list(self) -> None:
        item = _work_item(
            {
                "current_turn_mode": "review_execute",
                "review_owner_role_id": "cto",
                "progress_log": ["visible progress"],
                "employee_assignment": {"employee_id": "emp-1"},
            }
        )

        copied = copy_work_item_execution_metadata(item)

        self.assertEqual(copied["current_turn_mode"], "review_execute")
        self.assertEqual(copied["review_owner_role_id"], "cto")
        self.assertEqual(copied["employee_assignment"], {"employee_id": "emp-1"})
        self.assertNotIn("progress_log", copied)
        self.assertIn("current_turn_mode", EXECUTION_COPY_KEYS)

    def test_review_source_report_link_is_work_item_only_not_execution_copy(self) -> None:
        key = "review_source_report_work_item_id"
        item = _work_item(
            {
                key: "report::wi-1::v2",
                "review_target_work_item_id": "wi-1",
            }
        )

        self.assertEqual(metadata_owner_for_key(key), MetadataOwner.WORK_ITEM)
        self.assertTrue(is_work_item_owned_key(key))
        self.assertNotIn(key, EXECUTION_COPY_KEYS)

        copied = copy_work_item_execution_metadata(item)

        self.assertNotIn(key, copied)
        self.assertEqual(copied["review_target_work_item_id"], "wi-1")

        task = Task(id="task-1", metadata={key: "report::wi-1::v2"})
        removed = strip_disallowed_work_item_metadata_from_runtime_task(task)
        self.assertIn(key, removed)
        self.assertNotIn(key, task.metadata)

        for journal_key in (
            "review_resolution",
            "review_resolution_state",
            "review_resolution_applied_work_item_id",
        ):
            self.assertEqual(
                metadata_owner_for_key(journal_key),
                MetadataOwner.WORK_ITEM,
            )
            self.assertNotIn(journal_key, EXECUTION_COPY_KEYS)

    def test_validate_metadata_ownership_reports_task_only_work_item_field(self) -> None:
        item = _work_item({})
        task = Task(
            id="task-1",
            metadata={"progress_log": ["task-only"], "runtime_v2": {"runtime_session_id": "rt"}},
        )
        set_linked_work_item_id(task, item.work_item_id)

        issues = validate_metadata_ownership(item, task)

        self.assertIn("metadata_ownership_violation", {issue.code for issue in issues})
        self.assertIn("progress_log", {issue.key for issue in issues})
        self.assertNotIn("runtime_v2", {issue.key for issue in issues})

    def test_runtime_task_owned_updates_filter_out_work_item_fields(self) -> None:
        task = Task(id="task-1", metadata={})

        applied = update_runtime_task_owned_metadata(
            task,
            {
                "runtime_v2": {"runtime_session_id": "rt"},
                "progress_log": ["wrong owner"],
            },
        )

        self.assertEqual(applied, {"runtime_v2": {"runtime_session_id": "rt"}})
        self.assertEqual(task.metadata, {"runtime_v2": {"runtime_session_id": "rt"}})

    def test_owner_execution_copy_uses_work_item_seat(self) -> None:
        item = _work_item({"seat_id": "wrong-task-seat", "work_kind": "execute"})
        item.seat_id = "seat::team::ceo::cto"
        item.team_id = "team::ceo"
        item.manager_role_id = "ceo"
        item.manager_seat_id = "seat::team::ceo::ceo"

        copied = build_work_item_owner_execution_copy(item)

        self.assertEqual(copied["delegation_seat_id"], "seat::team::ceo::cto")
        self.assertEqual(copied["delegation_team_id"], "team::ceo")
        self.assertEqual(copied["manager_seat_id"], "seat::team::ceo::ceo")

    def test_strip_disallowed_work_item_metadata_keeps_runtime_audit(self) -> None:
        task = Task(
            id="task-1",
            metadata={
                "verification_evidence": {"status": "provided"},
                "work_item_artifact_index": [{"kind": "file"}],
                "current_turn_mode": "worker_execute",
                "runtime_verification_evidence": {"status": "provided"},
            },
        )

        removed = strip_disallowed_work_item_metadata_from_runtime_task(task)

        self.assertIn("verification_evidence", removed)
        self.assertIn("work_item_artifact_index", removed)
        self.assertNotIn("verification_evidence", task.metadata)
        self.assertNotIn("work_item_artifact_index", task.metadata)
        self.assertEqual(task.metadata["current_turn_mode"], "worker_execute")
        self.assertEqual(task.metadata["runtime_verification_evidence"], {"status": "provided"})

    def test_linked_company_output_keeps_verification_on_work_item_context(self) -> None:
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor

        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        executor._active_plan = None
        task = Task(
            id="task-1",
            title="Runtime",
            metadata={
                "work_item_projection_id": "wi-1",
                "work_item_turn_type": "execute",
                "work_kind": "execute",
            },
        )
        set_linked_work_item_id(task, "wi-1")

        bundle = executor._capture_work_item_outputs(
            task,
            TaskResult(
                status=TaskStatus.DONE,
                content="Done",
                artifacts={
                    "verification_evidence": {
                        "status": "provided",
                        "verdict": "pass",
                        "checks": [{"command": "pytest -q"}],
                    }
                },
            ),
        )

        self.assertNotIn("verification_evidence", task.metadata)
        self.assertEqual(task.metadata["runtime_verification_evidence"]["verdict"], "pass")
        self.assertEqual(bundle.work_item_updates["verification_evidence"]["verdict"], "pass")
        self.assertEqual(
            task.context_snapshot["work_item_owned_outputs"]["verification_evidence"]["verdict"],
            "pass",
        )

    def test_delivery_package_reads_work_item_owned_context(self) -> None:
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
        from opc.layer2_organization.org_work_item_planner import CompanyWorkItemRuntimePlan

        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        executor.org_engine = None
        worker = Task(
            id="worker",
            title="Worker item",
            assigned_to="engineer",
            status=TaskStatus.DONE,
            metadata={
                "work_item_projection_id": "engineer::execute::1",
                "work_item_turn_type": "execute",
            },
            context_snapshot={
                "work_item_owned_outputs": {
                    "work_item_summary": "Built the feature.",
                    "work_item_artifact_index": [{"kind": "file", "value": "app.py"}],
                    "risks": ["Needs staging validation"],
                    "structured_review_verdict": {"label": "reject", "summary": "Missing docs"},
                }
            },
        )
        delivery = Task(
            id="delivery",
            title="Delivery",
            assigned_to="ceo",
            status=TaskStatus.DONE,
            result={"content": "Ship it"},
            metadata={
                "execution_mode": "company_mode",
                "work_item_projection_id": "ceo::deliver::1",
                "work_item_turn_type": "deliver",
                "authoritative_output": True,
            },
        )

        package = executor._build_authoritative_delivery_package(
            CompanyWorkItemRuntimePlan(profile="corporate"),
            [worker, delivery],
            delivery,
        )

        self.assertEqual(package["delivered_items"][0]["summary"], "Built the feature.")
        self.assertEqual(package["artifact_manifest"][0]["value"], "app.py")
        self.assertIn("Needs staging validation", package["risks"])
        self.assertIn("Missing docs", package["open_issues"][0])

    def test_materialization_uses_central_execution_copy_helper(self) -> None:
        from opc.layer2_organization import company_mode

        source = Path(company_mode.__file__).read_text(encoding="utf-8")

        self.assertIn("copy_work_item_execution_metadata(work_item)", source)
        self.assertIn("build_work_item_owner_execution_copy(work_item)", source)
        self.assertNotIn(
            'for key in (\n                "current_turn_mode",',
            source,
            "WorkItem execution-copy fields must live in metadata_ownership.py, not an ad hoc tuple.",
        )

    def test_company_runtime_owner_seat_writes_use_owner_helper(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        offenders = []
        for rel in (
            "opc/layer2_organization/company_mode.py",
            "opc/engine.py",
            "opc/layer2_organization/company_runtime.py",
        ):
            source = (repo_root / rel).read_text(encoding="utf-8")
            if '"delegation_seat_id":' in source or '["delegation_seat_id"]' in source:
                offenders.append(rel)
        self.assertEqual(
            offenders,
            [],
            "Runtime code must get delegation_seat_id from build_work_item_owner_execution_copy, "
            "not ad hoc session metadata writes.",
        )

    def test_owner_matrix_doc_tracks_key_categories(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        doc_path = repo_root / "docs" / "company-metadata-ownership.md"
        if not doc_path.exists():
            self.skipTest("metadata ownership docs are local-only and excluded from release")
        doc = doc_path.read_text(encoding="utf-8")

        for token in ("work_item", "runtime_task", "execution_copy"):
            self.assertIn(token, doc)
        for key in ("progress_log", "runtime_v2", "external_resume_*"):
            self.assertIn(key, doc)

    def test_company_hot_paths_do_not_read_legacy_work_item_link_metadata(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        legacy_link_key = "delegation_" + "work_item_id"
        hot_paths = (
            "opc/layer2_organization/company_mode.py",
            "opc/layer1_perception/context_assembler.py",
            "opc/plugins/office_ui/snapshot_builder.py",
            "opc/plugins/cli_board/services/board_repository.py",
        )
        offenders = [
            rel
            for rel in hot_paths
            if legacy_link_key in (repo_root / rel).read_text(encoding="utf-8")
        ]
        self.assertEqual(
            offenders,
            [],
            "Company hot paths must use hydrated linked_work_item_id/link table, "
            "not legacy task metadata link keys.",
        )


class MetadataOwnershipMigratorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    async def test_migrator_dry_run_backfills_missing_work_item_owned_fields(self) -> None:
        item = _work_item({})
        task = Task(
            id="task-1",
            title="Runtime",
            metadata={
                "progress_log": ["legacy progress"],
                "employee_prompt_context": "legacy persona",
                "runtime_v2": {"runtime_session_id": "rt"},
            },
        )
        await self.store.save_task(task)
        await self.store.save_delegation_work_item(item)
        await self.store.link_work_item_runtime_task(item.work_item_id, task.id)

        dry = await migrate_work_item_owned_metadata_from_linked_tasks(
            self.store,
            run_id=item.run_id,
            dry_run=True,
        )

        self.assertTrue(dry.dry_run)
        self.assertEqual(dry.changed_work_items, 1)
        self.assertEqual(dry.changes[0].updates["progress_log"], ["legacy progress"])
        self.assertEqual(dry.changes[0].updates["employee_prompt_context"], "legacy persona")
        self.assertNotIn("runtime_v2", dry.changes[0].updates)
        after_dry = await self.store.get_delegation_work_item(item.work_item_id)
        self.assertNotIn("progress_log", after_dry.metadata)

        applied = await migrate_work_item_owned_metadata_from_linked_tasks(
            self.store,
            run_id=item.run_id,
            dry_run=False,
        )
        self.assertEqual(applied.changed_work_items, 1)
        after = await self.store.get_delegation_work_item(item.work_item_id)
        self.assertEqual(after.metadata["progress_log"], ["legacy progress"])
        self.assertEqual(after.metadata["employee_prompt_context"], "legacy persona")

    async def test_migrator_conflict_keeps_work_item_value(self) -> None:
        item = _work_item({"progress_log": ["work-item-wins"]})
        task = Task(id="task-1", title="Runtime", metadata={"progress_log": ["task-loses"]})
        await self.store.save_task(task)
        await self.store.save_delegation_work_item(item)
        await self.store.link_work_item_runtime_task(item.work_item_id, task.id)

        report = await migrate_work_item_owned_metadata_from_linked_tasks(
            self.store,
            run_id=item.run_id,
            dry_run=False,
        )

        self.assertEqual(report.changed_work_items, 0)
        self.assertIn("metadata_ownership_conflict", {issue.code for issue in report.issues})
        after = await self.store.get_delegation_work_item(item.work_item_id)
        self.assertEqual(after.metadata["progress_log"], ["work-item-wins"])

    async def test_owner_writers_append_progress_and_sync_turn_mode(self) -> None:
        item = _work_item({})
        await self.store.save_delegation_work_item(item)

        progress = await append_work_item_progress(self.store, item.work_item_id, "step 1")
        await append_work_item_progress(self.store, item.work_item_id, "step 1", dedupe=True)
        synced = await sync_work_item_current_turn_mode(self.store, item.work_item_id, "worker_execute")

        after = await self.store.get_delegation_work_item(item.work_item_id)
        self.assertEqual(progress, ["step 1"])
        self.assertTrue(synced)
        self.assertEqual(after.metadata["progress_log"], ["step 1"])
        self.assertEqual(after.metadata["current_turn_mode"], "worker_execute")

    async def test_authoritative_delivery_package_persists_to_work_item_not_task_metadata(self) -> None:
        from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
        from opc.layer2_organization.org_work_item_planner import CompanyWorkItemRuntimePlan
        from opc.layer2_organization.work_item_links import set_linked_work_item_id

        item = _work_item({"work_kind": "deliver", "work_item_turn_type": "deliver"})
        item.work_item_id = "delivery-wi"
        item.kind = "deliver"
        await self.store.save_delegation_work_item(item)
        task = Task(
            id="delivery-task",
            title="Delivery",
            assigned_to="ceo",
            status=TaskStatus.DONE,
            result={"content": "Final package"},
            metadata={
                "execution_mode": "company_mode",
                "work_item_projection_id": "ceo::deliver::1",
                "work_item_turn_type": "deliver",
                "authoritative_output": True,
                "user_visible": True,
                "feedback_scope": "final",
            },
        )
        set_linked_work_item_id(task, item.work_item_id)
        await self.store.save_task(task)
        await self.store.link_work_item_runtime_task(item.work_item_id, task.id)

        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        executor.store = self.store
        executor.org_engine = None
        executor._active_plan = CompanyWorkItemRuntimePlan(profile="corporate")
        executor._active_tasks = [task]
        executor.save_task = AsyncMock()
        executor._ceo_pre_delivery_assessment = AsyncMock(return_value={"deliverable": True, "summary": "ok"})

        await executor._finalize_completed_work_item(task)

        after = await self.store.get_delegation_work_item(item.work_item_id)
        self.assertIn("delivery_package", after.metadata)
        self.assertNotIn("delivery_package", task.metadata)
        self.assertIn("delivery_package", task.context_snapshot["work_item_owned_outputs"])
        executor.save_task.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
