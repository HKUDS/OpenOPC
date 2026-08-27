from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import importlib.util
import json
import re
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
import pytest


_HARNESS_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "issue35_company_e2e.py"
)
_SPEC = importlib.util.spec_from_file_location("issue35_company_e2e", _HARNESS_PATH)
assert _SPEC is not None and _SPEC.loader is not None
harness = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = harness
_SPEC.loader.exec_module(harness)


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({}, ""),
        (
            {
                "ceo_pre_delivery_assessment": {
                    "assessment_status": "completed",
                    "assessment_failure_kind": "",
                    "assessment_infrastructure_failure": False,
                    "deliverable": True,
                }
            },
            "",
        ),
        ({"pre_delivery_assessment_failure_kind": "top-level"}, "top-level"),
        (
            {
                "ceo_pre_delivery_assessment": {
                    "assessment_status": "unavailable",
                    "deliverable": True,
                }
            },
            "assessment_unavailable",
        ),
        (
            {
                "ceo_pre_delivery_assessment": {
                    "assessment_status": "completed",
                    "assessment_failure_kind": "nested-failure",
                    "deliverable": True,
                }
            },
            "nested-failure",
        ),
        (
            {
                "ceo_pre_delivery_assessment": {
                    "assessment_status": "completed",
                    "assessment_infrastructure_failure": True,
                    "deliverable": True,
                }
            },
            "assessment_infrastructure_failure",
        ),
    ],
)
def test_pre_delivery_assessment_failure_is_rejected_from_either_projection(
    metadata: dict[str, Any],
    expected: str,
) -> None:
    assert harness._pre_delivery_assessment_failure_kind(metadata) == expected


def _write_investment_quality_artifacts(
    root: Path,
    notes: dict[str, dict[str, Any]],
    report: str,
) -> None:
    output = root / "investment_case"
    output.mkdir(parents=True, exist_ok=True)
    for name, document in notes.items():
        (output / name).write_text(
            json.dumps(document, indent=2),
            encoding="utf-8",
        )
    (output / "report.md").write_text(report, encoding="utf-8")


