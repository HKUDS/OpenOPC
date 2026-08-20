"""Regression coverage for approval reply normalization and live dispatch."""
from __future__ import annotations

import unittest

from opc.core.models import CompanyMemberSession, Task
from opc.layer2_organization.approval import normalize_escalation_reply
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor


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
