from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from opc.database.store import OPCStore
from opc.core.models import ExternalSession, Task
from opc.layer3_agent.adapters.base import ExternalAgentAdapter
from opc.layer3_agent.adapters.jiuwen_adapter import JiuwenAdapter, JiuwenSwarmAdapter
from opc.layer3_agent.external_team_activity import reduce_external_team_events
from opc.plugins.office_ui.services.models import ServiceError
from opc.plugins.office_ui.services.runtime import RuntimeService


def _frame(event: str, payload: dict) -> str:
    return json.dumps({"type": "event", "event": event, "payload": payload})


def test_team_adapter_uses_only_official_structured_events() -> None:
    adapter = JiuwenSwarmAdapter()
    spawned = adapter.extract_external_team_events(
        _frame("team.member", {
            "session_id": "provider-1",
            "event": {
                "type": "team.member.spawned",
                "team_id": "team-1",
                "member_id": "researcher-a",
                "mode": "teammate",
            },
        }),
        "stdout",
    )
    created = adapter.extract_external_team_events(
        _frame("chat.final", {
            "event_type": "team.task",
            "session_id": "provider-1",
            "event": {
                "type": "team.task.created",
                "team_id": "team-1",
                "task_id": "task-a",
                "status": "pending",
            },
        }),
        "stdout",
    )

    assert spawned == [{
        "kind": "member_spawned",
        "team_id": "team-1",
        "raw_event_type": "team.member.spawned",
        "member": {
            "member_id": "researcher-a",
            "name": "researcher-a",
            "role": "",
            "mode": "teammate",
            "status": "",
            "execution_status": "",
            "reason": "",
            "restart_count": None,
        },
        "provider_session_id": "provider-1",
    }]
    assert created[0]["kind"] == "task_created"
    assert created[0]["task"]["task_id"] == "task-a"

    # These are intentionally not team membership/task observations.
    assert adapter.extract_external_team_events(
        _frame("todo.updated", {"items": [{"content": "delegate research"}]}),
        "stdout",
    ) == []
    assert adapter.extract_external_team_events(
        _frame("tool.call", {"tool_name": "task_tool", "arguments": {"agent": "researcher"}}),
        "stdout",
    ) == []
    assert adapter.extract_external_team_events(
        _frame("workflow.updated", {"event_type": "workflow.updated"}),
        "stdout",
    ) == []


@pytest.mark.parametrize(
    ("raw_type", "expected_kind", "payload_key"),
    [
        ("team.member.spawned", "member_spawned", "member"),
        ("team.member.status_changed", "member_status_changed", "member"),
        ("team.member.execution_changed", "member_execution_changed", "member"),
        ("team.member.restarted", "member_restarted", "member"),
        ("team.member.shutdown", "member_shutdown", "member"),
        ("team.task.created", "task_created", "task"),
        ("team.task.claimed", "task_claimed", "task"),
        ("team.task.completed", "task_completed", "task"),
        ("team.task.cancelled", "task_cancelled", "task"),
        ("team.task.unblocked", "task_unblocked", "task"),
        ("team.message.p2p", "message_p2p", "message"),
        ("team.message.broadcast", "message_broadcast", "message"),
    ],
)
def test_team_adapter_covers_direct_official_lifecycle_events(
    raw_type: str,
    expected_kind: str,
    payload_key: str,
) -> None:
    adapter = JiuwenSwarmAdapter()
    payload = {
        "type": raw_type,
        "session_id": "provider-direct",
        "team_id": "team-direct",
        "member_id": "worker-direct",
        "new_status": "working",
        "task_id": "task-direct",
        "content": "direct event content",
        "from_member": "leader",
        "to_member": "worker-direct",
    }
    events = adapter.extract_external_team_events(_frame(raw_type, payload), "stdout")

    assert len(events) == 1
    assert events[0]["kind"] == expected_kind
    assert events[0][payload_key]
    if raw_type == "team.member.execution_changed":
        assert events[0]["member"]["execution_status"] == "working"


