"""Regression: stop/resume must not kill runs over an unavailable agent pin.

OBS-11: a corporate role template's ``preferred_external_agent: codex`` was
stamped into execution identity even when the run was requested and executed
as native. On resume, the availability gate trusted that pin and failed every
non-terminal work item closed. These tests pin the four legs of the fix:

1. identity truth — seat enrichment and the dispatch selector record the
   backend that actually runs (native fallback when the external agent is
   provably unavailable),
2. resume gate symmetry — a pin without a resumable external session heals
   to native instead of failing the item,
3. control/content separation — a bare "continue" (or force_resume metadata,
   in either spelling) resumes the runtime instead of being routed to the
   final decider as a follow-up.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from opc.core.models import DelegationWorkItem, Task, TaskStatus
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer2_organization.phase import Phase
from opc.layer2_organization.work_item_links import set_linked_work_item_id


class _AdapterRegistryStub:
    def __init__(self, available: list[str]):
        self._available = list(available)

    def list_available(self) -> list[str]:
        return list(self._available)

    def get(self, name: str):
        return object() if name in self._available else None

    def get_ordered_available(self):
        return [(name, object()) for name in self._available]


class PlainResumeControlReplyTests(unittest.TestCase):
    def test_control_tokens_are_plain_resume(self) -> None:
        # includes the Chinese continuation spellings the product accepts
        for reply in ("continue", "  Resume ", "proceed", "ok", "y", "继续", "恢复", ""):
            self.assertTrue(OPCEngine._is_plain_resume_control_reply(reply), reply)

    def test_content_is_not_plain_resume(self) -> None:
        # the Chinese sample starts with a control word but carries content,
        # so it must NOT be treated as a bare control reply
        for reply in ("make a ppt outline", "继续，但先修复报告第3节", "deny"):
            self.assertFalse(OPCEngine._is_plain_resume_control_reply(reply), reply)

    def test_force_resume_metadata_both_spellings(self) -> None:
        self.assertTrue(OPCEngine._reply_metadata_requests_force_resume({"ui_force_resume": True}))
        self.assertTrue(OPCEngine._reply_metadata_requests_force_resume({"force_resume": True}))
        self.assertFalse(OPCEngine._reply_metadata_requests_force_resume({"other": True}))
        self.assertFalse(OPCEngine._reply_metadata_requests_force_resume(None))


class LockedAgentAvailabilityFallbackTests(unittest.IsolatedAsyncioTestCase):
    def _engine(self, available: list[str]) -> OPCEngine:
        engine = OPCEngine(project_id="p")
        engine.org_engine = SimpleNamespace()
        engine.adapter_registry = _AdapterRegistryStub(available)
        return engine

    async def test_locked_unavailable_external_falls_back_to_native(self) -> None:
        engine = self._engine(available=[])
        task = Task(id="t1", title="t", project_id="p", session_id="s")
        task.metadata["execution_agent_locked"] = True
        task.metadata["selected_execution_agent"] = "codex"

        selected = await engine._assign_task_execution_agent(task)

        self.assertIsNone(selected)
        self.assertIsNone(task.assigned_external_agent)
        self.assertEqual(task.metadata["selected_execution_agent"], "native")
        self.assertEqual(task.metadata["execution_agent_unavailable"], "codex")
        self.assertEqual(
            task.metadata["agent_selection"]["decision_reason"],
            "locked_external_agent_unavailable_native_fallback",
        )

    async def test_locked_available_external_is_kept(self) -> None:
        engine = self._engine(available=["codex"])
        task = Task(id="t2", title="t", project_id="p", session_id="s")
        task.metadata["execution_agent_locked"] = True
        task.metadata["selected_execution_agent"] = "codex"

        selected = await engine._assign_task_execution_agent(task)

        self.assertEqual(selected, "codex")
        self.assertEqual(task.assigned_external_agent, "codex")


class SeatEnrichmentIdentityTruthTests(unittest.TestCase):
    def _engine(self, available: list[str], role_preferred: str | None) -> OPCEngine:
        engine = OPCEngine(project_id="p")
        engine.adapter_registry = _AdapterRegistryStub(available)
        role = SimpleNamespace(preferred_external_agent=role_preferred)
        engine.org_engine = SimpleNamespace(
            get_agent=lambda role_id: role,
            get_employee=lambda employee_id: None,
            get_default_employee_for_role=lambda role_id: None,
            list_employees=lambda role_id=None: [],
            ensure_fallback_employee_for_role=lambda role_id, persist=False: None,
        )
        return engine

    def _enrich(self, engine: OPCEngine, preferred_agent: str | None) -> dict:
        decision = SimpleNamespace(preferred_agent=preferred_agent)
        topology = {"seats": [{"role_id": "cto", "seat_id": "seat-cto"}]}
        enriched = engine._enrich_runtime_delegation_topology(
            runtime_topology=topology,
            decision=decision,
            project_id="p",
        )
        return enriched["seats"][0]

    def test_explicit_native_wins_over_role_preference(self) -> None:
        engine = self._engine(available=["codex"], role_preferred="codex")
        seat = self._enrich(engine, preferred_agent="native")
        self.assertEqual(seat["selected_execution_agent"], "native")
        self.assertTrue(seat["force_native_execution"])

    def test_unavailable_role_preference_resolves_to_native(self) -> None:
        engine = self._engine(available=[], role_preferred="codex")
        seat = self._enrich(engine, preferred_agent=None)
        self.assertEqual(seat["selected_execution_agent"], "native")
        self.assertEqual(seat["execution_agent_unavailable"], "codex")

    def test_available_role_preference_is_kept(self) -> None:
        engine = self._engine(available=["codex"], role_preferred="codex")
        seat = self._enrich(engine, preferred_agent=None)
        self.assertEqual(seat["selected_execution_agent"], "codex")
        self.assertEqual(seat["execution_agent_unavailable"], "")


class ResumeGateHealTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.store = OPCStore(Path(self._tmp.name) / "tasks.db")
        await self.store.initialize()
        self.engine = OPCEngine(project_id="p")
        self.engine.store = self.store
        self.engine.adapter_registry = _AdapterRegistryStub([])

    async def asyncTearDown(self) -> None:
        await self.store.close()
        self._tmp.cleanup()

    async def _seed(self) -> Task:
        item = DelegationWorkItem(
            work_item_id="wi-1",
            run_id="run-1",
            role_id="cto",
            kind="execute",
            title="survey",
            phase=Phase.READY,
        )
        await self.store.save_delegation_work_item(item)
        task = Task(
            id="task-1",
            title="survey",
            project_id="p",
            session_id="s",
            status=TaskStatus.RUNNING,
            metadata={"delegation_run_id": "run-1"},
        )
        set_linked_work_item_id(task, "wi-1")
        await self.store.save_task(task)
        return task

    def _payload(self, task: Task, external_sessions: dict) -> dict:
        return {
            "checkpoint_id": "ckpt-1",
            "task_snapshots": [
                {
                    "task_id": task.id,
                    "execution_identity": {
                        "selected_execution_agent": "codex",
                        "assigned_external_agent": "codex",
                    },
                    "work_item": {"work_item_id": "wi-1"},
                }
            ],
            "active_work_items": [{"work_item_id": "wi-1", "phase": "ready"}],
            "external_sessions": external_sessions,
            "native_runtime_resume": {},
        }

    async def test_pin_without_external_session_heals_to_native(self) -> None:
        task = await self._seed()
        payload = self._payload(task, external_sessions={})

        refreshed = await self.engine._prepare_company_runtime_tasks_for_resume(
            [task], payload
        )

        prepared = refreshed[0]
        self.assertIsNone(prepared.assigned_external_agent)
        self.assertEqual(prepared.metadata.get("selected_execution_agent"), "native")
        self.assertEqual(
            prepared.metadata.get("resume_execution_agent_healed_from"), "codex"
        )
        pin = dict(prepared.metadata.get("_company_runtime_resume_execution_agent_pin", {}))
        self.assertEqual(pin.get("selected_execution_agent"), "native")
        self.assertEqual(pin.get("assigned_external_agent", ""), "")
        item = await self.store.get_delegation_work_item("wi-1")
        self.assertEqual(item.phase, Phase.READY)

    async def test_pin_with_live_external_session_still_fails_closed(self) -> None:
        task = await self._seed()
        payload = self._payload(
            task,
            external_sessions={
                task.id: {
                    "status": "active",
                    "agent_type": "codex",
                    "resume_session_id": "sess-1",
                }
            },
        )

        await self.engine._prepare_company_runtime_tasks_for_resume([task], payload)

        item = await self.store.get_delegation_work_item("wi-1")
        self.assertEqual(item.phase, Phase.FAILED)
        self.assertIn("pinned to external agent", str(item.blocked_reason or ""))


if __name__ == "__main__":
    unittest.main()
