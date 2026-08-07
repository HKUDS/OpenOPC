from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from opc.core.config import OPCConfig
from opc.core.events import EventBus
from opc.core.models import (
    ReorgChangeSet,
    ReorgProposalStatus,
    ReorgRoleChange,
    ReorgTaskAdjustment,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer2_organization.communication import CommunicationManager
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.reorg_manager import ReorgManager
from tests._temp_paths import WorkspaceTemporaryDirectory

_REAL_TEMPORARY_DIRECTORY = tempfile.TemporaryDirectory


def setUpModule() -> None:
    tempfile.TemporaryDirectory = WorkspaceTemporaryDirectory  # type: ignore[assignment]


def tearDownModule() -> None:
    tempfile.TemporaryDirectory = _REAL_TEMPORARY_DIRECTORY  # type: ignore[assignment]


class CompanyReorgTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.root = Path(self.tmpdir.name)
        self.store = OPCStore(self.root / "tasks.db")
        await self.store.initialize()
        self.config = OPCConfig()
        self.org_engine = OrgEngine(self.config, self.root)
        self.communication = CommunicationManager(self.store, EventBus(), org_engine=self.org_engine)
        self.manager = ReorgManager(
            store=self.store,
            org_engine=self.org_engine,
            approval_engine=None,
            communication=self.communication,
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()

    async def test_org_level_reorg_requires_user_confirmation_before_apply(self) -> None:
        proposal = await self.manager.propose_reorg(
            project_id="proj1",
            summary="Replace senior_engineer with implementer.",
            changeset=ReorgChangeSet(
                role_changes=[
                    ReorgRoleChange(
                        action="replace",
                        role_id="senior_engineer",
                        replacement_role_id="implementer",
                        role={
                            "id": "implementer",
                            "name": "Implementer",
                            "responsibility": "Concrete implementation and delivery.",
                        },
                    )
                ]
            ),
            source_role_id="coordinator",
        )
        self.assertTrue(proposal.user_confirmation_required)
        self.assertEqual(proposal.status, ReorgProposalStatus.PROPOSED)

        with self.assertRaises(ValueError):
            await self.manager.apply_reorg(proposal.proposal_id)

        await self.manager.set_reorg_approval(proposal.proposal_id, approved=True, notes="Looks good.")
        result = await self.manager.apply_reorg(proposal.proposal_id)
        self.assertEqual(result["status"], ReorgProposalStatus.APPLIED.value)
        self.assertIsNotNone(self.org_engine.get_agent("implementer"))

    async def test_low_risk_task_adjustment_can_auto_apply_for_top_level_role(self) -> None:
        task = Task(
            title="Engineering Execution",
            project_id="proj1",
            assigned_to="senior_engineer",
            status=TaskStatus.PENDING,
            metadata={"work_item_role_id": "senior_engineer", "work_item_projection_id": "engineering_execution"},
        )
        await self.store.save_task(task)

        result = await self.manager.suggest_task_adjustment(
            project_id="proj1",
            source_role_id="coordinator",
            summary="Reassign engineering execution to qa_analyst for a quick validation pass.",
            changeset=ReorgChangeSet(
                task_adjustments=[
                    ReorgTaskAdjustment(
                        task_id=task.id,
                        action="reassign",
                        new_role_id="qa_analyst",
                    )
                ]
            ),
        )
        self.assertTrue(result["auto_applied"])
        updated = await self.store.get_task(task.id)
        assert updated is not None
        self.assertEqual(updated.assigned_to, "qa_analyst")
        self.assertEqual(updated.metadata["reorg_proposal_id"], result["proposal"].proposal_id)

    async def test_deny_reorg_keeps_existing_roles(self) -> None:
        proposal = await self.manager.propose_reorg(
            project_id="proj1",
            summary="Add temporary architecture role.",
            changeset=ReorgChangeSet(
                role_changes=[
                    ReorgRoleChange(
                        action="add",
                        role={
                            "id": "architect",
                            "name": "Architect",
                            "responsibility": "Architecture design.",
                        },
                    )
                ]
            ),
            source_role_id="coordinator",
        )
        await self.manager.set_reorg_approval(proposal.proposal_id, approved=False, notes="Not needed yet.")
        denied = await self.store.get_reorg_proposal(proposal.proposal_id)
        assert denied is not None
        self.assertEqual(denied.status, ReorgProposalStatus.DENIED)
        self.assertIsNone(self.org_engine.get_agent("architect"))