@pytest.mark.parametrize(
    ("event_type", "expected_kind"),
    [
        ("team.runtime_ready", "runtime_ready"),
        ("team.completed", "team_completed"),
        ("team.error", "team_error"),
    ],
)
def test_team_adapter_covers_team_runtime_boundaries(event_type: str, expected_kind: str) -> None:
    events = JiuwenSwarmAdapter().extract_external_team_events(
        _frame(event_type, {
            "session_id": "provider-runtime",
            "team_id": "team-runtime",
            "message": "runtime boundary",
        }),
        "stdout",
    )
    assert len(events) == 1
    assert events[0]["kind"] == expected_kind


def test_unknown_team_events_are_timeline_only_and_do_not_create_entities() -> None:
    event = JiuwenSwarmAdapter().extract_external_team_events(
        _frame("team.member", {
            "event": {
                "type": "team.member.future_protocol_event",
                "member_id": "must-not-be-counted",
            },
        }),
        "stdout",
    )[0]
    assert event["kind"] == "provider_event"
    assert "member" not in event
    assert "must-not-be-counted" in event["summary"]


def test_teammate_delta_without_member_identity_is_not_aggregated() -> None:
    adapter = JiuwenSwarmAdapter()
    assert adapter.extract_external_team_events(
        _frame("chat.delta", {
            "session_id": "provider-1",
            "rid": "round-1",
            "role": "teammate",
            "content": "unscoped output",
        }),
        "stdout",
    ) == []
    assert adapter.extract_external_team_events(
        _frame("chat.llm_usage", {
            "session_id": "provider-1",
            "rid": "round-1",
            "role": "teammate",
        }),
        "stdout",
    ) == []


def test_single_and_base_adapters_never_emit_team_activity() -> None:
    raw = _frame("team.member", {
        "event": {"type": "team.member.spawned", "member_id": "worker"},
    })
    assert ExternalAgentAdapter.extract_external_team_events(None, raw, "stdout") == []
    assert JiuwenAdapter().extract_external_team_events(raw, "stdout") == []


def test_member_outputs_are_isolated_by_member() -> None:
    adapter = JiuwenSwarmAdapter()
    for member, content in (("alpha", "Alpha result"), ("beta", "Beta result")):
        assert adapter.extract_external_team_events(
            _frame("chat.delta", {
                "session_id": "provider-1",
                "rid": "round-1",
                "role": "teammate",
                "member_name": member,
                "content": content,
            }),
            "stdout",
        ) == []

    alpha = adapter.extract_external_team_events(
        _frame("chat.llm_usage", {
            "session_id": "provider-1",
            "rid": "round-1",
            "role": "teammate",
            "member_name": "alpha",
        }),
        "stdout",
    )
    beta = adapter.extract_external_team_events(
        _frame("chat.llm_usage", {
            "session_id": "provider-1",
            "rid": "round-1",
            "role": "teammate",
            "member_name": "beta",
        }),
        "stdout",
    )
    assert [(event["member"]["member_id"], event["output"]) for event in alpha + beta] == [
        ("alpha", "Alpha result"),
        ("beta", "Beta result"),
    ]


def test_member_final_boundary_flushes_one_output_without_duplication() -> None:
    adapter = JiuwenSwarmAdapter()
    adapter.extract_external_team_events(
        _frame("chat.delta", {
            "session_id": "provider-1",
            "rid": "round-1",
            "role": "teammate",
            "member_name": "alpha",
            "content": "Alpha result",
        }),
        "stdout",
    )
    events = adapter.extract_external_team_events(
        _frame("chat.final", {
            "session_id": "provider-1",
            "rid": "round-1",
            "role": "teammate",
            "member_name": "alpha",
            "content": "Alpha result",
        }),
        "stdout",
    )
    assert len(events) == 1
    assert events[0]["kind"] == "member_output"
    assert events[0]["output"] == "Alpha result"


def test_member_output_buffer_does_not_cross_invocation_boundary(tmp_path: Path) -> None:
    adapter = JiuwenSwarmAdapter()
    adapter.extract_external_team_events(
        _frame("chat.delta", {
            "session_id": "provider-resumed",
            "rid": "round-reused",
            "role": "teammate",
            "member_name": "worker",
            "content": "stale partial output",
        }),
        "stdout",
    )
    adapter.build_invocation(Task(title="next invocation"), str(tmp_path))
    assert adapter.extract_external_team_events(
        _frame("chat.llm_usage", {
            "session_id": "provider-resumed",
            "rid": "round-reused",
            "role": "teammate",
            "member_name": "worker",
        }),
        "stdout",
    ) == []


