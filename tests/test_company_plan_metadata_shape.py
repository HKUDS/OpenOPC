"""Regression tests for the company_work_item_plan / work_item_runtime_plan schema collision.

Work-item tasks store a per-item assignment spec under ``work_item_runtime_plan``
(keys: projection_id/turn_type/summary/deliverables/acceptance_criteria) while the
snapshot loaders used to read the same key as a serialized run-level
CompanyWorkItemRuntimePlan. Deserializing the spec shape silently produced an empty
plan, so every completed company session was misclassified as a legacy read-only run
and follow-up messages got the canned "Legacy company runtime run" reply
(project 000 forensics, 2026-07-07).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from opc.core.models import Task, TaskStatus
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer2_organization.company_mode import (
    is_serialized_company_work_item_runtime_plan,
    serialize_company_work_item_runtime_plan,
    serialized_company_plan_from_metadata,
)
from opc.layer2_organization.org_work_item_planner import (
    CompanyWorkItemRuntimePlan,
    WorkItemProjectionSpec,
)


# Shape actually persisted on work-item tasks (verified against project 000 data).
SPEC_SHAPED_PLAN = {
    "projection_id": "ceo::delivery::1899e271",
    "turn_type": "deliver",
    "summary": "Deliver the final result to the user.",
    "inputs": ["Use the global intent summary as the mission baseline."],
    "deliverables": ["Final delivery message."],
    "acceptance_criteria": ["User receives the deliverable."],
}


def _full_plan() -> CompanyWorkItemRuntimePlan:
    return CompanyWorkItemRuntimePlan(
        profile="corporate",
        projections=[
            WorkItemProjectionSpec(
                projection_id="corporate::intake::ceo",
                turn_type="intake",
                title="CEO Intake",
                summary="Frame the mission.",
                role_id="ceo",
            )
        ],
        metadata={
            "execution_model": "multi_team_org",
            "runtime_model": "multi_team_org",
        },
    )


class SerializedPlanShapeTests(unittest.TestCase):
    def test_full_plan_serialization_is_recognized(self) -> None:
        serialized = serialize_company_work_item_runtime_plan(_full_plan())
        self.assertTrue(is_serialized_company_work_item_runtime_plan(serialized))

    def test_per_work_item_spec_is_rejected(self) -> None:
        self.assertFalse(is_serialized_company_work_item_runtime_plan(SPEC_SHAPED_PLAN))

    def test_non_dict_and_empty_are_rejected(self) -> None:
        for value in (None, {}, [], "plan", 3):
            self.assertFalse(is_serialized_company_work_item_runtime_plan(value))

    def test_metadata_accessor_skips_spec_shape_and_null(self) -> None:
        metadata = {
            "company_work_item_plan": None,
            "work_item_runtime_plan": dict(SPEC_SHAPED_PLAN),
        }
        self.assertIsNone(serialized_company_plan_from_metadata(metadata))

    def test_metadata_accessor_prefers_canonical_key(self) -> None:
        serialized = serialize_company_work_item_runtime_plan(_full_plan())
        metadata = {
            "company_work_item_plan": serialized,
            "work_item_runtime_plan": dict(SPEC_SHAPED_PLAN),
        }
        self.assertEqual(serialized_company_plan_from_metadata(metadata), serialized)

    def test_metadata_accessor_accepts_full_plan_in_fallback_key(self) -> None:
        serialized = serialize_company_work_item_runtime_plan(_full_plan())
        metadata = {"work_item_runtime_plan": serialized}
        self.assertEqual(serialized_company_plan_from_metadata(metadata), serialized)


class SnapshotClassificationTests(unittest.IsolatedAsyncioTestCase):
    """A completed run whose tasks only carry spec-shaped plans must still classify
    as a multi-team-org work-item run (never as a legacy read-only run)."""

    PARENT_SESSION = "sess-parent"

    async def _store(self) -> OPCStore:
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        store = OPCStore(Path(tmpdir.name) / "tasks.db")
        await store.initialize()
        self.addAsyncCleanup(store.close)
        return store

    def _engine(self, store: OPCStore) -> OPCEngine:
        engine = OPCEngine()
        engine.project_id = "proj1"
        engine.store = store
        return engine

    def _work_item_task(
        self,
        task_id: str,
        projection_id: str,
        *,
        status: TaskStatus,
        turn_type: str = "execute",
        extra_metadata: dict | None = None,
    ) -> Task:
        metadata = {
            "company_profile": "corporate",
            "execution_model": "multi_team_org",
            "runtime_model": "multi_team_org",
            "work_item_runtime": True,
            "work_item_projection_id": projection_id,
            "work_item_turn_type": turn_type,
            # The 000 shape: canonical key null, fallback key holds the spec.
            "company_work_item_plan": None,
            "work_item_runtime_plan": dict(SPEC_SHAPED_PLAN, projection_id=projection_id),
        }
        metadata.update(extra_metadata or {})
        return Task(
            id=task_id,
            title=projection_id,
            session_id=f"{self.PARENT_SESSION}:{task_id}",
            parent_session_id=self.PARENT_SESSION,
            status=status,
            project_id="proj1",
            metadata=metadata,
        )

    async def _seed_completed_run(self, store: OPCStore) -> None:
        tasks = [
            self._work_item_task(
                "intake-task", "corporate::intake::ceo", status=TaskStatus.DONE, turn_type="intake"
            ),
            self._work_item_task(
                "execute-task", "cto::execute::d361bf3c", status=TaskStatus.DONE
            ),
            # Orphaned review attempt superseded by v2 (stuck non-terminal, as in 000).
            self._work_item_task(
                "review-v1-task", "review::467f36ff::v1", status=TaskStatus.PENDING, turn_type="review"
            ),
            self._work_item_task(
                "delivery-task",
                "ceo::delivery::1899e271",
                status=TaskStatus.DONE,
                turn_type="deliver",
                extra_metadata={"feedback_closed": True, "feedback_scope": "final"},
            ),
        ]
        for task in tasks:
            await store.save_task(task)

    async def test_snapshot_plan_classifies_as_multi_team_org(self) -> None:
        store = await self._store()
        await self._seed_completed_run(store)
        engine = self._engine(store)

        snapshot = await engine._load_company_runtime_snapshot(self.PARENT_SESSION)
        assert snapshot is not None
        plan, tasks = snapshot

        self.assertTrue(engine._runtime_uses_multi_team_org(plan))
        self.assertTrue(tasks)
        self.assertTrue(all(engine._task_uses_multi_team_org(task) for task in tasks))

    async def test_runtime_model_precedes_legacy_execution_model_during_classification(self) -> None:
        engine = OPCEngine(project_id="proj1")
        legacy_plan = CompanyWorkItemRuntimePlan(
            projections=[],
            metadata={"execution_model": "multi_team_org"},
        )
        public_contract_plan = CompanyWorkItemRuntimePlan(
            projections=[],
            metadata={"execution_model": "actor_runtime"},
        )
        canonical_other_plan = CompanyWorkItemRuntimePlan(
            projections=[],
            metadata={
                "execution_model": "multi_team_org",
                "runtime_model": "future_scheduler",
            },
        )

        self.assertTrue(engine._runtime_uses_multi_team_org(legacy_plan))
        self.assertFalse(engine._runtime_uses_multi_team_org(public_contract_plan))
        self.assertFalse(engine._runtime_uses_multi_team_org(canonical_other_plan))

        legacy_task = Task(title="legacy", metadata={"execution_model": "multi_team_org"})
        public_contract_task = Task(title="public", metadata={"execution_model": "actor_runtime"})
        canonical_other_task = Task(
            title="canonical",
            metadata={
                "execution_model": "multi_team_org",
                "runtime_model": "future_scheduler",
            },
        )
        self.assertTrue(engine._task_uses_multi_team_org(legacy_task))
        self.assertFalse(engine._task_uses_multi_team_org(public_contract_task))
        self.assertFalse(engine._task_uses_multi_team_org(canonical_other_task))

    async def test_followup_after_completed_run_never_reports_legacy(self) -> None:
        store = await self._store()
        await self._seed_completed_run(store)
        engine = self._engine(store)

        class DummyCompanyExecutor:
            async def execute(self, plan, tasks):  # noqa: ANN001
                return "follow-up handled"

        engine.company_executor = DummyCompanyExecutor()

        response = await engine._maybe_resume_existing_company_runtime(
            "where is the delivered file?",
            session_id=self.PARENT_SESSION,
        )

        if response is not None:
            self.assertNotIn("Legacy company runtime run", response)


if __name__ == "__main__":
    unittest.main()
