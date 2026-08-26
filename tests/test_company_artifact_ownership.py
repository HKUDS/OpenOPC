from __future__ import annotations

import asyncio
import copy
import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from opc.core.config import OPCConfig
from opc.core.models import (
    DelegationRun,
    DelegationWorkItem,
    ExecutionCheckpoint,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.core.company_controller import CompanyRunControllerLeaseLost
from opc.database.store import OPCStore
from opc.layer2_organization.work_item_links import (
    linked_work_item_id_for_task,
    set_linked_work_item_id,
)
from opc.layer3_agent.runtime_v2.tool_hooks import RuntimeCompanyControllerToolFence
from opc.layer3_agent.runtime_v2.subagents import SubagentManager
from opc.layer3_agent.runtime_v2.permissions import RuntimePermissionAdapter
from opc.layer3_agent.runtime_v2.streaming_tool_executor import StreamingToolExecutor
from opc.layer3_agent.runtime_v2.tool_planner import ToolPlanner
from opc.layer4_tools.file_ops import (
    FILE_MUTATION_TOOL_NAMES,
    file_mutation_paths,
)
from opc.layer4_tools.browser import create_browser_tools
from opc.layer4_tools.git_ops import create_git_tools
from opc.layer4_tools.opaque_execution import (
    OpaqueExecutionEnvelopeError,
    build_company_opaque_execution_plan,
    company_opaque_execution_identity,
    current_opaque_execution_plan,
    exact_tool_call_fingerprint,
    install_opaque_execution_plan,
    opaque_execution_envelope_digest,
    reset_opaque_execution_plan,
)
from opc.layer4_tools.python_exec import python_exec
from opc.layer4_tools.registry import (
    COMPANY_EFFECT_FORBIDDEN,
    COMPANY_EFFECT_OPAQUE_EXACT,
    COMPANY_EFFECT_UNKNOWN,
    ToolDefinition,
    ToolRegistry,
)
from opc.layer4_tools.shell import shell_exec
from opc.layer4_tools.workspace_fs import workspace_roots_for_task


class CompanyArtifactOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "workspace"
        self.root.mkdir()
        self.db_path = Path(self._tmp.name) / "tasks.db"
        self.store = OPCStore(self.db_path)
        await self.store.initialize()
        await self.store.save_delegation_run(
            DelegationRun(
                run_id="run-1",
                project_id="project-1",
                session_id="root-session",
                status="running",
                lifecycle_status="active",
                metadata={"comms_workspace_root": str(self.root)},
            )
        )
        lease = await self.store.acquire_delegation_run_controller_lease(
            "run-1",
            project_id="project-1",
            root_session_id="root-session",
            owner_token="controller-1",
            lease_seconds=60,
        )
        self.assertTrue(lease.acquired)
        self.generation = lease.generation
        self.owner_task = await self._seed_work_item(
            work_item_id="wi-owner",
            task_id="task-owner",
            role_id="analyst",
        )
        # Deliberately use the same role: authorization is the stable
        # WorkItem, never a role/title/projection shortcut.
        self.foreign_task = await self._seed_work_item(
            work_item_id="wi-foreign",
            task_id="task-foreign",
            role_id="analyst",
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _seed_work_item(
        self,
        *,
        work_item_id: str,
        task_id: str,
        role_id: str,
    ) -> Task:
        await self.store.save_delegation_work_item(
            DelegationWorkItem(
                work_item_id=work_item_id,
                run_id="run-1",
                role_id=role_id,
                seat_id=f"seat-{work_item_id}",
                projection_id=work_item_id,
                title=work_item_id,
            )
        )
        task = Task(
            id=task_id,
            session_id="root-session",
            project_id="project-1",
            title=work_item_id,
            assigned_to=role_id,
            metadata={
                "delegation_run_id": "run-1",
                "workspace_root": str(self.root),
                "delegation_role_session_id": f"role-session-{work_item_id}",
                "delegation_seat_id": f"seat-{work_item_id}",
            },
        )
        await self.store.save_task(task)
        self.assertTrue(
            await self.store.link_work_item_runtime_task(work_item_id, task_id)
        )
        set_linked_work_item_id(task, work_item_id)
        claimed = await self.store.claim_delegation_work_item_if_dispatchable(
            work_item_id,
            expected_phase="ready",
            role_runtime_session_id=f"role-session-{work_item_id}",
            seat_id=f"seat-{work_item_id}",
            task_id=task_id,
            controller_owner_token="controller-1",
            controller_lease_generation=self.generation,
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        task.metadata.update(
            {
                "company_run_controller_owner_token": "controller-1",
                "company_run_controller_lease_generation": self.generation,
                "claimed_work_item_attempt_seq": int(
                    claimed.metadata.get("attempt_seq", 0) or 0
                ),
            }
        )
        await self.store.save_task(task)
        return task

    async def _claim(
        self,
        task: Task,
        *paths: str,
        store: OPCStore | None = None,
    ):
        actual_workspace_root, actual_output_root = workspace_roots_for_task(task)
        return await (store or self.store).claim_company_artifact_paths_for_controller(
            project_id="project-1",
            run_id="run-1",
            work_item_id=task.linked_work_item_id,
            runtime_task_id=task.id,
            actual_workspace_root=actual_workspace_root,
            actual_output_root=actual_output_root,
            owner_token="controller-1",
            generation=self.generation,
            attempt_seq=int(
                task.metadata.get("claimed_work_item_attempt_seq", 0) or 0
            ),
            raw_paths=list(paths),
        )

    async def _executing_permit(
        self,
        task: Task,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        runtime_session_id: str,
        decision: str = "approve_once",
    ) -> dict[str, object]:
        execution_envelope = build_company_opaque_execution_plan(
            task,
            tool_name,
            arguments,
        ).envelope
        execution_identity = company_opaque_execution_identity(task)
        fingerprint = exact_tool_call_fingerprint(
            tool_call_id=call_id,
            tool_name=tool_name,
            arguments=arguments,
            runtime_session_id=runtime_session_id,
            execution_envelope=execution_envelope,
            execution_identity=execution_identity,
        )
        checkpoint = ExecutionCheckpoint(
            checkpoint_id=f"cp-{call_id}",
            project_id="project-1",
            session_id=task.session_id,
            checkpoint_type="tool_permission",
            task_id=task.id,
            payload={
                "schema_version": 2,
                "interaction": {
                    "kind": "tool_permission",
                    "domain_key": f"tool:{task.id}:{fingerprint}",
                    "prompt": "Allow exact call?",
                    "options": [
                        {"id": "approve_once", "label": "Approve once"},
                        {
                            "id": "approve_session",
                            "label": "Allow for this session",
                        },
                        {"id": "deny", "label": "Deny"},
                    ],
                    "ownership": {
                        "waiting_task_id": task.id,
                        "waiting_session_id": task.session_id,
                        "tool_runtime_session_id": runtime_session_id,
                    },
                },
                "tool_call": {
                    "id": call_id,
                    "name": tool_name,
                    "arguments": arguments,
                    "execution_envelope": execution_envelope,
                    "execution_identity": execution_identity,
                    "runtime_session_id": runtime_session_id,
                    "fingerprint": fingerprint,
                },
                "approval": {
                    "action_kind": "tool",
                    "action_name": tool_name,
                    "allowlist_enabled": False,
                    "allowlist_patterns": [],
                },
            },
        )
        await self.store.create_owner_interaction_checkpoint(
            checkpoint,
            interaction_key=f"tool:{task.id}:{fingerprint}",
        )
        accepted = await self.store.accept_execution_checkpoint_decision(
            checkpoint.checkpoint_id,
            project_id="project-1",
            checkpoint_type="tool_permission",
            request_id=f"answer-{call_id}",
            decision_hash=f"hash-{call_id}",
            decision={"option_id": decision},
        )
        self.assertTrue(accepted.acknowledged)
        claimed = await self.store.claim_answered_execution_checkpoint(
            checkpoint.checkpoint_id,
            project_id="project-1",
            checkpoint_type="tool_permission",
            consumer_id=f"consumer-{call_id}",
            claim_id=f"claim-{call_id}",
        )
        self.assertTrue(claimed.acquired)
        permit: dict[str, object] = {
            "id": call_id,
            "function": tool_name,
            "arguments": arguments,
            "execution_envelope": execution_envelope,
            "execution_identity": execution_identity,
            "fingerprint": fingerprint,
            "runtime_session_id": runtime_session_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "checkpoint_type": "tool_permission",
            "checkpoint_project_id": "project-1",
            "task_id": task.id,
            "claim_id": f"claim-{call_id}",
            "consumer_id": f"consumer-{call_id}",
            "decision": decision,
            "approved": True,
            "state": "ready",
        }
        persisted = await self.store.update_task_runtime_tool_permit(
            task.id,
            runtime_session_id=runtime_session_id,
            fingerprint=fingerprint,
            permit=permit,
        )
        task.context_snapshot = dict(persisted.context_snapshot or {})
        task.metadata = dict(persisted.metadata or {})
        started = await self.store.begin_exact_tool_permission_effect(permit)
        self.assertTrue(started.acquired)
        ledger = await self.store.get_task_runtime_tool_ledger(
            task.id,
            project_id="project-1",
        )
        assert ledger is not None
        executing = dict(ledger.permits[fingerprint])
        task.context_snapshot["runtime_resume"]["approved_tool_calls"][
            fingerprint
        ] = executing
        task.metadata["runtime_v2"]["approved_tool_calls"][fingerprint] = executing
        return executing

    async def _seed_subagent(
        self,
        agent_id: str,
        *,
        isolation: str = "shared",
        workspace_root: Path | None = None,
        persist_child: bool = True,
    ) -> Task:
        child_root = workspace_root or self.root
        await self.store.save_runtime_subagent_run(
            subagent_run_id=agent_id,
            runtime_session_id=f"native-{agent_id}",
            task_id=self.owner_task.id,
            agent_id=agent_id,
            profile="implement",
            status="running",
            worktree_path=(str(child_root) if isolation == "worktree" else ""),
            metadata={"isolation": isolation},
        )
        child = Task(
            id=f"child-{agent_id}",
            parent_id=self.owner_task.id,
            session_id=f"session-{agent_id}",
            project_id="project-1",
            title=f"child {agent_id}",
            assigned_to="analyst",
            metadata={
                **dict(self.owner_task.metadata),
                "_comms_endpoint_id": agent_id,
                "_company_parent_work_item_id": "wi-owner",
                "_execution_context": {
                    "workspace_root": str(child_root),
                    "output_root": str(child_root),
                },
            },
        )
        if persist_child:
            await self.store.save_task(child)
        return child

    async def test_same_work_item_retries_but_same_role_foreign_work_item_is_denied(self) -> None:
        first = await self._claim(self.owner_task, "reports/analysis.json")
        retry = await self._claim(
            self.owner_task,
            str(self.root / "reports" / "analysis.json"),
        )
        foreign = await self._claim(self.foreign_task, "reports/analysis.json")

        self.assertTrue(first.claimed)
        self.assertTrue(retry.claimed)
        self.assertEqual(foreign.outcome, "conflict")
        self.assertEqual(foreign.conflicting_work_item_id, "wi-owner")
        self.assertEqual(foreign.conflicting_path, "reports/analysis.json")

    async def test_multi_path_conflict_is_all_or_nothing(self) -> None:
        self.assertTrue(
            (await self._claim(self.owner_task, "owned.json")).claimed
        )
        blocked = await self._claim(
            self.foreign_task,
            "new.json",
            "owned.json",
        )
        self.assertEqual(blocked.outcome, "conflict")

        # The failed batch did not reserve its non-conflicting prefix.
        newly_claimed = await self._claim(self.owner_task, "new.json")
        self.assertTrue(newly_claimed.claimed)

    async def test_apply_patch_claims_add_update_delete_and_move_as_one_batch(self) -> None:
        self.assertTrue(
            (await self._claim(self.owner_task, "source.txt", "deleted.txt")).claimed
        )
        patch = "\n".join(
            [
                "*** Begin Patch",
                "*** Update File: source.txt",
                "*** Move to: moved.txt",
                "@@",
                "-old",
                "+new",
                "*** Delete File: deleted.txt",
                "*** Add File: added.txt",
                "+added",
                "*** End Patch",
            ]
        )
        self.assertEqual(
            file_mutation_paths("apply_patch", {"patch": patch}),
            ("source.txt", "moved.txt", "deleted.txt", "added.txt"),
        )
        self.assertEqual(
            file_mutation_paths(
                "file_move",
                {"source": "from.txt", "destination": "to.txt"},
            ),
            ("from.txt", "to.txt"),
        )
        self.assertEqual(
            file_mutation_paths("file_delete", {"path": "gone.txt"}),
            ("gone.txt",),
        )
        effects: list[str] = []

        async def owner_effect() -> dict[str, object]:
            effects.append("owner")
            return {"success": True}

        owner = await RuntimeCompanyControllerToolFence(store=self.store).run(
            task=self.owner_task,
            tool_name="apply_patch",
            arguments={"patch": patch},
            effect=owner_effect,
        )
        self.assertTrue(owner["success"])

        async def foreign_effect() -> dict[str, object]:
            effects.append("foreign")
            return {"success": True}

        foreign = await RuntimeCompanyControllerToolFence(store=self.store).run(
            task=self.foreign_task,
            tool_name="apply_patch",
            arguments={"patch": patch},
            effect=foreign_effect,
        )
        deleted_recreate = await RuntimeCompanyControllerToolFence(
            store=self.store
        ).run(
            task=self.foreign_task,
            tool_name="file_write",
            arguments={"path": "deleted.txt", "content": "foreign"},
            effect=foreign_effect,
        )
        self.assertFalse(foreign["success"])
        self.assertFalse(deleted_recreate["success"])
        self.assertEqual(effects, ["owner"])

    async def test_concurrent_first_mutation_has_one_durable_winner(self) -> None:
        second_store = OPCStore(self.db_path)
        await second_store.initialize(run_startup_maintenance=False)
        try:
            owner_result, foreign_result = await asyncio.gather(
                self._claim(self.owner_task, "race.txt"),
                self._claim(
                    self.foreign_task,
                    "race.txt",
                    store=second_store,
                ),
            )
        finally:
            await second_store.close()

        self.assertEqual(
            sorted((owner_result.outcome, foreign_result.outcome)),
            ["claimed", "conflict"],
        )

    async def test_failed_effect_keeps_reservation_for_retry_and_restart(self) -> None:
        fence = RuntimeCompanyControllerToolFence(store=self.store)
        effects: list[str] = []

        async def failed_effect() -> dict[str, object]:
            effects.append("owner-failed")
            return {"success": False, "error": "simulated write failure"}

        failed = await fence.run(
            task=self.owner_task,
            tool_name="file_write",
            arguments={"path": "reserved.txt", "content": "first"},
            effect=failed_effect,
        )
        self.assertFalse(failed["success"])
        self.assertEqual(effects, ["owner-failed"])

        async def forbidden_effect() -> dict[str, object]:
            effects.append("foreign-ran")
            return {"success": True}

        foreign = await fence.run(
            task=self.foreign_task,
            tool_name="file_edit",
            arguments={
                "path": "reserved.txt",
                "old_string": "first",
                "new_string": "second",
            },
            effect=forbidden_effect,
        )
        self.assertFalse(foreign["success"])
        self.assertEqual(
            foreign["artifact_ownership"]["conflicting_work_item_id"],
            "wi-owner",
        )
        self.assertEqual(effects, ["owner-failed"])

        await self.store.close()
        self.store = OPCStore(self.db_path)
        await self.store.initialize(run_startup_maintenance=False)
        restarted_fence = RuntimeCompanyControllerToolFence(store=self.store)

        async def retry_effect() -> dict[str, object]:
            effects.append("owner-retry")
            return {"success": True}

        retry = await restarted_fence.run(
            task=self.owner_task,
            tool_name="file_write",
            arguments={"path": "reserved.txt", "content": "second"},
            effect=retry_effect,
        )
        self.assertTrue(retry["success"])
        self.assertEqual(effects, ["owner-failed", "owner-retry"])

    async def test_stable_work_item_accepts_replacement_runtime_task(self) -> None:
        self.assertTrue(
            (await self._claim(self.owner_task, "durable.md")).claimed
        )
        await self.store.update_task_status(self.owner_task.id, TaskStatus.DONE)
        assert self.store._db is not None
        await self.store._db.execute(
            """UPDATE delegation_work_items
               SET phase = 'ready',
                   claimed_by_role_runtime_session_id = '',
                   claimed_by_seat_id = '',
                   metadata = json_remove(metadata, '$.claimed_task_id')
               WHERE work_item_id = ?""",
            ("wi-owner",),
        )
        await self.store._db.commit()
        replacement = Task(
            id="task-owner-retry",
            session_id="root-session",
            project_id="project-1",
            title="retry",
            assigned_to="analyst",
            metadata={
                "delegation_run_id": "run-1",
                "workspace_root": str(self.root),
                "delegation_role_session_id": "role-session-wi-owner",
                "delegation_seat_id": "seat-wi-owner",
            },
        )
        await self.store.save_task(replacement)
        self.assertTrue(
            await self.store.link_work_item_runtime_task(
                "wi-owner",
                replacement.id,
                allow_replace=True,
            )
        )
        set_linked_work_item_id(replacement, "wi-owner")
        claimed = await self.store.claim_delegation_work_item_if_dispatchable(
            "wi-owner",
            expected_phase="ready",
            role_runtime_session_id="role-session-wi-owner",
            seat_id="seat-wi-owner",
            task_id=replacement.id,
            controller_owner_token="controller-1",
            controller_lease_generation=self.generation,
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        replacement.metadata.update(
            {
                "company_run_controller_owner_token": "controller-1",
                "company_run_controller_lease_generation": self.generation,
                "claimed_work_item_attempt_seq": int(
                    claimed.metadata.get("attempt_seq", 0) or 0
                ),
            }
        )
        await self.store.save_task(replacement)

        retried = await self._claim(replacement, "durable.md")
        self.assertTrue(retried.claimed)
        assert self.store._db is not None
        async with self.store._db.execute(
            """SELECT owner_work_item_id, owner_runtime_task_id
               FROM company_artifact_ownership
               WHERE project_id = ? AND run_id = ? AND path_key = ?""",
            ("project-1", "run-1", "durable.md"),
        ) as cursor:
            row = await cursor.fetchone()
        self.assertEqual(row, ("wi-owner", replacement.id))

    async def test_case_and_unicode_aliases_have_one_canonical_owner_key(self) -> None:
        first = await self._claim(self.owner_task, "Reports/Caf\u00e9.JSON")
        blocked = await self._claim(
            self.foreign_task,
            "reports/Cafe\u0301.json",
        )

        self.assertEqual(first.path_keys, ("reports/caf\u00e9.json",))
        self.assertEqual(blocked.outcome, "conflict")
        self.assertEqual(blocked.conflicting_path, "reports/caf\u00e9.json")

        duplicate_aliases = await self._claim(
            self.owner_task,
            "Reports/Foo.txt",
            "reports/foo.TXT",
        )
        self.assertEqual(duplicate_aliases.path_keys, ("reports/foo.txt",))

    async def test_durable_run_root_rejects_outside_and_mismatched_task_roots(self) -> None:
        outside = await self._claim(self.owner_task, str(self.root.parent / "outside.txt"))
        self.assertEqual(outside.outcome, "invalid_path")

        durable_task = await self.store.get_task(self.owner_task.id)
        assert durable_task is not None
        durable_task.metadata["workspace_root"] = str(self.root / "different")
        await self.store.save_task(durable_task)
        mismatch = await self._claim(self.owner_task, "inside.txt")
        self.assertEqual(mismatch.outcome, "invalid_path")
        self.assertIn("does not match", mismatch.reason)

    async def test_missing_durable_run_root_fails_closed(self) -> None:
        assert self.store._db is not None
        await self.store._db.execute(
            "UPDATE delegation_runs SET metadata = '{}' WHERE run_id = ?",
            ("run-1",),
        )
        await self.store._db.commit()

        result = await self._claim(self.owner_task, "inside.txt")

        self.assertEqual(result.outcome, "invalid_path")
        self.assertIn("run has no workspace root", result.reason)

    async def test_in_memory_execution_context_cannot_redirect_the_effect(self) -> None:
        redirected = self.root.parent / "redirected"
        redirected.mkdir()
        self.owner_task.metadata["_execution_context"] = {
            "workspace_root": str(redirected),
            "output_root": str(redirected),
        }
        effects: list[str] = []

        async def effect() -> dict[str, object]:
            effects.append("ran")
            return {"success": True}

        result = await RuntimeCompanyControllerToolFence(store=self.store).run(
            task=self.owner_task,
            tool_name="file_write",
            arguments={"path": "redirected.txt", "content": "blocked"},
            effect=effect,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["artifact_ownership"]["outcome"], "invalid_path")
        self.assertEqual(effects, [])

    async def test_malformed_apply_patch_is_structured_failure_before_effect(self) -> None:
        effects: list[str] = []

        async def effect() -> dict[str, object]:
            effects.append("ran")
            return {"success": True}

        result = await RuntimeCompanyControllerToolFence(store=self.store).run(
            task=self.owner_task,
            tool_name="apply_patch",
            arguments={"patch": "this is not an apply patch"},
            effect=effect,
        )

        self.assertFalse(result["success"])
        self.assertEqual(result["artifact_ownership"]["outcome"], "invalid_path")
        self.assertEqual(effects, [])

    async def test_company_task_missing_controller_fields_never_runs_handler(self) -> None:
        self.owner_task.metadata.pop("company_run_controller_owner_token", None)
        self.owner_task.metadata.pop("company_run_controller_lease_generation", None)
        effects: list[str] = []

        async def effect() -> dict[str, object]:
            effects.append("ran")
            return {"success": True}

        with self.assertRaises(CompanyRunControllerLeaseLost):
            await RuntimeCompanyControllerToolFence(store=self.store).run(
                task=self.owner_task,
                tool_name="file_write",
                arguments={"path": "missing-credential.txt", "content": "blocked"},
                effect=effect,
            )
        self.assertEqual(effects, [])

    async def test_legacy_company_markers_without_credentials_never_run_handler(self) -> None:
        marker_payloads = (
            {"work_item_projection_id": "legacy-projection"},
            {"company_work_item_plan": {"items": []}},
            {"company_profile": "legacy-company"},
        )
        for index, metadata in enumerate(marker_payloads):
            with self.subTest(metadata=metadata):
                effects: list[str] = []

                async def effect() -> dict[str, object]:
                    effects.append("ran")
                    return {"success": True}

                task = Task(
                    id=f"legacy-company-{index}",
                    project_id="project-1",
                    metadata=metadata,
                )
                with self.assertRaises(CompanyRunControllerLeaseLost):
                    await RuntimeCompanyControllerToolFence(store=self.store).run(
                        task=task,
                        tool_name="shell_exec",
                        arguments={"command": "touch must-not-run"},
                        tool_call={"id": f"legacy-call-{index}"},
                        effect=effect,
                    )
                self.assertEqual(effects, [])

    async def test_company_opaque_effect_needs_exact_permit_and_consumes_it_once(self) -> None:
        fence = RuntimeCompanyControllerToolFence(store=self.store)
        effects: list[str] = []

        async def effect() -> dict[str, object]:
            effects.append("ran")
            return {"success": True}

        blocked = await fence.run(
            task=self.owner_task,
            tool_name="shell_exec",
            arguments={"command": "sed -i s/a/b/ artifact.txt"},
            tool_call={"id": "call-unapproved"},
            effect=effect,
        )
        self.assertFalse(blocked["success"])
        self.assertEqual(blocked["opaque_tool_permission"]["outcome"], "invalid_permit")
        self.assertEqual(effects, [])

        blocked_python = await fence.run(
            task=self.owner_task,
            tool_name="python_exec",
            arguments={"code": "open('artifact.txt', 'w').write('x')"},
            tool_call={"id": "call-python-unapproved"},
            effect=effect,
        )
        self.assertFalse(blocked_python["success"])
        self.assertEqual(effects, [])

        read_only = await fence.run(
            task=self.owner_task,
            tool_name="shell_exec",
            arguments={"command": "git status --short"},
            tool_call={"id": "call-read"},
            effect=effect,
        )
        self.assertTrue(read_only["success"])
        self.assertEqual(effects, ["ran"])

        outside_file = self.root.parent / "outside.txt"
        outside_file.write_text("outside", encoding="utf-8")
        outside_read = await fence.run(
            task=self.owner_task,
            tool_name="shell_exec",
            arguments={"command": f"cat {outside_file}"},
            tool_call={"id": "call-outside-read"},
            effect=effect,
        )
        self.assertFalse(outside_read["success"])
        self.assertEqual(
            outside_read["opaque_tool_permission"]["outcome"],
            "invalid_permit",
        )
        self.assertEqual(effects, ["ran"])

        arguments = {"command": "touch artifact.txt"}
        permit = await self._executing_permit(
            self.owner_task,
            call_id="call-approved",
            tool_name="shell_exec",
            arguments=arguments,
            runtime_session_id="rt-approved",
        )
        call = {
            "id": "call-approved",
            "function": "shell_exec",
            "arguments": arguments,
            "_approval_permit": permit,
        }
        approved = await fence.run(
            task=self.owner_task,
            tool_name="shell_exec",
            arguments=arguments,
            tool_call=call,
            effect=effect,
        )
        replay = await fence.run(
            task=self.owner_task,
            tool_name="shell_exec",
            arguments=arguments,
            tool_call=call,
            effect=effect,
        )

        self.assertTrue(approved["success"])
        self.assertFalse(replay["success"])
        self.assertEqual(replay["opaque_tool_permission"]["outcome"], "already_consumed")
        self.assertEqual(effects, ["ran", "ran"])

    async def test_company_shell_wrappers_cannot_bypass_exact_approval(self) -> None:
        fence = RuntimeCompanyControllerToolFence(store=self.store)
        effects: list[str] = []

        async def effect() -> dict[str, object]:
            effects.append("ran")
            return {"success": True}

        for tool_name, arguments in (
            ("git_status", {"working_directory": str(self.root)}),
            ("git_diff", {"working_directory": str(self.root)}),
            (
                "git_commit",
                {"message": "unsafe", "working_directory": str(self.root)},
            ),
            (
                "git_clone",
                {"url": "https://example.invalid/repo", "directory": str(self.root)},
            ),
        ):
            with self.subTest(tool_name=tool_name):
                result = await fence.run(
                    task=self.owner_task,
                    tool_name=tool_name,
                    arguments=arguments,
                    tool_call={"id": f"call-{tool_name}"},
                    tool_effect_kind=COMPANY_EFFECT_FORBIDDEN,
                    effect=effect,
                )
                self.assertFalse(result["success"])
                self.assertEqual(
                    result["opaque_tool_permission"]["outcome"],
                    "forbidden_company_effect",
                )
        self.assertEqual(effects, [])

        ordinary = Task(
            id="ordinary-git",
            project_id="project-1",
            metadata={"execution_mode": "task_mode"},
        )
        allowed = await fence.run(
            task=ordinary,
            tool_name="git_status",
            arguments={"working_directory": str(self.root)},
            effect=effect,
        )
        self.assertTrue(allowed["success"])
        self.assertEqual(effects, ["ran"])

    async def test_opaque_effect_crash_consumes_permit_before_handler(self) -> None:
        arguments = {"command": "touch crash-once.txt"}
        permit = await self._executing_permit(
            self.owner_task,
            call_id="call-crash-once",
            tool_name="shell_exec",
            arguments=arguments,
            runtime_session_id="rt-crash-once",
        )
        call = {
            "id": "call-crash-once",
            "function": "shell_exec",
            "arguments": arguments,
            "_approval_permit": permit,
        }
        effects: list[str] = []

        async def crashing_effect() -> dict[str, object]:
            effects.append("started")
            raise RuntimeError("simulated handler crash")

        fence = RuntimeCompanyControllerToolFence(store=self.store)
        with self.assertRaisesRegex(RuntimeError, "simulated handler crash"):
            await fence.run(
                task=self.owner_task,
                tool_name="shell_exec",
                arguments=arguments,
                tool_call=call,
                effect=crashing_effect,
            )
        replay = await fence.run(
            task=self.owner_task,
            tool_name="shell_exec",
            arguments=arguments,
            tool_call=call,
            effect=crashing_effect,
        )
        self.assertFalse(replay["success"], replay)
        self.assertEqual(
            replay["opaque_tool_permission"]["outcome"],
            "already_consumed",
        )
        self.assertEqual(effects, ["started"])

    async def test_concurrent_opaque_effect_has_one_durable_winner(self) -> None:
        arguments = {"command": "touch concurrent-once.txt"}
        permit = await self._executing_permit(
            self.owner_task,
            call_id="call-concurrent-once",
            tool_name="shell_exec",
            arguments=arguments,
            runtime_session_id="rt-concurrent-once",
        )
        call = {
            "id": "call-concurrent-once",
            "function": "shell_exec",
            "arguments": arguments,
            "_approval_permit": permit,
        }
        effects: list[str] = []

        async def effect() -> dict[str, object]:
            effects.append("ran")
            await asyncio.sleep(0)
            return {"success": True}

        fence = RuntimeCompanyControllerToolFence(store=self.store)
        results = await asyncio.gather(
            *(
                fence.run(
                    task=self.owner_task,
                    tool_name="shell_exec",
                    arguments=arguments,
                    tool_call=call,
                    effect=effect,
                )
                for _ in range(2)
            )
        )
        self.assertEqual(sum(bool(result["success"]) for result in results), 1)
        loser = next(result for result in results if not result["success"])
        self.assertEqual(
            loser["opaque_tool_permission"]["outcome"],
            "already_consumed",
        )
        self.assertEqual(effects, ["ran"])

    async def test_registered_unfenced_effects_default_deny_for_company(self) -> None:
        self.assertTrue(
            all(
                definition.company_effect_kind != COMPANY_EFFECT_UNKNOWN
                for definition in (*create_git_tools(), *create_browser_tools())
            )
        )
        registry = ToolRegistry()
        effects: list[str] = []

        async def handler(**_kwargs: object) -> dict[str, object]:
            effects.append("ran")
            return {"success": True}

        definitions = (
            ToolDefinition(
                "git_status",
                "",
                {"type": "object"},
                handler,
                company_effect_kind=COMPANY_EFFECT_FORBIDDEN,
            ),
            ToolDefinition(
                "git_diff",
                "",
                {"type": "object"},
                handler,
                company_effect_kind=COMPANY_EFFECT_FORBIDDEN,
            ),
            ToolDefinition(
                "git_commit",
                "",
                {"type": "object"},
                handler,
                company_effect_kind=COMPANY_EFFECT_FORBIDDEN,
            ),
            ToolDefinition(
                "git_clone",
                "",
                {"type": "object"},
                handler,
                company_effect_kind=COMPANY_EFFECT_FORBIDDEN,
            ),
            ToolDefinition(
                "browser_snapshot",
                "",
                {"type": "object"},
                handler,
                category="browser",
                company_effect_kind=COMPANY_EFFECT_FORBIDDEN,
            ),
            ToolDefinition(
                "browser_take_screenshot",
                "",
                {"type": "object"},
                handler,
                category="browser",
                company_effect_kind=COMPANY_EFFECT_FORBIDDEN,
            ),
            ToolDefinition(
                "remote_arbitrary_effect",
                "",
                {"type": "object"},
                handler,
                category="mcp",
            ),
            ToolDefinition(
                "new_unknown_writer",
                "",
                {"type": "object"},
                handler,
            ),
        )
        for definition in definitions:
            registry.register(definition)
        executor = StreamingToolExecutor(
            registry=registry,
            planner=ToolPlanner(registry),
            permission_resolver=RuntimePermissionAdapter(),
            controller_tool_fence=RuntimeCompanyControllerToolFence(
                store=self.store
            ),
        )
        for definition in registry.list_tools():
            arguments = (
                {"filename": "snapshot.md"}
                if definition.name == "browser_snapshot"
                else {}
            )
            with self.subTest(tool=definition.name):
                result = await executor._invoke_tool_effect(
                    tool_name=definition.name,
                    arguments=arguments,
                    task=self.owner_task,
                    on_progress=None,
                    call={"id": f"call-{definition.name}"},
                )
                self.assertFalse(result["success"], result)
        self.assertEqual(effects, [])

        ordinary = Task(
            id="ordinary-unknown-tool",
            project_id="project-1",
            metadata={"execution_mode": "task_mode"},
        )
        ordinary_result = await executor._invoke_tool_effect(
            tool_name="new_unknown_writer",
            arguments={},
            task=ordinary,
            on_progress=None,
            call={"id": "ordinary-unknown"},
        )
        self.assertTrue(ordinary_result["success"], ordinary_result)
        self.assertEqual(effects, ["ran"])

    async def test_runtime_internal_namespace_never_claims_or_runs_model_mutation(self) -> None:
        fence = RuntimeCompanyControllerToolFence(store=self.store)
        effects: list[str] = []

        async def effect() -> dict[str, object]:
            effects.append("ran")
            return {"success": True}

        internal = ".opc-comms/tool-results/task/result.txt"
        patches = (
            "\n".join(
                (
                    "*** Begin Patch",
                    f"*** Delete File: {internal}",
                    "*** End Patch",
                )
            ),
            "\n".join(
                (
                    "*** Begin Patch",
                    f"*** Update File: {internal}",
                    "*** Move to: stolen.txt",
                    "@@",
                    "-old",
                    "+new",
                    "*** End Patch",
                )
            ),
        )
        cases = (
            ("file_write", {"path": internal, "content": "bad"}),
            (
                "file_edit",
                {"path": internal, "old_string": "old", "new_string": "bad"},
            ),
            ("apply_patch", {"patch": patches[0]}),
            ("apply_patch", {"patch": patches[1]}),
        )
        for index, (tool_name, arguments) in enumerate(cases):
            with self.subTest(tool=tool_name, index=index):
                result = await fence.run(
                    task=self.owner_task,
                    tool_name=tool_name,
                    arguments=arguments,
                    effect=effect,
                )
                self.assertFalse(result["success"], result)
                self.assertEqual(
                    result["artifact_ownership"]["outcome"],
                    "invalid_path",
                )
        self.assertEqual(effects, [])
        assert self.store._db is not None
        async with self.store._db.execute(
            "SELECT COUNT(*) FROM company_artifact_ownership",
        ) as cursor:
            count = await cursor.fetchone()
        self.assertEqual(count[0], 0)

    async def test_sort_compress_program_requires_exact_permit(self) -> None:
        fence = RuntimeCompanyControllerToolFence(store=self.store)
        arguments = {
            "command": "sort --compress-program=/bin/sh -S 1K payload",
        }
        effects: list[str] = []

        async def effect() -> dict[str, object]:
            effects.append("ran")
            return {"success": True}

        blocked_commands = (
            arguments["command"],
            "sort -T scratch payload",
            "sort -rTscratch payload",
            "sort --temporary-directory=scratch payload",
            "file -C -m test.magic",
            "file -bCm test.magic",
            "file --compile -m test.magic",
            "date -us '2020-01-01'",
            "tree -ao tree.txt .",
        )
        for index, command in enumerate(blocked_commands):
            with self.subTest(command=command):
                blocked = await fence.run(
                    task=self.owner_task,
                    tool_name="shell_exec",
                    arguments={"command": command},
                    tool_call={"id": f"call-unsafe-read-{index}"},
                    effect=effect,
                )
                self.assertFalse(blocked["success"])
                self.assertEqual(
                    blocked["opaque_tool_permission"]["outcome"],
                    "invalid_permit",
                )
        self.assertEqual(effects, [])

        permit = await self._executing_permit(
            self.owner_task,
            call_id="call-sort-approved",
            tool_name="shell_exec",
            arguments=arguments,
            runtime_session_id="rt-sort-approved",
        )
        approved = await fence.run(
            task=self.owner_task,
            tool_name="shell_exec",
            arguments=arguments,
            tool_call={
                "id": "call-sort-approved",
                "function": "shell_exec",
                "arguments": arguments,
                "_approval_permit": permit,
            },
            effect=effect,
        )
        self.assertTrue(approved["success"])
        self.assertEqual(effects, ["ran"])

    async def test_opaque_permit_rejects_call_argument_and_work_item_transplants(self) -> None:
        fence = RuntimeCompanyControllerToolFence(store=self.store)
        arguments = {"command": "touch owned-by-exact-call.txt"}
        permit = await self._executing_permit(
            self.owner_task,
            call_id="call-transplant",
            tool_name="shell_exec",
            arguments=arguments,
            runtime_session_id="rt-transplant",
        )
        effects: list[str] = []

        async def effect() -> dict[str, object]:
            effects.append("ran")
            return {"success": True}

        for task, call_id, call_arguments in (
            (self.owner_task, "wrong-call-id", arguments),
            (self.owner_task, "call-transplant", {"command": "touch foreign.txt"}),
            (self.foreign_task, "call-transplant", arguments),
        ):
            blocked = await fence.run(
                task=task,
                tool_name="shell_exec",
                arguments=call_arguments,
                tool_call={
                    "id": call_id,
                    "function": "shell_exec",
                    "arguments": call_arguments,
                    "_approval_permit": permit,
                },
                effect=effect,
            )
            self.assertFalse(blocked["success"])
            self.assertEqual(effects, [])

        valid = await fence.run(
            task=self.owner_task,
            tool_name="shell_exec",
            arguments=arguments,
            tool_call={
                "id": "call-transplant",
                "function": "shell_exec",
                "arguments": arguments,
                "_approval_permit": permit,
            },
            effect=effect,
        )
        self.assertTrue(valid["success"])
        self.assertEqual(effects, ["ran"])

    async def test_opaque_permit_requires_approve_once_and_live_claim(self) -> None:
        fence = RuntimeCompanyControllerToolFence(store=self.store)
        effects: list[str] = []

        async def effect() -> dict[str, object]:
            effects.append("ran")
            return {"success": True}

        reusable_arguments = {"command": "touch reusable.txt"}
        reusable = await self._executing_permit(
            self.owner_task,
            call_id="call-reusable",
            tool_name="shell_exec",
            arguments=reusable_arguments,
            runtime_session_id="rt-reusable",
            decision="approve_session",
        )
        reusable_result = await fence.run(
            task=self.owner_task,
            tool_name="shell_exec",
            arguments=reusable_arguments,
            tool_call={
                "id": "call-reusable",
                "_approval_permit": reusable,
            },
            effect=effect,
        )
        self.assertFalse(reusable_result["success"])

        expired_arguments = {"command": "touch expired.txt"}
        expired = await self._executing_permit(
            self.owner_task,
            call_id="call-expired",
            tool_name="shell_exec",
            arguments=expired_arguments,
            runtime_session_id="rt-reusable",
        )
        assert self.store._db is not None
        await self.store._db.execute(
            """UPDATE execution_checkpoints
               SET payload = json_set(
                   payload,
                   '$.interaction.claim.lease_expires_at',
                   '2000-01-01T00:00:00'
               )
               WHERE checkpoint_id = ?""",
            (expired["checkpoint_id"],),
        )
        await self.store._db.commit()
        expired_result = await fence.run(
            task=self.owner_task,
            tool_name="shell_exec",
            arguments=expired_arguments,
            tool_call={
                "id": "call-expired",
                "_approval_permit": expired,
            },
            effect=effect,
        )
        self.assertFalse(expired_result["success"])
        self.assertEqual(effects, [])

    async def test_sandbox_retry_cannot_execute_one_shot_opaque_permit_twice(self) -> None:
        registry = ToolRegistry()
        observed_modes: list[str] = []

        async def shell_tool(
            command: str,
            task: Task | None = None,
        ) -> dict[str, object]:
            _ = command
            execution_context = dict(
                (getattr(task, "metadata", {}) or {}).get(
                    "_execution_context", {}
                )
                or {}
            )
            sandbox = dict(execution_context.get("sandbox", {}) or {})
            mode = str(sandbox.get("mode", "") or "off")
            observed_modes.append(mode)
            return {
                "stdout": "",
                "stderr": "sandbox unavailable",
                "exit_code": 1,
                "timed_out": False,
                "sandbox": {
                    "platform": "linux",
                    "requested_mode": mode,
                    "effective_mode": mode,
                    "available": False,
                    "fallback_used": False,
                },
            }

        registry.register(
            ToolDefinition(
                name="shell_exec",
                description="shell",
                parameters={"type": "object", "properties": {}},
                func=shell_tool,
                requires_confirmation=True,
                concurrency_safe=False,
                read_only=False,
                company_effect_kind=COMPANY_EFFECT_OPAQUE_EXACT,
            )
        )
        arguments = {"command": "touch sandbox-output.txt"}
        self.owner_task.metadata["_execution_context"] = {
            "workspace_root": str(self.root),
            "output_root": str(self.root),
            "sandbox": {
                "platform": "linux",
                "mode": "workspace-write",
                "allow_network": True,
            },
        }
        await self.store.save_task(self.owner_task)
        persisted_owner = await self.store.get_task(self.owner_task.id)
        assert persisted_owner is not None
        self.owner_task.context_snapshot = dict(
            persisted_owner.context_snapshot or {}
        )
        self.owner_task.metadata = dict(persisted_owner.metadata or {})
        permit = await self._executing_permit(
            self.owner_task,
            call_id="call-sandbox-once",
            tool_name="shell_exec",
            arguments=arguments,
            runtime_session_id="rt-sandbox-once",
        )
        replay_plan = build_company_opaque_execution_plan(
            self.owner_task,
            "shell_exec",
            arguments,
        )
        self.assertEqual(
            permit["fingerprint"],
            exact_tool_call_fingerprint(
                tool_call_id="call-sandbox-once",
                tool_name="shell_exec",
                arguments=arguments,
                runtime_session_id="rt-sandbox-once",
                execution_envelope=replay_plan.envelope,
                execution_identity=company_opaque_execution_identity(
                    self.owner_task
                ),
            ),
            {
                "approved": permit["execution_envelope"],
                "replayed": replay_plan.envelope,
            },
        )
        executor = StreamingToolExecutor(
            registry=registry,
            planner=ToolPlanner(registry),
            permission_resolver=RuntimePermissionAdapter(
                guardian=type(
                    "Guardian",
                    (),
                    {"auto_retry_sandbox": True},
                )()
            ),
            controller_tool_fence=RuntimeCompanyControllerToolFence(
                store=self.store
            ),
        )
        results = await executor.execute(
            [
                {
                    "id": "call-sandbox-once",
                    "function": "shell_exec",
                    "arguments": arguments,
                    "_approval_permit": permit,
                }
            ],
            task=self.owner_task,
        )

        self.assertFalse(results[0]["result"]["success"])
        self.assertEqual(
            results[0]["result"]["opaque_tool_permission"]["outcome"],
            "already_consumed",
            results,
        )
        self.assertEqual(observed_modes, ["workspace-write"])

    async def test_company_opaque_envelope_tampering_never_reaches_handler(self) -> None:
        alternate_root = self.root / "alternate"
        alternate_root.mkdir()
        cases = (
            ("prefix", "shell_exec", {"command": "printf safe"}),
            ("environment", "shell_exec", {"command": "printf safe"}),
            ("sandbox", "shell_exec", {"command": "printf safe"}),
            ("cwd", "shell_exec", {"command": "printf safe"}),
            ("python_executable", "python_exec", {"code": "print('safe')"}),
        )
        for index, (kind, tool_name, arguments) in enumerate(cases, 1):
            with self.subTest(kind=kind):
                durable = await self.store.get_task(self.owner_task.id)
                assert durable is not None
                self.owner_task.context_snapshot = copy.deepcopy(
                    durable.context_snapshot or {}
                )
                self.owner_task.metadata = copy.deepcopy(durable.metadata or {})
                permit = await self._executing_permit(
                    self.owner_task,
                    call_id=f"call-envelope-{index}",
                    tool_name=tool_name,
                    arguments=arguments,
                    runtime_session_id="rt-envelope-tamper",
                )
                metadata = self.owner_task.metadata
                if kind == "prefix":
                    metadata["environment_manifest"] = {
                        "shell_prefix": "printf injected",
                    }
                elif kind == "environment":
                    metadata["environment_manifest"] = {
                        "env_vars": {"OPC_ENVELOPE_TAMPER": "changed"},
                    }
                else:
                    context = dict(metadata.get("_execution_context", {}) or {})
                    context.update(
                        {
                            "workspace_root": str(self.root),
                            "output_root": str(self.root),
                        }
                    )
                    if kind == "sandbox":
                        context["sandbox"] = {
                            "platform": "linux",
                            "mode": "workspace-write",
                            "allow_network": False,
                        }
                    elif kind == "cwd":
                        context["workspace_root"] = str(alternate_root)
                        context["output_root"] = str(alternate_root)
                    else:
                        context["python_executable"] = "/bin/false"
                    metadata["_execution_context"] = context
                effects: list[str] = []

                async def effect() -> dict[str, object]:
                    effects.append(kind)
                    return {"success": True}

                result = await RuntimeCompanyControllerToolFence(
                    store=self.store
                ).run(
                    task=self.owner_task,
                    tool_name=tool_name,
                    arguments=arguments,
                    tool_call={
                        "id": f"call-envelope-{index}",
                        "_approval_permit": permit,
                    },
                    effect=effect,
                )
                self.assertFalse(result["success"], result)
                self.assertEqual(effects, [])

    async def test_frozen_python_uses_exact_stdin_bytes_without_tempfile(self) -> None:
        code = "if True:\n    print('<tag>`literal`')\n"
        plan = build_company_opaque_execution_plan(
            self.owner_task,
            "python_exec",
            {"code": code},
        )
        captured: dict[str, object] = {}

        class Process:
            returncode = 0

            async def communicate(self, input=None):
                captured["input"] = input
                return b"ok\n", b""

        create = AsyncMock(return_value=Process())
        token = install_opaque_execution_plan(plan)
        try:
            with patch(
                "opc.layer4_tools.python_exec.asyncio.create_subprocess_exec",
                create,
            ), patch(
                "opc.layer4_tools.python_exec.tempfile.NamedTemporaryFile",
                side_effect=AssertionError("company Python created a temp file"),
            ):
                result = await python_exec(code, task=self.owner_task)
        finally:
            reset_opaque_execution_plan(token)

        self.assertTrue(result["success"], result)
        self.assertEqual(captured["input"], code.encode("utf-8"))
        self.assertEqual(
            hashlib.sha256(captured["input"]).hexdigest(),
            plan.envelope["python_code_sha256"],
        )
        argv = create.await_args.args
        self.assertIn("-I", argv)
        self.assertIn("-c", argv)
        bootstrap = str(argv[argv.index("-c") + 1])
        self.assertIn(repr(str(self.root)), bootstrap)
        self.assertIn("sys.stdin.buffer.read()", bootstrap)
        self.assertEqual(list(self.root.glob("*.py")), [])

    async def test_frozen_python_can_import_workspace_module(self) -> None:
        (self.root / "approved_local_module.py").write_text(
            "VALUE = 'local-import-ok'\n",
            encoding="utf-8",
        )
        code = "import approved_local_module; print(approved_local_module.VALUE)"
        plan = build_company_opaque_execution_plan(
            self.owner_task,
            "python_exec",
            {"code": code},
        )
        token = install_opaque_execution_plan(plan)
        try:
            result = await python_exec(code, task=self.owner_task)
        finally:
            reset_opaque_execution_plan(token)

        self.assertTrue(result["success"], result)
        self.assertEqual(result["stdout"], "local-import-ok\n")
        self.assertEqual(list(self.root.glob("*.py")), [
            self.root / "approved_local_module.py"
        ])

    async def test_frozen_binary_wrapper_and_cwd_identity_tamper_block_spawn(self) -> None:
        fake_python = self.root / "python-copy"
        shutil.copy2(sys.executable, fake_python)
        fake_python.chmod(0o755)
        python_task = copy.deepcopy(self.owner_task)
        python_task.metadata["_execution_context"] = {
            "workspace_root": str(self.root),
            "output_root": str(self.root),
            "python_executable": str(fake_python),
        }
        python_plan = build_company_opaque_execution_plan(
            python_task,
            "python_exec",
            {"code": "print('safe')"},
        )
        fake_python.write_bytes(b"changed")
        token = install_opaque_execution_plan(python_plan)
        try:
            with patch(
                "opc.layer4_tools.python_exec.asyncio.create_subprocess_exec",
                side_effect=AssertionError("changed binary was spawned"),
            ):
                python_result = await python_exec("print('safe')")
        finally:
            reset_opaque_execution_plan(token)
        self.assertIn("executable changed", python_result["error"])

        wrapper = self.root / "wrapper-copy"
        shutil.copy2("/bin/sh", wrapper)
        wrapper.chmod(0o755)

        def wrap(args, *, cwd, context):
            _ = cwd, context
            return [str(wrapper), *args], {
                "platform": "linux",
                "requested_mode": "workspace-write",
                "effective_mode": "workspace-write",
                "available": True,
                "fallback_used": False,
            }

        with patch(
            "opc.layer4_tools.opaque_execution.wrap_command_for_context",
            side_effect=wrap,
        ):
            shell_plan = build_company_opaque_execution_plan(
                self.owner_task,
                "shell_exec",
                {"command": "printf safe"},
            )
        wrapper.write_bytes(b"changed")
        token = install_opaque_execution_plan(shell_plan)
        try:
            with patch(
                "opc.layer4_tools.shell.asyncio.create_subprocess_exec",
                side_effect=AssertionError("changed wrapper was spawned"),
            ):
                shell_result = await shell_exec("printf safe")
        finally:
            reset_opaque_execution_plan(token)
        self.assertIn("executable changed", shell_result["error"])

        cwd = self.root / "approved-cwd"
        cwd.mkdir()
        cwd_plan = build_company_opaque_execution_plan(
            self.owner_task,
            "shell_exec",
            {"command": "printf safe", "working_directory": str(cwd)},
        )
        moved = self.root / "original-cwd"
        cwd.rename(moved)
        cwd.mkdir()
        token = install_opaque_execution_plan(cwd_plan)
        try:
            with patch(
                "opc.layer4_tools.shell.asyncio.create_subprocess_exec",
                side_effect=AssertionError("replacement cwd was used"),
            ):
                cwd_result = await shell_exec("printf safe")
        finally:
            reset_opaque_execution_plan(token)
        self.assertIn("working directory changed", cwd_result["error"])

    async def test_opaque_execution_contextvar_is_task_local(self) -> None:
        first = build_company_opaque_execution_plan(
            self.owner_task,
            "shell_exec",
            {"command": "printf first"},
        )
        second = build_company_opaque_execution_plan(
            self.owner_task,
            "shell_exec",
            {"command": "printf second"},
        )
        ready = asyncio.Event()
        arrivals = 0

        async def observe(plan):
            nonlocal arrivals
            token = install_opaque_execution_plan(plan)
            try:
                arrivals += 1
                if arrivals == 2:
                    ready.set()
                await ready.wait()
                await asyncio.sleep(0)
                current = current_opaque_execution_plan("shell_exec")
                assert current is not None
                return current.effective_command
            finally:
                reset_opaque_execution_plan(token)

        observed = await asyncio.gather(observe(first), observe(second))
        self.assertEqual(observed, ["printf first", "printf second"])

    def test_opaque_payload_limit_fails_closed(self) -> None:
        with self.assertRaises(OpaqueExecutionEnvelopeError):
            build_company_opaque_execution_plan(
                self.owner_task,
                "shell_exec",
                {"command": "x" * 16_001},
            )
        with self.assertRaises(OpaqueExecutionEnvelopeError):
            build_company_opaque_execution_plan(
                self.owner_task,
                "python_exec",
                {"code": "x" * 16_001},
            )

    async def _record_verification_success(
        self,
        *,
        command: str,
        call_id: str,
        runtime_session_id: str,
        task: Task | None = None,
    ) -> dict[str, object]:
        target_task = task or self.owner_task
        arguments = {"command": command}
        permit = await self._executing_permit(
            target_task,
            call_id=call_id,
            tool_name="shell_exec",
            arguments=arguments,
            runtime_session_id=runtime_session_id,
        )
        result = await RuntimeCompanyControllerToolFence(
            store=self.store
        ).run(
            task=target_task,
            tool_name="shell_exec",
            arguments=arguments,
            tool_call={"id": call_id, "_approval_permit": permit},
            effect=lambda: asyncio.sleep(0, result={"success": True}),
        )
        self.assertTrue(result["success"], result)
        identity = {
            "project_id": "project-1",
            "delegation_run_id": "run-1",
            "work_item_id": linked_work_item_id_for_task(target_task),
            "runtime_task_id": target_task.id,
            "claimed_work_item_attempt_seq": int(
                target_task.metadata["claimed_work_item_attempt_seq"]
            ),
            "company_opaque_fingerprint": str(permit["fingerprint"]),
            "company_opaque_execution_envelope_digest": (
                opaque_execution_envelope_digest(
                    dict(permit["execution_envelope"])
                )
            ),
        }
        await self.store.save_runtime_tool_call(
            runtime_session_id=runtime_session_id,
            tool_call_id=call_id,
            tool_name="shell_exec",
            arguments=arguments,
            task_id=target_task.id,
            session_id=target_task.session_id,
            metadata=identity,
        )
        await self.store.save_runtime_tool_result(
            runtime_session_id=runtime_session_id,
            tool_call_id=call_id,
            tool_name="shell_exec",
            task_id=target_task.id,
            session_id=target_task.session_id,
            metadata=identity,
            payload={
                "success": True,
                "result": {
                    "success": True,
                    "exit_code": 0,
                    "stdout": "verified\n",
                    "stderr": "",
                },
            },
        )
        return identity

    async def test_automated_verification_requires_exact_current_attempt_evidence(self) -> None:
        command = "python -m pytest -q"
        identity = await self._record_verification_success(
            command=command,
            call_id="call-verify-success",
            runtime_session_id="rt-verify",
        )
        verified = await self.store.company_automated_verification_evidence(
            self.owner_task,
            [command],
        )
        self.assertTrue(verified["verified"], verified)
        self.assertEqual(verified["results"][0]["exit_code"], 0)

        # The runtime ToolCall row is an upsert keyed by session/call id.
        # Replacing its arguments while copying the old identity metadata must
        # not reuse the consumed exact-effect fence.
        await self.store.save_runtime_tool_call(
            runtime_session_id="rt-verify",
            tool_call_id="call-verify-success",
            tool_name="shell_exec",
            arguments={"command": "replacement command"},
            task_id=self.owner_task.id,
            session_id=self.owner_task.session_id,
            metadata=identity,
        )
        replaced = await self.store.company_automated_verification_evidence(
            self.owner_task,
            [command],
        )
        self.assertFalse(replaced["verified"], replaced)

        missing = await self.store.company_automated_verification_evidence(
            self.owner_task,
            ["missing command"],
        )
        self.assertFalse(missing["verified"])

        for suffix, changes in (
            ("foreign", {"work_item_id": "wi-foreign"}),
            ("old", {"claimed_work_item_attempt_seq": 0}),
        ):
            call_id = f"call-verify-{suffix}"
            bad_command = f"verify {suffix}"
            bad_identity = {**identity, **changes}
            await self.store.save_runtime_tool_call(
                runtime_session_id="rt-verify",
                tool_call_id=call_id,
                tool_name="shell_exec",
                arguments={"command": bad_command},
                task_id=self.owner_task.id,
                metadata=bad_identity,
            )
            await self.store.save_runtime_tool_result(
                runtime_session_id="rt-verify",
                tool_call_id=call_id,
                tool_name="shell_exec",
                task_id=self.owner_task.id,
                metadata=bad_identity,
                payload={
                    "success": True,
                    "result": {"success": True, "exit_code": 0},
                },
            )
            rejected = (
                await self.store.company_automated_verification_evidence(
                    self.owner_task,
                    [bad_command],
                )
            )
            self.assertFalse(rejected["verified"], rejected)

    async def test_automated_verification_becomes_stale_after_every_mutation_kind(self) -> None:
        command = "verify artifact"
        identity = await self._record_verification_success(
            command=command,
            call_id="call-verify-stale-base",
            runtime_session_id="rt-verify-stale",
        )
        assert self.store._db is not None
        for index, tool_name in enumerate(
            sorted(FILE_MUTATION_TOOL_NAMES),
            1,
        ):
            with self.subTest(tool_name=tool_name):
                call_id = f"call-stale-{index}"
                await self.store.save_runtime_tool_call(
                    runtime_session_id="rt-verify-stale",
                    tool_call_id=call_id,
                    tool_name=tool_name,
                    arguments={},
                    task_id=self.owner_task.id,
                    metadata=identity,
                )
                stale = (
                    await self.store.company_automated_verification_evidence(
                        self.owner_task,
                        [command],
                    )
                )
                self.assertFalse(stale["verified"], stale)
                await self.store._db.execute(
                    "DELETE FROM runtime_tool_calls WHERE tool_call_id = ?",
                    (call_id,),
                )
                await self.store._db.commit()

        later_arguments = {"command": "printf later"}
        later_permit = await self._executing_permit(
            self.owner_task,
            call_id="call-later-opaque",
            tool_name="shell_exec",
            arguments=later_arguments,
            runtime_session_id="rt-verify-stale",
        )
        later = await RuntimeCompanyControllerToolFence(store=self.store).run(
            task=self.owner_task,
            tool_name="shell_exec",
            arguments=later_arguments,
            tool_call={
                "id": "call-later-opaque",
                "_approval_permit": later_permit,
            },
            effect=lambda: asyncio.sleep(0, result={"success": True}),
        )
        self.assertTrue(later["success"], later)
        stale_opaque = (
            await self.store.company_automated_verification_evidence(
                self.owner_task,
                [command],
            )
        )
        self.assertFalse(stale_opaque["verified"], stale_opaque)

    async def test_every_required_verification_must_follow_last_mutation(self) -> None:
        first_command = "verify first"
        identity = await self._record_verification_success(
            command=first_command,
            call_id="call-verify-first",
            runtime_session_id="rt-verify-order",
        )
        await self.store.save_runtime_tool_call(
            runtime_session_id="rt-verify-order",
            tool_call_id="call-between-checks",
            tool_name="file_write",
            arguments={"path": "changed.txt", "content": "changed"},
            task_id=self.owner_task.id,
            metadata=identity,
        )
        await self.store.save_runtime_tool_result(
            runtime_session_id="rt-verify-order",
            tool_call_id="call-between-checks",
            tool_name="file_write",
            task_id=self.owner_task.id,
            metadata=identity,
            payload={"success": True, "result": {"success": True}},
        )
        second_command = "verify second"
        await self._record_verification_success(
            command=second_command,
            call_id="call-verify-second",
            runtime_session_id="rt-verify-order",
        )

        stale = await self.store.company_automated_verification_evidence(
            self.owner_task,
            [first_command, second_command],
        )
        self.assertFalse(stale["verified"], stale)
        self.assertIn("mutation", stale["reason"])

    async def test_subagent_mutation_invalidates_parent_verification(self) -> None:
        command = "verify before child mutation"
        await self._record_verification_success(
            command=command,
            call_id="call-verify-before-child",
            runtime_session_id="rt-parent-verification",
        )
        child = await self._seed_subagent("agent-verification-stale")
        child.metadata["runtime_v2"] = {
            "runtime_session_id": "rt-child-verification"
        }
        await self.store.save_task(child)
        child_identity = {
            "project_id": "project-1",
            "delegation_run_id": "run-1",
            "work_item_id": "wi-owner",
            "runtime_task_id": child.id,
            "claimed_work_item_attempt_seq": int(
                child.metadata["claimed_work_item_attempt_seq"]
            ),
        }
        await self.store.save_runtime_tool_call(
            runtime_session_id="rt-child-verification",
            tool_call_id="call-child-write-after-check",
            tool_name="file_write",
            arguments={"path": "child.txt", "content": "changed"},
            task_id=child.id,
            metadata=child_identity,
        )
        await self.store.save_runtime_tool_result(
            runtime_session_id="rt-child-verification",
            tool_call_id="call-child-write-after-check",
            tool_name="file_write",
            task_id=child.id,
            metadata=child_identity,
            payload={"success": True, "result": {"success": True}},
        )

        stale = await self.store.company_automated_verification_evidence(
            self.owner_task,
            [command],
        )
        self.assertFalse(stale["verified"], stale)
        self.assertIn("mutation", stale["reason"])

    async def test_verification_rejects_inflight_mutation_started_before_check(self) -> None:
        identity = {
            "project_id": "project-1",
            "delegation_run_id": "run-1",
            "work_item_id": "wi-owner",
            "runtime_task_id": self.owner_task.id,
            "claimed_work_item_attempt_seq": int(
                self.owner_task.metadata["claimed_work_item_attempt_seq"]
            ),
        }
        await self.store.save_runtime_tool_call(
            runtime_session_id="rt-verify-inflight",
            tool_call_id="call-inflight-write",
            tool_name="file_write",
            arguments={"path": "inflight.txt", "content": "inflight"},
            task_id=self.owner_task.id,
            metadata=identity,
        )
        command = "verify after inflight"
        await self._record_verification_success(
            command=command,
            call_id="call-verify-after-inflight",
            runtime_session_id="rt-verify-inflight",
        )

        stale = await self.store.company_automated_verification_evidence(
            self.owner_task,
            [command],
        )
        self.assertFalse(stale["verified"], stale)
        self.assertIn("lacks a successful durable ToolResult", stale["reason"])

    async def test_rejected_mutation_taints_only_its_current_attempt(self) -> None:
        first_identity = {
            "project_id": "project-1",
            "delegation_run_id": "run-1",
            "work_item_id": "wi-owner",
            "runtime_task_id": self.owner_task.id,
            "claimed_work_item_attempt_seq": int(
                self.owner_task.metadata["claimed_work_item_attempt_seq"]
            ),
        }
        await self.store.save_runtime_tool_call(
            runtime_session_id="rt-rejected-attempt-one",
            tool_call_id="call-rejected-mutation",
            tool_name="file_write",
            arguments={"path": "rejected.txt", "content": "blocked"},
            task_id=self.owner_task.id,
            metadata=first_identity,
        )
        await self.store.save_runtime_tool_result(
            runtime_session_id="rt-rejected-attempt-one",
            tool_call_id="call-rejected-mutation",
            tool_name="file_write",
            task_id=self.owner_task.id,
            metadata=first_identity,
            payload={"success": False, "error": "ownership conflict"},
        )
        first_command = "verify first attempt"
        await self._record_verification_success(
            command=first_command,
            call_id="call-verify-first-attempt",
            runtime_session_id="rt-rejected-attempt-one",
        )
        rejected = await self.store.company_automated_verification_evidence(
            self.owner_task,
            [first_command],
        )
        self.assertFalse(rejected["verified"], rejected)

        await self.store.update_task_status(self.owner_task.id, TaskStatus.DONE)
        assert self.store._db is not None
        await self.store._db.execute(
            """UPDATE delegation_work_items
               SET phase = 'ready',
                   claimed_by_role_runtime_session_id = '',
                   claimed_by_seat_id = '',
                   metadata = json_remove(metadata, '$.claimed_task_id')
               WHERE work_item_id = ?""",
            ("wi-owner",),
        )
        await self.store._db.commit()
        replacement = Task(
            id="task-owner-verification-rework",
            session_id="root-session",
            project_id="project-1",
            title="verification rework",
            assigned_to="analyst",
            metadata={
                "delegation_run_id": "run-1",
                "workspace_root": str(self.root),
                "delegation_role_session_id": "role-session-wi-owner",
                "delegation_seat_id": "seat-wi-owner",
            },
        )
        await self.store.save_task(replacement)
        self.assertTrue(
            await self.store.link_work_item_runtime_task(
                "wi-owner",
                replacement.id,
                allow_replace=True,
            )
        )
        set_linked_work_item_id(replacement, "wi-owner")
        claimed = await self.store.claim_delegation_work_item_if_dispatchable(
            "wi-owner",
            expected_phase="ready",
            role_runtime_session_id="role-session-wi-owner",
            seat_id="seat-wi-owner",
            task_id=replacement.id,
            controller_owner_token="controller-1",
            controller_lease_generation=self.generation,
        )
        assert claimed is not None
        replacement.metadata.update(
            {
                "company_run_controller_owner_token": "controller-1",
                "company_run_controller_lease_generation": self.generation,
                "claimed_work_item_attempt_seq": int(
                    claimed.metadata.get("attempt_seq", 0) or 0
                ),
            }
        )
        await self.store.save_task(replacement)
        second_command = "verify clean rework attempt"
        await self._record_verification_success(
            command=second_command,
            call_id="call-verify-rework-attempt",
            runtime_session_id="rt-clean-attempt-two",
            task=replacement,
        )
        recovered = await self.store.company_automated_verification_evidence(
            replacement,
            [second_command],
        )
        self.assertTrue(recovered["verified"], recovered)

    async def test_automated_verification_rejects_settled_attempt(self) -> None:
        command = "verify settled"
        await self._record_verification_success(
            command=command,
            call_id="call-verify-settled",
            runtime_session_id="rt-verify-settled",
        )
        assert self.store._db is not None
        await self.store._db.execute(
            """UPDATE delegation_work_items
               SET phase = 'approved',
                   metadata = json_set(metadata, '$.attempt_settled', 1)
               WHERE work_item_id = 'wi-owner'"""
        )
        await self.store._db.commit()
        rejected = await self.store.company_automated_verification_evidence(
            self.owner_task,
            [command],
        )
        self.assertFalse(rejected["verified"], rejected)

    async def test_approved_or_settled_attempt_cannot_claim_or_execute_late_effect(self) -> None:
        arguments = {"command": "touch late-approved.txt"}
        permit = await self._executing_permit(
            self.owner_task,
            call_id="call-late-approved",
            tool_name="shell_exec",
            arguments=arguments,
            runtime_session_id="rt-late-approved",
        )
        assert self.store._db is not None
        await self.store._db.execute(
            """UPDATE delegation_work_items
               SET phase = 'approved',
                   metadata = json_set(metadata, '$.attempt_settled', 1)
               WHERE work_item_id = ?""",
            ("wi-owner",),
        )
        await self.store._db.commit()

        claim = await self._claim(self.owner_task, "late-approved.json")
        effects: list[str] = []

        async def effect() -> dict[str, object]:
            effects.append("ran")
            return {"success": True}

        opaque = await RuntimeCompanyControllerToolFence(store=self.store).run(
            task=self.owner_task,
            tool_name="shell_exec",
            arguments=arguments,
            tool_call={
                "id": "call-late-approved",
                "_approval_permit": permit,
            },
            effect=effect,
        )
        self.assertEqual(claim.outcome, "invalid_identity")
        self.assertFalse(opaque["success"])
        self.assertEqual(effects, [])

    async def test_ready_released_attempt_cannot_claim_or_execute_late_effect(self) -> None:
        arguments = {"command": "touch late-ready.txt"}
        permit = await self._executing_permit(
            self.owner_task,
            call_id="call-late-ready",
            tool_name="shell_exec",
            arguments=arguments,
            runtime_session_id="rt-late-ready",
        )
        assert self.store._db is not None
        await self.store._db.execute(
            """UPDATE delegation_work_items
               SET phase = 'ready',
                   claimed_by_role_runtime_session_id = '',
                   claimed_by_seat_id = '',
                   metadata = json_remove(metadata, '$.claimed_task_id')
               WHERE work_item_id = ?""",
            ("wi-owner",),
        )
        await self.store._db.commit()

        claim = await self._claim(self.owner_task, "late-ready.json")
        effects: list[str] = []

        async def effect() -> dict[str, object]:
            effects.append("ran")
            return {"success": True}

        opaque = await RuntimeCompanyControllerToolFence(store=self.store).run(
            task=self.owner_task,
            tool_name="shell_exec",
            arguments=arguments,
            tool_call={"id": "call-late-ready", "_approval_permit": permit},
            effect=effect,
        )
        self.assertEqual(claim.outcome, "invalid_identity")
        self.assertFalse(opaque["success"])
        self.assertEqual(effects, [])

    async def test_subagent_old_attempt_or_missing_child_row_cannot_effect(self) -> None:
        missing = await self._seed_subagent(
            "agent-missing-child",
            persist_child=False,
        )
        effects: list[str] = []

        async def effect() -> dict[str, object]:
            effects.append("ran")
            return {"success": True}

        missing_result = await RuntimeCompanyControllerToolFence(
            store=self.store
        ).run(
            task=missing,
            tool_name="file_write",
            arguments={"path": "missing-child.json", "content": "{}"},
            effect=effect,
        )
        self.assertFalse(missing_result["success"])

        child = await self._seed_subagent("agent-old-attempt")
        arguments = {"command": "touch old-child.txt"}
        permit = await self._executing_permit(
            child,
            call_id="call-old-child",
            tool_name="shell_exec",
            arguments=arguments,
            runtime_session_id="rt-old-child",
        )
        assert self.store._db is not None
        await self.store._db.execute(
            """UPDATE delegation_work_items
               SET metadata = json_set(metadata, '$.attempt_seq', 2)
               WHERE work_item_id = ?""",
            ("wi-owner",),
        )
        await self.store._db.execute(
            """UPDATE tasks
               SET metadata = json_set(
                   metadata,
                   '$.claimed_work_item_attempt_seq',
                   2
               )
               WHERE id = ?""",
            (self.owner_task.id,),
        )
        await self.store._db.commit()

        structured = await RuntimeCompanyControllerToolFence(store=self.store).run(
            task=child,
            tool_name="file_write",
            arguments={"path": "old-attempt.json", "content": "{}"},
            effect=effect,
        )
        opaque = await RuntimeCompanyControllerToolFence(store=self.store).run(
            task=child,
            tool_name="shell_exec",
            arguments=arguments,
            tool_call={"id": "call-old-child", "_approval_permit": permit},
            effect=effect,
        )
        self.assertFalse(structured["success"])
        self.assertFalse(opaque["success"])
        self.assertEqual(effects, [])
        async with self.store._db.execute(
            """SELECT COUNT(*) FROM company_artifact_ownership
               WHERE path_key = 'old-attempt.json'"""
        ) as cursor:
            count = await cursor.fetchone()
        self.assertEqual(count[0], 0)

    async def test_shared_native_subagent_claims_for_its_durable_parent_work_item(self) -> None:
        await self.store.save_runtime_subagent_run(
            subagent_run_id="agent-shared",
            runtime_session_id="native-session",
            task_id=self.owner_task.id,
            agent_id="agent-shared",
            profile="implement",
            status="running",
            metadata={"isolation": "shared"},
        )
        child = Task(
            id="child-shared",
            parent_id=self.owner_task.id,
            session_id="child-session",
            project_id="project-1",
            title="shared child",
            assigned_to="analyst",
            metadata={
                **dict(self.owner_task.metadata),
                "_comms_endpoint_id": "agent-shared",
                "_company_parent_work_item_id": "wi-owner",
                "_execution_context": {
                    "workspace_root": str(self.root),
                    "output_root": str(self.root),
                },
            },
        )
        await self.store.save_task(child)
        effects: list[str] = []

        async def effect() -> dict[str, object]:
            effects.append("child")
            return {"success": True}

        result = await RuntimeCompanyControllerToolFence(store=self.store).run(
            task=child,
            tool_name="file_write",
            arguments={"path": "child-output.json", "content": "{}"},
            effect=effect,
        )

        self.assertTrue(result["success"])
        self.assertEqual(effects, ["child"])
        foreign = await self._claim(self.foreign_task, "child-output.json")
        self.assertEqual(foreign.outcome, "conflict")
        self.assertEqual(foreign.conflicting_work_item_id, "wi-owner")

        shell_arguments = {"command": "touch child-shell-output.txt"}
        permit = await self._executing_permit(
            child,
            call_id="call-child-approved",
            tool_name="shell_exec",
            arguments=shell_arguments,
            runtime_session_id="rt-child-approved",
        )
        child_call = {
            "id": "call-child-approved",
            "function": "shell_exec",
            "arguments": shell_arguments,
            "_approval_permit": permit,
        }
        approved = await RuntimeCompanyControllerToolFence(store=self.store).run(
            task=child,
            tool_name="shell_exec",
            arguments=shell_arguments,
            tool_call=child_call,
            effect=effect,
        )
        replay = await RuntimeCompanyControllerToolFence(store=self.store).run(
            task=child,
            tool_name="shell_exec",
            arguments=shell_arguments,
            tool_call=child_call,
            effect=effect,
        )
        self.assertTrue(approved["success"])
        self.assertFalse(replay["success"])
        self.assertEqual(effects, ["child", "child"])

        child.parent_id = self.foreign_task.id
        blocked = await RuntimeCompanyControllerToolFence(store=self.store).run(
            task=child,
            tool_name="file_write",
            arguments={"path": "wrong-parent.json", "content": "{}"},
            effect=effect,
        )
        self.assertFalse(blocked["success"])
        self.assertEqual(blocked["artifact_ownership"]["outcome"], "invalid_identity")
        self.assertEqual(effects, ["child", "child"])

    async def test_isolated_native_subagent_uses_durable_worktree_without_shared_claim(self) -> None:
        isolated_root = self.root.parent / "isolated-worktree"
        isolated_root.mkdir()
        await self.store.save_runtime_subagent_run(
            subagent_run_id="agent-isolated",
            runtime_session_id="native-session",
            task_id=self.owner_task.id,
            agent_id="agent-isolated",
            profile="implement",
            status="running",
            worktree_path=str(isolated_root),
            metadata={"isolation": "worktree"},
        )
        child = Task(
            id="child-isolated",
            parent_id=self.owner_task.id,
            session_id="child-session",
            project_id="project-1",
            title="isolated child",
            assigned_to="analyst",
            metadata={
                **dict(self.owner_task.metadata),
                "_comms_endpoint_id": "agent-isolated",
                "_execution_context": {
                    "workspace_root": str(isolated_root),
                    "output_root": str(isolated_root),
                },
            },
        )
        await self.store.save_task(child)
        effects: list[str] = []

        async def effect() -> dict[str, object]:
            effects.append("isolated")
            return {"success": True}

        fence = RuntimeCompanyControllerToolFence(store=self.store)
        result = await fence.run(
            task=child,
            tool_name="file_write",
            arguments={"path": "isolated.txt", "content": "ok"},
            effect=effect,
        )
        self.assertTrue(result["success"])
        self.assertEqual(effects, ["isolated"])

        assert self.store._db is not None
        async with self.store._db.execute(
            "SELECT COUNT(*) FROM company_artifact_ownership",
        ) as cursor:
            count = await cursor.fetchone()
        self.assertEqual(count[0], 0)

        child.metadata["_execution_context"] = {
            "workspace_root": str(self.root),
            "output_root": str(self.root),
        }
        blocked = await fence.run(
            task=child,
            tool_name="file_write",
            arguments={"path": "shared.txt", "content": "blocked"},
            effect=effect,
        )
        self.assertFalse(blocked["success"])
        self.assertEqual(blocked["artifact_ownership"]["outcome"], "invalid_path")
        self.assertEqual(effects, ["isolated"])

    async def test_subagent_manager_persists_company_child_before_shared_and_isolated_effects(self) -> None:
        fence = RuntimeCompanyControllerToolFence(store=self.store)
        observed: dict[str, object] = {}

        class SharedChildAgent:
            async def execute(_self, child: Task) -> TaskResult:
                shared_effects: list[str] = []

                async def effect() -> dict[str, object]:
                    shared_effects.append("ran")
                    return {"success": True}

                file_result = await fence.run(
                    task=child,
                    tool_name="file_write",
                    arguments={"path": "manager-shared.json", "content": "{}"},
                    effect=effect,
                )
                shell_arguments = {"command": "touch manager-shell.txt"}
                permit = await self._executing_permit(
                    child,
                    call_id="call-manager-shell",
                    tool_name="shell_exec",
                    arguments=shell_arguments,
                    runtime_session_id="rt-manager-shell",
                )
                shell_result = await fence.run(
                    task=child,
                    tool_name="shell_exec",
                    arguments=shell_arguments,
                    tool_call={
                        "id": "call-manager-shell",
                        "_approval_permit": permit,
                    },
                    effect=effect,
                )
                observed["shared_task_id"] = child.id
                observed["shared_file"] = file_result
                observed["shared_shell"] = shell_result
                observed["shared_effects"] = shared_effects
                return TaskResult(status=TaskStatus.DONE, content="shared done")

        shared_manager = SubagentManager(
            parent_task=self.owner_task,
            config=OPCConfig(),
            child_agent_factory=lambda *args: SharedChildAgent(),
            store=self.store,
            runtime_session_id="rt-manager-shared",
        )
        shared_result = await shared_manager.spawn(
            profile="implement",
            prompt="write shared output",
            background=False,
            isolation="shared",
        )
        self.assertTrue(shared_result["success"])
        self.assertTrue(observed["shared_file"]["success"])
        self.assertTrue(observed["shared_shell"]["success"])
        self.assertEqual(observed["shared_effects"], ["ran", "ran"])
        self.assertIsNotNone(
            await self.store.get_task(str(observed["shared_task_id"]))
        )

        isolated_root = self.root.parent / "manager-isolated"
        isolated_root.mkdir()

        class IsolatedChildAgent:
            async def execute(_self, child: Task) -> TaskResult:
                isolated_effects: list[str] = []

                async def effect() -> dict[str, object]:
                    isolated_effects.append("ran")
                    return {"success": True}

                result = await fence.run(
                    task=child,
                    tool_name="file_write",
                    arguments={"path": "manager-isolated.json", "content": "{}"},
                    effect=effect,
                )
                observed["isolated_file"] = result
                observed["isolated_effects"] = isolated_effects
                return TaskResult(status=TaskStatus.DONE, content="isolated done")

        worktree = {
            "path": str(isolated_root),
            "mode": "copy",
            "execution_context": {
                "workspace_root": str(isolated_root),
                "output_root": str(isolated_root),
            },
        }
        with patch(
            "opc.layer3_agent.runtime_v2.subagents.create_worktree",
            AsyncMock(return_value=worktree),
        ), patch(
            "opc.layer3_agent.runtime_v2.subagents.cleanup_worktree",
            AsyncMock(),
        ):
            isolated_manager = SubagentManager(
                parent_task=self.owner_task,
                config=OPCConfig(),
                child_agent_factory=lambda *args: IsolatedChildAgent(),
                store=self.store,
                runtime_session_id="rt-manager-isolated",
            )
            isolated_result = await isolated_manager.spawn(
                profile="implement",
                prompt="write isolated output",
                background=False,
                isolation="worktree",
            )
        self.assertTrue(isolated_result["success"])
        self.assertTrue(observed["isolated_file"]["success"])
        self.assertEqual(observed["isolated_effects"], ["ran"])
        assert self.store._db is not None
        async with self.store._db.execute(
            """SELECT COUNT(*) FROM company_artifact_ownership
               WHERE path_key = 'manager-isolated.json'"""
        ) as cursor:
            isolated_claims = await cursor.fetchone()
        self.assertEqual(isolated_claims[0], 0)

    async def test_read_never_claims_and_ordinary_task_bypasses_ownership(self) -> None:
        fence = RuntimeCompanyControllerToolFence(store=self.store)
        read_effects: list[str] = []

        async def read_effect() -> dict[str, object]:
            read_effects.append("read")
            return {"success": True, "content": "shared"}

        read = await fence.run(
            task=self.foreign_task,
            tool_name="file_read",
            arguments={"path": "shared.txt"},
            effect=read_effect,
        )
        self.assertTrue(read["success"])
        self.assertEqual(read_effects, ["read"])

        ordinary_effects: list[str] = []

        async def ordinary_effect() -> dict[str, object]:
            ordinary_effects.append("write")
            return {"success": True}

        ordinary = await fence.run(
            task=Task(id="ordinary", project_id="project-1"),
            tool_name="file_write",
            arguments={"path": "ordinary.txt", "content": "ok"},
            effect=ordinary_effect,
        )
        self.assertTrue(ordinary["success"])
        self.assertEqual(ordinary_effects, ["write"])

        assert self.store._db is not None
        async with self.store._db.execute(
            "SELECT COUNT(*) FROM company_artifact_ownership",
        ) as cursor:
            count = await cursor.fetchone()
        self.assertEqual(count[0], 0)


if __name__ == "__main__":
    unittest.main()