def test_team_projection_is_idempotent_and_lifecycle_complete() -> None:
    events = [
        {"event_id": "1", "sequence": 1, "occurred_at": "2026-09-03T10:00:00", "kind": "runtime_ready"},
        {
            "event_id": "2", "sequence": 2, "occurred_at": "2026-09-03T10:00:01", "kind": "member_spawned",
            "member": {"member_id": "worker", "status": "working"},
        },
        {
            "event_id": "3", "sequence": 3, "occurred_at": "2026-09-03T10:00:02", "kind": "task_created",
            "task": {"task_id": "internal-1", "status": "pending"},
        },
        {
            "event_id": "4", "sequence": 4, "occurred_at": "2026-09-03T10:00:03", "kind": "task_claimed",
            "task": {"task_id": "internal-1", "assignee": "worker"},
        },
        {
            "event_id": "5", "sequence": 5, "occurred_at": "2026-09-03T10:00:04", "kind": "task_completed",
            "task": {"task_id": "internal-1"},
        },
        {"event_id": "6", "sequence": 6, "occurred_at": "2026-09-03T10:00:05", "kind": "team_completed"},
    ]
    summary = reduce_external_team_events([events[4], *events, events[1]])
    assert summary["mode"] == "completed"
    assert summary["counts"]["members"] == 1
    assert summary["counts"]["tasks_completed"] == 1


async def _assert_persisted_events_are_isolated_and_paginated(tmp_path: Path) -> None:
    store = OPCStore(tmp_path / "store.db")
    await store.initialize()
    try:
        for sequence, invocation in ((1, "inv-a"), (2, "inv-b"), (3, "inv-a")):
            await store.save_runtime_event(
                "runtime-1",
                "external_team.activity",
                {
                    "event_id": f"event-{sequence}",
                    "sequence": sequence,
                    "external_invocation_id": invocation,
                    "kind": "provider_event",
                },
            )
        first = await store.list_external_team_events("runtime-1", "inv-a", limit=1)
        assert [row["payload"]["event_id"] for row in first["events"]] == ["event-3"]
        assert first["has_more"] is True
        cursor = first["next_cursor"]
        second = await store.list_external_team_events(
            "runtime-1",
            "inv-a",
            limit=1,
            before_created_at=cursor["before_created_at"],
            before_event_id=cursor["before_event_id"],
        )
        assert [row["payload"]["event_id"] for row in second["events"]] == ["event-1"]
        assert all(row["payload"]["external_invocation_id"] == "inv-a" for row in first["events"] + second["events"])

        lifecycle = [
            ("team-1", "runtime_ready", {}),
            ("team-2", "member_spawned", {"member": {"member_id": "researcher"}}),
            ("team-3", "task_created", {"task": {"task_id": "research"}}),
            ("team-4", "task_claimed", {"task": {"task_id": "research", "assignee": "researcher"}}),
            ("team-5", "message_broadcast", {"message": {"from_member": "team-leader", "content": "Research the market."}}),
            ("team-6", "member_output", {"member": {"member_id": "researcher"}, "output": "result"}),
        ]
        for event_id, kind, extra in lifecycle:
            await store.save_runtime_event(
                "runtime-2",
                "external_team.activity",
                {
                    "event_id": event_id,
                    "project_id": "project-a",
                    "task_id": "task-team",
                    "external_invocation_id": "inv-team",
                    "kind": kind,
                    **extra,
                },
            )
        await store.save_runtime_event(
            "runtime-2",
            "external_team.activity",
            {
                "event_id": "recovery-1",
                "project_id": "project-a",
                "task_id": "task-team",
                "external_invocation_id": "inv-recovery",
                "kind": "runtime_ready",
            },
        )
        invocations = await store.list_external_team_invocations(
            "runtime-2", "task-team"
        )
        by_invocation = {
            row["external_invocation_id"]: row for row in invocations
        }
        assert by_invocation["inv-team"]["member_count"] == 1
        assert by_invocation["inv-team"]["task_count"] == 1
        assert by_invocation["inv-team"]["message_count"] == 1
        assert by_invocation["inv-team"]["output_count"] == 1
        projection = await store.list_external_team_projection_events(
            "runtime-2", "inv-team"
        )
        assert {row["payload"]["kind"] for row in projection} == {
            "runtime_ready", "member_spawned", "task_created", "task_claimed",
            "message_broadcast",
        }
    finally:
        await store.close()