def _investment_quality_fixture(
    root: Path,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], str]:
    retrieval_date = "2026-08-13"
    facts = {
        "NVDA": {
            "company": "NVIDIA",
            "domain": "investor.nvidia.com",
            "value": "$68.1 billion",
            "period": "Q4 FY2026",
            "period_end": "2026-01-25",
        },
        "AMD": {
            "company": "AMD",
            "domain": "ir.amd.com",
            "value": "$10.3 billion",
            "period": "Q1 2026",
            "period_end": "2026-03-28",
        },
        "AVGO": {
            "company": "Broadcom AVGO",
            "domain": "investors.broadcom.com",
            "value": "$15.0 billion",
            "period": "Q2 FY2026",
            "period_end": "2026-05-03",
        },
    }
    runtime_details: list[dict[str, Any]] = []
    notes: dict[str, dict[str, Any]] = {}
    role_to_note = {
        "investment_analyst": "company_analysis.json",
        "risk_analyst": "risk_analysis.json",
    }
    urls_by_role: dict[str, dict[str, str]] = {}
    for role_id, note_name in role_to_note.items():
        role_suffix = "company" if role_id == "investment_analyst" else "risk"
        calls: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        claims: list[dict[str, Any]] = []
        urls_by_role[role_id] = {}
        for index, (ticker, fact) in enumerate(facts.items(), start=1):
            call_id = f"{role_suffix}-{ticker.lower()}"
            source_url = (
                f"https://{fact['domain']}/financial-results/"
                f"{role_suffix}-{ticker.lower()}-2026"
            )
            urls_by_role[role_id][ticker] = source_url
            calls.append(
                {
                    "tool_call_id": call_id,
                    "tool_name": "web_search",
                    "arguments": {
                        "query": (
                            f"{ticker} {fact['company']} 2026 latest official "
                            "reported financial results"
                        )
                    },
                }
            )
            results.append(
                {
                    "result_record_id": f"result-{call_id}",
                    "tool_call_id": call_id,
                    "tool_name": "web_search",
                    "created_at": f"{retrieval_date}T01:0{index}:00+00:00",
                    "payload": {
                        "success": True,
                        "result": {
                            "results": [
                                {
                                    "title": (
                                        f"{fact['company']} reports {fact['period']} "
                                        f"revenue {fact['value']}"
                                    ),
                                    "snippet": (
                                        f"Official {ticker} results for "
                                        f"{fact['period']} released in 2026."
                                    ),
                                    "url": source_url,
                                }
                            ]
                        },
                    },
                }
            )
            claims.append(
                {
                    "ticker": ticker,
                    "kind": "sourced_fact",
                    "value_token": fact["value"],
                    "period_token": fact["period"],
                    "period_end": fact["period_end"],
                    "source_url": source_url,
                    "retrieval_date": retrieval_date,
                }
            )
        runtime_details.append(
            {
                "task_id": f"task-{role_suffix}",
                "role_id": role_id,
                "runtime_session_id": f"runtime-{role_suffix}",
                "calls": calls,
                "results": results,
            }
        )
        note: dict[str, Any] = {
            "analysis_date": retrieval_date,
            "as_of": retrieval_date,
            "horizon_years": [2026, 2027, 2028],
            "critical_claims": claims,
            "analysis": "Substantive sourced analysis and scenario framing.",
            "scenarios": {
                "bear": "Demand and margins contract materially.",
                "base": "Reported execution broadly continues.",
                "bull": "AI infrastructure demand exceeds expectations.",
            },
        }
        if role_id == "investment_analyst":
            note.update(
                {
                    "company_profiles": {
                        ticker: {
                            "thesis": f"{ticker} has a differentiated AI thesis.",
                            "catalysts": [f"{ticker} product and demand catalyst."],
                            "valuation_caveats": [
                                f"{ticker} valuation requires execution discipline."
                            ],
                        }
                        for ticker in harness.INVESTMENT_TICKERS
                    },
                    "ranked_recommendation": (
                        "Rank NVDA first, AMD second, and AVGO third with caveats."
                    ),
                    "position_sizing_guardrails": [
                        "Cap each issuer and rebalance on thesis breaks."
                    ],
                }
            )
        else:
            note.update(
                {
                    "risk_register": {
                        ticker: {
                            "downside_risks": [f"{ticker} downside risk."],
                            "monitor_triggers": [f"{ticker} risk trigger."],
                            "sizing_guardrail": f"Cap {ticker} exposure.",
                        }
                        for ticker in harness.INVESTMENT_TICKERS
                    },
                    "portfolio_guardrails": [
                        "Cap aggregate exposure and review correlated drawdowns."
                    ],
                }
            )
        notes[note_name] = note
    report_lines = [
        "# Ranked recommendation",
        "Analysis date: 2026-08-13",
        "Horizon years: 2026, 2027, 2028",
        "",
        "## Verified critical facts",
        "| Child | Ticker | Value | Period | URL | Retrieved |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for role_id, note_name in role_to_note.items():
        for claim in notes[note_name]["critical_claims"]:
            report_lines.append(
                f"| {note_name} | {claim['ticker']} | {claim['value_token']} | "
                f"{claim['period_token']} | {urls_by_role[role_id][claim['ticker']]} | "
                f"{retrieval_date} |"
            )
    report = "\n".join(report_lines)
    _write_investment_quality_artifacts(root, notes, report)
    return runtime_details, notes, report


class _MemoryCheckpointStore:
    def __init__(self) -> None:
        self.rows: list[Any] = []
        self.tasks: list[Any] = []
        self.delegation_runs: list[Any] = []
        self.work_items_by_run: dict[str, list[Any]] = {}

    async def get_execution_checkpoints(
        self,
        *,
        project_id: str,
        checkpoint_types: list[str],
        statuses: list[str],
    ) -> list[Any]:
        return [
            row
            for row in self.rows
            if row.project_id == project_id
            and row.checkpoint_type in checkpoint_types
            and (not statuses or row.status in statuses)
        ]

    async def get_task(self, task_id: str) -> Any | None:
        return next(
            (task for task in self.tasks if task.id == task_id),
            None,
        )

    async def get_tasks(self, *, project_id: str) -> list[Any]:
        return [task for task in self.tasks if task.project_id == project_id]

    async def save_task(self, task: Any) -> None:
        self.tasks = [item for item in self.tasks if item.id != task.id]
        self.tasks.append(task)

    async def list_runtime_tool_results(
        self,
        runtime_session_id: str,
    ) -> list[dict[str, Any]]:
        del runtime_session_id
        return []

    async def list_delegation_runs(
        self,
        *,
        project_id: str,
        session_id: str,
    ) -> list[Any]:
        return [
            run
            for run in self.delegation_runs
            if str(getattr(run, "project_id", "") or "") == project_id
            and str(getattr(run, "session_id", "") or "") == session_id
        ]

    async def list_delegation_work_items(self, run_id: str) -> list[Any]:
        return list(self.work_items_by_run.get(run_id, []))


class _PreDeliveryLedgerStore:
    def __init__(
        self,
        *,
        tasks: list[Any],
        runtime_details: list[dict[str, Any]],
    ) -> None:
        self.tasks = list(tasks)
        self.sessions_by_task: dict[str, list[dict[str, Any]]] = {}
        self.calls_by_runtime: dict[str, list[dict[str, Any]]] = {}
        self.results_by_runtime: dict[str, list[dict[str, Any]]] = {}
        for index, detail in enumerate(runtime_details, start=1):
            task_id = str(detail["task_id"])
            runtime_session_id = f"rt_pre_delivery_{index}"
            self.sessions_by_task[task_id] = [
                {
                    "runtime_session_id": runtime_session_id,
                    "status": "completed",
                    "metadata": {
                        "runtime_session_id": runtime_session_id,
                        "resume_cursor": 3,
                    },
                    "created_at": f"2026-08-13T00:0{index}:00+00:00",
                    "updated_at": f"2026-08-13T00:0{index}:30+00:00",
                }
            ]
            self.calls_by_runtime[runtime_session_id] = copy.deepcopy(
                detail["calls"]
            )
            self.results_by_runtime[runtime_session_id] = copy.deepcopy(
                detail["results"]
            )

    async def get_tasks(self, *, project_id: str) -> list[Any]:
        return [
            task
            for task in self.tasks
            if str(getattr(task, "project_id", "") or "") == project_id
        ]

    async def list_runtime_sessions(
        self,
        *,
        project_id: str,
        task_id: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        del project_id, limit
        return copy.deepcopy(self.sessions_by_task.get(task_id, []))

    async def list_runtime_tool_calls(
        self,
        runtime_session_id: str,
    ) -> list[dict[str, Any]]:
        return copy.deepcopy(self.calls_by_runtime.get(runtime_session_id, []))

    async def list_runtime_tool_results(
        self,
        runtime_session_id: str,
    ) -> list[dict[str, Any]]:
        return copy.deepcopy(self.results_by_runtime.get(runtime_session_id, []))


def _checkpoint(
    *,
    checkpoint_id: str,
    checkpoint_type: str,
    session_id: str,
    payload: dict[str, Any],
) -> Any:
    return SimpleNamespace(
        checkpoint_id=checkpoint_id,
        checkpoint_type=checkpoint_type,
        project_id="project-a",
        session_id=session_id,
        task_id=None,
        status="pending",
        payload=payload,
    )


def _resumed_staffing_outcome_unknown_fixture() -> tuple[Any, Any]:
    session_id = "issue35-recovered-session"
    spec = harness.CaseSpec(
        case_id="recovered",
        title="Recovered staffing",
        org_id="recovered-native-org",
        organization_name="Recovered Native Org",
        roles=({"id": "lead"}, {"id": "worker"}),
        prompt="Run",
        required_artifacts=(),
    )
    checkpoint_id = "staffing-recovered"
    request_id = (
        f"issue35-e2e:{session_id}:{checkpoint_id}:native-staffing"
    )
    selections = {
        role_id: {"kind": "fallback", "id": ""}
        for role_id in ("lead", "worker")
    }
    role_agents = {role_id: "native" for role_id in ("lead", "worker")}
    decision_value = {
        "staffing_action": "manual_approve",
        "staffing_selections": selections,
        "recruitment_role_agents": role_agents,
        "recruitment_agent": "native",
        "text": "approve",
    }
    submission = {
        "checkpoint_id": checkpoint_id,
        "checkpoint_type": harness.STAFFING_CHECKPOINT_TYPE,
        "root_session_id": session_id,
        "company_profile": "custom",
        "org_id": spec.org_id,
        "staffing_action": "manual_approve",
        "staffing_selections": selections,
        "recruitment_role_agents": role_agents,
        "recruitment_agent": "native",
        "client_request_id": request_id,
        "receipt": {
            "accepted": True,
            "status": "answered",
            "checkpoint_id": checkpoint_id,
            "checkpoint_type": harness.STAFFING_CHECKPOINT_TYPE,
        },
    }
    run = harness.CaseRun(
        spec=spec,
        session_id=session_id,
        ui_anchor_task_id="df5d268c-55a6-4a52-a15a-c3cf9978df7d",
        started_at="now",
        staffing_decisions=[submission],
        resume_existing=True,
    )
    checkpoint = _checkpoint(
        checkpoint_id=checkpoint_id,
        checkpoint_type=harness.STAFFING_CHECKPOINT_TYPE,
        session_id=session_id,
        payload={
            "company_profile": "custom",
            "org_id": spec.org_id,
            "primary_session_id": session_id,
            "interaction": {
                "kind": harness.STAFFING_CHECKPOINT_TYPE,
                "domain_key": "durable-domain-key",
                "execution_scope": {
                    "company_profile": "custom",
                    "org_id": spec.org_id,
                },
                "decision": {
                    "request_id": request_id,
                    "decision_hash": harness._canonical_interaction_decision_hash(
                        decision_value
                    ),
                    "value": decision_value,
                },
                "claim": {
                    "claim_id": "expired-claim",
                    "consumer_id": "crashed-consumer",
                    "claimed_at": "2026-08-13T14:04:46.820421",
                    "lease_expires_at": "2026-08-13T14:13:16.849527",
                },
                "execution": {
                    "state": "outcome_unknown",
                    "claim_id": "expired-claim",
                    "consumer_id": "crashed-consumer",
                    "started_at": "2026-08-13T14:04:46.823296",
                    "detected_at": "2026-08-13T14:42:00.882799",
                    "reason": "execution_lease_expired",
                },
                "completion": {
                    "claim_id": "expired-claim",
                    "consumer_id": "crashed-consumer",
                    "finished_at": "2026-08-13T14:42:00.882799",
                    "final_status": "outcome_unknown",
                },
            }
        },
    )
    checkpoint.status = "outcome_unknown"
    return run, checkpoint


def _install_exact_staffing_recovery_runtime(
    store: _MemoryCheckpointStore,
    run: Any,
    staffing: Any,
    *,
    interruption_id: str = "runtime-interrupted",
    interruption_status: str = "pending",
    run_id: str = "delegation-run-recovered",
    historical_resolved: int = 0,
) -> tuple[Any, Any, Any]:
    """Install the durable run28 crash-recovery ownership chain."""

    origin_task_id = f"origin-{run_id}"
    root_work_item_id = f"root-{run_id}"
    claim = staffing.payload["interaction"]["claim"]
    origin = {
        "checkpoint_id": staffing.checkpoint_id,
        "checkpoint_type": harness.STAFFING_CHECKPOINT_TYPE,
        "project_id": "project-a",
        "claim_id": claim["claim_id"],
        "consumer_id": claim["consumer_id"],
    }
    delegation_run = SimpleNamespace(
        run_id=run_id,
        project_id="project-a",
        session_id=run.session_id,
        company_profile="custom",
        execution_model="actor_runtime",
        final_decider_role_id=str(run.spec.roles[0]["id"]),
        status="running",
        lifecycle_status=(
            "awaiting_owner" if interruption_status == "resolved" else "active"
        ),
        controller_owner_token="",
        recovery_pointer={
            "project_id": "project-a",
            "session_id": run.session_id,
        },
        metadata={
            "root_work_item_id": root_work_item_id,
            "runtime_spec": {
                "metadata": {"organization_id": run.spec.org_id}
            },
            "origin_owner_interaction": copy.deepcopy(origin),
        },
    )
    origin_task = SimpleNamespace(
        id=origin_task_id,
        project_id="project-a",
        parent_session_id=run.session_id,
        metadata={
            "delegation_run_id": run_id,
            "origin_owner_interaction": copy.deepcopy(origin),
        },
    )
    root_work_item = SimpleNamespace(
        work_item_id=root_work_item_id,
        projection_id="root",
        kind="execute",
        phase=SimpleNamespace(value="suspended"),
        blocked_reason="",
        metadata={"origin_owner_interaction": copy.deepcopy(origin)},
    )
    interruption_payload = {
        "checkpoint_type": "company_runtime_interrupted",
        "version": 2,
        "reason": "startup_recovery",
        "project_id": "project-a",
        "run_id": run_id,
        "company_profile": "custom",
        "parent_session_id": run.session_id,
        "session_id": run.session_id,
        "root_session_id": run.session_id,
        "origin_task_id": origin_task_id,
        "task_ids": [origin_task_id],
        "basis_hash": "run28-recovery-basis",
        "ui_anchor_task_id": run.ui_anchor_task_id,
    }
    if interruption_status == "resolved":
        interruption_payload.update(
            {
                "suspend_started_at": "2026-08-13T14:42:00+00:00",
                "suspend_finalized_at": "2026-08-13T14:42:01+00:00",
                "resume_started_at": "2026-08-13T14:42:30+00:00",
                "resume_handoff_at": "2026-08-13T14:43:00+00:00",
                "resume_resolved_at": "2026-08-13T14:43:01+00:00",
                "resume_state": "handoff_complete",
                "resume_controller_lease_generation": 2,
                "ui_anchor_task_id": run.ui_anchor_task_id,
            }
        )
    interrupted = _checkpoint(
        checkpoint_id=interruption_id,
        checkpoint_type="company_runtime_interrupted",
        session_id=run.session_id,
        payload=interruption_payload,
    )
    interrupted.task_id = origin_task_id
    interrupted.status = interruption_status

    store.delegation_runs.append(delegation_run)
    store.tasks.append(origin_task)
    store.work_items_by_run[run_id] = [root_work_item]
    for index in range(historical_resolved):
        historical = copy.deepcopy(interrupted)
        historical.checkpoint_id = f"historical-runtime-interrupted-{index}"
        historical.status = "resolved"
        historical.payload.update(
            {
                "suspend_started_at": "2026-08-12T14:42:00+00:00",
                "suspend_finalized_at": "2026-08-12T14:42:01+00:00",
                "resume_started_at": "2026-08-12T14:42:30+00:00",
                "resume_handoff_at": "2026-08-12T14:43:00+00:00",
                "resume_resolved_at": "2026-08-12T14:43:01+00:00",
                "resume_state": "handoff_complete",
                "resume_controller_lease_generation": 1,
                "ui_anchor_task_id": run.ui_anchor_task_id,
            }
        )
        store.rows.append(historical)
    store.rows.append(interrupted)
    return delegation_run, origin_task, interrupted


def _resolve_runtime_interruption(delegation_run: Any, interrupted: Any) -> None:
    delegation_run.lifecycle_status = "awaiting_owner"
    interrupted.status = "resolved"
    interrupted.payload.update(
        {
            "suspend_started_at": "2026-08-13T14:42:00+00:00",
            "suspend_finalized_at": "2026-08-13T14:42:01+00:00",
            "resume_started_at": "2026-08-13T14:42:30+00:00",
            "resume_handoff_at": "2026-08-13T14:43:00+00:00",
            "resume_resolved_at": "2026-08-13T14:43:01+00:00",
            "resume_state": "handoff_complete",
            "resume_controller_lease_generation": 2,
            "ui_anchor_task_id": interrupted.payload["ui_anchor_task_id"],
        }
    )


def _runtime_session(
    runtime_session_id: str,
    status: str,
    *,
    metadata: dict[str, Any] | None = None,
    created_at: str = "2026-08-12T12:00:00+00:00",
    updated_at: str = "",
) -> dict[str, Any]:
    return {
        "runtime_session_id": runtime_session_id,
        "status": status,
        "metadata": dict(metadata or {}),
        "created_at": created_at,
        "updated_at": updated_at,
    }


def test_native_runtime_evidence_ignores_newer_idle_role_session() -> None:
    rows = [
        _runtime_session(
            "role-session::run-15::engineer",
            "idle",
            metadata={
                "member_session_id": "role-session::run-15::engineer",
                "resident_status": "idle",
            },
            updated_at="2026-08-12T12:01:00",
        ),
        _runtime_session(
            "rt_completed_execution",
            "completed",
            metadata={
                "runtime_session_id": "rt_completed_execution",
                "resume_cursor": 9,
            },
            updated_at="2026-08-12T12:00:00",
        ),
    ]

    selected = harness._single_completed_native_execution_runtime(
        rows,
        case_id="run15",
        task_id="execution-task",
    )

    assert selected["runtime_session_id"] == "rt_completed_execution"


def test_native_runtime_evidence_selects_latest_completed_rework_attempt() -> None:
    rows = [
        _runtime_session(
            "rt_second",
            "completed",
            created_at="2026-08-12T12:02:00+00:00",
        ),
        _runtime_session(
            "rt_first",
            "failed",
            created_at="2026-08-12T12:01:00+00:00",
        ),
    ]

    selected = harness._single_completed_native_execution_runtime(
        rows,
        case_id="run15",
        task_id="execution-task",
    )

    assert selected["runtime_session_id"] == "rt_second"


@pytest.mark.parametrize("old_status", ["running", "awaiting_human", "blocked"])
def test_native_runtime_evidence_rejects_unsettled_older_attempt(
    old_status: str,
) -> None:
    rows = [
        _runtime_session(
            "rt_latest",
            "completed",
            created_at="2026-08-12T12:02:00+00:00",
        ),
        _runtime_session(
            "rt_old",
            old_status,
            created_at="2026-08-12T12:01:00+00:00",
        ),
    ]

    with pytest.raises(AssertionError, match="unsettled"):
        harness._single_completed_native_execution_runtime(
            rows,
            case_id="run15",
            task_id="execution-task",
        )


def test_native_runtime_evidence_rejects_noncompleted_latest_attempt() -> None:
    rows = [
        _runtime_session(
            "rt_latest",
            "failed",
            created_at="2026-08-12T12:02:00+00:00",
        ),
        _runtime_session(
            "rt_old",
            "completed",
            created_at="2026-08-12T12:01:00+00:00",
        ),
    ]

    with pytest.raises(AssertionError, match="latest.*not completed"):
        harness._single_completed_native_execution_runtime(
            rows,
            case_id="run15",
            task_id="execution-task",
        )


def test_native_runtime_evidence_rejects_tied_latest_attempts() -> None:
    rows = [
        _runtime_session("rt_second", "completed"),
        _runtime_session("rt_first", "completed"),
    ]

    with pytest.raises(AssertionError, match="ambiguous latest"):
        harness._single_completed_native_execution_runtime(
            rows,
            case_id="run15",
            task_id="execution-task",
        )


def test_native_runtime_evidence_rejects_missing_attempt_created_at() -> None:
    rows = [_runtime_session("rt_missing_time", "completed", created_at="")]

    with pytest.raises(AssertionError, match="lacks durable created_at"):
        harness._single_completed_native_execution_runtime(
            rows,
            case_id="run15",
            task_id="execution-task",
        )


def test_native_runtime_evidence_rejects_role_session_only() -> None:
    rows = [
        _runtime_session(
            "role-session::run-15::engineer",
            "idle",
            metadata={
                "member_session_id": "role-session::run-15::engineer",
                "resident_status": "idle",
            },
        )
    ]

    try:
        harness._single_completed_native_execution_runtime(
            rows,
            case_id="run15",
            task_id="execution-task",
        )
    except AssertionError as exc:
        assert "has no NativeRuntimeV2 execution runtime" in str(exc)
        assert "role-session::run-15::engineer" in str(exc)
    else:
        raise AssertionError("a role-session projection was accepted as execution")


def _pre_delivery_validator_fixture(
    tmp_path: Path,
) -> tuple[
    Any,
    _PreDeliveryLedgerStore,
    Any,
    list[Any],
    list[dict[str, Any]],
]:
    runtime_details, _notes, _report = _investment_quality_fixture(tmp_path)
    run_id = "investment-delegation-run"
    root_session_id = "issue35-investment-prefinal"
    common_metadata = {
        "delegation_run_id": run_id,
        "org_id": "issue35-investment-native",
        "execution_mode": "company_mode",
    }
    analyst = SimpleNamespace(
        id="task-company",
        project_id="project-a",
        session_id=f"{root_session_id}:analyst",
        parent_session_id=root_session_id,
        assigned_to="investment_analyst",
        metadata={
            **common_metadata,
            "work_item_turn_type": "execute",
            "work_item_projection_id": "investment-analyst-projection",
        },
    )
    risk = SimpleNamespace(
        id="task-risk",
        project_id="project-a",
        session_id=f"{root_session_id}:risk",
        parent_session_id=root_session_id,
        assigned_to="risk_analyst",
        metadata={
            **common_metadata,
            "work_item_turn_type": "execute",
            "work_item_projection_id": "risk-analyst-projection",
        },
    )
    delivery = SimpleNamespace(
        id="task-delivery",
        project_id="project-a",
        session_id=f"{root_session_id}:delivery",
        parent_session_id=root_session_id,
        assigned_to="investment_lead",
        metadata={
            **common_metadata,
            "work_item_turn_type": "deliver",
            "work_item_projection_id": "investment-delivery-projection",
        },
    )
    tasks = [analyst, risk, delivery]
    store = _PreDeliveryLedgerStore(
        tasks=tasks,
        runtime_details=runtime_details,
    )
    validator = harness._Issue35PreDeliveryQualityValidator(
        workplace=tmp_path,
        project_id="project-a",
    )
    validator.register_runs(
        [
            harness.CaseRun(
                spec=harness.CASES[0],
                session_id=root_session_id,
                ui_anchor_task_id="anchor",
                started_at="2026-08-13T00:00:00+00:00",
            )
        ]
    )
    validator.bind_store(store)
    return validator, store, delivery, tasks, runtime_details


def test_investment_pre_delivery_validator_uses_durable_ledger_and_artifacts(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        validator, store, delivery, tasks, _details = (
            _pre_delivery_validator_fixture(tmp_path)
        )

        result = await validator(delivery, None, tasks, {})

        assert result["valid"] is True
        assert result["issues"] == []
        assert result["rework_target_projection_ids"] == []
        evidence = result["evidence"]
        assert evidence["scope"] == "investment"
        assert set(evidence["artifact_sha256"]) == set(
            harness.INVESTMENT_REQUIRED_ARTIFACTS
        )
        assert len(evidence["consumed_web_ledger"]["tool_calls"]) == 6
        assert len(evidence["consumed_web_ledger"]["tool_results"]) == 6
        assert {
            item["runtime_session_id"] for item in evidence["runtime_inputs"]
        } == {"rt_pre_delivery_1", "rt_pre_delivery_2"}

        role_tasks = {
            task.assigned_to: task
            for task in tasks
            if task.assigned_to in {"investment_analyst", "risk_analyst"}
        }
        late_details = await harness._investment_runtime_details_from_store(
            store,
            project_id="project-a",
            role_tasks=role_tasks,
            case_id="late collector",
        )
        late_gate = harness._investment_data_quality_gate(
            tmp_path,
            late_details,
            "2026-08-13T00:00:00+00:00",
        )
        matched = harness._assert_investment_pre_delivery_evidence_matches(
            evidence,
            project_id="project-a",
            delegation_run_id="investment-delegation-run",
            root_session_id="issue35-investment-prefinal",
            run_started_at="2026-08-13T00:00:00+00:00",
            workplace=tmp_path,
            runtime_details=late_details,
            quality_gate=late_gate,
        )
        assert matched["matched"] is True
        assert matched["consumed_tool_call_count"] == 6
        assert matched["consumed_tool_result_count"] == 6

        with (tmp_path / "investment_case/report.md").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write("\nPost-validation drift.\n")
        with pytest.raises(AssertionError, match="no longer matches"):
            harness._assert_investment_pre_delivery_evidence_matches(
                evidence,
                project_id="project-a",
                delegation_run_id="investment-delegation-run",
                root_session_id="issue35-investment-prefinal",
                run_started_at="2026-08-13T00:00:00+00:00",
                workplace=tmp_path,
                runtime_details=late_details,
                quality_gate=late_gate,
            )

    asyncio.run(scenario())


def test_investment_late_evidence_reuses_execute_projection_scope(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        validator, store, delivery, tasks, _details = (
            _pre_delivery_validator_fixture(tmp_path)
        )
        persisted = await validator(delivery, None, tasks, {})
        assert persisted["valid"] is True

        common_metadata = dict(tasks[0].metadata)
        store.tasks.extend(
            [
                SimpleNamespace(
                    id="task-company-report",
                    project_id="project-a",
                    assigned_to="investment_analyst",
                    metadata={
                        **common_metadata,
                        "work_item_turn_type": "report",
                        "work_item_projection_id": "company-report-projection",
                    },
                ),
                SimpleNamespace(
                    id="task-risk-review",
                    project_id="project-a",
                    assigned_to="risk_analyst",
                    metadata={
                        **common_metadata,
                        "work_item_turn_type": "review",
                        "work_item_projection_id": "risk-review-projection",
                    },
                ),
            ]
        )
        durable_tasks = await store.get_tasks(project_id="project-a")
        role_tasks, late_details = (
            await harness._investment_execute_runtime_details_from_store(
                store,
                project_id="project-a",
                durable_tasks=durable_tasks,
                case_id="late evidence regression",
            )
        )

        assert {role: task.id for role, task in role_tasks.items()} == {
            "investment_analyst": "task-company",
            "risk_analyst": "task-risk",
        }
        assert [detail["task_id"] for detail in late_details] == [
            "task-company",
            "task-risk",
        ]
        late_gate = harness._investment_data_quality_gate(
            tmp_path,
            late_details,
            "2026-08-13T00:00:00+00:00",
        )
        evidence_kwargs = {
            "project_id": "project-a",
            "delegation_run_id": "investment-delegation-run",
            "root_session_id": "issue35-investment-prefinal",
            "run_started_at": "2026-08-13T00:00:00+00:00",
            "workplace": tmp_path,
            "quality_gate": late_gate,
        }
        assert harness._assert_investment_pre_delivery_evidence_matches(
            persisted["evidence"],
            runtime_details=late_details,
            **evidence_kwargs,
        )["matched"] is True

        reordered = harness._investment_pre_delivery_evidence(
            runtime_details=list(reversed(late_details)),
            **evidence_kwargs,
        )
        assert reordered == persisted["evidence"]

        invalid_scopes = [
            late_details[:1],
            [
                late_details[0],
                {
                    **late_details[1],
                    "work_item_turn_type": "review",
                },
            ],
            [
                late_details[0],
                {
                    **late_details[1],
                    "role_id": "investment_analyst",
                },
            ],
            [
                *late_details,
                {
                    **late_details[0],
                    "task_id": "task-company-report",
                    "role_id": "investment_lead",
                    "work_item_turn_type": "report",
                    "work_item_projection_id": "company-report-projection",
                    "runtime_session_id": "runtime-company-report",
                },
            ],
        ]
        for invalid_scope in invalid_scopes:
            with pytest.raises(AssertionError, match="pre-delivery evidence"):
                harness._investment_pre_delivery_evidence(
                    runtime_details=invalid_scope,
                    **evidence_kwargs,
                )

    asyncio.run(scenario())


def test_investment_pre_delivery_validator_maps_child_and_report_rework(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        risk_root = tmp_path / "risk-failure"
        validator, _store, delivery, tasks, _details = (
            _pre_delivery_validator_fixture(risk_root)
        )
        (risk_root / "investment_case/risk_analysis.json").write_text(
            "{", encoding="utf-8"
        )

        risk_result = await validator(delivery, None, tasks, {})

        assert risk_result["valid"] is False
        assert risk_result["rework_target_projection_ids"] == [
            "risk-analyst-projection"
        ]
        assert list(risk_result["rework_issues_by_projection_id"]) == [
            "risk-analyst-projection"
        ]
        assert "risk_analysis.json" in risk_result["issues"][0]

        report_root = tmp_path / "report-failure"
        validator, _store, delivery, tasks, _details = (
            _pre_delivery_validator_fixture(report_root)
        )
        (report_root / "investment_case/report.md").unlink()

        report_result = await validator(delivery, None, tasks, {})

        assert report_result["valid"] is False
        assert report_result["rework_target_projection_ids"] == [
            "investment-delivery-projection"
        ]
        assert list(report_result["rework_issues_by_projection_id"]) == [
            "investment-delivery-projection"
        ]
        assert "report.md" in report_result["issues"][0]

    asyncio.run(scenario())


def test_investment_pre_delivery_attributes_invalid_report_url_to_delivery(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        validator, _store, delivery, tasks, _details = (
            _pre_delivery_validator_fixture(tmp_path)
        )
        report_path = tmp_path / "investment_case/report.md"
        report_path.write_text(
            report_path.read_text(encoding="utf-8")
            + "\nDo not leave a bare `http://` placeholder in the report.\n",
            encoding="utf-8",
        )

        result = await validator(delivery, None, tasks, {})

        assert result["valid"] is False
        assert result["rework_target_projection_ids"] == [
            "investment-delivery-projection"
        ]
        assert result["rework_issues_by_projection_id"] == {
            "investment-delivery-projection": result["issues"]
        }
        assert any(
            issue.startswith("report.md:")
            and "unsupported evidence URL" in issue
            for issue in result["issues"]
        )

    asyncio.run(scenario())


def test_investment_report_origin_takes_rework_routing_precedence() -> None:
    role_projection_ids = {
        "investment_analyst": "investment-analyst-projection",
        "risk_analyst": "risk-analyst-projection",
    }

    assert harness._investment_rework_targets_for_issue(
        "report.md: Verified critical facts lacks a unique exact row for "
        "company_analysis.json critical_claims[0]",
        role_projection_ids=role_projection_ids,
        delivery_projection_id="investment-delivery-projection",
    ) == ["investment-delivery-projection"]
    assert harness._investment_rework_targets_for_issue(
        "opaque validator failure",
        role_projection_ids=role_projection_ids,
        delivery_projection_id="investment-delivery-projection",
    ) == [
        "investment-analyst-projection",
        "risk-analyst-projection",
    ]


def test_investment_pre_delivery_validator_aggregates_multi_domain_rework(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        validator, _store, delivery, tasks, _details = (
            _pre_delivery_validator_fixture(tmp_path)
        )
        artifact_root = tmp_path / "investment_case"
        (artifact_root / "company_analysis.json").write_text(
            "{",
            encoding="utf-8",
        )
        risk = json.loads(
            (artifact_root / "risk_analysis.json").read_text(encoding="utf-8")
        )
        risk["critical_claims"][0]["period_end"] = "2025-07-27"
        (artifact_root / "risk_analysis.json").write_text(
            json.dumps(risk),
            encoding="utf-8",
        )
        report = (artifact_root / "report.md").read_text(encoding="utf-8")
        report = report.replace(
            "| company_analysis.json | NVDA |",
            "| company_analysis | NVDA |",
        )
        (artifact_root / "report.md").write_text(report, encoding="utf-8")

        result = await validator(delivery, None, tasks, {})

        assert result["valid"] is False
        assert result["rework_target_projection_ids"] == [
            "investment-analyst-projection",
            "risk-analyst-projection",
            "investment-delivery-projection",
        ]
        assert list(result["rework_issues_by_projection_id"]) == [
            "investment-analyst-projection",
            "risk-analyst-projection",
            "investment-delivery-projection",
        ]
        for projection_id, projection_issues in result[
            "rework_issues_by_projection_id"
        ].items():
            assert projection_issues
            if projection_id == "investment-delivery-projection":
                assert all("report.md" in issue for issue in projection_issues)
            elif projection_id == "investment-analyst-projection":
                assert all(
                    "company_analysis.json" in issue
                    for issue in projection_issues
                )
            else:
                assert all(
                    "risk_analysis.json" in issue for issue in projection_issues
                )
        assert any("company_analysis.json" in issue for issue in result["issues"])
        assert any("risk_analysis.json" in issue for issue in result["issues"])
        assert any("report.md" in issue for issue in result["issues"])
        assert result["evidence"]["quality_failures"] == result["issues"]

    asyncio.run(scenario())


def test_investment_pre_delivery_validator_aggregates_current_year_by_role(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        validator, store, delivery, tasks, _details = (
            _pre_delivery_validator_fixture(tmp_path)
        )
        for calls in store.calls_by_runtime.values():
            calls[0]["arguments"]["query"] = calls[0]["arguments"][
                "query"
            ].replace("2026", "latest")

        result = await validator(delivery, None, tasks, {})

        assert result["valid"] is False
        assert any(
            "investment_analyst lacks" in issue and "NVDA" in issue
            for issue in result["issues"]
        )
        assert any(
            "risk_analyst lacks" in issue and "NVDA" in issue
            for issue in result["issues"]
        )
        assert result["rework_target_projection_ids"] == [
            "investment-analyst-projection",
            "risk-analyst-projection",
        ]
        assert all(
            "investment_analyst" in issue
            for issue in result["rework_issues_by_projection_id"][
                "investment-analyst-projection"
            ]
        )
        assert all(
            "risk_analyst" in issue
            for issue in result["rework_issues_by_projection_id"][
                "risk-analyst-projection"
            ]
        )

    asyncio.run(scenario())


def test_investment_issue_aggregation_uses_strict_markdown_visibility(
    tmp_path: Path,
) -> None:
    runtime_details, _notes, _report = _investment_quality_fixture(tmp_path)
    report_path = tmp_path / "investment_case/report.md"
    report_path.write_text(
        report_path.read_text(encoding="utf-8")
        + "\n```text\nAnalysis date: 1999-01-01\n"
        + "## Verified critical facts\n```\n",
        encoding="utf-8",
    )

    assert harness._investment_quality_issues(
        tmp_path,
        runtime_details,
        "2026-08-13T00:00:00+00:00",
    ) == []


def test_investment_pre_delivery_validator_rejects_live_old_attempt(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        validator, store, delivery, tasks, _details = (
            _pre_delivery_validator_fixture(tmp_path)
        )
        store.sessions_by_task["task-company"].insert(
            0,
            {
                "runtime_session_id": "rt_unsettled_old",
                "status": "running",
                "metadata": {},
                "created_at": "2026-08-13T00:00:30+00:00",
                "updated_at": "2026-08-13T00:00:31+00:00",
            },
        )

        with pytest.raises(AssertionError, match="unsettled"):
            await validator(delivery, None, tasks, {})

    asyncio.run(scenario())


def test_pre_delivery_validator_is_investment_only_without_store_binding() -> None:
    async def scenario() -> None:
        validator = harness._Issue35PreDeliveryQualityValidator(
            workplace=Path("unused"),
            project_id="project-a",
        )
        app_run = harness.CaseRun(
            spec=harness.CASES[1],
            session_id="issue35-app-prefinal",
            ui_anchor_task_id="app-anchor",
            started_at="2026-08-13T00:00:00+00:00",
        )
        validator.register_runs([app_run])
        app_delivery = SimpleNamespace(
            session_id="issue35-app-prefinal:delivery",
            parent_session_id="issue35-app-prefinal",
            metadata={
                "org_id": "issue35-app-native",
                "work_item_projection_id": "app-delivery",
            },
            assigned_to="engineering_manager",
        )

        result = await validator(app_delivery, None, [app_delivery], {})

        assert result == {
            "valid": True,
            "evidence": {
                "validator_id": "issue35_investment_data_quality",
                "schema_version": 1,
                "scope": "not_applicable",
                "org_id": "issue35-app-native",
            },
            "issues": [],
            "rework_target_projection_ids": [],
        }

    asyncio.run(scenario())


@pytest.mark.parametrize("org_id", ["", "issue35-app-native", "wrong-org"])
def test_investment_pre_delivery_validator_fails_closed_on_org_drift(
    tmp_path: Path,
    org_id: str,
) -> None:
    async def scenario() -> None:
        validator, _store, delivery, tasks, _details = (
            _pre_delivery_validator_fixture(tmp_path)
        )
        delivery.metadata["org_id"] = org_id

        with pytest.raises(RuntimeError, match="delivery org_id"):
            await validator(delivery, None, tasks, {})

    asyncio.run(scenario())


def test_investment_prompt_matches_strict_source_url_validator(
    tmp_path: Path,
) -> None:
    investment = harness.CASES[0]
    lead = next(
        role for role in investment.roles if role["id"] == "investment_lead"
    )
    prompt = investment.prompt
    responsibility = str(lead["responsibility"])

    assert "copy the exact analysis date" in prompt
    assert "into both delegate_work descriptions and acceptance criteria" in prompt
    assert "delegated briefs" in responsibility
    assert "repeat the root mandate's exact analysis date" in responsibility
    assert "critical-claim evidence rules" in responsibility
    assert "at least three literal, complete `http://` or `https://` URLs" in prompt
    assert "source IDs, citation IDs, or footnotes do not substitute" in prompt
    assert "same source-table row as every URL" in prompt
    assert "Verified critical facts" in prompt
    assert "exactly six rows" in prompt
    assert "do not deduplicate identical claims" in prompt
    assert "exactly three sourced facts" in prompt
    assert "exactly one NVDA, one AMD, and one AVGO" in prompt
    assert "seven keys and no others" in prompt
    assert "Forecasts, outlook, guidance, estimates" in prompt
    assert '"critical_claims":[' in prompt
    assert "explicitly audit all seven fields" in prompt
    assert "| Child | Ticker | Value | Period | URL | Retrieved |" in prompt
    assert "Do not put this section or table inside a code fence" in prompt
    assert "file_read on investment_case/report.md" in prompt
    assert "use file_edit to correct the report" in prompt
    assert "at least three literal complete http:// or https:// URLs" in responsibility
    assert "each paired with its retrieval date" in responsibility
    assert "use file_read to self-check" in responsibility
    assert "Verified critical facts table with exactly six rows" in responsibility
    assert "exact three-object critical_claims example" in responsibility
    assert "explicitly read back all seven fields" in responsibility
    for required_key in (
        "company_profiles",
        "ranked_recommendation",
        "scenarios",
        "position_sizing_guardrails",
        "risk_register",
        "portfolio_guardrails",
    ):
        assert required_key in prompt
        assert required_key in responsibility
    assert "period_end` must be in the analysis calendar year" in prompt
    assert "semantically equivalent wording only" in prompt
    assert "role-specific substantive-section example" in prompt
    for role_id in ("investment_analyst", "risk_analyst"):
        role = next(role for role in investment.roles if role["id"] == role_id)
        role_text = str(role["responsibility"])
        assert "exactly three critical_claims" in role_text
        assert "one each for NVDA, AMD, and AVGO" in role_text
        assert "forecast, guidance, outlook, or future period" in role_text
        assert "file_read the JSON and verify every field" in role_text
    analyst_text = str(
        next(
            role
            for role in investment.roles
            if role["id"] == "investment_analyst"
        )["responsibility"]
    )
    risk_text = str(
        next(
            role for role in investment.roles if role["id"] == "risk_analyst"
        )["responsibility"]
    )
    assert "company_profiles" in analyst_text
    assert "position_sizing_guardrails" in analyst_text
    assert "risk_register" in risk_text
    assert "portfolio_guardrails" in risk_text

    output_dir = tmp_path / "investment_case"
    output_dir.mkdir()
    note_payload = {
        "analysis": "current public evidence " * 20,
        "source": "https://research.example/native-note",
    }
    for name in ("company_analysis.json", "risk_analysis.json"):
        (output_dir / name).write_text(
            json.dumps(note_payload),
            encoding="utf-8",
        )
    report_prefix = (
        "# Recommendation\nNVIDIA AMD Broadcom bear base bull. "
        + ("Evidence, caveats, sizing, and scenario analysis. " * 30)
    )
    valid_source_table = """
| Source | URL | Retrieved |
| --- | --- | --- |
| Filing | https://example.com/filing | 2026-08-10 |
| Results | http://example.net/results | 2026-08-11 |
| Market | https://example.org/market | 2026-08-12 |
"""
    report_path = output_dir / "report.md"
    report_path.write_text(report_prefix + valid_source_table, encoding="utf-8")

    checks = harness._validate_real_artifacts(investment, tmp_path)
    assert checks["source_urls_present"] is True
    assert checks["retrieval_dates_present"] is True

    report_path.write_text(
        report_prefix
        + "Background link without a retrieval date: https://example.edu/context\n"
        + valid_source_table,
        encoding="utf-8",
    )
    checks = harness._validate_real_artifacts(investment, tmp_path)
    assert checks["source_urls_present"] is True
    assert checks["retrieval_dates_present"] is True

    missing_date = valid_source_table.replace(
        "| Market | https://example.org/market | 2026-08-12 |",
        "| Market | https://example.org/market | not recorded |",
    )
    report_path.write_text(report_prefix + missing_date, encoding="utf-8")
    try:
        harness._validate_real_artifacts(investment, tmp_path)
    except AssertionError as exc:
        assert "retrieval_dates_present" in str(exc)
    else:
        raise AssertionError("a source URL without its retrieval date was accepted")

    source_ids_only = re.sub(r"https?://[^\s|<>()]+", "source-id", valid_source_table)
    report_path.write_text(report_prefix + source_ids_only, encoding="utf-8")
    try:
        harness._validate_real_artifacts(investment, tmp_path)
    except AssertionError as exc:
        assert "source_urls_present" in str(exc)
    else:
        raise AssertionError("source IDs were accepted in place of literal URLs")


def test_investment_prompt_renders_exact_dynamic_date_and_horizon() -> None:
    investment = next(
        spec for spec in harness.CASES if spec.case_id == "investment"
    )

    rendered = harness._render_case_prompt(
        investment,
        "2031-02-03T23:59:00+08:00",
    )

    assert harness.INVESTMENT_RUN_DATE_PLACEHOLDER not in rendered
    assert harness.INVESTMENT_HORIZON_PLACEHOLDER not in rendered
    assert "analysis date for this run is 2031-02-03" in rendered
    assert "Analysis date: 2031-02-03" in rendered
    assert "Horizon years: 2031, 2032, 2033" in rendered
    assert "current year" not in rendered or "2031" in rendered


def test_investment_quality_gate_accepts_closed_role_provenance(
    tmp_path: Path,
) -> None:
    runtime_details, _, _ = _investment_quality_fixture(tmp_path)

    evidence = harness._investment_data_quality_gate(
        tmp_path,
        runtime_details,
        "2026-08-13T00:00:00+08:00",
    )

    assert evidence["analysis_date"] == "2026-08-13"
    assert evidence["horizon_years"] == [2026, 2027, 2028]
    assert evidence["role_scoped_provenance_closed"] is True
    assert evidence["critical_claims_supported"] is True
    assert evidence["report_distinct_url_count"] == 6
    assert len(evidence["verified_critical_fact_rows"]) == 6
    assert all(
        set(role_calls) == set(harness.INVESTMENT_TICKERS)
        for role_calls in evidence["current_query_tool_calls"].values()
    )
    assert all(
        note["substantive_schema"]["substantive_contract_valid"] is True
        for note in evidence["notes"].values()
    )
    assert all(
        note["official_claims_by_ticker"]
        == {"NVDA": 1, "AMD": 1, "AVGO": 1}
        for note in evidence["notes"].values()
    )


def test_investment_quality_rejects_run22_minimal_risk_shape(
    tmp_path: Path,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    notes = copy.deepcopy(notes)
    risk_note = notes["risk_analysis.json"]
    notes["risk_analysis.json"] = {
        key: risk_note[key]
        for key in ("analysis_date", "as_of", "horizon_years", "critical_claims")
    }
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(AssertionError, match="risk_analysis.json: scenarios"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


@pytest.mark.parametrize(
    ("path", "invalid_value", "message"),
    [
        (("company_profiles",), {}, "company_profiles"),
        (("company_profiles", "NVDA", "thesis"), "", "thesis"),
        (("company_profiles", "AMD", "catalysts"), [], "catalysts"),
        (
            ("company_profiles", "AVGO", "valuation_caveats"),
            [],
            "valuation_caveats",
        ),
        (("ranked_recommendation",), "", "ranked_recommendation"),
        (("scenarios", "bear"), "", "scenario"),
        (("position_sizing_guardrails",), [], "position_sizing_guardrails"),
    ],
)
def test_investment_quality_rejects_incomplete_company_substantive_schema(
    tmp_path: Path,
    path: tuple[str, ...],
    invalid_value: Any,
    message: str,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    notes = copy.deepcopy(notes)
    target: dict[str, Any] = notes["company_analysis.json"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = invalid_value
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(AssertionError, match=message):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


@pytest.mark.parametrize(
    ("path", "invalid_value", "message"),
    [
        (("risk_register",), {}, "risk_register"),
        (("risk_register", "NVDA", "downside_risks"), [], "downside_risks"),
        (("risk_register", "AMD", "monitor_triggers"), [], "monitor_triggers"),
        (("risk_register", "AVGO", "sizing_guardrail"), "", "sizing_guardrail"),
        (("scenarios", "bull"), "", "scenario"),
        (("portfolio_guardrails",), [], "portfolio_guardrails"),
    ],
)
def test_investment_quality_rejects_incomplete_risk_substantive_schema(
    tmp_path: Path,
    path: tuple[str, ...],
    invalid_value: Any,
    message: str,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    notes = copy.deepcopy(notes)
    target: dict[str, Any] = notes["risk_analysis.json"]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = invalid_value
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(AssertionError, match=message):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


def test_investment_quality_rejects_report_claim_without_exact_verified_row(
    tmp_path: Path,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    report = report.replace("$68.1 billion", "$68.2 billion", 1)
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(
        AssertionError,
        match="Verified critical facts (?:lacks|requires)",
    ):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00+08:00",
        )


@pytest.mark.parametrize("mutation", ["late_heading", "ticker_alias", "prose"])
def test_investment_quality_rejects_fake_verified_fact_section_rows(
    tmp_path: Path,
    mutation: str,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    if mutation == "late_heading":
        report = report.replace("## Verified critical facts\n", "", 1)
        report = f"{report}\n\n## Verified critical facts\n"
    elif mutation == "ticker_alias":
        report = report.replace(
            "| company_analysis.json | NVDA |",
            "| company_analysis.json | NVIDIA |",
            1,
        )
    else:
        report = "\n".join(
            line.strip("| ") if "_analysis.json" in line else line
            for line in report.splitlines()
        )
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(
        AssertionError,
        match="Verified critical facts (?:lacks|requires)",
    ):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00+08:00",
        )


@pytest.mark.parametrize("mutation", ["fenced", "commented", "no_schema"])
def test_investment_quality_rejects_hidden_or_headerless_verified_table(
    tmp_path: Path,
    mutation: str,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    if mutation == "fenced":
        report = report.replace(
            "## Verified critical facts",
            "```md\n## Verified critical facts",
            1,
        ) + "\n```"
    elif mutation == "commented":
        report = report.replace(
            "## Verified critical facts",
            "<!--\n## Verified critical facts",
            1,
        ) + "\n-->"
    else:
        report = report.replace(
            "| Child | Ticker | Value | Period | URL | Retrieved |\n"
            "| --- | --- | --- | --- | --- | --- |\n",
            "",
            1,
        )
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(
        AssertionError,
        match="Verified critical facts|hidden Markdown",
    ):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00+08:00",
        )


def test_investment_quality_rejects_extra_unmatched_verified_fact_row(
    tmp_path: Path,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    claim = notes["company_analysis.json"]["critical_claims"][0]
    report += (
        "\n| company_analysis.json | NVDA | $999 billion | Q4 FY2026 | "
        f"{claim['source_url']} | {claim['retrieval_date']} |"
    )
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(AssertionError, match="unmatched or extra data rows"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00+08:00",
        )


def test_investment_provenance_unwraps_legacy_ddg_and_rejects_empty_results() -> None:
    legacy_target = "https%3A%2F%2Finvestor.nvidia.com%2Fresults%3Fa%3D1"
    details = [
        {
            "role_id": "investment_analyst",
            "runtime_session_id": "rt",
            "calls": [
                {
                    "tool_call_id": "good",
                    "tool_name": "web_search",
                    "arguments": {"query": "NVDA 2026 official result"},
                },
                {
                    "tool_call_id": "outer-false",
                    "tool_name": "web_search",
                    "arguments": {"query": "AMD 2026 official result"},
                },
                {
                    "tool_call_id": "empty",
                    "tool_name": "web_search",
                    "arguments": {"query": "AVGO 2026 official result"},
                },
            ],
            "results": [
                {
                    "tool_call_id": "good",
                    "tool_name": "web_search",
                    "created_at": "2026-08-13T01:00:00Z",
                    "payload": {
                        "success": True,
                        "result": {
                            "results": [
                                {
                                    "title": "NVIDIA Q4 FY2026 $68.1 billion",
                                    "snippet": "NVDA official result",
                                    "url": (
                                        "//duckduckgo.com/l/?uddg="
                                        f"{legacy_target}&amp;rut=ignored"
                                    ),
                                }
                            ]
                        },
                    },
                },
                {
                    "tool_call_id": "outer-false",
                    "tool_name": "web_search",
                    "created_at": "2026-08-13T01:00:00Z",
                    "payload": {
                        "success": False,
                        "result": {
                            "results": [
                                {
                                    "title": "AMD",
                                    "snippet": "2026 $1 billion",
                                    "url": "https://ir.amd.com/results",
                                }
                            ]
                        },
                    },
                },
                {
                    "tool_call_id": "empty",
                    "tool_name": "web_search",
                    "created_at": "2026-08-13T01:00:00Z",
                    "payload": {"success": True, "result": {"results": []}},
                },
            ],
        }
    ]

    provenance = harness._investment_web_provenance(details)

    assert len(provenance["calls_by_role"]["investment_analyst"]) == 1
    assert list(provenance["hits_by_role"]["investment_analyst"]) == [
        "https://investor.nvidia.com/results?a=1"
    ]


def test_investment_quality_rejects_cross_role_url_leakage(tmp_path: Path) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    notes = copy.deepcopy(notes)
    notes["company_analysis.json"]["critical_claims"][0]["source_url"] = (
        notes["risk_analysis.json"]["critical_claims"][0]["source_url"]
    )
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(AssertionError, match="not returned by investment_analyst"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


def test_investment_quality_rejects_tool_result_date_outside_run_date(
    tmp_path: Path,
) -> None:
    runtime_details, _, _ = _investment_quality_fixture(tmp_path)
    runtime_details[0]["results"][0]["created_at"] = "2026-08-14T00:00:01Z"

    with pytest.raises(AssertionError, match="analysis-date boundary"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


def test_investment_quality_rejects_artifact_retrieval_date_mismatch(
    tmp_path: Path,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    notes = copy.deepcopy(notes)
    notes["company_analysis.json"]["critical_claims"][0]["retrieval_date"] = (
        "2026-08-12"
    )
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(AssertionError, match="URL/date was not returned"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


@pytest.mark.parametrize(
    ("role_index", "ticker_index", "ticker"),
    [
        (role_index, ticker_index, ticker)
        for role_index in (0, 1)
        for ticker_index, ticker in enumerate(harness.INVESTMENT_TICKERS)
    ],
)
def test_investment_quality_rejects_stale_current_query_for_each_role_and_ticker(
    tmp_path: Path,
    role_index: int,
    ticker_index: int,
    ticker: str,
) -> None:
    runtime_details, _, _ = _investment_quality_fixture(tmp_path)
    runtime_details[role_index]["calls"][ticker_index]["arguments"]["query"] = (
        f"{ticker} 2025 latest official reported results"
    )

    with pytest.raises(
        AssertionError,
        match=rf"current-year web_search for {ticker}",
    ):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


def test_investment_quality_rejects_secondary_source_as_primary(
    tmp_path: Path,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    notes = copy.deepcopy(notes)
    secondary = "https://news.example/amd-results-2026"
    old_url = notes["company_analysis.json"]["critical_claims"][1]["source_url"]
    notes["company_analysis.json"]["critical_claims"][1]["source_url"] = secondary
    report = report.replace(old_url, secondary)
    runtime_details[0]["results"][1]["payload"]["result"]["results"][0][
        "url"
    ] = secondary
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(AssertionError, match="must use the AMD issuer's official domain"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


def test_investment_quality_rejects_assumption_as_critical_claim(
    tmp_path: Path,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    notes = copy.deepcopy(notes)
    notes["risk_analysis.json"]["critical_claims"][0]["kind"] = "assumption"
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(AssertionError, match="assumptions cannot satisfy"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


@pytest.mark.parametrize(
    "bad_value_token",
    [
        "$81.6 billion, up 20% from the previous quarter and up 85% from a year ago",
        (
            "Second quarter revenue was $11.5 billion, gross margin was 54%, "
            "operating income was $2.0 billion"
        ),
        "$13 billion, plus or minus $300 million",
        "$68",
        "$22,18 million",
        "143% year-over-year",
    ],
)
def test_investment_quality_rejects_run21_multi_metric_or_ambiguous_value_shape(
    tmp_path: Path,
    bad_value_token: str,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    notes = copy.deepcopy(notes)
    notes["risk_analysis.json"]["critical_claims"][0]["value_token"] = (
        bad_value_token
    )
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(
        AssertionError,
        match="one exact quantitative value_token|tokens are absent",
    ):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


@pytest.mark.parametrize(
    "valid_value_token",
    ["$81.6 billion", "$22,187 million", "$1.38", "143%"],
)
def test_investment_value_token_contract_accepts_one_exact_metric(
    valid_value_token: str,
) -> None:
    assert harness.INVESTMENT_VALUE_TOKEN_PATTERN.fullmatch(valid_value_token)


@pytest.mark.parametrize(
    "invalid_value_token",
    ["$01.38", "$00", "01%", "$22,18 million", "$1,000,00"],
)
def test_investment_value_token_contract_rejects_leading_zero_or_bad_grouping(
    invalid_value_token: str,
) -> None:
    assert harness.INVESTMENT_VALUE_TOKEN_PATTERN.fullmatch(invalid_value_token) is None


@pytest.mark.parametrize(
    "valid_period_token",
    [
        "Q1 FY2027",
        "First Quarter Fiscal 2027",
        "First Quarter Fiscal Year 2027",
        "First Quarter of Fiscal Year 2027",
        "Second Quarter 2026",
    ],
)
def test_investment_period_token_contract_accepts_exact_variants(
    valid_period_token: str,
) -> None:
    assert harness.INVESTMENT_PERIOD_TOKEN_PATTERN.fullmatch(valid_period_token)


@pytest.mark.parametrize(
    "invalid_period_token",
    [
        "reported First Quarter Fiscal 2027",
        "First Quarter Fiscal 2027 results",
        "guidance for Second Quarter 2026",
        "$81.6 billion First Quarter Fiscal 2027",
    ],
)
def test_investment_period_token_contract_rejects_surrounding_prose(
    invalid_period_token: str,
) -> None:
    assert harness.INVESTMENT_PERIOD_TOKEN_PATTERN.fullmatch(invalid_period_token) is None


@pytest.mark.parametrize(
    ("claim_period", "hit_period"),
    [
        ("Q1 FY2027", "First Quarter Fiscal 2027"),
        ("First Quarter Fiscal 2027", "First Quarter of Fiscal Year 2027"),
        ("First Quarter FY2027", "Q1 Fiscal Year 2027"),
        ("Q1 2026", "First Quarter of 2026"),
    ],
)
def test_investment_period_semantics_accept_equivalent_strict_wording(
    claim_period: str,
    hit_period: str,
) -> None:
    assert harness._investment_period_is_semantically_evidenced(
        claim_period,
        f"Issuer reported actual revenue for {hit_period}.",
    )


@pytest.mark.parametrize(
    ("claim_period", "hit_period"),
    [
        ("Q1 FY2027", "Second Quarter Fiscal 2027"),
        ("Q1 FY2027", "First Quarter Fiscal 2026"),
        ("Q1 FY2027", "First Quarter 2027"),
        ("Q1 2027", "First Quarter Fiscal 2027"),
    ],
)
def test_investment_period_semantics_reject_mismatched_meaning(
    claim_period: str,
    hit_period: str,
) -> None:
    assert not harness._investment_period_is_semantically_evidenced(
        claim_period,
        f"Issuer reported actual revenue for {hit_period}.",
    )


def test_investment_quality_accepts_equivalent_period_in_same_hit(
    tmp_path: Path,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    notes = copy.deepcopy(notes)
    claim = notes["company_analysis.json"]["critical_claims"][0]
    claim["period_token"] = "First Quarter Fiscal 2027"
    claim["period_end"] = "2026-04-26"
    hit = runtime_details[0]["results"][0]["payload"]["result"]["results"][0]
    hit["title"] = (
        "NVIDIA reports First Quarter of Fiscal Year 2027 revenue $68.1 billion"
    )
    hit["snippet"] = "Official NVDA actual results released in 2026."
    report = report.replace(
        "| company_analysis.json | NVDA | $68.1 billion | Q4 FY2026 |",
        (
            "| company_analysis.json | NVDA | $68.1 billion | "
            "First Quarter Fiscal 2027 |"
        ),
    )
    _write_investment_quality_artifacts(tmp_path, notes, report)

    evidence = harness._investment_data_quality_gate(
        tmp_path,
        runtime_details,
        "2026-08-13T00:00:00Z",
    )

    company_claim = evidence["notes"]["company_analysis.json"]["claims"][0]
    assert company_claim["period_token"] == "First Quarter Fiscal 2027"
    assert company_claim["period_end"] == "2026-04-26"


def test_investment_quality_accepts_actual_value_with_later_guidance_in_same_hit(
    tmp_path: Path,
) -> None:
    runtime_details, _, _ = _investment_quality_fixture(tmp_path)
    hit = runtime_details[0]["results"][0]["payload"]["result"]["results"][0]
    hit["title"] = "NVIDIA reports Q4 FY2026 revenue $68.1 billion."
    hit["snippet"] = "NVDA outlook expects Q1 FY2027 revenue of $75 billion."

    evidence = harness._investment_data_quality_gate(
        tmp_path,
        runtime_details,
        "2026-08-13T00:00:00Z",
    )

    assert evidence["critical_claims_supported"] is True


@pytest.mark.parametrize(
    ("value_token", "evidence"),
    [
        ("$22,187 million", "Broadcom actual revenue was $22,187 million."),
        ("$81.6 billion", "NVIDIA actual revenue was $81.6 billion, up 85%."),
        ("$1.38", "AMD actual diluted earnings per share was $1.38."),
        ("143%", "Broadcom actual AI revenue grew 143%; demand remained strong."),
    ],
)
def test_investment_clean_value_occurrence_allows_normal_trailing_punctuation(
    value_token: str,
    evidence: str,
) -> None:
    assert harness._hit_has_clean_value_occurrence(
        {"title": evidence, "snippet": ""},
        value_token,
    )


def test_investment_clean_value_occurrence_rejects_numeric_prefix() -> None:
    assert not harness._hit_has_clean_value_occurrence(
        {"title": "NVIDIA actual revenue was $68.1 billion.", "snippet": ""},
        "$68",
    )
    assert not harness._hit_has_clean_value_occurrence(
        {"title": "Broadcom actual revenue grew 143%.", "snippet": ""},
        "43%",
    )


def test_investment_clean_value_occurrence_allows_actual_before_later_guidance() -> None:
    evidence = (
        "NVIDIA reported actual Q1 FY2027 revenue of $81.6 billion, up 20% "
        "from the previous quarter and up 85% from a year ago and expects "
        "demand to remain strong; its outlook follows separately."
    )
    assert harness._hit_has_clean_value_occurrence(
        {"title": evidence, "snippet": ""},
        "$81.6 billion",
    )


@pytest.mark.parametrize(
    "evidence",
    [
        "NVIDIA Q1 FY2027 revenue will be $81.6 billion.",
        "NVIDIA Q1 FY2027 revenue is expected to reach $81.6 billion.",
        "NVIDIA Q1 FY2027 revenue target is $81.6 billion.",
        "NVIDIA Q1 FY2027 revenue was $81.6 billion guidance.",
        "NVIDIA Q1 FY2027 revenue was $81.6 billion, outlook.",
    ],
)
def test_investment_clean_value_occurrence_rejects_forward_value_clause(
    evidence: str,
) -> None:
    assert not harness._hit_has_clean_value_occurrence(
        {"title": evidence, "snippet": ""},
        "$81.6 billion",
    )


def test_investment_quality_rejects_value_only_in_guidance_clause(
    tmp_path: Path,
) -> None:
    runtime_details, _, _ = _investment_quality_fixture(tmp_path)
    hit = runtime_details[0]["results"][0]["payload"]["result"]["results"][0]
    hit["title"] = "NVIDIA Q4 FY2026 financial results"
    hit["snippet"] = "NVDA outlook expects revenue of $68.1 billion."

    with pytest.raises(AssertionError, match="tokens are absent"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


def test_investment_quality_rejects_value_and_period_split_across_hits(
    tmp_path: Path,
) -> None:
    runtime_details, _, _ = _investment_quality_fixture(tmp_path)
    result_payload = runtime_details[0]["results"][0]["payload"]["result"]
    source_url = result_payload["results"][0]["url"]
    result_payload["results"] = [
        {
            "title": "NVIDIA reports revenue $68.1 billion",
            "snippet": "Official NVDA actual result.",
            "url": source_url,
        },
        {
            "title": "NVIDIA reports Q4 FY2026 financial results",
            "snippet": "Official NVDA period.",
            "url": source_url,
        },
    ]

    with pytest.raises(AssertionError, match="tokens are absent"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


def test_investment_quality_rejects_more_than_three_claims_or_duplicate_ticker(
    tmp_path: Path,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    extra = copy.deepcopy(notes)
    extra["company_analysis.json"]["critical_claims"].append(
        copy.deepcopy(extra["company_analysis.json"]["critical_claims"][0])
    )
    _write_investment_quality_artifacts(tmp_path, extra, report)
    with pytest.raises(AssertionError, match="exactly three entries"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )

    duplicate = copy.deepcopy(notes)
    duplicate["company_analysis.json"]["critical_claims"][1] = copy.deepcopy(
        duplicate["company_analysis.json"]["critical_claims"][0]
    )
    _write_investment_quality_artifacts(tmp_path, duplicate, report)
    with pytest.raises(AssertionError, match="exactly one claim per ticker"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


def test_investment_quality_rejects_extra_claim_field(tmp_path: Path) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    notes = copy.deepcopy(notes)
    notes["company_analysis.json"]["critical_claims"][0]["rationale"] = (
        "Run21-style extra field"
    )
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(AssertionError, match="exactly the seven required fields"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


def test_investment_quality_rejects_nonquantitative_value_token(
    tmp_path: Path,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    notes = copy.deepcopy(notes)
    notes["company_analysis.json"]["critical_claims"][0]["value_token"] = (
        "revenue"
    )
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(AssertionError, match="one exact quantitative value_token"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


@pytest.mark.parametrize(
    ("field", "bad_token"),
    [
        ("period_token", "$68.1 billion"),
        ("value_token", "$68"),
    ],
)
def test_investment_quality_rejects_ambiguous_claim_tokens(
    tmp_path: Path,
    field: str,
    bad_token: str,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    notes = copy.deepcopy(notes)
    notes["company_analysis.json"]["critical_claims"][0][field] = bad_token
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(
        AssertionError,
        match="one exact quantitative value_token|tokens are absent",
    ):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


def test_investment_quality_rejects_ticker_claim_supported_by_other_company(
    tmp_path: Path,
) -> None:
    runtime_details, _, _ = _investment_quality_fixture(tmp_path)
    hit = runtime_details[0]["results"][0]["payload"]["result"]["results"][0]
    hit["title"] = "AMD reports Q4 FY2026 revenue $68.1 billion"
    hit["snippet"] = "Official AMD results released in 2026."

    with pytest.raises(AssertionError, match="tokens are absent"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


def test_investment_quality_rejects_stale_or_future_fact_period(
    tmp_path: Path,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    for period_end in ("2024-01-01", "2026-08-14"):
        mutated = copy.deepcopy(notes)
        mutated["risk_analysis.json"]["critical_claims"][0][
            "period_end"
        ] = period_end
        _write_investment_quality_artifacts(tmp_path, mutated, report)
        with pytest.raises(
            AssertionError,
            match="ended, fresh, and in the analysis calendar year",
        ):
            harness._investment_data_quality_gate(
                tmp_path,
                runtime_details,
                "2026-08-13T00:00:00Z",
            )


def test_investment_quality_rejects_prior_year_period_end_within_freshness_window(
    tmp_path: Path,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    notes = copy.deepcopy(notes)
    notes["risk_analysis.json"]["critical_claims"][0]["period_end"] = (
        "2025-12-31"
    )
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(
        AssertionError,
        match="ended, fresh, and in the analysis calendar year",
    ):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


def test_investment_quality_rejects_report_tool_result_date_mismatch(
    tmp_path: Path,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    first_source_row = next(
        line for line in report.splitlines() if "| NVDA |" in line
    )
    report = report.replace(
        first_source_row,
        first_source_row.replace("2026-08-13", "2026-08-12"),
    )
    _write_investment_quality_artifacts(tmp_path, notes, report)

    with pytest.raises(AssertionError, match="only its one ToolResult date"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


def test_investment_quality_rejects_ambiguous_report_dates_and_metadata(
    tmp_path: Path,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    first_source_row = next(
        line for line in report.splitlines() if "| NVDA |" in line
    )
    ambiguous_row_report = report.replace(
        first_source_row,
        first_source_row.replace(
            "2026-08-13 |",
            "2025-01-01 / 2026-08-13 |",
        ),
    )
    _write_investment_quality_artifacts(tmp_path, notes, ambiguous_row_report)
    with pytest.raises(AssertionError, match="only its one ToolResult date"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )

    duplicate_metadata_report = (
        report + "\nAnalysis date: 2025-01-01\nHorizon years: 2025, 2026, 2027"
    )
    _write_investment_quality_artifacts(
        tmp_path,
        notes,
        duplicate_metadata_report,
    )
    with pytest.raises(AssertionError, match="unique exact Analysis date"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


def test_investment_quality_rejects_stale_report_metadata_or_new_url(
    tmp_path: Path,
) -> None:
    runtime_details, notes, report = _investment_quality_fixture(tmp_path)
    stale_report = report.replace(
        "Analysis date: 2026-08-13",
        "Analysis date: 2025-08-13",
    )
    _write_investment_quality_artifacts(tmp_path, notes, stale_report)
    with pytest.raises(AssertionError, match="exact Analysis date"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )

    new_url_report = report + "\n| Extra | https://unsearched.example/fact | 2026-08-13 |"
    _write_investment_quality_artifacts(tmp_path, notes, new_url_report)
    with pytest.raises(AssertionError, match="not validated in a child JSON"):
        harness._investment_data_quality_gate(
            tmp_path,
            runtime_details,
            "2026-08-13T00:00:00Z",
        )


def test_investment_prompt_and_roles_forbid_workspace_placeholder_writes() -> None:
    investment = next(
        spec for spec in harness.CASES if spec.case_id == "investment"
    )

    required_text = (
        "only files any role may create or edit are "
        "investment_case/company_analysis.json, "
        "investment_case/risk_analysis.json, and investment_case/report.md"
    )
    for text in [
        investment.prompt,
        *(str(role["responsibility"]) for role in investment.roles),
    ]:
        assert "Do not perform workspace-preparation writes" in text
        assert "Never create `.gitkeep`, placeholder, scratch, preview" in text
        assert required_text in text
        assert "first file_write to one of those required artifacts" in text


def test_investment_file_mutation_ledger_still_rejects_gitkeep(
    tmp_path: Path,
) -> None:
    investment = next(
        spec for spec in harness.CASES if spec.case_id == "investment"
    )
    runtime_details = [
        {
            "task_id": "investment-lead-task",
            "role_id": "investment_lead",
            "runtime_session_id": "rt_investment_lead",
            "calls": [
                {
                    "tool_call_id": "write-placeholder",
                    "tool_name": "file_write",
                    "arguments": {
                        "path": "investment_case/.gitkeep",
                        "content": "",
                    },
                    "created_at": "2026-08-12T10:00:00",
                }
            ],
            "results": [
                {
                    "result_record_id": "result-write-placeholder",
                    "tool_call_id": "write-placeholder",
                    "tool_name": "file_write",
                    "payload": {"success": True, "result": {"success": True}},
                    "created_at": "2026-08-12T10:00:01",
                }
            ],
        }
    ]

    try:
        harness._native_successful_file_mutations(
            investment,
            tmp_path,
            runtime_details=runtime_details,
        )
    except AssertionError as exc:
        assert "outside required_artifacts" in str(exc)
        assert "investment_case/.gitkeep" in str(exc)
    else:
        raise AssertionError("investment .gitkeep placeholder passed evidence")


def _app_contract_runtime_details(
    workplace: Path,
    *,
    qa_call_at: str = "2026-08-12T10:00:07",
    qa_result_at: str = "2026-08-12T10:00:08",
    qa_success: bool = True,
    developer_validation_at: str | None = None,
    extra_write: bool = False,
) -> list[dict[str, Any]]:
    developer_calls: list[dict[str, Any]] = []
    developer_results: list[dict[str, Any]] = []
    artifacts = (
        "app_case/index.html",
        "app_case/styles.css",
        "app_case/app.js",
    )
    for index, relative in enumerate(artifacts, start=1):
        call_id = f"developer-write-{index}"
        developer_calls.append(
            {
                "tool_call_id": call_id,
                "tool_name": "file_write",
                "arguments": {"path": relative, "content": "durable content"},
                "created_at": f"2026-08-12T10:00:0{index * 2 - 1}",
            }
        )
        developer_results.append(
            {
                "result_record_id": f"result-{call_id}",
                "tool_call_id": call_id,
                "tool_name": "file_write",
                "payload": {"success": True, "result": {"success": True}},
                "created_at": f"2026-08-12T10:00:0{index * 2}",
            }
        )
    if extra_write:
        developer_calls.append(
            {
                "tool_call_id": "developer-extra-write",
                "tool_name": "file_write",
                "arguments": {
                    "path": "app_case/qa_report.md",
                    "content": "not a required artifact",
                },
                "created_at": "2026-08-12T10:00:06.100000",
            }
        )
        developer_results.append(
            {
                "result_record_id": "result-developer-extra-write",
                "tool_call_id": "developer-extra-write",
                "tool_name": "file_write",
                "payload": {"success": True, "result": {"success": True}},
                "created_at": "2026-08-12T10:00:06.200000",
            }
        )
    if developer_validation_at is not None:
        developer_calls.append(
            {
                "tool_call_id": "developer-node-check",
                "tool_name": "shell_exec",
                "arguments": {
                    "command": "node --check app_case/app.js",
                    "working_directory": str(workplace),
                },
                "created_at": developer_validation_at,
            }
        )
        developer_results.append(
            {
                "result_record_id": "result-developer-node-check",
                "tool_call_id": "developer-node-check",
                "tool_name": "shell_exec",
                "payload": {"success": True, "result": {"exit_code": 0}},
                "created_at": "2026-08-12T10:00:10",
            }
        )
    return [
        {
            "task_id": "developer-task",
            "role_id": "developer",
            "runtime_session_id": "rt_developer",
            "calls": developer_calls,
            "results": developer_results,
        },
        {
            "task_id": "qa-task",
            "role_id": "qa_engineer",
            "runtime_session_id": "rt_qa",
            "calls": [
                {
                    "tool_call_id": "qa-node-check",
                    "tool_name": "shell_exec",
                    "arguments": {
                        "command": "node --check app_case/app.js",
                        "working_directory": str(workplace),
                    },
                    "created_at": qa_call_at,
                }
            ],
            "results": [
                {
                    "result_record_id": "result-qa-node-check",
                    "tool_call_id": "qa-node-check",
                    "tool_name": "shell_exec",
                    "payload": {
                        "success": qa_success,
                        "result": {"exit_code": 0 if qa_success else 1},
                    },
                    "created_at": qa_result_at,
                }
            ],
        },
    ]


def test_app_prompt_requires_same_batch_scope_dependency_before_qa() -> None:
    app = next(spec for spec in harness.CASES if spec.case_id == "app")

    assert "preferably call delegate_work once with both child items" in app.prompt
    assert "two sequential delegate_work calls" in app.prompt
    assert "append to the same stable batch_id" in app.prompt
    assert "stable scope_key `issue35-app-implementation`" in app.prompt
    assert '`depends_on: [{"scope_key": "issue35-app-implementation"}]`' in app.prompt
    assert "Never start or dispatch QA before the developer item completes" in app.prompt
    assert "do not remove or rewire that dependency" in app.prompt
    assert "wait until all three required files durably exist" in app.prompt


def test_app_native_tool_contract_accepts_qa_after_final_developer_write(
    tmp_path: Path,
) -> None:
    app = next(spec for spec in harness.CASES if spec.case_id == "app")

    evidence = harness._app_native_tool_contract(
        app,
        tmp_path,
        runtime_details=_app_contract_runtime_details(tmp_path),
    )

    assert set(evidence["developer_artifact_writes"]) == set(
        app.required_artifacts
    )
    assert len(evidence["successful_file_mutations"]) == 3
    assert evidence["qa_javascript_validation"][
        "after_last_developer_write"
    ] is True
    assert evidence["qa_javascript_validation"]["runtime_session_id"] == "rt_qa"


def test_app_native_tool_contract_rejects_qa_before_final_developer_write(
    tmp_path: Path,
) -> None:
    app = next(spec for spec in harness.CASES if spec.case_id == "app")
    details = _app_contract_runtime_details(
        tmp_path,
        qa_call_at="2026-08-12T10:00:05.500000",
        qa_result_at="2026-08-12T10:00:05.750000",
        developer_validation_at="2026-08-12T10:00:09",
    )

    try:
        harness._app_native_tool_contract(
            app,
            tmp_path,
            runtime_details=details,
        )
    except AssertionError as exc:
        assert "before the final developer write ToolResult" in str(exc)
    else:
        raise AssertionError(
            "an early QA run passed because a later developer check substituted for it"
        )


def test_failed_early_qa_cannot_be_replaced_by_developer_success(
    tmp_path: Path,
) -> None:
    app = next(spec for spec in harness.CASES if spec.case_id == "app")
    details = _app_contract_runtime_details(
        tmp_path,
        qa_call_at="2026-08-12T10:00:01",
        qa_result_at="2026-08-12T10:00:02",
        qa_success=False,
        developer_validation_at="2026-08-12T10:00:09",
    )

    try:
        harness._app_native_tool_contract(
            app,
            tmp_path,
            runtime_details=details,
        )
    except AssertionError as exc:
        assert "QA exact validation ToolCall" in str(exc)
        assert "did not succeed" in str(exc)
    else:
        raise AssertionError("a developer syntax check replaced failed QA evidence")


def test_app_native_tool_contract_rejects_successful_extra_write(
    tmp_path: Path,
) -> None:
    app = next(spec for spec in harness.CASES if spec.case_id == "app")

    try:
        harness._app_native_tool_contract(
            app,
            tmp_path,
            runtime_details=_app_contract_runtime_details(
                tmp_path,
                extra_write=True,
            ),
        )
    except AssertionError as exc:
        assert "outside required_artifacts" in str(exc)
        assert "qa_report.md" in str(exc)
    else:
        raise AssertionError("a successful write to qa_report.md passed evidence")


def test_app_native_tool_contract_requires_durable_qa_timestamp(
    tmp_path: Path,
) -> None:
    app = next(spec for spec in harness.CASES if spec.case_id == "app")
    details = _app_contract_runtime_details(tmp_path)
    details[1]["calls"][0].pop("created_at")

    try:
        harness._app_native_tool_contract(
            app,
            tmp_path,
            runtime_details=details,
        )
    except AssertionError as exc:
        assert "lacks durable created_at" in str(exc)
    else:
        raise AssertionError("QA evidence without a durable timestamp was accepted")


def _app_dependency_items(
    *,
    developer_invocation_id: str = "invocation-one",
    qa_invocation_id: str = "invocation-one",
    developer_invocation_index: int = 0,
    qa_invocation_index: int = 1,
    developer_sequence_index: int = 0,
    qa_sequence_index: int = 1,
    qa_run_id: str = "run-app",
    qa_batch_id: str = "batch-one",
    qa_parent_work_item_id: str = "manager-item",
    qa_source_seat_id: str = "seat::engineering::manager",
) -> tuple[SimpleNamespace, SimpleNamespace]:
    developer = SimpleNamespace(
        work_item_id="developer-item",
        role_id="developer",
        run_id="run-app",
        batch_id="batch-one",
        parent_work_item_id="manager-item",
        source_seat_id="seat::engineering::manager",
        batch_index=0,
        metadata={
            "scope_key": harness.APP_DEVELOPER_SCOPE_KEY,
            "created_by_delegate_work": True,
            "delegate_invocation_id": developer_invocation_id,
            "delegate_invocation_index": developer_invocation_index,
            "delegate_sequence_index": developer_sequence_index,
        },
    )
    qa = SimpleNamespace(
        work_item_id="qa-item",
        role_id="qa_engineer",
        run_id=qa_run_id,
        batch_id=qa_batch_id,
        parent_work_item_id=qa_parent_work_item_id,
        source_seat_id=qa_source_seat_id,
        batch_index=0,
        metadata={
            "scope_key": harness.APP_QA_SCOPE_KEY,
            "created_by_delegate_work": True,
            "delegate_invocation_id": qa_invocation_id,
            "delegate_invocation_index": qa_invocation_index,
            "delegate_sequence_index": qa_sequence_index,
            "dependency_work_item_ids": ["developer-item"],
            "resolved_dependencies": [
                {
                    "input": '{"scope_key": "issue35-app-implementation"}',
                    "work_item_id": "developer-item",
                    "resolved_by": "scope_key",
                }
            ],
        },
    )
    return developer, qa


def test_app_dependency_contract_accepts_same_invocation_order() -> None:
    developer, qa = _app_dependency_items()

    evidence = harness._app_delegation_dependency_contract([developer, qa])
    assert evidence["validated"] is True
    assert evidence["sequencing_mode"] == "same_invocation"
    assert evidence["developer_delegate_invocation_index"] == 0
    assert evidence["qa_delegate_invocation_index"] == 1
    assert evidence["developer_delegate_sequence_index"] == 0
    assert evidence["qa_delegate_sequence_index"] == 1


def test_app_dependency_contract_accepts_split_append_with_call_local_batch_indexes() -> None:
    developer, qa = _app_dependency_items(
        developer_invocation_id="invocation-developer",
        qa_invocation_id="invocation-qa",
        developer_invocation_index=0,
        qa_invocation_index=0,
    )

    assert developer.batch_index == qa.batch_index == 0
    evidence = harness._app_delegation_dependency_contract([developer, qa])
    assert evidence["validated"] is True
    assert evidence["sequencing_mode"] == "split_append"


def test_app_dependency_contract_requires_scope_hard_dependency() -> None:
    developer, qa = _app_dependency_items()

    qa.metadata["dependency_work_item_ids"] = []
    try:
        harness._app_delegation_dependency_contract([developer, qa])
    except AssertionError as exc:
        assert "durable hard dependency" in str(exc)
    else:
        raise AssertionError("QA with a removed durable dependency was accepted")


def test_app_dependency_contract_rejects_missing_duplicate_or_bad_sequence() -> None:
    mutations = (
        lambda qa: qa.metadata.pop("delegate_sequence_index"),
        lambda qa: qa.metadata.__setitem__("delegate_sequence_index", 0),
        lambda qa: qa.metadata.__setitem__("delegate_sequence_index", "1"),
    )
    for mutate in mutations:
        developer, qa = _app_dependency_items()
        mutate(qa)
        try:
            harness._app_delegation_dependency_contract([developer, qa])
        except AssertionError:
            pass
        else:
            raise AssertionError(
                "QA with missing, duplicate, or invalid durable sequence passed"
            )


def test_app_dependency_contract_rejects_different_manager_sequence_scope() -> None:
    for field, value in (
        ("run_id", "run-other"),
        ("batch_id", "batch-other"),
        ("parent_work_item_id", "parent-other"),
        ("source_seat_id", "seat::other::manager"),
    ):
        developer, qa = _app_dependency_items()
        setattr(qa, field, value)
        try:
            harness._app_delegation_dependency_contract([developer, qa])
        except AssertionError as exc:
            assert "same-scope durable hard dependency" in str(exc)
        else:
            raise AssertionError(f"QA with a different {field} scope passed")


def test_uncheckpointed_successful_wc_cannot_pass_native_shell_closure(
    tmp_path: Path,
) -> None:
    spec = harness.CASES[0]
    details = [
        {
            "task_id": "lead-task",
            "role_id": "investment_lead",
            "runtime_session_id": "rt_lead",
            "calls": [
                {
                    "tool_call_id": "call-wc",
                    "tool_name": "shell_exec",
                    "arguments": {
                        "command": "wc -l investment_case/company_analysis.json",
                        "working_directory": str(tmp_path),
                    },
                }
            ],
            "results": [
                {
                    "result_record_id": "result-wc",
                    "tool_call_id": "call-wc",
                    "tool_name": "shell_exec",
                    "payload": {"success": True, "result": {"exit_code": 0}},
                }
            ],
        }
    ]

    try:
        harness._native_shell_ledger_closure(
            spec,
            tmp_path,
            runtime_details=details,
            tool_checkpoint_evidence=[],
        )
    except AssertionError as exc:
        assert "unexpected native shell ToolCall" in str(exc)
        assert "succeeded outside the E2E model" in str(exc)
        assert "call-wc" in str(exc)
    else:
        raise AssertionError("an uncheckpointed successful wc call passed evidence")


def test_modeled_successful_shell_cannot_bypass_permission_checkpoint(
    tmp_path: Path,
) -> None:
    spec = harness.CASES[1]
    details = [
        {
            "task_id": "qa-task",
            "role_id": "qa_engineer",
            "runtime_session_id": "rt_qa",
            "calls": [
                {
                    "tool_call_id": "call-node-check",
                    "tool_name": "shell_exec",
                    "arguments": {
                        "command": "node --check app_case/app.js",
                        "working_directory": str(tmp_path),
                    },
                }
            ],
            "results": [
                {
                    "result_record_id": "result-node-check",
                    "tool_call_id": "call-node-check",
                    "tool_name": "shell_exec",
                    "payload": {"success": True, "result": {"exit_code": 0}},
                }
            ],
        }
    ]

    try:
        harness._native_shell_ledger_closure(
            spec,
            tmp_path,
            runtime_details=details,
            tool_checkpoint_evidence=[],
        )
    except AssertionError as exc:
        assert "modeled native shell ToolCall" in str(exc)
        assert "bypassed the execute-only all-shell permission guard" in str(exc)
    else:
        raise AssertionError("a modeled shell bypassed its required checkpoint")


def test_old_rework_attempt_shell_and_checkpoint_remain_in_global_closure(
    tmp_path: Path,
) -> None:
    spec = harness.CASES[0]
    arguments = {
        "command": "python3 -m json.tool investment_case/company_analysis.json",
        "working_directory": str(tmp_path),
    }
    details = [
        {
            "task_id": "analyst-task",
            "role_id": "investment_analyst",
            "runtime_session_id": "rt_old_attempt",
            "calls": [
                {
                    "tool_call_id": "old-json-check",
                    "tool_name": "shell_exec",
                    "arguments": arguments,
                }
            ],
            "results": [
                {
                    "result_record_id": "old-json-result",
                    "tool_call_id": "old-json-check",
                    "tool_name": "shell_exec",
                    "payload": {"success": True, "result": {"exit_code": 0}},
                }
            ],
        },
        {
            "task_id": "analyst-task",
            "role_id": "investment_analyst",
            "runtime_session_id": "rt_latest_attempt",
            "calls": [],
            "results": [],
        },
    ]
    checkpoint = {
        "checkpoint_id": "checkpoint-old-json",
        "tool_runtime_session_id": "rt_old_attempt",
        "tool_call_id": "old-json-check",
        "tool_name": "shell_exec",
        "tool_arguments": arguments,
        "decision": "approve_once",
        "rejected": False,
        "exact_modeled_call": True,
        "tool_result": {
            "result_record_id": "old-json-result",
            "success": True,
            "permission_resolution": "allow",
            "checkpoint_tool_result_persisted": True,
            "checkpoint_execution_state": "result_persisted",
            "checkpoint_completion_status": "resolved",
        },
    }

    global_ledger = harness._native_tool_ledger_closure(
        spec,
        runtime_details=details,
    )
    shell_evidence = harness._native_shell_ledger_closure(
        spec,
        tmp_path,
        runtime_details=details,
        tool_checkpoint_evidence=[checkpoint],
    )

    assert len(global_ledger) == 1
    assert shell_evidence[0]["runtime_session_id"] == "rt_old_attempt"
    assert shell_evidence[0]["checkpoint_id"] == "checkpoint-old-json"


def test_uncheckpointed_shell_in_old_rework_attempt_still_fails_global_closure(
    tmp_path: Path,
) -> None:
    spec = harness.CASES[0]
    arguments = {
        "command": "python3 -m json.tool investment_case/company_analysis.json",
        "working_directory": str(tmp_path),
    }
    details = [
        {
            "task_id": "analyst-task",
            "role_id": "investment_analyst",
            "runtime_session_id": "rt_old_attempt",
            "calls": [
                {
                    "tool_call_id": "old-json-check",
                    "tool_name": "shell_exec",
                    "arguments": arguments,
                }
            ],
            "results": [
                {
                    "result_record_id": "old-json-result",
                    "tool_call_id": "old-json-check",
                    "tool_name": "shell_exec",
                    "payload": {"success": True, "result": {"exit_code": 0}},
                }
            ],
        },
        {
            "task_id": "analyst-task",
            "role_id": "investment_analyst",
            "runtime_session_id": "rt_latest_attempt",
            "calls": [],
            "results": [],
        },
    ]

    with pytest.raises(AssertionError, match="bypassed.*permission guard"):
        harness._native_shell_ledger_closure(
            spec,
            tmp_path,
            runtime_details=details,
            tool_checkpoint_evidence=[],
        )


def test_extra_file_mutation_in_old_rework_attempt_still_fails_containment(
    tmp_path: Path,
) -> None:
    spec = harness.CASES[0]
    details = [
        {
            "task_id": "analyst-task",
            "role_id": "investment_analyst",
            "runtime_session_id": "rt_old_attempt",
            "calls": [
                {
                    "tool_call_id": "old-extra-write",
                    "tool_name": "file_write",
                    "arguments": {
                        "path": "investment_case/old-scratch.txt",
                        "content": "stale",
                    },
                    "created_at": "2026-08-13T00:00:00+00:00",
                }
            ],
            "results": [
                {
                    "result_record_id": "old-extra-result",
                    "tool_call_id": "old-extra-write",
                    "tool_name": "file_write",
                    "payload": {"success": True, "result": {"success": True}},
                    "created_at": "2026-08-13T00:00:01+00:00",
                }
            ],
        },
        {
            "task_id": "analyst-task",
            "role_id": "investment_analyst",
            "runtime_session_id": "rt_latest_attempt",
            "calls": [],
            "results": [],
        },
    ]

    with pytest.raises(AssertionError, match="outside required_artifacts"):
        harness._native_successful_file_mutations(
            spec,
            tmp_path,
            runtime_details=details,
        )


def test_failed_modeled_qa_call_cannot_pass_all_shell_closure(
    tmp_path: Path,
) -> None:
    app = next(spec for spec in harness.CASES if spec.case_id == "app")
    details = _app_contract_runtime_details(
        tmp_path,
        qa_success=False,
    )[1:]

    try:
        harness._native_shell_ledger_closure(
            app,
            tmp_path,
            runtime_details=details,
            tool_checkpoint_evidence=[],
        )
    except AssertionError as exc:
        assert "modeled native shell ToolCall" in str(exc)
        assert "did not have one successful ToolResult" in str(exc)
    else:
        raise AssertionError("a failed modeled QA shell call passed ledger closure")


def test_modeled_validation_waits_until_target_artifact_exists(
    tmp_path: Path,
) -> None:
    app_spec = next(spec for spec in harness.CASES if spec.case_id == "app")
    checkpoint = SimpleNamespace(
        payload={
            "tool_call": {
                "name": "shell_exec",
                "arguments": {
                    "command": "node --check app_case/app.js",
                    "working_directory": str(tmp_path),
                },
            }
        }
    )

    assert not harness._modeled_tool_call_inputs_ready(
        app_spec,
        tmp_path,
        checkpoint,
    )
    target = tmp_path / "app_case" / "app.js"
    target.parent.mkdir(parents=True)
    target.write_text("const ready = true;\n", encoding="utf-8")
    assert harness._modeled_tool_call_inputs_ready(
        app_spec,
        tmp_path,
        checkpoint,
    )

    # Unknown shell is denied immediately rather than hidden behind the
    # producer-readiness wait.
    checkpoint.payload["tool_call"]["arguments"]["command"] = "wc -l app_case/app.js"
    assert harness._modeled_tool_call_inputs_ready(
        app_spec,
        tmp_path,
        checkpoint,
    )


def test_canonical_shell_deny_can_continue_in_same_runtime(
    tmp_path: Path,
) -> None:
    spec = harness.CASES[1]
    runtime_session_id = "rt_same_after_deny"
    exact_arguments = {
        "command": "node --check app_case/app.js",
        "working_directory": str(tmp_path),
    }
    denied_arguments = {
        "command": "wc -l app_case/app.js",
        "working_directory": str(tmp_path),
    }
    details = [
        {
            "task_id": "qa-task",
            "role_id": "qa_engineer",
            "runtime_session_id": runtime_session_id,
            "calls": [
                {
                    "tool_call_id": "call-denied",
                    "tool_name": "shell_exec",
                    "arguments": denied_arguments,
                },
                {
                    "tool_call_id": "call-exact",
                    "tool_name": "shell_exec",
                    "arguments": exact_arguments,
                },
            ],
            "results": [
                {
                    "result_record_id": "result-denied",
                    "tool_call_id": "call-denied",
                    "tool_name": "shell_exec",
                    "payload": {
                        "success": False,
                        "error": "Owner denied this exact ToolCall.",
                    },
                },
                {
                    "result_record_id": "result-exact",
                    "tool_call_id": "call-exact",
                    "tool_name": "shell_exec",
                    "payload": {"success": True, "result": {"exit_code": 0}},
                },
            ],
        }
    ]
    checkpoints = [
        {
            "checkpoint_id": "checkpoint-denied",
            "tool_runtime_session_id": runtime_session_id,
            "tool_call_id": "call-denied",
            "tool_name": "shell_exec",
            "tool_arguments": denied_arguments,
            "decision": "deny",
            "rejected": True,
            "exact_modeled_call": False,
            "tool_result": {
                "result_record_id": "result-denied",
                "success": False,
                "permission_resolution": "deny",
                "checkpoint_tool_result_persisted": True,
                "checkpoint_execution_state": "result_persisted",
                "checkpoint_completion_status": "resolved",
            },
        },
        {
            "checkpoint_id": "checkpoint-exact",
            "tool_runtime_session_id": runtime_session_id,
            "tool_call_id": "call-exact",
            "tool_name": "shell_exec",
            "tool_arguments": exact_arguments,
            "decision": "approve_once",
            "rejected": False,
            "exact_modeled_call": True,
            "tool_result": {
                "result_record_id": "result-exact",
                "success": True,
                "permission_resolution": "allow",
                "checkpoint_tool_result_persisted": True,
                "checkpoint_execution_state": "result_persisted",
                "checkpoint_completion_status": "resolved",
            },
        },
    ]

    evidence = harness._native_shell_ledger_closure(
        spec,
        tmp_path,
        runtime_details=details,
        tool_checkpoint_evidence=checkpoints,
    )

    assert [item["outcome"] for item in evidence] == [
        "canonical_deny",
        "approved_success",
    ]
    assert {item["runtime_session_id"] for item in evidence} == {
        runtime_session_id
    }


def test_execute_shell_overlay_routes_all_shell_to_real_predictor_and_restores(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    system_config = config_dir / "system_config.yaml"
    original = b"""system: {}\nautonomy:\n  enabled: true\n  allow_native_tool_auto_approval: true\n  permissions_v2:\n    enabled: true\n    dangerous_shell_patterns:\n      - '\\brm\\s+-rf\\b'\ncapabilities: {}\n"""
    system_config.write_bytes(original)
    system_config.chmod(0o640)
    overlay = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )

    evidence = overlay.install()
    installed = yaml.safe_load(system_config.read_text(encoding="utf-8"))
    assert installed["autonomy"]["allow_native_tool_auto_approval"] is True
    assert (
        harness.E2E_ALL_SHELL_REVIEW_PATTERN
        in installed["autonomy"]["permissions_v2"]["dangerous_shell_patterns"]
    )
    assert {
        (probe["resolution"], probe["source"])
        for probe in evidence["predictor_probes"].values()
    } == {("ask", "shell_pattern")}
    async_evidence = asyncio.run(
        harness._validate_installed_shell_async_authorization(
            config_dir,
            workplace=tmp_path / "workplace",
        )
    )
    assert async_evidence["validated"] is True
    assert len(async_evidence["card_boundary_calls"]) == 2
    assert {
        item["policy_source"]
        for item in async_evidence["card_boundary_calls"]
    } == {"shell_pattern"}

    overlay.restore()
    assert system_config.read_bytes() == original
    assert system_config.stat().st_mode & 0o777 == 0o640
    assert not overlay.journal_path.exists()


def test_execute_shell_overlay_restores_after_exact_opc_config_save(
    tmp_path: Path,
) -> None:
    from opc.core.config import OPCConfig

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    system_config = config_dir / "system_config.yaml"
    original = (
        b"system:\n"
        b"  log_level: INFO\n"
        b"autonomy:\n"
        b"  enabled: true\n"
        b"  allow_native_tool_auto_approval: true\n"
        b"  permissions_v2:\n"
        b"    enabled: true\n"
        b"    dangerous_shell_patterns: []\n"
        b"mcp_servers:\n"
        b"  - name: disabled-probe\n"
        b"    type: remote\n"
        b"    url: https://example.invalid/mcp\n"
        b"    enabled: false\n"
        b"capabilities: {}\n"
    )
    system_config.write_bytes(original)
    system_config.chmod(0o640)
    overlay = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )

    overlay.install()
    raw_overlay = system_config.read_bytes()
    journal = json.loads(overlay.journal_path.read_text(encoding="utf-8"))
    expected_normalized = base64.b64decode(
        journal["normalized_overlay_base64"],
        validate=True,
    )

    # This is the real canonical save path that normalized run 29's overlay.
    OPCConfig.load(config_dir).save(config_dir)
    assert system_config.read_bytes() == expected_normalized
    assert expected_normalized != raw_overlay

    overlay.restore()
    assert system_config.read_bytes() == original
    assert system_config.stat().st_mode & 0o777 == 0o640
    assert not overlay.journal_path.exists()


def test_execute_shell_overlay_restores_all_permission_mode_bits(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    system_config = config_dir / "system_config.yaml"
    original = (
        b"autonomy:\n"
        b"  permissions_v2:\n"
        b"    dangerous_shell_patterns: []\n"
    )
    system_config.write_bytes(original)
    system_config.chmod(0o3640)
    overlay = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )

    overlay.install()
    assert stat.S_IMODE(system_config.stat().st_mode) == 0o3640
    overlay.restore()

    assert system_config.read_bytes() == original
    assert stat.S_IMODE(system_config.stat().st_mode) == 0o3640


def test_execute_shell_overlay_recovers_interrupted_process_before_reinstall(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    system_config = config_dir / "system_config.yaml"
    original = b"""system: {}\nautonomy:\n  enabled: true\n  allow_native_tool_auto_approval: true\n  permissions_v2:\n    enabled: true\n    dangerous_shell_patterns: []\n"""
    system_config.write_bytes(original)
    first = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )
    first.install()
    overlaid = system_config.read_bytes()
    assert overlaid != original
    assert first.journal_path.is_file()

    # Simulate a new process after the first one died without finally. The
    # second install must recover the journaled original before constructing
    # its own overlay, so its later restore cannot preserve an old overlay.
    second = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )
    second.install()
    assert system_config.read_bytes() == overlaid
    second.restore()

    assert system_config.read_bytes() == original
    assert not second.journal_path.exists()


def test_execute_shell_overlay_recovers_interrupted_normalized_v2_overlay(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    system_config = config_dir / "system_config.yaml"
    original = (
        b"autonomy:\n"
        b"  enabled: true\n"
        b"  allow_native_tool_auto_approval: true\n"
        b"  permissions_v2:\n"
        b"    enabled: true\n"
        b"    dangerous_shell_patterns: []\n"
    )
    system_config.write_bytes(original)
    system_config.chmod(0o600)
    interrupted = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )
    interrupted.install()
    journal = json.loads(interrupted.journal_path.read_text(encoding="utf-8"))
    normalized = base64.b64decode(
        journal["normalized_overlay_base64"],
        validate=True,
    )
    system_config.write_bytes(normalized)

    recovery = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )
    recovery._recover_interrupted_install()

    assert system_config.read_bytes() == original
    assert system_config.stat().st_mode & 0o777 == 0o600
    assert not recovery.journal_path.exists()


def test_execute_shell_overlay_recovers_run29_shape_legacy_v1_normalized_overlay(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    system_config = config_dir / "system_config.yaml"
    original = (
        b"system: {}\n"
        b"autonomy:\n"
        b"  enabled: true\n"
        b"  allow_native_tool_auto_approval: true\n"
        b"  permissions_v2:\n"
        b"    enabled: true\n"
        b"    dangerous_shell_patterns: []\n"
        b"capabilities: {}\n"
    )
    system_config.write_bytes(original)
    system_config.chmod(0o640)
    interrupted = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )
    interrupted.install()
    v2 = json.loads(interrupted.journal_path.read_text(encoding="utf-8"))
    normalized = base64.b64decode(
        v2["normalized_overlay_base64"],
        validate=True,
    )
    # Exact field shape emitted by the pre-fix process. Its mode used st_mode,
    # rather than only permission bits, and it did not store normalized bytes.
    legacy_v1 = {
        "version": 1,
        "config_path": str(system_config),
        "original_base64": v2["original_base64"],
        "original_sha256": v2["original_sha256"],
        "overlay_sha256": v2["raw_overlay_sha256"],
        "original_mode": system_config.stat().st_mode,
    }
    interrupted.journal_path.write_text(
        json.dumps(legacy_v1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    system_config.write_bytes(normalized)

    recovery = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )
    recovery._recover_interrupted_install()

    assert system_config.read_bytes() == original
    assert system_config.stat().st_mode & 0o777 == 0o640
    assert not recovery.journal_path.exists()


def test_execute_shell_overlay_refuses_external_config_change(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    system_config = config_dir / "system_config.yaml"
    original = b"""system: {}\nautonomy:\n  enabled: true\n  permissions_v2:\n    enabled: true\n    dangerous_shell_patterns: []\n"""
    system_config.write_bytes(original)
    overlay = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )
    overlay.install()
    installed = system_config.read_bytes()
    external = b"autonomy:\n  enabled: false\n"
    system_config.write_bytes(external)

    try:
        overlay.restore()
    except RuntimeError as exc:
        assert "refused to overwrite externally modified config" in str(exc)
    else:
        raise AssertionError("overlay restore overwrote an external config change")
    assert system_config.read_bytes() == external
    assert overlay.journal_path.is_file()

    # A later process must fail closed for the same hash conflict too.
    recovery = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )
    try:
        recovery.install()
    except RuntimeError as exc:
        assert "refused to overwrite externally modified config" in str(exc)
    else:
        raise AssertionError("recovery overwrote an external config change")

    # Repair the simulated external edit so the test leaves no journaled
    # overlay behind in its temporary directory.
    system_config.write_bytes(installed)
    overlay.restore()
    assert system_config.read_bytes() == original
    assert not overlay.journal_path.exists()


def test_execute_shell_overlay_refuses_semantically_equivalent_byte_drift(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    system_config = config_dir / "system_config.yaml"
    original = (
        b"autonomy:\n"
        b"  enabled: true\n"
        b"  permissions_v2:\n"
        b"    enabled: true\n"
        b"    dangerous_shell_patterns: []\n"
    )
    system_config.write_bytes(original)
    overlay = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )
    overlay.install()
    installed = system_config.read_bytes()
    system_config.write_bytes(installed + b"# semantically equivalent external edit\n")

    with pytest.raises(
        RuntimeError,
        match="refused to overwrite externally modified config",
    ):
        overlay.restore()

    system_config.write_bytes(installed)
    overlay.restore()
    assert system_config.read_bytes() == original


@pytest.mark.parametrize(
    "tamper",
    (
        "missing_field",
        "extra_field",
        "raw_bytes_and_hash",
        "normalized_bytes_and_hash",
        "invalid_version",
    ),
)
def test_execute_shell_overlay_rejects_tampered_v2_journal(
    tmp_path: Path,
    tamper: str,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    system_config = config_dir / "system_config.yaml"
    original = (
        b"autonomy:\n"
        b"  allow_native_tool_auto_approval: true\n"
        b"  permissions_v2:\n"
        b"    dangerous_shell_patterns: []\n"
    )
    system_config.write_bytes(original)
    overlay = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )
    overlay.install()
    installed = system_config.read_bytes()
    valid_journal_bytes = overlay.journal_path.read_bytes()
    journal = json.loads(valid_journal_bytes)

    if tamper == "missing_field":
        journal.pop("normalized_overlay_sha256")
    elif tamper == "extra_field":
        journal["unexpected"] = "field"
    elif tamper == "raw_bytes_and_hash":
        tampered = b"autonomy: {}\n"
        journal["raw_overlay_base64"] = base64.b64encode(tampered).decode("ascii")
        journal["raw_overlay_sha256"] = hashlib.sha256(tampered).hexdigest()
    elif tamper == "normalized_bytes_and_hash":
        tampered = b"system: {}\nautonomy: {}\ncapabilities: {}\n"
        journal["normalized_overlay_base64"] = base64.b64encode(tampered).decode(
            "ascii"
        )
        journal["normalized_overlay_sha256"] = hashlib.sha256(tampered).hexdigest()
    else:
        journal["version"] = 3
    overlay.journal_path.write_text(
        json.dumps(journal, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    recovery = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )
    with pytest.raises(RuntimeError):
        recovery.install()
    assert system_config.read_bytes() == installed
    assert recovery.journal_path.exists()

    overlay.journal_path.write_bytes(valid_journal_bytes)
    overlay.restore()
    assert system_config.read_bytes() == original


def test_execute_shell_overlay_rejects_duplicate_json_members(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    system_config = config_dir / "system_config.yaml"
    original = (
        b"autonomy:\n"
        b"  permissions_v2:\n"
        b"    dangerous_shell_patterns: []\n"
    )
    system_config.write_bytes(original)
    overlay = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )
    overlay.install()
    installed = system_config.read_bytes()
    valid_journal_bytes = overlay.journal_path.read_bytes()
    journal_text = valid_journal_bytes.decode("utf-8").strip()
    assert journal_text.startswith("{")
    overlay.journal_path.write_text(
        '{"version": 2,' + journal_text[1:],
        encoding="utf-8",
    )

    recovery = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )
    with pytest.raises(
        RuntimeError,
        match="cannot read E2E shell overlay recovery journal",
    ):
        recovery.install()
    assert system_config.read_bytes() == installed

    overlay.journal_path.write_bytes(valid_journal_bytes)
    overlay.restore()
    assert system_config.read_bytes() == original


def test_execute_shell_overlay_rejects_transplanted_recovery_journal(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    system_config = config_dir / "system_config.yaml"
    system_config.write_text(
        "autonomy:\n  permissions_v2:\n    dangerous_shell_patterns: []\n",
        encoding="utf-8",
    )
    overlay = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )
    overlay.install()
    journal = json.loads(overlay.journal_path.read_text(encoding="utf-8"))
    journal["config_path"] = str(tmp_path / "other" / "system_config.yaml")
    overlay.journal_path.write_text(json.dumps(journal), encoding="utf-8")

    recovery = harness._E2EShellReviewOverlay(
        config_dir,
        workplace=tmp_path / "workplace",
    )
    try:
        recovery.install()
    except RuntimeError as exc:
        assert "belongs to a different config path" in str(exc)
    else:
        raise AssertionError("a transplanted recovery journal was accepted")


def test_execute_run_preserves_primary_exception_when_overlay_restore_also_fails(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    primary = LookupError("inner run failed")
    restore_calls: list[bool] = []

    class FailingRestoreOverlay:
        def __init__(self, config_dir: Path, *, workplace: Path) -> None:
            del config_dir, workplace

        def install(self) -> dict[str, Any]:
            return {}

        def restore(self) -> None:
            restore_calls.append(True)
            raise RuntimeError("restore failed too")

    async def validate_async(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"validated": True}

    async def fail_inner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise primary

    monkeypatch.setattr(harness, "_E2EShellReviewOverlay", FailingRestoreOverlay)
    monkeypatch.setattr(
        harness,
        "_validate_setup_without_execution",
        lambda *args, **kwargs: {"workplace": str(tmp_path / "workplace")},
    )
    monkeypatch.setattr(
        harness,
        "_validate_installed_shell_async_authorization",
        validate_async,
    )
    monkeypatch.setattr(harness, "_run_with_installed_shell_review", fail_inner)

    args = SimpleNamespace(
        opc_home=tmp_path / ".opc",
        project_id="project-a",
    )
    with pytest.raises(LookupError, match="inner run failed") as caught:
        asyncio.run(harness._run(args))

    assert caught.value is primary
    assert restore_calls == [True]
    assert any(
        "Secondary E2E shell overlay restore failure" in note
        and "restore failed too" in note
        for note in getattr(caught.value, "__notes__", [])
    )


def test_execute_run_restores_overlay_before_successful_return(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    restore_calls: list[bool] = []

    class RecordingOverlay:
        def __init__(self, config_dir: Path, *, workplace: Path) -> None:
            del config_dir, workplace

        def install(self) -> dict[str, Any]:
            return {}

        def restore(self) -> None:
            restore_calls.append(True)

    async def validate_async(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"validated": True}

    expected = {"success": True}

    async def succeed_inner(*args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return expected

    monkeypatch.setattr(harness, "_E2EShellReviewOverlay", RecordingOverlay)
    monkeypatch.setattr(
        harness,
        "_validate_setup_without_execution",
        lambda *args, **kwargs: {"workplace": str(tmp_path / "workplace")},
    )
    monkeypatch.setattr(
        harness,
        "_validate_installed_shell_async_authorization",
        validate_async,
    )
    monkeypatch.setattr(harness, "_run_with_installed_shell_review", succeed_inner)

    result = asyncio.run(
        harness._run(
            SimpleNamespace(
                opc_home=tmp_path / ".opc",
                project_id="project-a",
            )
        )
    )

    assert result is expected
    assert restore_calls == [True]


@pytest.mark.parametrize(
    ("terminal_kind", "expected_error"),
    (
        ("closed_run", "terminal failure"),
        (
            "failed_delivery",
            "final delivery entered a terminal failed/cancelled phase",
        ),
        (
            "cancelled_delivery",
            "final delivery entered a terminal failed/cancelled phase",
        ),
        (
            "closed_impossible_frontier",
            "closed impossible final-delivery frontier",
        ),
    ),
)
def test_drive_case_fails_fast_on_terminal_company_state(
    tmp_path: Path,
    terminal_kind: str,
    expected_error: str,
) -> None:
    async def scenario() -> None:
        session_id = f"terminal-{terminal_kind}"
        spec = harness.CaseSpec(
            case_id="terminal",
            title="Terminal state regression",
            org_id="terminal-native-org",
            organization_name="Terminal Native Org",
            roles=({"id": "lead"},),
            prompt="Run",
            required_artifacts=(),
        )
        run = harness.CaseRun(
            spec=spec,
            session_id=session_id,
            ui_anchor_task_id="6e461115-a7f4-4a76-ae0c-7ba6c1e74e7f",
            started_at="now",
        )

        class TerminalStore(_MemoryCheckpointStore):
            async def list_delegation_runs(
                self,
                *,
                project_id: str,
                session_id: str,
            ) -> list[Any]:
                assert project_id == "project-a"
                assert session_id == run.session_id
                return [
                    SimpleNamespace(
                        run_id="run-terminal",
                        status=(
                            "failed"
                            if terminal_kind == "closed_run"
                            else "running"
                        ),
                        lifecycle_status=(
                            "closed_failed"
                            if terminal_kind == "closed_run"
                            else "active"
                        ),
                        controller_owner_token="",
                        metadata={
                            "run_failure": {"failure_kind": "quality_gate"}
                        },
                    )
                ]

            async def list_delegation_work_items(
                self,
                run_id: str,
            ) -> list[Any]:
                assert run_id == "run-terminal"
                if terminal_kind == "closed_impossible_frontier":
                    return [
                        SimpleNamespace(
                            work_item_id="required-child",
                            projection_id="required_child",
                            kind="execute",
                            phase=SimpleNamespace(value="failed"),
                            blocked_reason="provider quota exhausted",
                            metadata={"work_kind": "execute"},
                        ),
                        SimpleNamespace(
                            work_item_id="delivery-terminal",
                            projection_id="final_delivery",
                            kind="delivery",
                            phase=SimpleNamespace(value="ready_for_rework"),
                            blocked_reason="",
                            metadata={
                                "work_kind": "delivery",
                                "feedback_scope": "final",
                                "authoritative_output": True,
                                "dependency_work_item_ids": ["required-child"],
                                "waiting_on_work_item_ids": ["required-child"],
                            },
                        ),
                    ]
                if terminal_kind not in {
                    "failed_delivery",
                    "cancelled_delivery",
                }:
                    return []
                return [
                    SimpleNamespace(
                        work_item_id="delivery-terminal",
                        projection_id="final_delivery",
                        kind="delivery",
                        phase=SimpleNamespace(
                            value=(
                                "cancelled"
                                if terminal_kind == "cancelled_delivery"
                                else "failed"
                            )
                        ),
                        blocked_reason="quality validation failed",
                        metadata={
                            "work_kind": "delivery",
                            "work_item_turn_type": "deliver",
                            "feedback_scope": "final",
                            "authoritative_output": True,
                        },
                    )
                ]

        store = TerminalStore()
        await harness._persist_office_ui_root_task(
            store,
            run,
            project_id="project-a",
        )

        class FakeEngine:
            def __init__(self) -> None:
                self.store = store

            async def process_message(self, *_args: Any, **_kwargs: Any) -> str:
                await asyncio.sleep(60)
                return "unexpected completion"

        with pytest.raises(AssertionError, match=expected_error):
            await asyncio.wait_for(
                harness._drive_case(
                    FakeEngine(),
                    run,
                    project_id="project-a",
                    workplace=tmp_path,
                    poll_seconds=0.001,
                    timeout_seconds=2700.0,
                    state_changed=lambda: None,
                ),
                timeout=1.0,
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "variant",
    (
        "live_controller",
        "soft_dependency",
        "info_dependency",
        "nonfinal_delivery",
        "nonauthoritative_delivery",
        "not_waiting_on_failed_dependency",
    ),
)
def test_closed_impossible_frontier_ignores_recoverable_or_nonfinal_state(
    variant: str,
) -> None:
    dependency_classes: dict[str, str] = {}
    waiting_on = ["required-child"]
    feedback_scope = "final"
    authoritative_output = True
    controller_owner_token = ""
    if variant == "live_controller":
        controller_owner_token = "live-owner"
    elif variant == "soft_dependency":
        dependency_classes["required-child"] = "soft"
    elif variant == "info_dependency":
        dependency_classes["required-child"] = "info"
    elif variant == "nonfinal_delivery":
        feedback_scope = "intermediate"
    elif variant == "nonauthoritative_delivery":
        authoritative_output = False
    elif variant == "not_waiting_on_failed_dependency":
        waiting_on = []

    delegation_run = SimpleNamespace(
        run_id="run-recoverable",
        controller_owner_token=controller_owner_token,
    )
    child = SimpleNamespace(
        work_item_id="required-child",
        kind="execute",
        phase=SimpleNamespace(value="failed"),
        metadata={"work_kind": "execute"},
    )
    delivery = SimpleNamespace(
        work_item_id="delivery-recoverable",
        projection_id="final_delivery",
        kind="delivery",
        phase=SimpleNamespace(value="ready_for_rework"),
        metadata={
            "feedback_scope": feedback_scope,
            "authoritative_output": authoritative_output,
            "dependency_work_item_ids": [child.work_item_id],
            "waiting_on_work_item_ids": waiting_on,
            "dependency_classes": dependency_classes,
        },
    )

    assert harness._closed_impossible_final_delivery_frontiers(
        delegation_run,
        [child, delivery],
    ) == []


def test_failed_delivery_filter_ignores_failed_self_evolution_auxiliary() -> None:
    auxiliary = SimpleNamespace(
        work_item_id="self-evolution-auxiliary",
        projection_id="self_evolution::lead",
        kind="self_evolution",
        phase=SimpleNamespace(value="failed"),
        metadata={
            "work_kind": "self_evolution",
            "work_item_turn_type": "self_evolution",
            "self_evolution_work_item": True,
            "self_evolution_delivery_task_id": "task-delivery",
        },
    )
    final_delivery = SimpleNamespace(
        work_item_id="final-delivery",
        projection_id="final_delivery",
        kind="delivery",
        phase=SimpleNamespace(value="failed"),
        metadata={
            "work_item_turn_type": "deliver",
            "feedback_scope": "final",
            "authoritative_output": True,
        },
    )

    assert harness._failed_delivery_work_items([auxiliary]) == []
    assert harness._failed_delivery_work_items(
        [auxiliary, final_delivery]
    ) == [final_delivery]


@pytest.mark.parametrize(
    ("kind", "auxiliary_metadata"),
    (
        (
            "self_evolution",
            {
                "self_evolution_work_item": True,
                "work_item_turn_type": "self_evolution",
            },
        ),
        (
            "runtime_auxiliary",
            {
                "runtime_auxiliary_task": True,
                "runtime_auxiliary_kind": "meeting_turn",
            },
        ),
        (
            "delivery",
            {
                "company_runtime_auxiliary_task": True,
                "runtime_auxiliary_kind": "role_prompt",
            },
        ),
        ("attention", {"attention_work_item": True}),
        ("report", {"report_execution_work_item": True}),
        ("review", {"review_execution_work_item": True}),
    ),
)
def test_closed_impossible_frontier_ignores_auxiliary_with_stale_final_markers(
    kind: str,
    auxiliary_metadata: dict[str, Any],
) -> None:
    child = SimpleNamespace(
        work_item_id="failed-child",
        kind="execute",
        phase=SimpleNamespace(value="failed"),
        metadata={"work_kind": "execute"},
    )
    auxiliary = SimpleNamespace(
        work_item_id=f"{kind}-auxiliary",
        projection_id=f"{kind}_auxiliary",
        kind=kind,
        phase=SimpleNamespace(value="ready_for_rework"),
        metadata={
            # Deliberately model a stale/copy-contaminated helper card.
            "work_kind": "delivery",
            "feedback_scope": "final",
            "authoritative_output": True,
            "dependency_work_item_ids": [child.work_item_id],
            "waiting_on_work_item_ids": [child.work_item_id],
            **auxiliary_metadata,
        },
    )

    assert harness._closed_impossible_final_delivery_frontiers(
        SimpleNamespace(
            run_id="run-auxiliary",
            controller_owner_token="",
        ),
        [child, auxiliary],
    ) == []
    auxiliary.phase = SimpleNamespace(value="cancelled")
    assert harness._failed_delivery_work_items([auxiliary]) == []


def test_terminal_failure_check_rereads_run_before_declaring_closed_frontier() -> None:
    async def scenario() -> None:
        initial_run = SimpleNamespace(
            run_id="run-racing-owner",
            status="running",
            lifecycle_status="active",
            controller_owner_token="",
            metadata={},
        )
        acquired_run = SimpleNamespace(
            run_id="run-racing-owner",
            status="running",
            lifecycle_status="active",
            controller_owner_token="replacement-controller",
            metadata={},
        )
        child = SimpleNamespace(
            work_item_id="failed-child",
            kind="execute",
            phase=SimpleNamespace(value="failed"),
            metadata={"work_kind": "execute"},
        )
        delivery = SimpleNamespace(
            work_item_id="final-delivery",
            projection_id="final_delivery",
            kind="delivery",
            phase=SimpleNamespace(value="ready_for_rework"),
            metadata={
                "work_item_turn_type": "deliver",
                "feedback_scope": "final",
                "authoritative_output": True,
                "dependency_work_item_ids": [child.work_item_id],
                "waiting_on_work_item_ids": [child.work_item_id],
            },
        )

        class RacingOwnerStore:
            def __init__(self) -> None:
                self.get_run_calls = 0

            async def list_delegation_runs(
                self,
                *,
                project_id: str,
                session_id: str,
            ) -> list[Any]:
                assert project_id == "project-race"
                assert session_id == "session-race"
                return [initial_run]

            async def list_delegation_work_items(
                self,
                run_id: str,
            ) -> list[Any]:
                assert run_id == initial_run.run_id
                return [child, delivery]

            async def get_delegation_run(self, run_id: str) -> Any:
                assert run_id == initial_run.run_id
                self.get_run_calls += 1
                return acquired_run

        store = RacingOwnerStore()
        await harness._raise_if_case_terminally_failed(
            store,
            project_id="project-race",
            session_id="session-race",
            case_id="owner-race",
        )
        assert store.get_run_calls == 1

    asyncio.run(scenario())


def test_drive_case_continues_after_process_returns_staffing_pause(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        session_id = "issue35-selftest-session"
        spec = harness.CaseSpec(
            case_id="selftest",
            title="Harness self-test",
            org_id="selftest-native-org",
            organization_name="Self-test Native Org",
            roles=(
                {"id": "lead"},
                {"id": "worker"},
            ),
            prompt="Run the self-test company task",
            required_artifacts=(),
        )
        run = harness.CaseRun(
            spec=spec,
            session_id=session_id,
            ui_anchor_task_id="a054b4a5-8416-4a48-b4d2-e144127d210c",
            started_at="now",
        )
        store = _MemoryCheckpointStore()
        await harness._persist_office_ui_root_task(
            store,
            run,
            project_id="project-a",
        )

        class FakeEngine:
            def __init__(self) -> None:
                self.store = store
                self.process_calls: list[dict[str, Any]] = []

            async def process_message(self, content: str, **kwargs: Any) -> str:
                self.process_calls.append({"content": content, **kwargs})
                store.rows.append(
                    _checkpoint(
                        checkpoint_id="staffing-1",
                        checkpoint_type=harness.STAFFING_CHECKPOINT_TYPE,
                        session_id=session_id,
                        payload={
                            "company_profile": "custom",
                            "org_id": spec.org_id,
                            "primary_session_id": session_id,
                            "staffing_roles": [
                                {
                                    "role_id": role_id,
                                    "default_agent": "native",
                                    "selected_agent": "native",
                                }
                                for role_id in ("lead", "worker")
                            ],
                            "interaction": {
                                "kind": harness.STAFFING_CHECKPOINT_TYPE,
                                "execution_scope": {
                                    "company_profile": "custom",
                                    "org_id": spec.org_id,
                                },
                            },
                        },
                    )
                )
                return "pending manual staffing selection"

            async def submit_checkpoint_decision(
                self,
                **kwargs: Any,
            ) -> dict[str, Any]:
                staffing = next(
                    row for row in store.rows if row.checkpoint_id == "staffing-1"
                )
                decision = dict(kwargs["decision"])
                assert decision["staffing_action"] == "manual_approve"
                assert decision["recruitment_agent"] == "native"
                assert set(decision["recruitment_role_agents"].values()) == {
                    "native"
                }
                staffing.status = "resolved"
                staffing.payload["interaction"]["decision"] = {
                    "value": decision
                }
                store.rows.append(
                    _checkpoint(
                        checkpoint_id="feedback-1",
                        checkpoint_type=harness.FINAL_CHECKPOINT_TYPE,
                        session_id=session_id,
                        payload={
                            "interaction": {
                                "kind": harness.FINAL_CHECKPOINT_TYPE,
                                "execution_scope": {
                                    "company_profile": "custom",
                                    "org_id": spec.org_id,
                                },
                            }
                        },
                    )
                )
                return {
                    "accepted": True,
                    "status": "answered",
                    "checkpoint_id": staffing.checkpoint_id,
                    "checkpoint_type": staffing.checkpoint_type,
                }

        engine = FakeEngine()
        state_changes = 0

        def state_changed() -> None:
            nonlocal state_changes
            state_changes += 1

        await harness._drive_case(
            engine,
            run,
            project_id="project-a",
            workplace=tmp_path,
            poll_seconds=0.001,
            timeout_seconds=1.0,
            state_changed=state_changed,
        )

        assert len(engine.process_calls) == 1
        assert engine.process_calls[0]["mode"] == "org"
        assert engine.process_calls[0]["org_id"] == spec.org_id
        assert engine.process_calls[0]["preferred_agent"] == "native"
        assert (
            engine.process_calls[0]["origin_task_id"]
            == run.ui_anchor_task_id
        )
        assert run.response == "pending manual staffing selection"
        assert run.feedback_checkpoint_id == "feedback-1"
        assert len(run.staffing_decisions) == 1
        assert run.staffing_decisions[0]["staffing_action"] == "manual_approve"
        assert set(
            run.staffing_decisions[0]["recruitment_role_agents"].values()
        ) == {"native"}
        assert state_changes >= 2

    asyncio.run(scenario())


def test_drive_case_waits_for_staffing_completion_before_final_frontier(
    tmp_path: Path,
) -> None:
    """A final card may precede the outer staffing consumer by one tick."""

    async def scenario() -> None:
        session_id = "staffing-final-overlap"
        spec = harness.CaseSpec(
            case_id="overlap",
            title="Overlap regression",
            org_id="overlap-native-org",
            organization_name="Overlap Native Org",
            roles=({"id": "lead"},),
            prompt="Run",
            required_artifacts=(),
        )
        run = harness.CaseRun(
            spec=spec,
            session_id=session_id,
            ui_anchor_task_id="fa329c38-a9b9-4aa1-9583-28b28b0f6c73",
            started_at="now",
        )
        store = _MemoryCheckpointStore()
        await harness._persist_office_ui_root_task(
            store,
            run,
            project_id="project-a",
        )
        settle_tasks: list[asyncio.Task[None]] = []

        class FakeEngine:
            def __init__(self) -> None:
                self.store = store

            async def process_message(self, content: str, **kwargs: Any) -> str:
                del content, kwargs
                store.rows.append(
                    _checkpoint(
                        checkpoint_id="staffing-overlap",
                        checkpoint_type=harness.STAFFING_CHECKPOINT_TYPE,
                        session_id=session_id,
                        payload={
                            "company_profile": "custom",
                            "org_id": spec.org_id,
                            "primary_session_id": session_id,
                            "staffing_roles": [
                                {
                                    "role_id": "lead",
                                    "default_agent": "native",
                                    "selected_agent": "native",
                                    "default_selection": {"kind": "fallback"},
                                }
                            ],
                            "interaction": {
                                "kind": harness.STAFFING_CHECKPOINT_TYPE,
                                "execution_scope": {
                                    "company_profile": "custom",
                                    "org_id": spec.org_id,
                                },
                            },
                        },
                    )
                )
                return "pending manual staffing selection"

            async def submit_checkpoint_decision(
                self,
                **kwargs: Any,
            ) -> dict[str, Any]:
                staffing = next(
                    row
                    for row in store.rows
                    if row.checkpoint_id == "staffing-overlap"
                )
                staffing.status = "consuming"
                staffing.payload["interaction"]["decision"] = {
                    "value": dict(kwargs["decision"])
                }
                store.rows.append(
                    _checkpoint(
                        checkpoint_id="feedback-during-staffing",
                        checkpoint_type=harness.FINAL_CHECKPOINT_TYPE,
                        session_id=session_id,
                        payload={
                            "interaction": {
                                "kind": harness.FINAL_CHECKPOINT_TYPE,
                                "execution_scope": {
                                    "company_profile": "custom",
                                    "org_id": spec.org_id,
                                },
                            }
                        },
                    )
                )

                async def settle_staffing() -> None:
                    await asyncio.sleep(0.01)
                    staffing.status = "resolved"

                settle_tasks.append(asyncio.create_task(settle_staffing()))
                return {
                    "accepted": True,
                    "status": "answered",
                    "checkpoint_id": staffing.checkpoint_id,
                    "checkpoint_type": staffing.checkpoint_type,
                }

        try:
            await harness._drive_case(
                FakeEngine(),
                run,
                project_id="project-a",
                workplace=tmp_path,
                poll_seconds=0.001,
                timeout_seconds=1.0,
                state_changed=lambda: None,
            )
        finally:
            if settle_tasks:
                await asyncio.gather(*settle_tasks)

        assert run.feedback_checkpoint_id == "feedback-during-staffing"
        staffing = next(
            row for row in store.rows if row.checkpoint_id == "staffing-overlap"
        )
        assert staffing.status == "resolved"

    asyncio.run(scenario())


def test_exact_resumed_staffing_outcome_unknown_is_accepted_fail_closed() -> None:
    run, checkpoint = _resumed_staffing_outcome_unknown_fixture()

    assert harness._is_exact_resumed_staffing_outcome_unknown(run, checkpoint)
    assert harness._staffing_checkpoint_is_accepted(run, checkpoint)

    fresh_run = copy.deepcopy(run)
    fresh_run.resume_existing = False
    assert not harness._is_exact_resumed_staffing_outcome_unknown(
        fresh_run,
        checkpoint,
    )


@pytest.mark.parametrize(
    "malformation",
    (
        "decision_hash",
        "checkpoint_session",
        "missing_timestamp",
        "unordered_timestamps",
        "receipt_status",
        "submission_root_session",
        "submission_company_profile",
        "submission_org",
    ),
)
def test_exact_resumed_staffing_outcome_unknown_rejects_contract_drift(
    malformation: str,
) -> None:
    run, checkpoint = _resumed_staffing_outcome_unknown_fixture()

    if malformation == "decision_hash":
        checkpoint.payload["interaction"]["decision"]["decision_hash"] = "forged"
    elif malformation == "checkpoint_session":
        checkpoint.session_id = "another-session"
    elif malformation == "missing_timestamp":
        checkpoint.payload["interaction"]["completion"].pop("finished_at")
    elif malformation == "unordered_timestamps":
        checkpoint.payload["interaction"]["execution"]["detected_at"] = (
            "2026-08-13T14:00:00"
        )
    elif malformation == "receipt_status":
        run.staffing_decisions[0]["receipt"]["status"] = "pending"
    elif malformation == "submission_root_session":
        run.staffing_decisions[0]["root_session_id"] = "another-session"
    elif malformation == "submission_company_profile":
        run.staffing_decisions[0]["company_profile"] = "corporate"
    elif malformation == "submission_org":
        run.staffing_decisions[0]["org_id"] = "another-org"

    assert not harness._is_exact_resumed_staffing_outcome_unknown(run, checkpoint)


def test_recovered_staffing_evidence_binds_exact_run_root_and_interruption() -> None:
    async def scenario() -> None:
        run, staffing = _resumed_staffing_outcome_unknown_fixture()
        store = _MemoryCheckpointStore()
        store.rows.append(staffing)
        delegation_run, _origin_task, interrupted = (
            _install_exact_staffing_recovery_runtime(
                store,
                run,
                staffing,
                historical_resolved=1,
            )
        )

        evidence = await harness._resumed_staffing_runtime_recovery_evidence(
            store,
            run,
            staffing,
            project_id="project-a",
        )

        assert evidence is not None
        assert evidence["delegation_run_id"] == delegation_run.run_id
        assert evidence["checkpoint_id"] == interrupted.checkpoint_id
        assert evidence["origin_task_id"] == interrupted.task_id
        assert evidence["staffing_claim_id"] == "expired-claim"

    asyncio.run(scenario())


@pytest.mark.parametrize("active_count", (0, 2))
def test_recovered_staffing_evidence_requires_one_active_interruption(
    active_count: int,
) -> None:
    async def scenario() -> None:
        run, staffing = _resumed_staffing_outcome_unknown_fixture()
        store = _MemoryCheckpointStore()
        store.rows.append(staffing)
        _delegation_run, _origin_task, interrupted = (
            _install_exact_staffing_recovery_runtime(store, run, staffing)
        )
        if active_count == 0:
            interrupted.status = "resolved"
        else:
            duplicate = copy.deepcopy(interrupted)
            duplicate.checkpoint_id = "second-active-interruption"
            store.rows.append(duplicate)

        with pytest.raises(AssertionError, match="exactly one active"):
            await harness._resumed_staffing_runtime_recovery_evidence(
                store,
                run,
                staffing,
                project_id="project-a",
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("drift", ("origin", "run"))
def test_recovered_staffing_evidence_rejects_origin_or_run_drift(
    drift: str,
) -> None:
    async def scenario() -> None:
        run, staffing = _resumed_staffing_outcome_unknown_fixture()
        store = _MemoryCheckpointStore()
        store.rows.append(staffing)
        _delegation_run, origin_task, interrupted = (
            _install_exact_staffing_recovery_runtime(store, run, staffing)
        )
        if drift == "origin":
            origin_task.metadata["origin_owner_interaction"]["claim_id"] = (
                "another-claim"
            )
        else:
            interrupted.payload["run_id"] = "another-delegation-run"

        with pytest.raises(AssertionError, match="scope|origin"):
            await harness._resumed_staffing_runtime_recovery_evidence(
                store,
                run,
                staffing,
                project_id="project-a",
            )

    asyncio.run(scenario())


def test_recovered_staffing_final_requires_same_resolved_handoff() -> None:
    async def scenario() -> None:
        run, staffing = _resumed_staffing_outcome_unknown_fixture()
        store = _MemoryCheckpointStore()
        store.rows.append(staffing)
        delegation_run, _origin_task, interrupted = (
            _install_exact_staffing_recovery_runtime(store, run, staffing)
        )
        preflight = await harness._resumed_staffing_runtime_recovery_evidence(
            store,
            run,
            staffing,
            project_id="project-a",
        )
        assert preflight is not None

        # Model the controller's run-level handoff before the durable
        # interruption card itself has crossed its resolved fence.
        delegation_run.lifecycle_status = "awaiting_owner"
        with pytest.raises(AssertionError, match="missing or duplicated"):
            await harness._resumed_staffing_runtime_recovery_evidence(
                store,
                run,
                staffing,
                project_id="project-a",
                expected_checkpoint_id="a-different-interruption",
                expected_delegation_run_id=delegation_run.run_id,
                require_resolved=True,
            )
        with pytest.raises(AssertionError, match="active|status"):
            await harness._resumed_staffing_runtime_recovery_evidence(
                store,
                run,
                staffing,
                project_id="project-a",
                expected_checkpoint_id=interrupted.checkpoint_id,
                expected_delegation_run_id=delegation_run.run_id,
                require_resolved=True,
            )

        _resolve_runtime_interruption(delegation_run, interrupted)
        final = await harness._resumed_staffing_runtime_recovery_evidence(
            store,
            run,
            staffing,
            project_id="project-a",
            expected_checkpoint_id=interrupted.checkpoint_id,
            expected_delegation_run_id=delegation_run.run_id,
            require_resolved=True,
        )
        assert final is not None
        assert final["checkpoint_status"] == "resolved"

        interrupted.payload["resume_handoff_at"] = "2026-08-13T14:41:00+00:00"
        with pytest.raises(AssertionError, match="handoff drifted"):
            await harness._resumed_staffing_runtime_recovery_evidence(
                store,
                run,
                staffing,
                project_id="project-a",
                expected_checkpoint_id=interrupted.checkpoint_id,
                expected_delegation_run_id=delegation_run.run_id,
                require_resolved=True,
            )

    asyncio.run(scenario())


def test_drive_case_invalid_recovery_preflight_does_not_process_message(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        run, staffing = _resumed_staffing_outcome_unknown_fixture()
        store = _MemoryCheckpointStore()
        await store.save_task(
            harness._build_office_ui_root_task(run, project_id="project-a")
        )
        store.rows.append(staffing)

        class FakeEngine:
            def __init__(self) -> None:
                self.store = store
                self.process_calls: list[dict[str, Any]] = []

            async def process_message(self, content: str, **kwargs: Any) -> str:
                self.process_calls.append({"content": content, **kwargs})
                return "must not run"

        engine = FakeEngine()
        with pytest.raises(AssertionError, match="exactly one DelegationRun"):
            await harness._drive_case(
                engine,
                run,
                project_id="project-a",
                workplace=tmp_path,
                poll_seconds=0.001,
                timeout_seconds=1.0,
                state_changed=lambda: None,
            )
        assert engine.process_calls == []

    asyncio.run(scenario())


def test_drive_case_persists_recovery_anchor_before_process_message(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        run, staffing = _resumed_staffing_outcome_unknown_fixture()
        store = _MemoryCheckpointStore()
        await store.save_task(
            harness._build_office_ui_root_task(run, project_id="project-a")
        )
        store.rows.append(staffing)
        _install_exact_staffing_recovery_runtime(store, run, staffing)

        class FakeEngine:
            def __init__(self) -> None:
                self.store = store
                self.process_calls: list[dict[str, Any]] = []

            async def process_message(self, content: str, **kwargs: Any) -> str:
                self.process_calls.append({"content": content, **kwargs})
                return "must not run"

        engine = FakeEngine()

        def state_changed() -> None:
            assert run.staffing_recovery_checkpoint_id
            assert run.staffing_recovery_run_id
            raise RuntimeError("journal write failed")

        with pytest.raises(RuntimeError, match="journal write failed"):
            await harness._drive_case(
                engine,
                run,
                project_id="project-a",
                workplace=tmp_path,
                poll_seconds=0.001,
                timeout_seconds=1.0,
                state_changed=state_changed,
            )
        assert engine.process_calls == []

    asyncio.run(scenario())


def test_drive_case_resumes_past_exact_staffing_crash_terminal(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        run, staffing = _resumed_staffing_outcome_unknown_fixture()
        store = _MemoryCheckpointStore()
        await store.save_task(
            harness._build_office_ui_root_task(
                run,
                project_id="project-a",
            )
        )
        store.rows.append(staffing)
        delegation_run, _origin_task, interrupted = (
            _install_exact_staffing_recovery_runtime(store, run, staffing)
        )
        state_changes: list[tuple[str, str]] = []

        class FakeEngine:
            def __init__(self) -> None:
                self.store = store
                self.process_calls: list[dict[str, Any]] = []

            async def process_message(self, content: str, **kwargs: Any) -> str:
                self.process_calls.append({"content": content, **kwargs})
                assert content == "continue"
                assert kwargs["message_metadata"]["ui_force_resume"] is True
                assert kwargs["message_metadata"]["response_to_checkpoint_id"] == (
                    interrupted.checkpoint_id
                )
                assert kwargs["message_metadata"]["response_to_checkpoint_type"] == (
                    "company_runtime_interrupted"
                )
                _resolve_runtime_interruption(delegation_run, interrupted)
                store.rows.append(
                    _checkpoint(
                        checkpoint_id="feedback-after-recovery",
                        checkpoint_type=harness.FINAL_CHECKPOINT_TYPE,
                        session_id=run.session_id,
                        payload={
                            "feedback_scope": "final",
                            "review_level": "human",
                            "interaction": {
                                "kind": harness.FINAL_CHECKPOINT_TYPE,
                                "execution_scope": {
                                    "company_profile": "custom",
                                    "org_id": run.spec.org_id,
                                },
                            },
                        },
                    )
                )
                return "resumed existing company run"

        engine = FakeEngine()
        await harness._drive_case(
            engine,
            run,
            project_id="project-a",
            workplace=tmp_path,
            poll_seconds=0.001,
            timeout_seconds=1.0,
            state_changed=lambda: state_changes.append(
                (
                    run.staffing_recovery_checkpoint_id,
                    run.staffing_recovery_run_id,
                )
            ),
        )

        assert len(engine.process_calls) == 1
        assert interrupted.status == "resolved"
        assert run.feedback_checkpoint_id == "feedback-after-recovery"
        assert run.response == "resumed existing company run"
        assert len(run.staffing_decisions) == 1
        assert run.staffing_recovery_checkpoint_id == interrupted.checkpoint_id
        assert run.staffing_recovery_run_id == delegation_run.run_id
        assert state_changes

    asyncio.run(scenario())


def test_mixed_resume_routes_started_investment_and_root_only_app(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        investment, _staffing = _resumed_staffing_outcome_unknown_fixture()
        investment.spec = harness.CaseSpec(
            case_id="investment",
            title="Investment",
            org_id="investment-org",
            organization_name="Investment Org",
            roles=({"id": "lead"},),
            prompt="Analyze investments",
            required_artifacts=(),
        )
        investment.staffing_decisions = []
        app = harness.CaseRun(
            spec=harness.CaseSpec(
                case_id="app",
                title="App",
                org_id="app-org",
                organization_name="App Org",
                roles=({"id": "lead"},),
                prompt="Build the application",
                required_artifacts=(),
            ),
            session_id="issue35-app-root-only",
            ui_anchor_task_id="3d4b8987-f563-41c5-9a67-1b07410d7d18",
            started_at="2026-08-13T15:00:00+00:00",
            resume_existing=True,
        )
        store = _MemoryCheckpointStore()
        await store.save_task(
            harness._build_office_ui_root_task(investment, project_id="project-a")
        )
        await store.save_task(
            harness._build_office_ui_root_task(app, project_id="project-a")
        )
        store.delegation_runs.append(
            SimpleNamespace(
                run_id="investment-started-run",
                project_id="project-a",
                session_id=investment.session_id,
            )
        )

        await harness._refresh_case_resume_routing(
            store,
            [investment, app],
            project_id="project-a",
        )

        assert investment.resume_existing is True
        assert app.resume_existing is False
        assert harness._case_input_content(investment, tmp_path) == "continue"
        app_content = harness._case_input_content(app, tmp_path)
        assert app.spec.prompt in app_content
        assert str(tmp_path) in app_content
        assert app_content != "continue"
        assert investment.session_id == "issue35-recovered-session"
        assert investment.ui_anchor_task_id == (
            "df5d268c-55a6-4a52-a15a-c3cf9978df7d"
        )
        assert app.session_id == "issue35-app-root-only"
        assert app.ui_anchor_task_id == (
            "3d4b8987-f563-41c5-9a67-1b07410d7d18"
        )
        ui_roots = {
            task.id
            for task in store.tasks
            if task.id in {
                investment.ui_anchor_task_id,
                app.ui_anchor_task_id,
            }
        }
        assert ui_roots == {
            investment.ui_anchor_task_id,
            app.ui_anchor_task_id,
        }

    asyncio.run(scenario())


def test_native_staffing_decision_rejects_unscoped_custom_card() -> None:
    async def scenario() -> None:
        spec = harness.CaseSpec(
            case_id="selftest",
            title="Harness self-test",
            org_id="selftest-native-org",
            organization_name="Self-test Native Org",
            roles=({"id": "lead"},),
            prompt="Run",
            required_artifacts=(),
        )
        checkpoint = _checkpoint(
            checkpoint_id="staffing-unscoped",
            checkpoint_type=harness.STAFFING_CHECKPOINT_TYPE,
            session_id="selftest-session",
            payload={
                "company_profile": "custom",
                "org_id": spec.org_id,
                "primary_session_id": "selftest-session",
                "staffing_roles": [
                    {
                        "role_id": "lead",
                        "default_agent": "native",
                        "selected_agent": "native",
                    }
                ],
                "interaction": {
                    "kind": harness.STAFFING_CHECKPOINT_TYPE,
                    "execution_scope": {
                        "company_profile": "custom",
                        "org_id": "",
                    },
                },
            },
        )
        try:
            await harness._approve_native_staffing_checkpoint(
                SimpleNamespace(),
                checkpoint,
                spec=spec,
                session_id="selftest-session",
                client_request_id="request-1",
            )
        except AssertionError as exc:
            assert "durable scope drifted" in str(exc)
        else:
            raise AssertionError("unscoped custom staffing card was accepted")

    asyncio.run(scenario())


def test_ui_root_producer_persists_one_office_shaped_non_work_item_anchor(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        from opc.core.models import Task, TaskStatus
        from opc.database.store import OPCStore
        from opc.layer2_organization.company_runtime_identity import (
            build_company_runtime_identity_index,
            is_pure_company_ui_anchor,
        )

        spec = harness.CaseSpec(
            case_id="app",
            title="Build the app",
            org_id="selftest-native-org",
            organization_name="Self-test Native Org",
            roles=({"id": "lead"}, {"id": "worker"}),
            prompt="Build",
            required_artifacts=("app_case/app.js",),
        )
        run = harness.CaseRun(
            spec=spec,
            session_id="root-company-session",
            ui_anchor_task_id="ad6b944f-ce3f-43d3-85bd-c04664ecec62",
            started_at="now",
        )
        store = OPCStore(tmp_path / "ui-root-producer.db")
        await store.initialize()
        try:
            root = await harness._persist_office_ui_root_task(
                store,
                run,
                project_id="project-a",
            )

            assert root.status == TaskStatus.PENDING
            assert root.parent_id is None
            assert root.parent_session_id is None
            assert root.linked_work_item_id == ""
            assert root.description == ""
            assert root.metadata == harness._office_ui_root_metadata(spec)
            assert is_pure_company_ui_anchor(root, run.session_id)

            child = Task(
                id="runtime-work-item-task",
                project_id="project-a",
                session_id=f"{run.session_id}:worker",
                parent_session_id=run.session_id,
                assigned_to="worker",
                linked_work_item_id="work-item-1",
                metadata={
                    "mode": "company",
                    "company_profile": "custom",
                    "org_id": spec.org_id,
                    "work_item_projection_id": "execute-worker",
                },
            )
            await store.save_task(child)
            identity = build_company_runtime_identity_index(
                await store.get_tasks(project_id="project-a")
            ).resolve(task_id=child.id)
            assert identity is not None
            assert identity.ui_anchor_task_id == run.ui_anchor_task_id
            assert identity.config_source_task_id == run.ui_anchor_task_id
            assert child.id != identity.ui_anchor_task_id

            run.resume_existing = True
            resumed = await harness._persist_office_ui_root_task(
                store,
                run,
                project_id="project-a",
            )
            assert resumed.id == root.id
            persisted = await store.get_tasks(project_id="project-a")
            assert len(
                [task for task in persisted if task.session_id == run.session_id]
            ) == 1
        finally:
            await store.close()

    asyncio.run(scenario())


def test_dry_run_reports_ui_root_origin_contract_without_persisting(
    monkeypatch: Any,
    capsys: Any,
    tmp_path: Path,
) -> None:
    calls: list[tuple[Path, str]] = []
    system_config = tmp_path / "repo" / ".opc" / "config" / "system_config.yaml"
    system_config.parent.mkdir(parents=True)
    original_config = b"autonomy:\n  allow_native_tool_auto_approval: true\n"
    system_config.write_bytes(original_config)

    def validate(opc_home: Path, *, project_id: str) -> dict[str, Any]:
        calls.append((opc_home, project_id))
        return {"validated": True, "llm_calls_made": False}

    monkeypatch.setattr(
        harness,
        "_parse_args",
        lambda: SimpleNamespace(
            opc_home=tmp_path / "repo" / ".opc",
            execute=False,
            project_id="project-a",
        ),
    )
    monkeypatch.setattr(harness, "_validate_setup_without_execution", validate)

    assert harness.main() == 0
    output = json.loads(capsys.readouterr().out)
    assert calls == [((tmp_path / "repo" / ".opc").resolve(), "project-a")]
    assert output["execute"] is False
    assert output["setup_validation"]["llm_calls_made"] is False
    assert system_config.read_bytes() == original_config
    for case in output["cases"]:
        contract = case["ui_root_task_contract"]
        assert contract["created_before_process_message"] is True
        assert contract["one_fresh_uuid_per_session"] is True
        assert contract["parent_id"] is None
        assert contract["parent_session_id"] is None
        assert contract["linked_work_item_id"] == ""
        assert contract["metadata"]["origin_task_id"] == ""
        assert (
            contract["process_message_origin_task_id"]
            == "<same persisted Task.id>"
        )


def test_tool_permission_uses_journaled_canonical_ui_root_actor(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root_session_id = "root-company-session"
        child_session_id = f"{root_session_id}:child-work-item"
        store = _MemoryCheckpointStore()
        store.tasks = [
            SimpleNamespace(
                id="ui-root-task",
                project_id="project-a",
                session_id=root_session_id,
                parent_session_id=None,
                parent_id=None,
                title="App",
                description="",
                org_id="selftest-native-org",
                assigned_to="",
                linked_work_item_id="",
                metadata={
                    "exec_mode": "org",
                    "mode": "org",
                    "execution_mode": "company_mode",
                    "company_profile": "custom",
                    "org_id": "selftest-native-org",
                },
            ),
            SimpleNamespace(
                id="config-source-task",
                project_id="project-a",
                session_id=root_session_id,
                parent_session_id=root_session_id,
                parent_id="",
                assigned_to="lead",
                metadata={
                    "mode": "company",
                    "company_profile": "custom",
                    "org_id": "selftest-native-org",
                },
            ),
            SimpleNamespace(
                id="waiting-child-task",
                project_id="project-a",
                session_id=child_session_id,
                parent_session_id=root_session_id,
                parent_id="",
                assigned_to="worker",
                assigned_external_agent=None,
                metadata={
                    "mode": "company",
                    "company_profile": "custom",
                    "org_id": "selftest-native-org",
                    "work_item_projection_id": "execute-worker",
                },
            ),
        ]
        checkpoint = _checkpoint(
            checkpoint_id="tool-permission-1",
            checkpoint_type="tool_permission",
            session_id=child_session_id,
            payload={
                "tool_call": {
                    "id": "call-1",
                    "name": "file_read",
                    "arguments": {"path": "app_case/app.js"},
                    "fingerprint": "fingerprint-1",
                    "runtime_session_id": "runtime-1",
                },
                "interaction": {
                    "kind": "tool_permission",
                    "options": [
                        {"id": "approve_once", "label": "Approve once"}
                    ],
                    "execution_scope": {
                        "company_profile": "custom",
                        "org_id": "selftest-native-org",
                    },
                    "ownership": {
                        "ui_anchor_task_id": "ui-root-task",
                        "ui_anchor_session_id": root_session_id,
                        "waiting_task_id": "waiting-child-task",
                        "waiting_session_id": child_session_id,
                        "root_session_id": root_session_id,
                        "company_runtime_session_id": root_session_id,
                    },
                },
            },
        )
        checkpoint.task_id = "waiting-child-task"
        submissions: list[dict[str, Any]] = []

        class FakeEngine:
            def __init__(self) -> None:
                self.store = store

            async def submit_checkpoint_decision(
                self,
                **kwargs: Any,
            ) -> dict[str, Any]:
                submissions.append(kwargs)
                return {
                    "accepted": True,
                    "status": "answered",
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "checkpoint_type": checkpoint.checkpoint_type,
                }

        spec = harness.CaseSpec(
            case_id="app",
            title="App",
            org_id="selftest-native-org",
            organization_name="Self-test Native Org",
            roles=({"id": "lead"}, {"id": "worker"}),
            prompt="Build",
            required_artifacts=("app_case/app.js",),
        )
        evidence = await harness._approve_tool_checkpoint(
            FakeEngine(),
            checkpoint,
            spec=spec,
            workplace=tmp_path,
            session_id=root_session_id,
            ui_anchor_task_id="ui-root-task",
            client_request_id="tool-request-1",
        )

        assert len(submissions) == 1
        assert submissions[0]["requester_task_id"] == "ui-root-task"
        assert submissions[0]["requester_session_id"] == root_session_id
        assert evidence["ui_anchor_task_id"] == "ui-root-task"
        assert evidence["root_session_id"] == root_session_id

    asyncio.run(scenario())


def test_tool_permission_rejects_child_fallback_as_recorded_ui_anchor(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        root_session_id = "root-company-session"
        child_session_id = f"{root_session_id}:child-work-item"
        store = _MemoryCheckpointStore()
        store.tasks = [
            SimpleNamespace(
                id="ui-root-task",
                project_id="project-a",
                session_id=root_session_id,
                parent_session_id=None,
                parent_id=None,
                title="App",
                description="",
                org_id="selftest-native-org",
                assigned_to="",
                linked_work_item_id="",
                metadata={
                    "exec_mode": "org",
                    "mode": "org",
                    "execution_mode": "company_mode",
                    "company_profile": "custom",
                    "org_id": "selftest-native-org",
                },
            ),
            SimpleNamespace(
                id="config-source-task",
                project_id="project-a",
                session_id=root_session_id,
                parent_session_id=root_session_id,
                parent_id="",
                assigned_to="lead",
                metadata={"mode": "company", "company_profile": "custom"},
            ),
            SimpleNamespace(
                id="waiting-child-task",
                project_id="project-a",
                session_id=child_session_id,
                parent_session_id=root_session_id,
                parent_id="",
                assigned_to="worker",
                metadata={
                    "mode": "company",
                    "company_profile": "custom",
                    "work_item_projection_id": "execute-worker",
                },
            ),
        ]
        checkpoint = _checkpoint(
            checkpoint_id="tool-permission-bad-anchor",
            checkpoint_type="tool_permission",
            session_id=child_session_id,
            payload={
                "tool_call": {
                    "id": "call-1",
                    "name": "file_read",
                    "arguments": {"path": "app_case/app.js"},
                    "fingerprint": "fingerprint-1",
                    "runtime_session_id": "runtime-1",
                },
                "interaction": {
                    "kind": "tool_permission",
                    "options": [{"id": "approve_once", "label": "Approve"}],
                    "execution_scope": {
                        "company_profile": "custom",
                        "org_id": "selftest-native-org",
                    },
                    "ownership": {
                        "ui_anchor_task_id": "waiting-child-task",
                        "ui_anchor_session_id": root_session_id,
                        "waiting_task_id": "waiting-child-task",
                        "waiting_session_id": child_session_id,
                        "root_session_id": root_session_id,
                        "company_runtime_session_id": root_session_id,
                    },
                },
            },
        )
        checkpoint.task_id = "waiting-child-task"
        spec = harness.CaseSpec(
            case_id="app",
            title="App",
            org_id="selftest-native-org",
            organization_name="Self-test Native Org",
            roles=({"id": "lead"}, {"id": "worker"}),
            prompt="Build",
            required_artifacts=("app_case/app.js",),
        )

        try:
            await harness._approve_tool_checkpoint(
                SimpleNamespace(store=store),
                checkpoint,
                spec=spec,
                workplace=tmp_path,
                session_id=root_session_id,
                ui_anchor_task_id="ui-root-task",
                client_request_id="tool-request-1",
            )
        except AssertionError as exc:
            assert "recorded UI anchor drifted" in str(exc)
        else:
            raise AssertionError("child fallback was accepted as a UI anchor")

    asyncio.run(scenario())


def test_shell_permission_rejects_case_output_directory_mkdir_as_unmodeled(
    tmp_path: Path,
) -> None:
    cases = (
        (harness.CASES[0], f"mkdir -p {tmp_path / 'investment_case'}"),
        (harness.CASES[1], "mkdir -p app_case"),
    )
    for spec, command in cases:
        try:
            harness._validate_test_tool_call(
                spec,
                tmp_path,
                {
                    "name": "shell_exec",
                    "arguments": {
                        "command": command,
                        "working_directory": str(tmp_path),
                    },
                },
            )
        except AssertionError as exc:
            assert "non-whitelisted validation command" in str(exc)
        else:
            raise AssertionError(
                f"{spec.case_id} case modeled a separate output-directory mkdir"
            )


def test_permission_preflight_classifies_output_directory_mkdir_as_denied(
    tmp_path: Path,
) -> None:
    evidence = harness._validate_permission_policy_without_execution(
        tmp_path,
        safe_command_prefixes=[],
    )

    assert "investment_output_directory_mkdir" in evidence["rejected_cases"]
    assert "app_output_directory_mkdir" in evidence["rejected_cases"]
    assert not {
        "investment_output_directory",
        "app_output_directory",
        "investment_output_directory_mkdir",
        "app_output_directory_mkdir",
    } & set(evidence["allowed_cases"])
    assert set(evidence["approval_required_commands"]) == {
        "python3 -m json.tool investment_case/company_analysis.json",
        "python3 -m json.tool investment_case/risk_analysis.json",
        "node --check app_case/app.js",
    }


def test_uncheckpointed_successful_mkdir_cannot_pass_native_shell_closure(
    tmp_path: Path,
) -> None:
    spec = harness.CASES[0]
    arguments = {
        "command": "mkdir -p investment_case",
        "working_directory": str(tmp_path),
    }
    details = [
        {
            "task_id": "investment-lead-task",
            "role_id": "investment_lead",
            "runtime_session_id": "rt_investment_lead",
            "calls": [
                {
                    "tool_call_id": "call-mkdir",
                    "tool_name": "shell_exec",
                    "arguments": arguments,
                }
            ],
            "results": [
                {
                    "result_record_id": "result-mkdir",
                    "tool_call_id": "call-mkdir",
                    "tool_name": "shell_exec",
                    "payload": {"success": True, "result": {"exit_code": 0}},
                }
            ],
        }
    ]

    try:
        harness._native_shell_ledger_closure(
            spec,
            tmp_path,
            runtime_details=details,
            tool_checkpoint_evidence=[],
        )
    except AssertionError as exc:
        assert "unexpected native shell ToolCall" in str(exc)
        assert "succeeded outside the E2E model" in str(exc)
        assert "call-mkdir" in str(exc)
    else:
        raise AssertionError("an uncheckpointed successful mkdir passed evidence")


def test_canonical_denied_mkdir_passes_unexpected_shell_closure(
    tmp_path: Path,
) -> None:
    spec = harness.CASES[1]
    runtime_session_id = "rt_app_manager"
    arguments = {
        "command": "mkdir -p app_case",
        "working_directory": str(tmp_path),
    }
    details = [
        {
            "task_id": "app-manager-task",
            "role_id": "engineering_manager",
            "runtime_session_id": runtime_session_id,
            "calls": [
                {
                    "tool_call_id": "call-mkdir",
                    "tool_name": "shell_exec",
                    "arguments": arguments,
                }
            ],
            "results": [
                {
                    "result_record_id": "result-mkdir-denied",
                    "tool_call_id": "call-mkdir",
                    "tool_name": "shell_exec",
                    "payload": {
                        "success": False,
                        "error": "Owner denied this exact ToolCall.",
                    },
                }
            ],
        }
    ]
    checkpoints = [
        {
            "checkpoint_id": "checkpoint-mkdir-denied",
            "tool_runtime_session_id": runtime_session_id,
            "tool_call_id": "call-mkdir",
            "tool_name": "shell_exec",
            "tool_arguments": arguments,
            "decision": "deny",
            "rejected": True,
            "exact_modeled_call": False,
            "tool_result": {
                "result_record_id": "result-mkdir-denied",
                "success": False,
                "permission_resolution": "deny",
                "checkpoint_tool_result_persisted": True,
                "checkpoint_execution_state": "result_persisted",
                "checkpoint_completion_status": "resolved",
            },
        }
    ]

    evidence = harness._native_shell_ledger_closure(
        spec,
        tmp_path,
        runtime_details=details,
        tool_checkpoint_evidence=checkpoints,
    )

    assert evidence == [
        {
            "runtime_session_id": runtime_session_id,
            "task_id": "app-manager-task",
            "role_id": "engineering_manager",
            "tool_call_id": "call-mkdir",
            "command": "mkdir -p app_case",
            "modeled": False,
            "outcome": "canonical_deny",
            "checkpoint_id": "checkpoint-mkdir-denied",
            "result_record_id": "result-mkdir-denied",
        }
    ]


def test_investment_case_rejects_another_cases_output_directory_mkdir(
    tmp_path: Path,
) -> None:
    investment = harness.CASES[0]
    try:
        harness._validate_test_tool_call(
            investment,
            tmp_path,
            {
                "name": "shell_exec",
                "arguments": {
                    "command": "mkdir -p app_case",
                    "working_directory": str(tmp_path),
                },
            },
        )
    except AssertionError as exc:
        assert "non-whitelisted validation command" in str(exc)
    else:
        raise AssertionError("investment case accepted another case's output directory")


def test_unexpected_compound_shell_is_denied_then_exact_call_reaches_final_path(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        session_id = "issue35-deny-recover-session"
        spec = harness.CaseSpec(
            case_id="app",
            title="Build the app",
            org_id="selftest-native-org",
            organization_name="Self-test Native Org",
            roles=({"id": "lead"}, {"id": "worker"}),
            prompt="Build",
            required_artifacts=("app_case/app.js",),
        )
        run = harness.CaseRun(
            spec=spec,
            session_id=session_id,
            ui_anchor_task_id="c1947d55-55c7-4ff6-89a2-d033de68500c",
            started_at="now",
        )
        store = _MemoryCheckpointStore()
        await harness._persist_office_ui_root_task(
            store,
            run,
            project_id="project-a",
        )
        child_session_id = f"{session_id}:worker"
        store.tasks.append(
            SimpleNamespace(
                id="worker-task",
                project_id="project-a",
                session_id=child_session_id,
                parent_session_id=session_id,
                parent_id="",
                assigned_to="worker",
                assigned_external_agent=None,
                metadata={
                    "mode": "company",
                    "company_profile": "custom",
                    "org_id": spec.org_id,
                    "work_item_projection_id": "execute-worker",
                },
            )
        )
        submissions: list[dict[str, Any]] = []

        def tool_checkpoint(
            checkpoint_id: str,
            *,
            command: str,
            call_id: str,
        ) -> Any:
            row = _checkpoint(
                checkpoint_id=checkpoint_id,
                checkpoint_type=harness.TOOL_CHECKPOINT_TYPES[0],
                session_id=child_session_id,
                payload={
                    "tool_call": {
                        "id": call_id,
                        "name": "shell_exec",
                        "arguments": {
                            "command": command,
                            "working_directory": str(tmp_path),
                        },
                        "fingerprint": f"fingerprint-{call_id}",
                        "runtime_session_id": "runtime-worker",
                    },
                    "interaction": {
                        "kind": harness.TOOL_CHECKPOINT_TYPES[0],
                        "options": [
                            {"id": "approve_once", "label": "Approve once"},
                            {"id": "deny", "label": "Deny"},
                        ],
                        "execution_scope": {
                            "company_profile": "custom",
                            "org_id": spec.org_id,
                        },
                        "ownership": {
                            "ui_anchor_task_id": run.ui_anchor_task_id,
                            "ui_anchor_session_id": session_id,
                            "waiting_task_id": "worker-task",
                            "waiting_session_id": child_session_id,
                            "root_session_id": session_id,
                            "company_runtime_session_id": session_id,
                        },
                    },
                },
            )
            row.task_id = "worker-task"
            return row

        class FakeEngine:
            def __init__(self) -> None:
                self.store = store

            async def process_message(self, content: str, **kwargs: Any) -> str:
                store.rows.append(
                    _checkpoint(
                        checkpoint_id="staffing-recovery",
                        checkpoint_type=harness.STAFFING_CHECKPOINT_TYPE,
                        session_id=session_id,
                        payload={
                            "company_profile": "custom",
                            "org_id": spec.org_id,
                            "primary_session_id": session_id,
                            "staffing_roles": [
                                {
                                    "role_id": role_id,
                                    "default_agent": "native",
                                    "selected_agent": "native",
                                }
                                for role_id in ("lead", "worker")
                            ],
                            "interaction": {
                                "kind": harness.STAFFING_CHECKPOINT_TYPE,
                                "execution_scope": {
                                    "company_profile": "custom",
                                    "org_id": spec.org_id,
                                },
                            },
                        },
                    )
                )
                return "pending manual staffing selection"

            async def submit_checkpoint_decision(
                self,
                **kwargs: Any,
            ) -> dict[str, Any]:
                submissions.append(kwargs)
                row = next(
                    item
                    for item in store.rows
                    if item.checkpoint_id == kwargs["checkpoint_id"]
                )
                decision = dict(kwargs["decision"])
                row.status = "resolved"
                row.payload.setdefault("interaction", {})["decision"] = {
                    "value": decision
                }
                if row.checkpoint_type == harness.TOOL_CHECKPOINT_TYPES[0]:
                    row.payload["interaction"]["execution"] = {
                        "state": "result_persisted"
                    }
                    row.payload["interaction"]["completion"] = {
                        "final_status": "resolved"
                    }
                    row.payload["approval_result"] = {
                        "approved": decision["option_id"] == "approve_once",
                        "tool_result_persisted": True,
                    }
                if row.checkpoint_type == harness.STAFFING_CHECKPOINT_TYPE:
                    store.rows.append(
                        tool_checkpoint(
                            "tool-compound",
                            command=(
                                "node --check app_case/app.js && "
                                "wc -l app_case/app.js"
                            ),
                            call_id="call-compound",
                        )
                    )
                elif row.checkpoint_id == "tool-compound":
                    assert decision["option_id"] == "deny"
                    assert "use only one listed standalone command" in decision["text"]
                    target = tmp_path / "app_case" / "app.js"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("const ready = true;\n", encoding="utf-8")
                    store.rows.append(
                        tool_checkpoint(
                            "tool-exact",
                            command="node --check app_case/app.js",
                            call_id="call-exact",
                        )
                    )
                elif row.checkpoint_id == "tool-exact":
                    assert decision == {
                        "option_id": "approve_once",
                        "text": "approve_once",
                    }
                    store.rows.append(
                        _checkpoint(
                            checkpoint_id="feedback-after-recovery",
                            checkpoint_type=harness.FINAL_CHECKPOINT_TYPE,
                            session_id=session_id,
                            payload={
                                "interaction": {
                                    "kind": harness.FINAL_CHECKPOINT_TYPE,
                                    "execution_scope": {
                                        "company_profile": "custom",
                                        "org_id": spec.org_id,
                                    },
                                }
                            },
                        )
                    )
                return {
                    "accepted": True,
                    "status": "answered",
                    "checkpoint_id": row.checkpoint_id,
                    "checkpoint_type": row.checkpoint_type,
                }

        state_changes = 0

        def state_changed() -> None:
            nonlocal state_changes
            state_changes += 1

        await harness._drive_case(
            FakeEngine(),
            run,
            project_id="project-a",
            workplace=tmp_path,
            poll_seconds=0.001,
            timeout_seconds=1.0,
            state_changed=state_changed,
        )

        assert run.feedback_checkpoint_id == "feedback-after-recovery"
        assert [item["decision"] for item in run.tool_decisions] == [
            "deny",
            "approve_once",
        ]
        assert run.tool_decisions[0]["rejected"] is True
        assert run.tool_decisions[0]["exact_modeled_call"] is False
        assert run.tool_decisions[1]["rejected"] is False
        assert run.tool_decisions[1]["exact_modeled_call"] is True
        assert {
            item["tool_runtime_session_id"] for item in run.tool_decisions
        } == {"runtime-worker"}
        assert [
            call["decision"]["option_id"]
            for call in submissions
            if call["checkpoint_type"] == harness.TOOL_CHECKPOINT_TYPES[0]
        ] == ["deny", "approve_once"]
        assert state_changes >= 4

    asyncio.run(scenario())


def test_drive_case_polls_later_batch_card_without_blocking_first_denial(
    monkeypatch: Any,
    capsys: Any,
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        spec = harness.CaseSpec(
            case_id="app",
            title="App",
            org_id="batch-org",
            organization_name="Batch Org",
            roles=({"id": "lead"}, {"id": "worker"}),
            prompt="Build",
            required_artifacts=("app_case/app.js",),
        )
        run = harness.CaseRun(
            spec=spec,
            session_id="issue35-batch-session",
            ui_anchor_task_id="ui-batch-root",
            started_at="now",
        )
        staffing = SimpleNamespace(
            checkpoint_id="staffing-resolved",
            checkpoint_type=harness.STAFFING_CHECKPOINT_TYPE,
            status="resolved",
        )
        visible_tools = [
            SimpleNamespace(
                checkpoint_id="deny-a",
                checkpoint_type="tool_permission",
                status="pending",
            ),
        ]
        final = SimpleNamespace(checkpoint_id="final-after-batch")
        submitted: list[str] = []
        final_visible = False

        class Store:
            async def get_task(self, task_id: str) -> Any:
                assert task_id == run.ui_anchor_task_id
                return SimpleNamespace(id=task_id)

        class Engine:
            def __init__(self) -> None:
                self.store = Store()

            async def process_message(self, content: str, **kwargs: Any) -> str:
                del content, kwargs
                return "runtime continued after the denied batch"

        async def find_checkpoints(
            store: Any,
            *,
            project_id: str,
            session_id: str,
            checkpoint_types: tuple[str, ...],
            statuses: tuple[str, ...] | None,
        ) -> list[Any]:
            del store, project_id, session_id
            if checkpoint_types == (harness.STAFFING_CHECKPOINT_TYPE,):
                return [] if statuses == ("pending",) else [staffing]
            if checkpoint_types == harness.TOOL_CHECKPOINT_TYPES:
                if statuses == ("pending",):
                    return [row for row in visible_tools if row.status == "pending"]
                return list(visible_tools)
            return []

        async def approve_tool(
            engine: Any,
            checkpoint: Any,
            **kwargs: Any,
        ) -> dict[str, Any]:
            nonlocal final_visible
            del engine
            decision = {
                "checkpoint_id": checkpoint.checkpoint_id,
                "checkpoint_type": checkpoint.checkpoint_type,
                "decision": "deny",
                "tool_name": "shell_exec",
                "tool_call_id": f"call-{checkpoint.checkpoint_id}",
                "tool_call_fingerprint": f"fp-{checkpoint.checkpoint_id}",
                "tool_runtime_session_id": "rt-batch",
                "tool_arguments": {
                    "command": f"wc -l {checkpoint.checkpoint_id}",
                },
                "tool_command": f"wc -l {checkpoint.checkpoint_id}",
                "tool_call_signature": checkpoint.checkpoint_id,
                "root_session_id": run.session_id,
                "ui_anchor_task_id": run.ui_anchor_task_id,
                "waiting_task_id": "worker-task",
                "exact_modeled_call": False,
                "rejected": True,
                "rejection_reason": "unmodeled shell",
                "decision_text": "deny",
                "client_request_id": kwargs["client_request_id"].replace(
                    ":approve_once",
                    ":deny",
                ),
                "receipt": {
                    "accepted": True,
                    "status": "answered",
                },
                "submission_state": "acknowledged",
            }
            decision_started = kwargs.get("decision_started")
            assert callable(decision_started)
            decision_started(
                {
                    **decision,
                    "receipt": {},
                    "submission_state": "planned",
                }
            )
            submitted.append(checkpoint.checkpoint_id)
            checkpoint.status = "answered"
            if checkpoint.checkpoint_id == "deny-a":
                # Runtime publishes the sibling permission only after the
                # first durable answer, so it was absent from the snapshot
                # that contained deny-a.
                visible_tools.append(
                    SimpleNamespace(
                        checkpoint_id="deny-b",
                        checkpoint_type="tool_permission",
                        status="pending",
                    )
                )
            else:
                assert submitted == ["deny-a", "deny-b"]
                for row in visible_tools:
                    row.status = "resolved"
                final_visible = True
            return decision

        async def pending_final(store: Any, **kwargs: Any) -> Any | None:
            del store, kwargs
            return final if final_visible else None

        async def final_frontier(store: Any, **kwargs: Any) -> tuple[Any, list[Any]]:
            del store, kwargs
            return final, []

        async def reconcile(*args: Any, **kwargs: Any) -> None:
            del args, kwargs

        monkeypatch.setattr(harness, "_validate_office_ui_root_task", lambda *a, **k: None)
        monkeypatch.setattr(harness, "_find_case_checkpoints", find_checkpoints)
        monkeypatch.setattr(harness, "_approve_tool_checkpoint", approve_tool)
        monkeypatch.setattr(harness, "_single_pending_final_feedback", pending_final)
        monkeypatch.setattr(harness, "_final_owner_interaction_frontier", final_frontier)
        monkeypatch.setattr(harness, "_reconcile_planned_tool_decisions", reconcile)

        state_changes = 0

        def state_changed() -> None:
            nonlocal state_changes
            state_changes += 1

        await harness._drive_case(
            Engine(),
            run,
            project_id="project-a",
            workplace=tmp_path,
            poll_seconds=0.001,
            timeout_seconds=1.0,
            state_changed=state_changed,
        )

        assert submitted == ["deny-a", "deny-b"]
        assert [item["checkpoint_id"] for item in run.tool_decisions] == [
            "deny-a",
            "deny-b",
        ]
        assert len({item["checkpoint_id"] for item in run.tool_decisions}) == 2
        assert state_changes >= 5

    asyncio.run(scenario())
    output = capsys.readouterr().out
    assert output.count("safely denied unexpected") == 2
    assert output.count("checkpoint deny-a:") == 1
    assert output.count("checkpoint deny-b:") == 1


def test_duplicate_active_final_feedback_cards_fail_closed() -> None:
    async def scenario() -> None:
        store = _MemoryCheckpointStore()
        for checkpoint_id, status in (
            ("feedback-pending", "pending"),
            ("feedback-consuming", "consuming"),
        ):
            row = _checkpoint(
                checkpoint_id=checkpoint_id,
                checkpoint_type=harness.FINAL_CHECKPOINT_TYPE,
                session_id="one-owner-session",
                payload={"interaction": {"ownership": {}}},
            )
            row.status = status
            store.rows.append(row)

        try:
            await harness._single_pending_final_feedback(
                store,
                project_id="project-a",
                session_id="one-owner-session",
                case_id="app",
                allow_none=False,
            )
        except AssertionError as exc:
            assert "at most one active pending" in str(exc)
        else:
            raise AssertionError("duplicate active final cards were accepted")

    asyncio.run(scenario())


def test_final_owner_frontier_rejects_any_other_active_owner_wait() -> None:
    async def scenario() -> None:
        store = _MemoryCheckpointStore()
        store.rows.extend(
            [
                _checkpoint(
                    checkpoint_id="feedback-only-allowed",
                    checkpoint_type=harness.FINAL_CHECKPOINT_TYPE,
                    session_id="frontier-session",
                    payload={"interaction": {"ownership": {}}},
                ),
                _checkpoint(
                    checkpoint_id="input-still-active",
                    checkpoint_type="task_user_input",
                    session_id="frontier-session",
                    payload={"interaction": {"ownership": {}}},
                ),
            ]
        )
        try:
            await harness._final_owner_interaction_frontier(
                store,
                project_id="project-a",
                session_id="frontier-session",
                case_id="app",
            )
        except AssertionError as exc:
            assert "exactly one active checkpoint" in str(exc)
        else:
            raise AssertionError("another active owner wait was accepted")

    asyncio.run(scenario())


def test_exact_shell_commands_are_bound_to_the_required_child_roles() -> None:
    investment, app = harness.CASES
    assert harness._required_exact_shell_roles(investment) == {
        "python3 -m json.tool investment_case/company_analysis.json": (
            "investment_analyst"
        ),
        "python3 -m json.tool investment_case/risk_analysis.json": "risk_analyst",
    }
    assert harness._required_exact_shell_roles(app) == {
        "node --check app_case/app.js": "qa_engineer"
    }


def test_required_shell_pair_allows_manager_revalidation_after_child_success() -> None:
    investment = harness.CASES[0]
    company_command = (
        "python3 -m json.tool investment_case/company_analysis.json"
    )
    risk_command = "python3 -m json.tool investment_case/risk_analysis.json"
    approved_pairs = {
        (company_command, "investment_analyst"),
        (risk_command, "risk_analyst"),
        (company_command, "investment_lead"),
    }

    assert (
        harness._missing_required_exact_shell_role_pairs(
            investment,
            approved_pairs,
        )
        == ()
    )


def test_manager_revalidation_cannot_replace_required_child_pair() -> None:
    investment = harness.CASES[0]
    company_command = (
        "python3 -m json.tool investment_case/company_analysis.json"
    )
    risk_command = "python3 -m json.tool investment_case/risk_analysis.json"

    assert harness._missing_required_exact_shell_role_pairs(
        investment,
        {
            (company_command, "investment_lead"),
            (risk_command, "risk_analyst"),
        },
    ) == ((company_command, "investment_analyst"),)


def test_wrong_role_cannot_satisfy_required_exact_shell_pair() -> None:
    app = harness.CASES[1]
    command = "node --check app_case/app.js"

    assert harness._missing_required_exact_shell_role_pairs(
        app,
        {(command, "engineering_manager")},
    ) == ((command, "qa_engineer"),)


def test_planned_tool_decision_recovers_only_exact_durable_submission() -> None:
    async def scenario() -> None:
        store = _MemoryCheckpointStore()
        decision_value = {
            "option_id": "deny",
            "text": "exact bounded denial feedback",
        }
        encoded = json.dumps(
            decision_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        row = _checkpoint(
            checkpoint_id="tool-crash-window",
            checkpoint_type=harness.TOOL_CHECKPOINT_TYPES[0],
            session_id="journal-session:worker",
            payload={
                "tool_call": {
                    "id": "call-crash-window",
                    "name": "shell_exec",
                    "arguments": {"command": "wc -l app_case/app.js"},
                    "fingerprint": "fingerprint-crash-window",
                    "runtime_session_id": "runtime-crash-window",
                },
                "interaction": {
                    "ownership": {"root_session_id": "journal-session"},
                    "decision": {
                        "request_id": (
                            "issue35-e2e:journal-session:"
                            "tool-crash-window:deny"
                        ),
                        "decision_hash": harness.hashlib.sha256(
                            encoded.encode("utf-8")
                        ).hexdigest(),
                        "value": decision_value,
                    },
                },
            },
        )
        row.status = "resolved"
        store.rows.append(row)
        spec = harness.CaseSpec(
            case_id="app",
            title="App",
            org_id="selftest-native-org",
            organization_name="Self-test Native Org",
            roles=({"id": "lead"}, {"id": "worker"}),
            prompt="Build",
            required_artifacts=("app_case/app.js",),
        )
        run = harness.CaseRun(
            spec=spec,
            session_id="journal-session",
            ui_anchor_task_id="ui-root",
            started_at="now",
            tool_decisions=[
                {
                    "checkpoint_id": "tool-crash-window",
                    "checkpoint_type": harness.TOOL_CHECKPOINT_TYPES[0],
                    "tool_call_id": "call-crash-window",
                    "tool_call_fingerprint": "fingerprint-crash-window",
                    "tool_runtime_session_id": "runtime-crash-window",
                    "decision": "deny",
                    "decision_text": decision_value["text"],
                    "client_request_id": (
                        "issue35-e2e:journal-session:"
                        "tool-crash-window:deny"
                    ),
                    "receipt": {},
                    "submission_state": "planned",
                }
            ],
        )
        state_changes = 0

        def changed() -> None:
            nonlocal state_changes
            state_changes += 1

        await harness._reconcile_planned_tool_decisions(
            SimpleNamespace(store=store),
            run,
            project_id="project-a",
            state_changed=changed,
        )

        recovered = run.tool_decisions[0]
        assert harness._receipt_acknowledged(recovered["receipt"])
        assert recovered["submission_state"] == "recovered_after_submit"
        assert state_changes == 1

    asyncio.run(scenario())
