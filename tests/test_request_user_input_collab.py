from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from opc.core.company_tools import (
    COLLAB_PROFILE_COORDINATOR_DEFAULT,
    COLLAB_PROFILE_MANAGER_DEFAULT,
    COLLAB_PROFILE_WORKER_DEFAULT,
    REVIEW_EXECUTE_TURN_MODE,
    resolve_allowed_collaboration_tools,
)
from opc.core.events import EventBus
from opc.core.models import (
    ExecutionCheckpoint,
    ExecutionMode,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer0_interaction.coordinator import InteractionCoordinator
from opc.layer2_organization.communication import CommunicationManager
from opc.layer2_organization.work_item_links import set_linked_work_item_id
from opc.layer4_tools.collaboration import build_external_cli_tool_contract_lines, create_collaboration_tools
from opc.layer4_tools.collaboration_dispatch import dispatch_collaboration_tool
from opc.layer4_tools.user_input import request_user_input


class RequestUserInputCollabTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_user_input_normalizes_legacy_and_choice_questions(self) -> None:
        result = await request_user_input(
            reason="Need deployment input.",
            questions=[
                "Which region should we deploy to?",
                {
                    "id": "deployment_region",
                    "header": "Deployment region",
                    "question": "Which region should I target?",
                    "options": [
                        {"label": "US East", "description": "Use us-east-1"},
                        {"label": "EU West", "description": "Use eu-west-1"},
                        {"label": "Asia", "description": "Use ap-east-1"},
                        {"label": "Overflow option"},
                    ],
                },
            ],
            required_fields=["deployment_region"],
        )

        self.assertTrue(result["requires_user_input"])
        self.assertEqual(
            result["questions"],
            ["Which region should we deploy to?", "Which region should I target?"],
        )
        self.assertEqual(result["input_questions"][0]["id"], "question_1")
        self.assertEqual(result["input_questions"][0]["options"], [])
        self.assertTrue(result["input_questions"][0]["allow_freeform"])
        structured = result["input_questions"][1]
        self.assertEqual(structured["id"], "deployment_region")
        self.assertEqual([option["id"] for option in structured["options"]], ["a", "b", "c"])
        self.assertEqual(len(structured["options"]), 3)
        self.assertTrue(structured["allow_freeform"])
        self.assertTrue(structured["required"])

    def test_company_toolsets_do_not_include_request_user_input(self) -> None:
        for profile in (
            COLLAB_PROFILE_WORKER_DEFAULT,
            COLLAB_PROFILE_MANAGER_DEFAULT,
            COLLAB_PROFILE_COORDINATOR_DEFAULT,
        ):
            with self.subTest(profile=profile):
                self.assertNotIn("request_user_input", resolve_allowed_collaboration_tools(profile))

        review_task = Task(
            metadata={
                "runtime_model": "multi_team_org",
                "current_turn_mode": REVIEW_EXECUTE_TURN_MODE,
            }
        )
        self.assertNotIn(
            "request_user_input",
            resolve_allowed_collaboration_tools(COLLAB_PROFILE_WORKER_DEFAULT, task=review_task),
        )

    def test_close_human_review_is_only_added_for_delivery_review_followup(self) -> None:
        task = Task(
            metadata={
                "execution_mode": ExecutionMode.COMPANY_MODE.value,
                "runtime_model": "multi_team_org",
                "current_turn_mode": "dispatch_required",
            },
        )
        self.assertNotIn(
            "close_human_review",
            resolve_allowed_collaboration_tools(COLLAB_PROFILE_MANAGER_DEFAULT, task=task),
        )
        task.metadata["human_review_close_allowed"] = True
        self.assertIn(
            "close_human_review",
            resolve_allowed_collaboration_tools(COLLAB_PROFILE_MANAGER_DEFAULT, task=task),
        )

    def test_external_contract_omits_request_user_input_for_company_collab(self) -> None:
        lines = build_external_cli_tool_contract_lines({"request_user_input"})
        rendered = "\n".join(lines)
        self.assertNotIn("`request_user_input` arguments", rendered)
        self.assertIn("Use the exact argument names", rendered)

    async def test_company_collaboration_tools_do_not_register_request_user_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            try:
                communication = CommunicationManager(store, EventBus())
                tool_names = {tool.name for tool in create_collaboration_tools(communication)}
                self.assertNotIn("request_user_input", tool_names)
            finally:
                await store.close()

    async def test_opc_collab_dispatch_request_user_input_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            try:
                task = Task(
                    id="task-cli-input",
                    title="CLI scoped work",
                    project_id="proj1",
                    session_id="child-session",
                    parent_session_id="parent-session",
                    assigned_to="cto",
                    status=TaskStatus.RUNNING,
                    metadata={
                        "execution_mode": ExecutionMode.COMPANY_MODE.value,
                        "work_item_projection_id": "cto_plan",
                        "delegation_seat_id": "seat::team::ceo::cto",
                    },
                )
                set_linked_work_item_id(task, "work-item-cli")
                await store.save_task(task)
                env = {
                    "OPC_COMMS_FROM": "cto",
                    "OPC_COMMS_PROJECT": "proj1",
                    "OPC_COMMS_SESSION": "parent-session",
                    "OPC_WORKSPACE_ROOT": tmpdir,
                    "OPC_TASK_ID": task.id,
                    "OPC_WORK_ITEM_ID": "work-item-cli",
                    "OPC_PROJECT_DB_PATH": str(store.db_path),
                    "OPC_ALLOWED_COLLAB_TOOLS": json.dumps(["request_user_input"]),
                }

                payload, is_error = await dispatch_collaboration_tool(
                    "request_user_input",
                    {"reason": "Need approval.", "target_task_id": "other-task"},
                    env=env,
                )
                self.assertTrue(is_error)
                self.assertIn("unknown tool: request_user_input", payload["error"])
            finally:
                await store.close()

    async def test_engine_does_not_coerce_external_user_input_markers(self) -> None:
        engine = OPCEngine.__new__(OPCEngine)
        self.assertFalse(hasattr(engine, "_coerce_external_user_input_pause"))

    async def test_resume_task_checkpoint_stores_plain_user_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = OPCStore(Path(tmpdir) / "tasks.db")
            await store.initialize()
            try:
                pause_request = (await request_user_input(
                    reason="Need deployment region.",
                    questions=[
                        {
                            "id": "deployment_region",
                            "header": "Deployment region",
                            "question": "Which region?",
                            "options": [{"id": "a", "label": "US East"}],
                        }
                    ],
                ))
                task = Task(
                    id="task-resume-input",
                    title="Resume input",
                    project_id="proj1",
                    session_id="session-1",
                    assigned_to="engineer",
                    status=TaskStatus.AWAITING_HUMAN,
                )
                await store.save_task(task)
                checkpoint = ExecutionCheckpoint(
                    checkpoint_id="cp-resume-input",
                    project_id="proj1",
                    session_id="session-1",
                    checkpoint_type="task_user_input",
                    task_id=task.id,
                    payload={
                        "task_id": task.id,
                        "task_ids": [task.id],
                        "execution_mode": ExecutionMode.SINGLE_AGENT.value,
                        "pause_request": pause_request,
                        "runtime_v2": {"runtime_session_id": "rt-1"},
                        "interaction": {
                            "kind": "task_user_input",
                            "domain_key": "task-user-input:cp-resume-input",
                            "ownership": {
                                "waiting_task_id": task.id,
                                "waiting_session_id": task.session_id,
                                "ui_anchor_task_id": task.id,
                                "ui_anchor_session_id": task.session_id,
                            },
                        },
                    },
                )
                checkpoint, _created = await store.publish_owner_interaction_checkpoint(
                    checkpoint,
                    interaction_key="task-user-input:cp-resume-input",
                    supersede_pending_scope=False,
                )
                engine = OPCEngine(project_id="proj1", store=store, owns_store=False)
                engine.interaction_coordinator = InteractionCoordinator(
                    store=store,
                    project_id="proj1",
                )
                engine._initialized = True
                # The test drives the claimed consumer synchronously after the
                # durable ACK so the transport and domain boundaries are both
                # asserted without a background-task race.
                engine._schedule_interaction_consumption = lambda *_args: None

                async def _ensure_checkpoint_runtime_v2_payload(cp: ExecutionCheckpoint) -> ExecutionCheckpoint:
                    return cp

                async def _execute_single_agent(_tasks: list[Task], _agent: str | None) -> str:
                    return "resumed"

                engine._ensure_checkpoint_runtime_v2_payload = _ensure_checkpoint_runtime_v2_payload
                engine._restore_runtime_state_from_checkpoint = lambda _task, _payload: None
                engine._execute_single_agent = _execute_single_agent

                receipt = await engine.submit_checkpoint_decision(
                    checkpoint_id=checkpoint.checkpoint_id,
                    checkpoint_type=checkpoint.checkpoint_type,
                    decision={"text": "Selected US East."},
                    client_request_id="task-input-reply-1",
                    requester_task_id=task.id,
                    requester_session_id=task.session_id,
                )
                self.assertTrue(receipt["accepted"])
                self.assertEqual(receipt["status"], "answered")
                await engine._consume_answered_interaction(
                    checkpoint.checkpoint_id,
                    checkpoint.checkpoint_type,
                )

                refreshed = await store.get_task(task.id)
                self.assertEqual(refreshed.context_snapshot["user_supplied_input"], "Selected US East.")
                self.assertNotIn("user_input_answers", refreshed.context_snapshot)
                checkpoints = await store.get_execution_checkpoints(project_id="proj1")
                self.assertEqual(checkpoints[0].status, "resolved")
                self.assertEqual(
                    checkpoints[0].payload["interaction_result"]["message"],
                    "resumed",
                )
            finally:
                await store.close()


if __name__ == "__main__":
    unittest.main()