def test_persisted_events_are_isolated_and_paginated(tmp_path: Path) -> None:
    asyncio.run(_assert_persisted_events_are_isolated_and_paginated(tmp_path))


async def _assert_activity_service_revalidates_project_and_team_identity() -> None:
    class FakeStore:
        async def get_task(self, task_id: str):
            return Task(id=task_id, title="Team turn", project_id="project-a")

        async def list_external_sessions(self, **_kwargs):
            return [
                ExternalSession(
                    agent_type="jiuwenswarm",
                    task_id="task-a",
                    metadata={"execution_unit_kind": "external_agent", "external_invocation_id": "wrong"},
                ),
                ExternalSession(
                    agent_type="jiuwenswarm",
                    task_id="task-a",
                    session_id="provider-a",
                    metadata={
                        "execution_unit_kind": "opaque_external_team",
                        "external_invocation_id": "inv-a",
                        "runtime_session_id": "runtime-a",
                    },
                ),
            ]

        async def list_external_team_events(self, runtime_session_id: str, invocation_id: str, **_kwargs):
            assert runtime_session_id == "runtime-a"
            assert invocation_id == "inv-a"
            return {
                "events": [{"payload": {
                    "schema_version": 1,
                    "event_id": "ready-a",
                    "provider": "jiuwenswarm",
                    "project_id": "project-a",
                    "task_id": "task-a",
                    "opc_session_id": "session-a",
                    "external_invocation_id": "inv-a",
                    "sequence": 1,
                    "occurred_at": "2026-09-03T10:00:00",
                    "kind": "runtime_ready",
                }}],
                "has_more": False,
                "next_cursor": None,
            }

    class Context:
        async def engine_for_project(self, project_id: str):
            assert project_id in {"project-a", "project-b"}
            return SimpleNamespace(store=FakeStore())

    service = RuntimeService(Context(), SimpleNamespace())
    result = await service.external_team_activity(project_id="project-a", task_id="task-a")
    assert result.payload["available"] is True
    assert result.payload["external_invocation_id"] == "inv-a"
    assert result.payload["summary"]["mode"] == "leader_only"

    missing = await service.external_team_activity(
        project_id="project-a",
        task_id="task-a",
        external_invocation_id="inv-missing",
    )
    assert missing.payload["available"] is False
    assert missing.payload["external_invocation_id"] == "inv-missing"

    with pytest.raises(ServiceError, match="task_not_found"):
        await service.external_team_activity(project_id="project-b", task_id="task-a")


def test_activity_service_revalidates_project_and_team_identity() -> None:
    asyncio.run(_assert_activity_service_revalidates_project_and_team_identity())


