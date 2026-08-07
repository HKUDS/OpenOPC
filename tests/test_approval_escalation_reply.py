"""Regression: the unified approval decision channel (OBS-7).

An approval escalation that outlives its inline wait survives as a
``task_user_input`` checkpoint whose payload carries the runtime block's
``permission_context``. A reply that expresses a decision must reach the
approval engine — ``normalize_escalation_reply`` maps human phrasing to a
decision token, ``escalation_context_for_blocked_tool`` rebuilds the
allowlist context, and ``apply_deferred_escalation_decision`` persists the
grant exactly like the Office UI card click. Non-decision text stays plain
task input (never a silent deny).
"""
from __future__ import annotations

import unittest

from opc.core.config import AutonomyConfig
from opc.core.models import CompanyMemberSession, Task
from opc.layer2_organization.approval import (
    ApprovalEngine,
    normalize_escalation_reply,
)
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor


class _PreferencesStub:
    def get_autonomy_preferences(self, project_id=None):
        _ = project_id
        return {"learned_actions": {}}

    def record_autonomy_feedback(self, **kwargs):
        _ = kwargs


class _StoreStub:
    async def record_approval(self, **kwargs):
        _ = kwargs


class _MemoryStub:
    def append_autonomy_event(self, event, project=False):
        _ = (event, project)


def _engine() -> ApprovalEngine:
    return ApprovalEngine(
        llm=object(),
        store=_StoreStub(),
        preferences=_PreferencesStub(),
        memory=_MemoryStub(),
        escalation=None,
        config=AutonomyConfig(),
    )


class NormalizeEscalationReplyTests(unittest.TestCase):
    def test_exact_tokens_pass_through(self) -> None:
        for token in ("approve_once", "approve_session", "always_project",
                      "always_global", "deny"):
            self.assertEqual(normalize_escalation_reply(token), token)

    def test_approve_synonyms_map_to_approve_once(self) -> None:
        for text in ("approve", "Yes", " y ", "同意", "允许"):
            self.assertEqual(normalize_escalation_reply(text), "approve_once")

    def test_deny_synonyms_map_to_deny(self) -> None:
        for text in ("no", "Reject", "拒绝"):
            self.assertEqual(normalize_escalation_reply(text), "deny")

    def test_plain_content_is_not_a_decision(self) -> None:
        for text in ("", "please use conda instead", "proceed with best judgment"):
            self.assertEqual(normalize_escalation_reply(text), "")


class EscalationContextForBlockedToolTests(unittest.TestCase):
    def test_shell_exec_context_carries_command_patterns(self) -> None:
        engine = _engine()
        task = Task(id="t1", title="t", project_id="p1", session_id="s1")
        context = engine.escalation_context_for_blocked_tool(
            task,
            tool_name="shell_exec",
            arguments={"command": "pip install pandas"},
        )
        self.assertEqual(context["action_kind"], "tool")
        self.assertEqual(context["action_name"], "shell_exec")
        self.assertEqual(context["project_id"], "p1")
        self.assertTrue(context["allowlist_enabled"])
        self.assertTrue(context["candidates"])
        self.assertTrue(
            any("pip" in str(item) for item in context["candidates"]),
            context["candidates"],
        )

    def test_decision_applies_through_deferred_channel(self) -> None:
        engine = _engine()
        task = Task(id="t2", title="t", project_id="p1", session_id="s1")
        task.metadata["session_scope_id"] = "scope-1"
        context = engine.escalation_context_for_blocked_tool(
            task,
            tool_name="shell_exec",
            arguments={"command": "pip install pandas"},
        )
        outcome = engine.apply_deferred_escalation_decision("approve_session", context)
        self.assertTrue(outcome.get("approved"))
        deny = engine.apply_deferred_escalation_decision("deny", context)
        self.assertFalse(deny.get("approved"))


class LiveRunDispatcherRegistryTests(unittest.TestCase):
    """OBS-4: checkpoint answers wake a live dispatcher instead of re-entry."""

    def _executor(self) -> CompanyWorkItemExecutor:
        import asyncio

        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        executor._live_run_dispatchers = {}
        executor._dispatcher_wake = asyncio.Event()
        return executor

    def test_wake_returns_false_when_no_live_dispatcher(self) -> None:
        executor = self._executor()
        self.assertFalse(executor.wake_live_run_dispatcher("run-1"))
        self.assertFalse(executor._dispatcher_wake.is_set())

    def test_wake_signals_live_dispatcher(self) -> None:
        executor = self._executor()
        executor._live_run_dispatchers["run-1"] = 1
        self.assertTrue(executor.wake_live_run_dispatcher("run-1"))
        self.assertTrue(executor._dispatcher_wake.is_set())

    def test_run_id_extraction_prefers_first_tagged_task(self) -> None:
        t1 = Task(id="a", title="a", project_id="p", session_id="s")
        t2 = Task(id="b", title="b", project_id="p", session_id="s")
        t2.metadata["delegation_run_id"] = "run-9"
        self.assertEqual(
            CompanyWorkItemExecutor._delegation_run_id_for_tasks([t1, t2]),
            "run-9",
        )
        self.assertEqual(
            CompanyWorkItemExecutor._delegation_run_id_for_tasks([t1]),
            "",
        )

    def test_member_session_import_smoke(self) -> None:
        # Guard against accidental import regressions in the test module.
        self.assertTrue(CompanyMemberSession)


if __name__ == "__main__":
    unittest.main()
