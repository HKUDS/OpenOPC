from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opc.core.models import ExecutionCheckpoint, ReorgProposal
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer0_interaction.coordinator import InteractionCoordinator
from opc.layer2_organization.reorg_manager import ReorgManager
from opc.plugins.office_ui.ws_handler import WSHandler


class _WS:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.closed = False

    async def send_json(self, payload: dict) -> None:
        self.messages.append(payload)


class OfficeReorgInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.engine = OPCEngine(
            project_id="project-a",
            store=self.store,
            owns_store=False,
        )
        self.engine.interaction_coordinator = InteractionCoordinator(
            store=self.store,
            project_id="project-a",
        )
        self.fixture_reorg_manager = ReorgManager(
            store=self.store,
            org_engine=SimpleNamespace(),
            communication=None,
            interaction_coordinator=self.engine.interaction_coordinator,
        )
        self.engine._initialized = True
        self.engine._schedule_interaction_consumption = lambda *_args: None
        self.engine.reorg_manager = SimpleNamespace(
            set_reorg_approval=AsyncMock(),
            apply_reorg=AsyncMock(),
        )

        self.handler = WSHandler.__new__(WSHandler)
        self.handler.engine = self.engine
        self.handler._clients = set()
        self.handler._shutting_down = False
        self.handler._project_accepted_interaction_reply = AsyncMock()

        async def engine_for_request(data: dict):
            if data.get("project_id") != "project-a":
                raise AssertionError("request was not scoped to project-a")
            return self.engine, "project-a"

        self.handler._engine_for_request = engine_for_request

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _seed_taskless_reorg(
        self,
        proposal: ReorgProposal | None = None,
    ) -> ExecutionCheckpoint:
        proposal = proposal or ReorgProposal(
            proposal_id="proposal-1",
            project_id="project-a",
            title="Approve the proposed reorganization?",
            summary="Approve the proposed reorganization?",
            user_confirmation_required=True,
        )
        _, _, persisted, _ = (
            await self.engine.interaction_coordinator.publish_reorg_proposal(
                proposal,
                await self.fixture_reorg_manager.build_reorg_owner_checkpoint(
                    proposal
                ),
            )
        )
        return persisted

    async def test_taskless_org_decision_uses_canonical_durable_submit(self) -> None:
        checkpoint = await self._seed_taskless_reorg()
        ws = _WS()

        await self.handler._handle_interaction_reply(ws, {
            "type": "interaction_reply",
            "project_id": "project-a",
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_type": checkpoint.checkpoint_type,
            "client_request_id": "org-ui-request-1",
            "decision": {"text": "approve", "option_id": "approve"},
        })

        self.assertEqual(len(ws.messages), 1)
        ack = ws.messages[0]
        self.assertEqual(ack["type"], "ack")
        self.assertTrue(ack["payload"]["accepted"], ack)
        persisted = await self.store.get_execution_checkpoint(
            checkpoint.checkpoint_id,
            project_id="project-a",
            checkpoint_type="company_reorg_pending",
        )
        self.assertIsNotNone(persisted)
        self.assertEqual(persisted.status, "answered")
        self.assertEqual(
            persisted.payload["interaction"]["decision"]["value"]["option_id"],
            "approve",
        )
        self.engine.reorg_manager.set_reorg_approval.assert_not_awaited()
        self.engine.reorg_manager.apply_reorg.assert_not_awaited()

    async def test_reorg_list_projects_the_unique_pending_checkpoint(self) -> None:
        proposal = ReorgProposal(
            proposal_id="proposal-1",
            project_id="project-a",
            title="Add architecture reviewer",
            summary="Add an independent architecture review role.",
            user_confirmation_required=True,
        )
        checkpoint = await self._seed_taskless_reorg(proposal)
        ws = _WS()

        await self.handler._handle_reorg_list(
            ws,
            {"type": "reorg_list", "project_id": "project-a"},
        )

        payload = ws.messages[0]["payload"]
        self.assertEqual(payload["project_id"], "project-a")
        self.assertEqual(len(payload["proposals"]), 1)
        projected = payload["proposals"][0]
        self.assertEqual(projected["project_id"], "project-a")
        self.assertEqual(projected["checkpoint_id"], checkpoint.checkpoint_id)
        self.assertEqual(projected["checkpoint_type"], "company_reorg_pending")
        self.assertEqual(projected["checkpoint_status"], "pending")
        self.assertEqual(projected["requester_task_id"], "")
        self.assertEqual(projected["requester_session_id"], "")
        self.assertEqual(
            projected["changeset"]["work_item_projection_changes"],
            [],
        )

    async def test_reorg_list_fails_closed_when_active_rows_are_ambiguous(self) -> None:
        proposal = ReorgProposal(
            proposal_id="proposal-1",
            project_id="project-a",
            title="Ambiguous proposal",
            summary="Two active rows must never produce an approval control.",
            user_confirmation_required=True,
        )
        await self._seed_taskless_reorg(proposal)
        duplicate = ExecutionCheckpoint(
            checkpoint_id="reorg-checkpoint-2",
            project_id="project-a",
            checkpoint_type="company_reorg_pending",
            payload={
                "proposal_id": "proposal-1",
                "interaction": {
                    "kind": "company_reorg_pending",
                    "domain_key": "reorg:proposal-1:duplicate",
                },
            },
        )
        # Deliberately inject a second domain row to model legacy corruption;
        # normal fixtures use the atomic proposal/card producer above.
        await self.store.publish_owner_interaction_checkpoint(
            duplicate,
            interaction_key="reorg:proposal-1:duplicate",
            supersede_pending_scope=False,
        )
        ws = _WS()

        await self.handler._handle_reorg_list(
            ws,
            {"type": "reorg_list", "project_id": "project-a"},
        )

        projected = ws.messages[0]["payload"]["proposals"][0]
        self.assertEqual(projected["checkpoint_status"], "ambiguous")
        self.assertEqual(projected["checkpoint_id"], "")
        self.assertEqual(projected["checkpoint_type"], "")

    def test_no_second_reorg_decision_handler_is_registered(self) -> None:
        self.assertNotIn("reorg_decide", WSHandler._HANDLERS)
        self.assertFalse(hasattr(WSHandler, "_handle_reorg_decide"))


if __name__ == "__main__":
    unittest.main()