async def _assert_activity_service_prefers_observed_collaboration() -> None:
    def payload(invocation: str, event_id: str, kind: str, **extra):
        return {
            "schema_version": 1,
            "event_id": event_id,
            "provider": "jiuwenswarm",
            "project_id": "project-a",
            "task_id": "task-a",
            "opc_session_id": "session-a",
            "external_invocation_id": invocation,
            "sequence": 1,
            "occurred_at": "2026-09-04T10:00:00",
            "kind": kind,
            **extra,
        }

    collaboration_events = [
        payload("inv-team", "ready", "runtime_ready"),
        payload(
            "inv-team", "spawn", "member_spawned",
            member={"member_id": "researcher", "name": "researcher"},
        ),
        payload(
            "inv-team", "claimed", "task_claimed",
            task={"task_id": "research", "assignee": "researcher"},
        ),
    ]

    class FakeStore:
        async def get_task(self, task_id: str):
            return Task(id=task_id, title="Team turn", project_id="project-a")

        async def list_external_sessions(self, **_kwargs):
            return [ExternalSession(
                agent_type="jiuwenswarm",
                task_id="task-a",
                session_id="provider-a",
                metadata={
                    "execution_unit_kind": "opaque_external_team",
                    "external_invocation_id": "inv-recovery",
                    "runtime_session_id": "runtime-a",
                },
            )]

        async def list_external_team_invocations(self, *_args):
            return [
                {
                    "external_invocation_id": "inv-team", "started_at": "2026-09-04T09:00:00",
                    "last_event_at": "2026-09-04T09:10:00", "event_count": 20,
                    "member_count": 1, "task_count": 1, "message_count": 2, "output_count": 1,
                },
                {
                    "external_invocation_id": "inv-recovery", "started_at": "2026-09-04T10:00:00",
                    "last_event_at": "2026-09-04T10:01:00", "event_count": 10,
                    "member_count": 0, "task_count": 0, "message_count": 0, "output_count": 0,
                },
            ]

        async def list_external_team_events(self, _runtime_id, invocation_id, **_kwargs):
            events = collaboration_events if invocation_id == "inv-team" else [
                payload("inv-recovery", "recovery-ready", "runtime_ready")
            ]
            return {
                "events": [{"payload": event} for event in events],
                "has_more": False,
                "next_cursor": None,
            }

        async def list_external_team_projection_events(self, _runtime_id, invocation_id):
            events = collaboration_events if invocation_id == "inv-team" else [
                payload("inv-recovery", "recovery-ready", "runtime_ready")
            ]
            return [{"payload": event} for event in events]

    class Context:
        async def engine_for_project(self, _project_id: str):
            return SimpleNamespace(store=FakeStore())

    service = RuntimeService(Context(), SimpleNamespace())
    preferred = await service.external_team_activity(
        project_id="project-a", task_id="task-a"
    )
    assert preferred.payload["external_invocation_id"] == "inv-team"
    assert preferred.payload["summary"]["counts"]["members"] == 1
    assert next(
        row for row in preferred.payload["invocations"]
        if row["external_invocation_id"] == "inv-team"
    )["is_preferred"] is True
    assert next(
        row for row in preferred.payload["invocations"]
        if row["external_invocation_id"] == "inv-recovery"
    )["is_latest"] is True

    recovery = await service.external_team_activity(
        project_id="project-a",
        task_id="task-a",
        external_invocation_id="inv-recovery",
    )
    assert recovery.payload["external_invocation_id"] == "inv-recovery"
    assert recovery.payload["summary"]["counts"]["members"] == 0


def test_activity_service_prefers_observed_collaboration() -> None:
    asyncio.run(_assert_activity_service_prefers_observed_collaboration())


@pytest.mark.skipif(
    os.getenv("OPENOPC_RUN_LIVE_JIUWEN_TEAM_SMOKE") != "1",
    reason="set OPENOPC_RUN_LIVE_JIUWEN_TEAM_SMOKE=1 to use a real Jiuwen gateway",
)
def test_live_jiuwen_gateway_emits_team_activity(tmp_path: Path) -> None:
    async def run() -> None:
        adapter = JiuwenSwarmAdapter()
        if not await adapter.is_available():
            pytest.skip("Jiuwen gateway is unavailable")
        task = Task(
            id="live-team-telemetry-smoke",
            title="Reply with a one-line confirmation; delegate only if you judge it necessary.",
            project_id="live-team-telemetry-smoke",
            session_id="live-team-telemetry-smoke",
        )
        command, metadata = adapter.build_invocation(task, str(tmp_path))
        process = await adapter.start_process(
            command,
            str(tmp_path),
            task=task,
            launch_metadata=metadata,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        stdout_reader = asyncio.create_task(process.stdout.read())
        stderr_reader = asyncio.create_task(process.stderr.read())
        try:
            await asyncio.wait_for(process.wait(), timeout=180)
            stdout, stderr = await asyncio.gather(stdout_reader, stderr_reader)
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()
            for reader in (stdout_reader, stderr_reader):
                if not reader.done():
                    reader.cancel()
            await adapter.cleanup_process(process)
        assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
        events = [
            event
            for line in stdout.decode("utf-8", errors="replace").splitlines()
            for event in adapter.extract_external_team_events(line, "stdout")
        ]
        assert any(event["kind"] == "runtime_ready" for event in events)
        assert any(
            event["kind"] in {"leader_output", "member_output", "team_completed"}
            for event in events
        )

    asyncio.run(run())
