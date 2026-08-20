from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from opc.core.config import OPCConfig
from opc.core.events import EventBus
from opc.core.models import (
    ReorgChangeSet,
    ReorgProposalStatus,
    ReorgRoleChange,
    ReorgScope,
    ReorgTaskAdjustment,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer2_organization.communication import CommunicationManager
from opc.layer2_organization.collaboration_service import (
    CollaborationContext,
    CollaborationService,
)
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.reorg_manager import ReorgManager
from opc.layer0_interaction.coordinator import (
    InteractionCoordinator,
    InteractionDecisionLease,
)
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
        self.coordinator = InteractionCoordinator(
            store=self.store,
            project_id="proj1",
        )
        self.manager = ReorgManager(
            store=self.store,
            org_engine=self.org_engine,
            communication=self.communication,
            interaction_coordinator=self.coordinator,
        )

    async def asyncTearDown(self) -> None:
        await self.coordinator.shutdown()
        await self.store.close()

    async def _decide(self, proposal, *, approved: bool):
        checkpoint = next(
            row
            for row in await self.store.get_execution_checkpoints(
                project_id="proj1",
                checkpoint_types=["company_reorg_pending"],
                statuses=["pending"],
            )
            if row.payload.get("proposal_id") == proposal.proposal_id
        )
        decision = {"option_id": "approve" if approved else "deny"}
        receipt = await self.coordinator.submit(
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_type=checkpoint.checkpoint_type,
            decision=decision,
            client_request_id=f"test:{proposal.proposal_id}:{approved}",
            checkpoint=checkpoint,
        )
        self.assertTrue(receipt.acknowledged)
        claim = await self.store.claim_answered_execution_checkpoint(
            checkpoint.checkpoint_id,
            project_id="proj1",
            checkpoint_type=checkpoint.checkpoint_type,
            consumer_id="company-reorg-test",
        )
        self.assertTrue(claim.acquired)
        assert claim.checkpoint is not None
        lease = InteractionDecisionLease(
            checkpoint=claim.checkpoint,
            decision=decision,
            consumer_id="company-reorg-test",
            claim_id=claim.claim_id,
        )
        return await self.manager.set_reorg_approval(
            proposal.proposal_id,
            approved=approved,
            notes="Looks good." if approved else "Not needed yet.",
            interaction_lease=lease,
        )

    def _pause_projection_engine(self) -> OPCEngine:
        engine = OPCEngine.__new__(OPCEngine)
        engine.store = self.store
        engine.project_id = "proj1"
        engine.reorg_manager = self.manager
        engine.interaction_coordinator = self.coordinator
        return engine

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

        await self._decide(proposal, approved=True)
        result = await self.manager.apply_reorg(proposal.proposal_id)
        self.assertEqual(result["status"], ReorgProposalStatus.APPLIED.value)
        self.assertIsNotNone(self.org_engine.get_agent("implementer"))

    async def test_required_reorg_atomically_publishes_complete_custom_task_card(self) -> None:
        anchor = Task(
            id="company-ui-anchor",
            session_id="company-root-session",
            project_id="proj1",
            status=TaskStatus.PENDING,
            org_id="custom-org-17",
            metadata={
                "mode": "company",
                "exec_mode": "custom",
                "execution_mode": "company_mode",
                "company_profile": "custom",
                "org_id": "custom-org-17",
            },
        )
        task = Task(
            session_id="custom-child-session",
            parent_session_id="company-root-session",
            title="Custom company replan",
            project_id="proj1",
            assigned_to="coordinator",
            status=TaskStatus.RUNNING,
            org_id="custom-org-17",
            metadata={
                "exec_mode": "custom",
                "company_profile": "custom",
                "execution_task_ids": ["task-a", "task-b"],
                "company_work_item_plan": {
                    "profile": "custom",
                    "metadata": {"original_request": "Build the application"},
                },
                "original_request": "Build the application",
            },
        )
        await self.store.save_task(anchor)
        await self.store.save_task(task)

        proposal = await self.manager.propose_reorg(
            project_id="proj1",
            session_id=task.session_id,
            task_id=task.id,
            summary="Add a custom architecture reviewer.",
            source_role_id="coordinator",
            changeset=ReorgChangeSet(
                role_changes=[
                    ReorgRoleChange(
                        action="add",
                        role={
                            "id": "custom_architecture_reviewer",
                            "name": "Custom Architecture Reviewer",
                            "responsibility": "Review the custom application architecture.",
                        },
                    )
                ]
            ),
        )
        rows = await self.store.get_execution_checkpoints(
            project_id="proj1",
            checkpoint_types=["company_reorg_pending"],
        )
        matching = [
            row
            for row in rows
            if row.payload.get("proposal_id") == proposal.proposal_id
        ]
        self.assertEqual(len(matching), 1)
        checkpoint = matching[0]
        self.assertEqual(checkpoint.status, "pending")
        self.assertEqual(checkpoint.task_id, task.id)
        self.assertEqual(checkpoint.payload["waiting_task_id"], task.id)
        self.assertEqual(
            checkpoint.payload["parent_session_id"],
            "company-root-session",
        )
        self.assertEqual(
            checkpoint.payload["company_work_item_plan"],
            task.metadata["company_work_item_plan"],
        )
        self.assertEqual(
            checkpoint.payload["interaction"]["execution_scope"],
            {"company_profile": "custom", "org_id": "custom-org-17"},
        )
        ownership = checkpoint.payload["interaction"]["ownership"]
        self.assertEqual(ownership["waiting_task_id"], task.id)
        self.assertEqual(ownership["waiting_session_id"], task.session_id)
        self.assertEqual(ownership["ui_anchor_task_id"], anchor.id)
        self.assertEqual(ownership["ui_anchor_session_id"], anchor.session_id)
        engine = self._pause_projection_engine()
        self.assertFalse(await engine.can_answer_checkpoint(
            checkpoint,
            requester_task_id=task.id,
            requester_session_id=task.session_id,
        ))
        self.assertTrue(await engine.can_answer_checkpoint(
            checkpoint,
            requester_task_id=anchor.id,
            requester_session_id=anchor.session_id,
        ))

        # The atomic producer is permanently idempotent for the proposal
        # source event.
        rebuilt = await self.manager.build_reorg_owner_checkpoint(proposal)
        retried = await self.coordinator.publish_reorg_proposal(proposal, rebuilt)
        self.assertFalse(retried[1])
        self.assertFalse(retried[3])
        rows = await self.store.get_execution_checkpoints(
            project_id="proj1",
            checkpoint_types=["company_reorg_pending"],
        )
        self.assertEqual(
            len(
                [
                    row
                    for row in rows
                    if row.payload.get("proposal_id") == proposal.proposal_id
                ]
            ),
            1,
        )

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
        self.assertFalse(result["proposal"].user_confirmation_required)
        updated = await self.store.get_task(task.id)
        assert updated is not None
        self.assertEqual(updated.assigned_to, "qa_analyst")
        self.assertEqual(updated.metadata["reorg_proposal_id"], result["proposal"].proposal_id)
        cards = await self.store.get_execution_checkpoints(
            project_id="proj1",
            checkpoint_types=["company_reorg_pending"],
        )
        self.assertFalse(
            any(
                card.payload.get("proposal_id")
                == result["proposal"].proposal_id
                for card in cards
            )
        )

    async def test_public_reorg_api_cannot_claim_system_decision_authority(self) -> None:
        task = Task(
            title="Public API adjustment",
            project_id="proj1",
            assigned_to="senior_engineer",
            status=TaskStatus.PENDING,
        )
        await self.store.save_task(task)
        changeset = ReorgChangeSet(
            task_adjustments=[
                ReorgTaskAdjustment(task_id=task.id, action="request_review")
            ]
        )

        with self.assertRaises(TypeError):
            await self.manager.propose_reorg(
                project_id="proj1",
                summary="Attempt to bypass the owner card.",
                source_role_id="coordinator",
                changeset=changeset,
                scope=ReorgScope.TASK_ADJUSTMENT,
                task_id=task.id,
                system_decision_authorized=True,  # type: ignore[call-arg]
            )

        proposal = await self.manager.propose_reorg(
            project_id="proj1",
            summary="Public low-risk proposals still require the owner.",
            source_role_id="coordinator",
            changeset=changeset,
            scope=ReorgScope.TASK_ADJUSTMENT,
            task_id=task.id,
        )
        self.assertTrue(proposal.user_confirmation_required)
        cards = await self.store.get_execution_checkpoints(
            project_id="proj1",
            checkpoint_types=["company_reorg_pending"],
        )
        self.assertEqual(
            len(
                [
                    card
                    for card in cards
                    if card.payload.get("proposal_id") == proposal.proposal_id
                ]
            ),
            1,
        )
        with self.assertRaises(TypeError):
            await self.manager.set_reorg_approval(
                proposal.proposal_id,
                approved=True,
                system_decision=True,  # type: ignore[call-arg]
            )
        with self.assertRaises(ValueError):
            await self.manager.set_reorg_approval(
                proposal.proposal_id,
                approved=True,
            )

        forged = deepcopy(proposal)
        forged.proposal_id = "forged-owner-free-proposal"
        forged.user_confirmation_required = False
        forged.metadata = {"system_decision_authorized": True}
        with self.assertRaises(ValueError):
            await self.store.create_reorg_proposal_with_interaction(
                forged,
                owner_checkpoint=None,
            )
        with self.assertRaises(TypeError):
            await self.store.decide_reorg_proposal(
                proposal.proposal_id,
                approved=True,
                system_decision=True,  # type: ignore[call-arg]
            )
        self.assertIsNotNone(self.manager.interaction_coordinator)
        with self.assertRaises(TypeError):
            await self.manager.interaction_coordinator.decide_reorg_proposal(
                proposal.proposal_id,
                approved=True,
                system_decision=True,  # type: ignore[call-arg]
            )

    async def test_low_risk_adjustment_from_regular_role_requires_one_owner_card(self) -> None:
        task = Task(
            session_id="regular-role-child",
            parent_session_id="regular-role-root",
            title="Implementation",
            project_id="proj1",
            assigned_to="implementer",
            status=TaskStatus.RUNNING,
            metadata={"work_item_role_id": "implementer"},
        )
        await self.store.save_task(task)

        result = await self.manager.suggest_task_adjustment(
            project_id="proj1",
            source_role_id="implementer",
            summary="Request one additional review pass.",
            changeset=ReorgChangeSet(
                task_adjustments=[
                    ReorgTaskAdjustment(
                        task_id=task.id,
                        action="request_review",
                    )
                ]
            ),
            session_id=task.parent_session_id,
            task_id=task.id,
        )

        self.assertFalse(result["auto_applied"])
        proposal = result["proposal"]
        self.assertTrue(proposal.user_confirmation_required)
        self.assertEqual(proposal.status, ReorgProposalStatus.PROPOSED)
        cards = await self.store.get_execution_checkpoints(
            project_id="proj1",
            checkpoint_types=["company_reorg_pending"],
            statuses=["pending"],
        )
        matching = [
            card
            for card in cards
            if card.payload.get("proposal_id") == proposal.proposal_id
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].task_id, task.id)
        persisted_task = await self.store.get_task(task.id)
        assert persisted_task is not None
        self.assertEqual(
            persisted_task.metadata["pending_reorg_proposal_id"],
            proposal.proposal_id,
        )
        self.assertEqual(
            persisted_task.metadata["pending_reorg_scope"],
            ReorgScope.TASK_ADJUSTMENT.value,
        )

        # The later AWAITING_HUMAN Task projection must reuse the atomic card,
        # not publish a second interaction with the child session identity.
        before = [
            row
            for row in await self.store.get_execution_checkpoints(
                project_id="proj1",
                checkpoint_types=["company_reorg_pending"],
            )
            if row.payload.get("proposal_id") == proposal.proposal_id
        ]
        self.assertEqual(len(before), 1)
        await self._pause_projection_engine()._save_task_pause_checkpoint(
            persisted_task,
            TaskResult(
                status=TaskStatus.AWAITING_HUMAN,
                content="Reorg confirmation required.",
            ),
        )
        after = [
            row
            for row in await self.store.get_execution_checkpoints(
                project_id="proj1",
                checkpoint_types=["company_reorg_pending"],
            )
            if row.payload.get("proposal_id") == proposal.proposal_id
        ]
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0].checkpoint_id, before[0].checkpoint_id)

    async def test_reorg_pause_projection_repairs_only_the_canonical_missing_card(self) -> None:
        task = Task(
            session_id="repair-child",
            parent_session_id="repair-root",
            title="Repair reorg card",
            project_id="proj1",
            assigned_to="implementer",
            status=TaskStatus.RUNNING,
            metadata={"work_item_role_id": "implementer"},
        )
        await self.store.save_task(task)
        result = await self.manager.suggest_task_adjustment(
            project_id="proj1",
            source_role_id="implementer",
            summary="Repair the missing confirmation card.",
            changeset=ReorgChangeSet(
                task_adjustments=[
                    ReorgTaskAdjustment(task_id=task.id, action="request_review")
                ]
            ),
            session_id=task.parent_session_id,
            task_id=task.id,
        )
        proposal = result["proposal"]
        canonical = await self.manager.build_reorg_owner_checkpoint(proposal)
        assert self.store._db is not None
        await self.store._db.execute(
            "DELETE FROM execution_checkpoints WHERE checkpoint_id = ?",
            (canonical.checkpoint_id,),
        )
        await self.store._db.commit()
        marked = await self.store.get_task(task.id)
        assert marked is not None

        await self._pause_projection_engine()._save_task_pause_checkpoint(
            marked,
            TaskResult(
                status=TaskStatus.AWAITING_HUMAN,
                content="Reorg confirmation required.",
            ),
        )

        repaired = [
            row
            for row in await self.store.get_execution_checkpoints(
                project_id="proj1",
                checkpoint_types=["company_reorg_pending"],
            )
            if row.payload.get("proposal_id") == proposal.proposal_id
        ]
        self.assertEqual(len(repaired), 1)
        self.assertEqual(repaired[0].checkpoint_id, canonical.checkpoint_id)
        self.assertEqual(repaired[0].status, "pending")

    async def test_stale_reorg_pause_projection_does_not_reopen_answered_card(self) -> None:
        task = Task(
            session_id="answered-child",
            parent_session_id="answered-root",
            title="Answered reorg card",
            project_id="proj1",
            assigned_to="implementer",
            status=TaskStatus.RUNNING,
            metadata={"work_item_role_id": "implementer"},
        )
        await self.store.save_task(task)
        result = await self.manager.suggest_task_adjustment(
            project_id="proj1",
            source_role_id="implementer",
            summary="Confirm once, even if the Task projection is late.",
            changeset=ReorgChangeSet(
                task_adjustments=[
                    ReorgTaskAdjustment(task_id=task.id, action="request_review")
                ]
            ),
            session_id=task.parent_session_id,
            task_id=task.id,
        )
        proposal = result["proposal"]
        stale_task = deepcopy(await self.store.get_task(task.id))
        assert stale_task is not None
        await self._decide(proposal, approved=False)
        card = next(
            row
            for row in await self.store.get_execution_checkpoints(
                project_id="proj1",
                checkpoint_types=["company_reorg_pending"],
            )
            if row.payload.get("proposal_id") == proposal.proposal_id
        )
        claim = dict(
            dict(card.payload.get("interaction", {}) or {}).get("claim", {}) or {}
        )
        lease = InteractionDecisionLease(
            checkpoint=card,
            decision={"option_id": "deny"},
            consumer_id=str(claim.get("consumer_id", "company-reorg-test")),
            claim_id=str(claim.get("claim_id", "")),
        )
        finished = await self.coordinator.finish(lease, final_status="resolved")
        self.assertTrue(finished.applied)

        await self._pause_projection_engine()._save_task_pause_checkpoint(
            stale_task,
            TaskResult(
                status=TaskStatus.AWAITING_HUMAN,
                content="Stale late pause projection.",
            ),
        )
        rows = [
            row
            for row in await self.store.get_execution_checkpoints(
                project_id="proj1",
                checkpoint_types=["company_reorg_pending"],
            )
            if row.payload.get("proposal_id") == proposal.proposal_id
        ]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, "resolved")

    async def test_immediate_reorg_answer_cannot_be_overwritten_by_stale_task_save(self) -> None:
        task = Task(
            session_id="race-child",
            parent_session_id="race-root",
            title="Race-safe implementation",
            project_id="proj1",
            assigned_to="implementer",
            status=TaskStatus.RUNNING,
            metadata={"work_item_role_id": "implementer", "before_proposal": True},
        )
        await self.store.save_task(task)
        observed_atomic_marker: list[str] = []

        async def answer_on_first_publication(checkpoint) -> None:
            if (
                checkpoint.checkpoint_type != "company_reorg_pending"
                or checkpoint.status != "pending"
                or checkpoint.task_id != task.id
            ):
                return
            marked = await self.store.get_task(task.id)
            assert marked is not None
            observed_atomic_marker.append(
                str(marked.metadata.get("pending_reorg_proposal_id", ""))
            )
            decision = {"option_id": "deny"}
            accepted = await self.coordinator.submit(
                checkpoint_id=checkpoint.checkpoint_id,
                checkpoint_type=checkpoint.checkpoint_type,
                decision=decision,
                client_request_id="immediate-reorg-denial",
                checkpoint=checkpoint,
            )
            self.assertTrue(accepted.acknowledged)
            claim = await self.store.claim_answered_execution_checkpoint(
                checkpoint.checkpoint_id,
                project_id="proj1",
                checkpoint_type=checkpoint.checkpoint_type,
                consumer_id="immediate-consumer",
            )
            self.assertTrue(claim.acquired)
            assert claim.checkpoint is not None
            lease = InteractionDecisionLease(
                checkpoint=claim.checkpoint,
                decision=decision,
                consumer_id="immediate-consumer",
                claim_id=claim.claim_id,
            )
            await self.manager.set_reorg_approval(
                str(checkpoint.payload.get("proposal_id", "")),
                approved=False,
                interaction_lease=lease,
            )
            finished = await self.coordinator.finish(lease, final_status="resolved")
            self.assertTrue(finished.applied)
            decided_task = await self.store.get_task(task.id)
            assert decided_task is not None
            decided_task.metadata = dict(decided_task.metadata or {})
            decided_task.metadata.pop("pending_reorg_proposal_id", None)
            decided_task.metadata.pop("pending_reorg_scope", None)
            decided_task.metadata["answered_during_publication"] = True
            await self.store.save_task(decided_task)

        self.coordinator.checkpoint_changed_callback = answer_on_first_publication
        service = CollaborationService(
            SimpleNamespace(
                store=self.store,
                event_bus=EventBus(),
                org_engine=self.org_engine,
                task_adjustment_suggester=self.manager.suggest_task_adjustment,
            )
        )
        result = await service.propose_task_adjustment(
            CollaborationContext.from_task(task, role_id="implementer"),
            summary="Request another review.",
            changeset={
                "task_adjustments": [
                    {"task_id": task.id, "action": "request_review"}
                ]
            },
        )

        self.assertEqual(len(observed_atomic_marker), 1)
        self.assertTrue(observed_atomic_marker[0])
        self.assertFalse(result["requires_user_input"])
        self.assertEqual(result["status"], ReorgProposalStatus.DENIED.value)
        persisted = await self.store.get_task(task.id)
        assert persisted is not None
        self.assertTrue(persisted.metadata["answered_during_publication"])
        self.assertNotIn("pending_reorg_proposal_id", persisted.metadata)
        self.assertNotIn("pending_reorg_proposal_id", task.metadata)
        self.assertTrue(task.metadata["answered_during_publication"])

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
        await self._decide(proposal, approved=False)
        denied = await self.store.get_reorg_proposal(proposal.proposal_id)
        assert denied is not None
        self.assertEqual(denied.status, ReorgProposalStatus.DENIED)
        self.assertIsNone(self.org_engine.get_agent("architect"))
