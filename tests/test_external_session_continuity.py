"""Fix 5 PR6 — external agent session continuity across tasks.

Goal: the same role running consecutive tasks resumes the same external
agent session (codex thread / claude-code session / opencode session).
The resume token lives on ``role_runtime_session.adapter_session_state``
keyed by ``agent_type`` — not per-task.

Covers:
- Store helpers ``update_role_session_adapter_state`` /
  ``get_role_session_adapter_state`` (atomic merge, per-agent isolation,
  clear semantics).
- Broker ``_persist_session`` writes the role adapter state after a
  DONE task with a usable token.
- Broker ``_restore_session_resume_from_store`` prefers the role
  adapter state over the legacy ExternalSession row.
- Applies to all three external adapters (codex, claude_code, opencode)
  without special-casing — they share the broker path.
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
from opc.layer3_agent.adapters.base import ExternalAgentAdapter
from opc.layer3_agent.external_broker import ExternalAgentBroker
from opc.layer3_agent.external_session_identity import select_best_external_resume_session


# ── Store helper unit tests ──────────────────────────────────────────────


class AdapterSessionStateHelperTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()
        self.sid = "role-runtime::run-1::cto"
        await self.store.save_delegation_role_session(
            DelegationRoleSession(
                role_session_id=self.sid, run_id="run-1", role_id="cto",
            )
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    async def test_update_then_get_roundtrip(self) -> None:
        ok = await self.store.update_role_session_adapter_state(
            self.sid,
            "codex",
            {"resume_session_id": "thread-abc", "last_task_id": "t1"},
        )
        self.assertTrue(ok)
        entry = await self.store.get_role_session_adapter_state(self.sid, "codex")
        self.assertEqual(entry["resume_session_id"], "thread-abc")
        self.assertEqual(entry["last_task_id"], "t1")

    async def test_second_agent_does_not_overwrite_first(self) -> None:
        await self.store.update_role_session_adapter_state(
            self.sid, "codex", {"resume_session_id": "thread-codex"},
        )
        await self.store.update_role_session_adapter_state(
            self.sid, "claude_code", {"resume_session_id": "sess-cc"},
        )
        codex = await self.store.get_role_session_adapter_state(self.sid, "codex")
        claude = await self.store.get_role_session_adapter_state(self.sid, "claude_code")
        self.assertEqual(codex["resume_session_id"], "thread-codex")
        self.assertEqual(claude["resume_session_id"], "sess-cc")

    async def test_repeated_update_merges_latest_for_same_agent(self) -> None:
        await self.store.update_role_session_adapter_state(
            self.sid, "codex", {"resume_session_id": "thread-old", "last_task_id": "t1"},
        )
        await self.store.update_role_session_adapter_state(
            self.sid, "codex", {"resume_session_id": "thread-new", "last_task_id": "t2"},
        )
        entry = await self.store.get_role_session_adapter_state(self.sid, "codex")
        # Latest wins — no stale fields from the earlier record.
        self.assertEqual(entry["resume_session_id"], "thread-new")
        self.assertEqual(entry["last_task_id"], "t2")

    async def test_update_none_clears_entry(self) -> None:
        await self.store.update_role_session_adapter_state(
            self.sid, "codex", {"resume_session_id": "thread-abc"},
        )
        role_session = await self.store.get_delegation_role_session(self.sid)
        role_session.adapter_session_state.update({
            "selected_execution_agent": "codex",
            "external_resume_session_id": "thread-abc",
            "external_resume_session_scope_id": "sess-a",
            "external_resume_agent_type": "codex",
            "resume_source_session": "thread-abc",
        })
        await self.store.save_delegation_role_session(role_session)
        await self.store.update_role_session_adapter_state(self.sid, "codex", None)
        entry = await self.store.get_role_session_adapter_state(self.sid, "codex")
        refreshed = await self.store.get_delegation_role_session(self.sid)
        self.assertIsNone(entry)
        self.assertEqual(
            refreshed.adapter_session_state.get("selected_execution_agent"),
            "codex",
        )
        self.assertNotIn(
            "external_resume_session_id",
            refreshed.adapter_session_state,
        )
        self.assertNotIn("resume_source_session", refreshed.adapter_session_state)

    async def test_missing_session_returns_false(self) -> None:
        ok = await self.store.update_role_session_adapter_state(
            "no::such::session", "codex", {"resume_session_id": "x"},
        )
        self.assertFalse(ok)

    async def test_get_missing_entry_returns_none(self) -> None:
        # Agent never written to this role.
        self.assertIsNone(
            await self.store.get_role_session_adapter_state(self.sid, "opencode")
        )

    async def test_state_persists_across_save_and_read(self) -> None:
        await self.store.update_role_session_adapter_state(
            self.sid, "opencode", {"resume_session_id": "sess-op"},
        )
        # A full RoleRuntimeSession read should also expose the merged dict.
        loaded = await self.store.get_delegation_role_session(self.sid)
        self.assertIn("opencode", loaded.adapter_session_state)
        self.assertEqual(
            loaded.adapter_session_state["opencode"]["resume_session_id"],
            "sess-op",
        )


# ── Broker-level tests ──────────────────────────────────────────────────


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

    def __init__(self, *, agent_type: str, can_resume_blank: bool = True) -> None:
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


class BrokerPersistWritesRoleStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()
        self.role_session_id = "role-runtime::run-pr6::cto"
        await self.store.save_delegation_role_session(
            DelegationRoleSession(
                role_session_id=self.role_session_id,
                run_id="run-pr6",
                role_id="cto",
            )
        )
        self.broker = ExternalAgentBroker(self.store, _ApprovalStub())

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    def _task(self, *, task_id: str) -> Task:
        return Task(
            id=task_id, title="t", description="",
            assigned_to="cto",
            status=TaskStatus.RUNNING,
            project_id="proj1", session_id="sess-a",
            metadata={"delegation_role_session_id": self.role_session_id},
        )

    async def _persist_run(
        self,
        *,
        task_id: str,
        agent_type: str,
        resume_session_id: str,
        status: TaskStatus = TaskStatus.DONE,
    ) -> None:
        adapter = _MiniAdapter(agent_type=agent_type)
        task = self._task(task_id=task_id)
        result = TaskResult(
            status=status,
            content="ok",
            artifacts={"resume_session_id": resume_session_id},
        )
        await self.broker._persist_session(
            adapter=adapter,
            task=task,
            workspace_path="/tmp/ws",
            metadata={"command": "codex exec", "model": "(cli default)"},
            result=result,
        )

    async def test_done_with_resume_token_writes_role_state(self) -> None:
        await self._persist_run(
            task_id="task-1", agent_type="codex", resume_session_id="thread-pr6",
        )

        entry = await self.store.get_role_session_adapter_state(
            self.role_session_id, "codex"
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry["resume_session_id"], "thread-pr6")
        self.assertEqual(entry["agent_type"], "codex")
        self.assertEqual(entry["last_task_id"], "task-1")

    async def test_failed_task_does_not_pin_stale_token(self) -> None:
        await self._persist_run(
            task_id="task-fail",
            agent_type="codex",
            resume_session_id="thread-broken",
            status=TaskStatus.FAILED,
        )
        entry = await self.store.get_role_session_adapter_state(
            self.role_session_id, "codex"
        )
        self.assertIsNone(entry)

    async def test_failed_resume_clears_matching_older_role_token_and_task_metadata(self) -> None:
        await self._persist_run(
            task_id="task-old",
            agent_type="codex",
            resume_session_id="thread-full",
        )
        adapter = _MiniAdapter(agent_type="codex")
        adapter.config.session_mode = "resume"
        adapter.config.session_id = "thread-full"
        task = self._task(task_id="task-retry")
        task.metadata.update({
            "external_resume_session_id": "thread-full",
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
                "resume_session_id": "thread-full",
            },
            result=TaskResult(
                status=TaskStatus.FAILED,
                content="context window full",
                artifacts={"resume_session_id": "thread-full"},
            ),
        )

        self.assertIsNone(
            await self.store.get_role_session_adapter_state(
                self.role_session_id,
                "codex",
            )
        )
        self.assertNotIn("external_resume_session_id", task.metadata)
        self.assertEqual(
            task.metadata.get("external_resume_fallback"),
            "provider_terminal_failure",
        )

    async def test_awaiting_human_park_keeps_role_token_and_task_metadata(self) -> None:
        # An approval park is not a terminal failure: the run resumes the
        # same provider thread once the human approves, so neither the role
        # token nor the task's resume pin may be cleared.
        await self._persist_run(
            task_id="task-old",
            agent_type="codex",
            resume_session_id="thread-live",
        )
        adapter = _MiniAdapter(agent_type="codex")
        adapter.config.session_mode = "resume"
        adapter.config.session_id = "thread-live"
        task = self._task(task_id="task-parked")
        task.metadata.update({
            "external_resume_session_id": "thread-live",
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
                "resume_session_id": "thread-live",
            },
            result=TaskResult(
                status=TaskStatus.AWAITING_HUMAN,
                content="External action blocked by autonomy policy",
                artifacts={
                    "resume_session_id": "thread-live",
                    "requires_user_input": True,
                },
            ),
        )

        entry = await self.store.get_role_session_adapter_state(
            self.role_session_id,
            "codex",
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry["resume_session_id"], "thread-live")
        self.assertEqual(
            task.metadata.get("external_resume_session_id"), "thread-live"
        )
        self.assertNotIn("external_resume_fallback", task.metadata)

    async def test_consecutive_tasks_overwrite_with_latest_token(self) -> None:
        await self._persist_run(
            task_id="task-1", agent_type="codex", resume_session_id="thread-1",
        )
        await self._persist_run(
            task_id="task-2", agent_type="codex", resume_session_id="thread-2",
        )
        entry = await self.store.get_role_session_adapter_state(
            self.role_session_id, "codex"
        )
        self.assertEqual(entry["resume_session_id"], "thread-2")
        self.assertEqual(entry["last_task_id"], "task-2")

    def test_newer_failed_row_vetoes_older_done_row_for_same_token(self) -> None:
        now = datetime.now()
        older = ExternalSession(
            agent_type="codex",
            project_id="proj1",
            session_id="thread-full",
            status="done",
            metadata={"resume_session_id": "thread-full"},
            updated_at=now - timedelta(minutes=1),
        )
        newer = ExternalSession(
            agent_type="codex",
            project_id="proj1",
            session_id="thread-full",
            status="failed",
            metadata={"resume_session_id": "thread-full"},
            updated_at=now,
        )

        selected, token = select_best_external_resume_session(
            [older, newer],
            agent_type="codex",
            project_id="proj1",
        )

        self.assertIsNone(selected)
        self.assertEqual(token, "")

    async def test_per_agent_isolation_codex_claude_opencode(self) -> None:
        # All three external adapters land in the same broker path. Each
        # gets its own slot under adapter_session_state keyed by agent_type.
        await self._persist_run(
            task_id="t-cx", agent_type="codex", resume_session_id="thread-cx",
        )
        await self._persist_run(
            task_id="t-cc", agent_type="claude_code", resume_session_id="sess-cc",
        )
        await self._persist_run(
            task_id="t-op", agent_type="opencode", resume_session_id="sess-op",
        )

        codex = await self.store.get_role_session_adapter_state(
            self.role_session_id, "codex"
        )
        claude = await self.store.get_role_session_adapter_state(
            self.role_session_id, "claude_code"
        )
        opencode = await self.store.get_role_session_adapter_state(
            self.role_session_id, "opencode"
        )
        self.assertEqual(codex["resume_session_id"], "thread-cx")
        self.assertEqual(claude["resume_session_id"], "sess-cc")
        self.assertEqual(opencode["resume_session_id"], "sess-op")


class BrokerRestorePrefersRoleStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.store = OPCStore(db_path=Path(self._tmpdir.name) / "store.db")
        await self.store.initialize()
        self.role_session_id = "role-runtime::run-pr6::cto"
        await self.store.save_delegation_role_session(
            DelegationRoleSession(
                role_session_id=self.role_session_id,
                run_id="run-pr6",
                role_id="cto",
            )
        )
        self.broker = ExternalAgentBroker(self.store, _ApprovalStub())

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmpdir.cleanup()

    def _task(self) -> Task:
        return Task(
            id="task-new", title="t", description="",
            assigned_to="cto",
            status=TaskStatus.PENDING,
            project_id="proj1", session_id="sess-a",
            metadata={"delegation_role_session_id": self.role_session_id},
        )

    async def test_restore_reads_role_state_first(self) -> None:
        # Seed the role adapter state — canonical PR6 source of truth.
        await self.store.update_role_session_adapter_state(
            self.role_session_id,
            "codex",
            {"resume_session_id": "thread-role", "last_task_id": "prior"},
        )
        adapter = _MiniAdapter(agent_type="codex")
        task = self._task()

        await self.broker._restore_session_resume_from_store(adapter, task)

        # Adapter config must now reflect resume-mode with the role token.
        self.assertEqual(adapter.config.session_mode, "resume")
        self.assertEqual(adapter.config.session_id, "thread-role")
        self.assertEqual(
            task.metadata.get("external_resume_session_id"), "thread-role"
        )
        self.assertEqual(task.metadata.get("external_resume_agent_type"), "codex")

    async def test_restore_no_role_state_falls_through_to_external_sessions(self) -> None:
        # No adapter_session_state entry + no ExternalSession row → adapter
        # stays in auto mode (nothing to resume).
        adapter = _MiniAdapter(agent_type="codex", can_resume_blank=False)
        task = self._task()

        await self.broker._restore_session_resume_from_store(adapter, task)

        self.assertNotEqual(adapter.config.session_mode, "resume")
        self.assertEqual(adapter.config.session_id, "")

    async def test_restore_isolates_by_agent_type(self) -> None:
        # Role has a codex token but we ask for claude_code — must not
        # leak the codex token into a different adapter's config.
        await self.store.update_role_session_adapter_state(
            self.role_session_id,
            "codex",
            {"resume_session_id": "thread-codex-only"},
        )
        adapter = _MiniAdapter(agent_type="claude_code", can_resume_blank=False)
        task = self._task()

        await self.broker._restore_session_resume_from_store(adapter, task)

        self.assertNotEqual(adapter.config.session_id, "thread-codex-only")

    async def test_restore_never_treats_synthetic_monitor_identity_as_provider_token(self) -> None:
        synthetic_id = "codex:proj1:task-new"
        await self.store.update_role_session_adapter_state(
            self.role_session_id,
            "codex",
            {
                "resume_session_id": synthetic_id,
                "provider_session_id": synthetic_id,
            },
        )
        await self.store.save_external_session(
            ExternalSession(
                agent_type="codex",
                project_id="proj1",
                session_id=synthetic_id,
                opc_session_id=self.role_session_id,
                task_id="task-new",
                workspace_path="/tmp/ws",
                run_mode="exec",
                status="working",
                metadata={},
            )
        )
        adapter = _MiniAdapter(agent_type="codex", can_resume_blank=False)
        task = self._task()

        await self.broker._restore_session_resume_from_store(adapter, task)

        self.assertNotEqual(adapter.config.session_mode, "resume")
        self.assertEqual(adapter.config.session_id, "")
        self.assertNotIn("external_resume_session_id", task.metadata)

    async def test_restore_rejects_failed_early_provider_stream_token(self) -> None:
        token = "thread-failed"
        await self.store.update_role_session_adapter_state(
            self.role_session_id,
            "codex",
            {
                "resume_session_id": token,
                "provider_session_id": token,
                "last_task_id": "task-new",
                "source": "provider_stream",
                "status": "working",
            },
        )
        await self.store.save_external_session(ExternalSession(
            agent_type="codex",
            project_id="proj1",
            session_id=token,
            opc_session_id=self.role_session_id,
            task_id="task-new",
            workspace_path="/tmp/ws",
            run_mode="exec",
            status="failed",
            metadata={
                "resume_session_id": token,
                "provider_session_id": token,
            },
        ))
        adapter = _MiniAdapter(agent_type="codex", can_resume_blank=False)
        task = self._task()

        await self.broker._restore_session_resume_from_store(adapter, task)

        self.assertNotEqual(adapter.config.session_mode, "resume")
        self.assertEqual(adapter.config.session_id, "")
        self.assertIsNone(await self.store.get_role_session_adapter_state(
            self.role_session_id,
            "codex",
        ))

    async def test_restore_rejects_terminal_normal_role_token_even_when_preconfigured(self) -> None:
        token = "thread-terminal"
        await self.store.update_role_session_adapter_state(
            self.role_session_id,
            "codex",
            {
                "resume_session_id": token,
                "provider_session_id": token,
                "last_task_id": "task-old",
            },
        )
        await self.store.save_external_session(ExternalSession(
            agent_type="codex",
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
        adapter = _MiniAdapter(agent_type="codex", can_resume_blank=False)
        adapter.config.session_mode = "resume"
        adapter.config.session_id = token
        task = self._task()
        task.metadata.update({
            "external_resume_session_id": token,
            "external_resume_session_scope_id": "sess-a",
            "external_resume_agent_type": "codex",
        })

        await self.broker._restore_session_resume_from_store(adapter, task)

        self.assertEqual(adapter.config.session_mode, "new")
        self.assertEqual(adapter.config.session_id, "")
        self.assertIsNone(await self.store.get_role_session_adapter_state(
            self.role_session_id,
            "codex",
        ))
        self.assertNotIn("external_resume_session_id", task.metadata)
        self.assertEqual(
            task.metadata.get("external_resume_fallback"),
            "provider_terminal_failure",
        )

    async def test_restore_keeps_role_token_when_newest_row_is_parked_awaiting_human(self) -> None:
        # A canonical role token whose newest run parked on approval must
        # keep its pin: awaiting_human is not evidence the thread is dead,
        # and resume-after-approval depends on this token surviving.
        token = "thread-parked"
        await self.store.update_role_session_adapter_state(
            self.role_session_id,
            "codex",
            {
                "resume_session_id": token,
                "provider_session_id": token,
                "last_task_id": "task-old",
            },
        )
        await self.store.save_external_session(ExternalSession(
            agent_type="codex",
            project_id="proj1",
            session_id=token,
            opc_session_id=self.role_session_id,
            task_id="task-parked",
            workspace_path="/tmp/ws",
            run_mode="exec",
            status="awaiting_human",
            metadata={
                "resume_session_id": token,
                "provider_session_id": token,
            },
        ))
        adapter = _MiniAdapter(agent_type="codex", can_resume_blank=False)
        task = self._task()

        await self.broker._restore_session_resume_from_store(adapter, task)

        self.assertEqual(adapter.config.session_mode, "resume")
        self.assertEqual(adapter.config.session_id, token)
        self.assertIsNotNone(await self.store.get_role_session_adapter_state(
            self.role_session_id,
            "codex",
        ))

    async def test_restore_still_rejects_unfinalized_provider_stream_token(self) -> None:
        # provider_stream tokens keep the strict pre-existing rule: a row
        # that never finalized (still "working") may belong to a crashed
        # attempt and must not be resumed.
        token = "thread-unfinalized"
        await self.store.update_role_session_adapter_state(
            self.role_session_id,
            "codex",
            {
                "resume_session_id": token,
                "provider_session_id": token,
                "last_task_id": "task-new",
                "source": "provider_stream",
                "status": "working",
            },
        )
        await self.store.save_external_session(ExternalSession(
            agent_type="codex",
            project_id="proj1",
            session_id=token,
            opc_session_id=self.role_session_id,
            task_id="task-new",
            workspace_path="/tmp/ws",
            run_mode="exec",
            status="working",
            metadata={
                "resume_session_id": token,
                "provider_session_id": token,
            },
        ))
        adapter = _MiniAdapter(agent_type="codex", can_resume_blank=False)
        task = self._task()

        await self.broker._restore_session_resume_from_store(adapter, task)

        self.assertNotEqual(adapter.config.session_mode, "resume")
        self.assertEqual(adapter.config.session_id, "")


if __name__ == "__main__":
    unittest.main()
