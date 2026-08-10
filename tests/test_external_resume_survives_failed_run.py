"""Durable-transcript agents keep their resume token across failed runs.

Claude Code persists every session transcript under
``~/.claude/projects/<cwd>/<session-id>.jsonl`` and ``claude --resume <id>``
replays it even when the launching run failed, was cancelled, or timed out.
Treating a bad run outcome as "session dead" therefore silently drops the
whole conversation context: the next turn starts a blank provider session
and the agent answers as if the conversation never happened.

These tests pin the new behavior:

- ``external_session_status_allows_resume`` / ``select_best_external_resume_session``
  / ``provider_token_from_external_session`` treat terminal-but-failed rows of
  durable-transcript agents (claude_code) as resumable.
- Broker restore re-seeds ``--resume`` from a failed/cancelled claude_code row.
- Broker persist keeps the role token and task resume pin after a failed
  claude_code run.
- Codex (no durability claim) keeps the pre-existing conservative gate.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from opc.core.models import (
    ApprovalAction,
    ApprovalDecision,
    DelegationRoleSession,
    ExternalSession,
    RiskLevel,
    Task,
    TaskResult,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.layer3_agent.external_broker import ExternalAgentBroker
from opc.layer3_agent.external_session_identity import (
    agent_resume_survives_run_failure,
    external_session_status_allows_resume,
    provider_token_from_external_session,
    select_best_external_resume_session,
)


class IdentityHelpersDurableAgentTests(unittest.TestCase):
    def test_status_gate_still_blocks_non_durable_agents(self) -> None:
        self.assertFalse(external_session_status_allows_resume("failed"))
        self.assertFalse(
            external_session_status_allows_resume("failed", agent_type="codex")
        )
        self.assertFalse(
            external_session_status_allows_resume("cancelled", agent_type="opencode")
        )

    def test_status_gate_passes_durable_agent_for_all_statuses(self) -> None:
        for status in (
            "failed",
            "cancelled",
            "hard_timeout",
            "idle_timeout",
            "startup_timeout",
            "denied",
            "rejected",
        ):
            self.assertTrue(
                external_session_status_allows_resume(
                    status, agent_type="claude_code"
                ),
                status,
            )

    def test_capability_lookup_normalizes_input(self) -> None:
        self.assertTrue(agent_resume_survives_run_failure("claude_code"))
        self.assertTrue(agent_resume_survives_run_failure(" Claude_Code "))
        self.assertFalse(agent_resume_survives_run_failure("codex"))
        self.assertFalse(agent_resume_survives_run_failure(""))
        self.assertFalse(agent_resume_survives_run_failure(None))

    def test_newer_failed_claude_row_still_selected_for_resume(self) -> None:
        now = datetime.now()
        older = ExternalSession(
            agent_type="claude_code",
            project_id="proj1",
            session_id="sess-cc",
            status="done",
            metadata={"resume_session_id": "sess-cc"},
            updated_at=now - timedelta(minutes=1),
        )
        newer = ExternalSession(
            agent_type="claude_code",
            project_id="proj1",
            session_id="sess-cc",
            status="failed",
            metadata={"resume_session_id": "sess-cc"},
            updated_at=now,
        )

        selected, token = select_best_external_resume_session(
            [older, newer],
            agent_type="claude_code",
            project_id="proj1",
        )

        self.assertIs(selected, newer)
        self.assertEqual(token, "sess-cc")

    def test_newer_failed_codex_row_keeps_vetoing(self) -> None:
        now = datetime.now()
        newer = ExternalSession(
            agent_type="codex",
            project_id="proj1",
            session_id="thread-full",
            status="failed",
            metadata={"resume_session_id": "thread-full"},
            updated_at=now,
        )

        selected, token = select_best_external_resume_session(
            [newer],
            agent_type="codex",
            project_id="proj1",
        )

        self.assertIsNone(selected)
        self.assertEqual(token, "")

    def test_provider_token_extracted_from_failed_claude_row(self) -> None:
        row = ExternalSession(
            agent_type="claude_code",
            project_id="proj1",
            session_id="sess-cc",
            status="cancelled",
            metadata={"resume_session_id": "sess-cc"},
        )
        self.assertEqual(
            provider_token_from_external_session(
                row, agent_type="claude_code", project_id="proj1"
            ),
            "sess-cc",
        )

    def test_provider_token_still_empty_for_failed_codex_row(self) -> None:
        row = ExternalSession(
            agent_type="codex",
            project_id="proj1",
            session_id="thread-x",
            status="failed",
            metadata={"resume_session_id": "thread-x"},
        )
        self.assertEqual(
            provider_token_from_external_session(
                row, agent_type="codex", project_id="proj1"
            ),
            "",
        )


class _ApprovalStub:
    async def authorize_external_action(self, task, agent_name, metadata, on_progress=None):
        return True, ApprovalDecision(
            action=ApprovalAction.AUTO_APPROVE,
            risk_level=RiskLevel.LOW,
            rationale="ok",
            confidence=1.0,
            policy_source="test",
        )

    async def authorize_tool_call(self, task, tool_name, arguments, metadata=None, on_progress=None):
        return True, ApprovalDecision(
            action=ApprovalAction.AUTO_APPROVE,
            risk_level=RiskLevel.LOW,
            rationale="ok",
            confidence=1.0,
            policy_source="test",
        )


class _MiniAdapter:
    """Minimal test double — just what _persist_session / restore need."""

    def __init__(self, *, agent_type: str, can_resume_blank: bool = False) -> None:
        self.agent_type = agent_type
        self.config = SimpleNamespace(
            run_mode="exec",
            session_mode="auto",
            session_id="",
            resume_session_flag="--resume",
        )
        self._can_resume_blank = can_resume_blank

    def supports_session_resume(self) -> bool:
        return True

    def can_resume_without_session_id(self) -> bool:
        return self._can_resume_blank


class BrokerResumeSurvivesFailedRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()
        self.role_session_id = "role-runtime::run-cc::cto"
        await self.store.save_delegation_role_session(
            DelegationRoleSession(
                role_session_id=self.role_session_id,
                run_id="run-cc",
                role_id="cto",
            )
        )
        self.broker = ExternalAgentBroker(self.store, _ApprovalStub())

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    def _task(self, *, task_id: str = "task-new", with_role: bool = True) -> Task:
        metadata = (
            {"delegation_role_session_id": self.role_session_id}
            if with_role
            else {}
        )
        return Task(
            id=task_id, title="t", description="",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            project_id="proj1", session_id="sess-a",
            metadata=metadata,
        )

    async def test_restore_keeps_claude_role_token_after_terminal_failure(self) -> None:
        # Mirror of the codex rejection test: the newest row for the token
        # finalized "failed", but claude transcripts survive the run, so the
        # preconfigured resume pin must be kept, not cleared.
        token = "sess-terminal"
        await self.store.update_role_session_adapter_state(
            self.role_session_id,
            "claude_code",
            {
                "resume_session_id": token,
                "provider_session_id": token,
                "last_task_id": "task-old",
            },
        )
        await self.store.save_external_session(ExternalSession(
            agent_type="claude_code",
            project_id="proj1",
            session_id=token,
            opc_session_id=self.role_session_id,
            task_id="task-failed",
            workspace_path="/tmp/ws",
            run_mode="exec",
            status="failed",
            metadata={
                "resume_session_id": token,
                "provider_session_id": token,
            },
        ))
        adapter = _MiniAdapter(agent_type="claude_code")
        adapter.config.session_mode = "resume"
        adapter.config.session_id = token
        task = self._task()
        task.metadata.update({
            "external_resume_session_id": token,
            "external_resume_session_scope_id": "sess-a",
            "external_resume_agent_type": "claude_code",
        })

        await self.broker._restore_session_resume_from_store(adapter, task)

        self.assertEqual(adapter.config.session_mode, "resume")
        self.assertEqual(adapter.config.session_id, token)
        self.assertIsNotNone(await self.store.get_role_session_adapter_state(
            self.role_session_id,
            "claude_code",
        ))
        self.assertEqual(
            task.metadata.get("external_resume_session_id"), token
        )

    async def test_restore_reseeds_resume_from_cancelled_claude_row(self) -> None:
        # No role state (task-mode turn): the only trace is an
        # ExternalSession row whose run was cancelled mid-flight. The next
        # turn must resume that provider session instead of starting blank.
        token = "sess-cancelled"
        await self.store.save_external_session(ExternalSession(
            agent_type="claude_code",
            project_id="proj1",
            session_id=token,
            opc_session_id="sess-a",
            task_id="task-new",
            workspace_path="/tmp/ws",
            run_mode="exec",
            status="cancelled",
            metadata={
                "resume_session_id": token,
                "provider_session_id": token,
            },
        ))
        adapter = _MiniAdapter(agent_type="claude_code")
        task = self._task(with_role=False)

        await self.broker._restore_session_resume_from_store(adapter, task)

        self.assertEqual(adapter.config.session_mode, "resume")
        self.assertEqual(adapter.config.session_id, token)
        self.assertEqual(
            task.metadata.get("external_resume_session_id"), token
        )

    async def test_restore_still_starts_fresh_for_failed_codex_row(self) -> None:
        token = "thread-failed"
        await self.store.save_external_session(ExternalSession(
            agent_type="codex",
            project_id="proj1",
            session_id=token,
            opc_session_id="sess-a",
            task_id="task-new",
            workspace_path="/tmp/ws",
            run_mode="exec",
            status="failed",
            metadata={
                "resume_session_id": token,
                "provider_session_id": token,
            },
        ))
        adapter = _MiniAdapter(agent_type="codex")
        task = self._task(with_role=False)

        await self.broker._restore_session_resume_from_store(adapter, task)

        self.assertNotEqual(adapter.config.session_mode, "resume")
        self.assertEqual(adapter.config.session_id, "")

    async def test_persist_failed_claude_run_keeps_role_token_and_pin(self) -> None:
        # Mirror of test_failed_resume_clears_matching_older_role_token...:
        # for claude_code the transcript survives, so a failed retry must not
        # clear the role token or the task's resume metadata.
        token = "sess-keep"
        await self.store.update_role_session_adapter_state(
            self.role_session_id,
            "claude_code",
            {
                "resume_session_id": token,
                "provider_session_id": token,
                "last_task_id": "task-old",
            },
        )
        adapter = _MiniAdapter(agent_type="claude_code")
        adapter.config.session_mode = "resume"
        adapter.config.session_id = token
        task = self._task(task_id="task-retry")
        task.metadata.update({
            "external_resume_session_id": token,
            "external_resume_session_scope_id": "sess-a",
            "external_resume_agent_type": "claude_code",
        })

        await self.broker._persist_session(
            adapter=adapter,
            task=task,
            workspace_path="/tmp/ws",
            metadata={
                "command": "claude --print",
                "model": "(cli default)",
                "resume_session_id": token,
            },
            result=TaskResult(
                status=TaskStatus.FAILED,
                content="network error mid-run",
                artifacts={"resume_session_id": token},
            ),
        )

        entry = await self.store.get_role_session_adapter_state(
            self.role_session_id,
            "claude_code",
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry["resume_session_id"], token)
        self.assertEqual(
            task.metadata.get("external_resume_session_id"), token
        )
        self.assertNotIn("external_resume_fallback", task.metadata)

    async def test_persist_failed_codex_run_still_clears(self) -> None:
        token = "thread-clear"
        await self.store.update_role_session_adapter_state(
            self.role_session_id,
            "codex",
            {
                "resume_session_id": token,
                "provider_session_id": token,
                "last_task_id": "task-old",
            },
        )
        adapter = _MiniAdapter(agent_type="codex")
        adapter.config.session_mode = "resume"
        adapter.config.session_id = token
        task = self._task(task_id="task-retry")
        task.metadata.update({
            "external_resume_session_id": token,
            "external_resume_session_scope_id": "sess-a",
            "external_resume_agent_type": "codex",
        })

        await self.broker._persist_session(
            adapter=adapter,
            task=task,
            workspace_path="/tmp/ws",
            metadata={
                "command": "codex exec resume",
                "model": "(cli default)",
                "resume_session_id": token,
            },
            result=TaskResult(
                status=TaskStatus.FAILED,
                content="context window full",
                artifacts={"resume_session_id": token},
            ),
        )

        self.assertIsNone(await self.store.get_role_session_adapter_state(
            self.role_session_id,
            "codex",
        ))
        self.assertNotIn("external_resume_session_id", task.metadata)
        self.assertEqual(
            task.metadata.get("external_resume_fallback"),
            "provider_terminal_failure",
        )


if __name__ == "__main__":
    unittest.main()
