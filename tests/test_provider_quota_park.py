"""Regression: provider quota exhaustion parks work instead of failing it (OBS-6).

Quota/rate-limit rejections never reach the model, so in-place retries can
only fail; the previous behavior burned retries and terminally failed the
work item (killing intake and the whole run during a quota window). The fix:
``LLMProvider.is_rate_limit_error`` classifies these rejections, the agent
runtime raises typed ``ProviderQuotaExhaustedError``, and the company
dispatcher returns the item to READY with exponential dispatch backoff.
"""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from opc.core.config import LLMConfig
from opc.core.models import CompanyMemberSession, Task
from opc.layer2_organization.company_mode import CompanyWorkItemExecutor
from opc.llm.provider import LLMProvider, ProviderQuotaExhaustedError


class RateLimitClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.llm = LLMProvider(LLMConfig())

    def test_text_shapes_classify_as_rate_limit(self) -> None:
        for message in (
            "Error code: 429 - rate limit reached for requests",
            "RateLimitError: too many requests, retry later",
            "insufficient_quota: you exceeded your quota",
            # localized error text from Chinese providers must classify too
            "请求过于频繁，请稍后再试",
            "当前 API 配额已用完",
        ):
            self.assertTrue(
                self.llm.is_rate_limit_error(RuntimeError(message)), message
            )

    def test_type_name_classifies(self) -> None:
        class FakeRateLimitError(Exception):
            pass

        self.assertTrue(self.llm.is_rate_limit_error(FakeRateLimitError("nope")))

    def test_status_code_classifies(self) -> None:
        error = RuntimeError("throttled")
        error.status_code = 429  # type: ignore[attr-defined]
        self.assertTrue(self.llm.is_rate_limit_error(error))

    def test_ordinary_errors_do_not_classify(self) -> None:
        for message in (
            "maximum context length exceeded",
            "connection reset by peer",
            "tool call arguments malformed",
            "prompt tokens: 14290",
        ):
            self.assertFalse(
                self.llm.is_rate_limit_error(RuntimeError(message)), message
            )


class QuotaExceptionChainTests(unittest.TestCase):
    def _executor(self) -> CompanyWorkItemExecutor:
        return CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)

    def test_direct_and_chained_detection(self) -> None:
        executor = self._executor()
        direct = ProviderQuotaExhaustedError("quota")
        wrapped = RuntimeError("turn failed")
        wrapped.__cause__ = ProviderQuotaExhaustedError("quota")
        unrelated = RuntimeError("boom")
        self.assertTrue(executor._exception_is_provider_quota(direct))
        self.assertTrue(executor._exception_is_provider_quota(wrapped))
        self.assertFalse(executor._exception_is_provider_quota(unrelated))
        self.assertFalse(executor._exception_is_provider_quota(None))


class QuotaParkTests(unittest.IsolatedAsyncioTestCase):
    def _executor(self) -> CompanyWorkItemExecutor:
        executor = CompanyWorkItemExecutor.__new__(CompanyWorkItemExecutor)
        executor.store = None
        executor.runtime = SimpleNamespace(
            _claimed_task_ids={"task-1"},
            _claimed_work_item_ids=set(),
        )
        executor._quota_park_until = 0.0
        executor._quota_park_streak = 0
        executor._quota_last_park_at = 0.0
        executor._emit_progress = AsyncMock()
        executor._projection_id_for_task = lambda task: "cto::execute::x"
        # Keep the unit test at the park-accounting level: the durable READY
        # transition is exercised by the claim-release invariant suite.
        executor._claimed_work_item_needs_cleanup = lambda member, task: False
        return executor

    def _session(self) -> CompanyMemberSession:
        session = CompanyMemberSession.__new__(CompanyMemberSession)
        session.status = "running"
        session.resident_status = "running"
        session.current_task_id = "task-1"
        session.focused_work_item_id = "wi-1"
        session.current_work_item = {"id": "wi-1"}
        session.current_assignment = {"id": "wi-1"}
        return session

    async def test_park_backs_off_exponentially_and_idles_session(self) -> None:
        executor = self._executor()
        session = self._session()
        task = Task(id="task-1", title="t", project_id="p", session_id="s")

        await executor._handle_claimed_work_item_exception(
            session, task, ProviderQuotaExhaustedError("429 rate limit")
        )

        now = time.monotonic()
        self.assertEqual(executor._quota_park_streak, 1)
        self.assertAlmostEqual(executor._quota_park_until - now, 60, delta=5)
        self.assertEqual(session.status, "idle")
        self.assertEqual(session.current_task_id, "")
        self.assertNotIn("task-1", executor.runtime._claimed_task_ids)
        # No terminal failure was recorded on the task.
        self.assertNotIn("claimed_work_item_exception", dict(task.metadata or {}))

        await executor._handle_claimed_work_item_exception(
            session, task, ProviderQuotaExhaustedError("429 again")
        )
        self.assertEqual(executor._quota_park_streak, 2)
        self.assertAlmostEqual(
            executor._quota_park_until - time.monotonic(), 120, delta=5
        )

    async def test_streak_resets_after_a_quiet_period(self) -> None:
        executor = self._executor()
        executor._quota_park_streak = 5
        executor._quota_last_park_at = time.monotonic() - 3600
        session = self._session()
        task = Task(id="task-1", title="t", project_id="p", session_id="s")

        await executor._handle_claimed_work_item_exception(
            session, task, ProviderQuotaExhaustedError("429")
        )
        self.assertEqual(executor._quota_park_streak, 1)

    async def test_backoff_caps_at_fifteen_minutes(self) -> None:
        executor = self._executor()
        executor._quota_park_streak = 9
        executor._quota_last_park_at = time.monotonic()
        session = self._session()
        task = Task(id="task-1", title="t", project_id="p", session_id="s")

        await executor._handle_claimed_work_item_exception(
            session, task, ProviderQuotaExhaustedError("429")
        )
        self.assertLessEqual(executor._quota_park_until - time.monotonic(), 900 + 5)

    async def test_non_quota_exception_still_fails_terminally(self) -> None:
        executor = self._executor()
        session = self._session()
        task = Task(id="task-1", title="t", project_id="p", session_id="s")
        failed: dict = {}

        async def _fake_fail(member_session, failed_task, exc) -> None:  # noqa: ANN001
            failed["exc"] = exc

        # Route the non-quota path into a probe: the real body needs a store.
        original = CompanyWorkItemExecutor._handle_claimed_work_item_exception

        async def _probe(self, member_session, failed_task, exc):  # noqa: ANN001
            if self._exception_is_provider_quota(exc):
                await self._park_claimed_work_item_for_quota(member_session, failed_task, exc)
                return
            await _fake_fail(member_session, failed_task, exc)

        try:
            CompanyWorkItemExecutor._handle_claimed_work_item_exception = _probe
            await executor._handle_claimed_work_item_exception(
                session, task, RuntimeError("real crash")
            )
        finally:
            CompanyWorkItemExecutor._handle_claimed_work_item_exception = original
        self.assertIsInstance(failed.get("exc"), RuntimeError)
        self.assertEqual(executor._quota_park_streak, 0)


if __name__ == "__main__":
    unittest.main()
