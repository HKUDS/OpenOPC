"""Real company/org-mode verification harness for issue #35.

The harness intentionally uses a real LLM and the real native-agent runtime.
It creates two small, saved custom organizations in an isolated test
``OPC_HOME`` and drives two independent sessions to the final
``company_delivery_feedback`` checkpoint.  Tool permission cards are answered
only through ``OPCEngine.submit_checkpoint_decision``; no CLI callback,
WebSocket shortcut, or direct Store mutation is used.

Nothing runs unless ``--execute`` is supplied.  A normal invocation is:

    OPC_HOME=/tmp/openopc-issue35-native-e2e/repo/.opc \
      python scripts/issue35_company_e2e.py --execute

The working project root is inferred as ``OPC_HOME.parent`` so OpenOPC's normal
workplace resolver writes into the sibling ``repo_workplace`` directory.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlsplit, urlunsplit

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OPC_HOME = (
    Path(tempfile.gettempdir()) / "openopc-issue35-native-e2e" / "repo" / ".opc"
)
DEFAULT_PROJECT_ID = "issue35-native-e2e"
TOOL_CHECKPOINT_TYPES = ("tool_permission",)
STAFFING_CHECKPOINT_TYPE = "company_staffing_selection"
FINAL_CHECKPOINT_TYPE = "company_delivery_feedback"
ACTIVE_OWNER_CHECKPOINT_STATUSES = frozenset(
    {"pending", "answered", "consuming", "resuming"}
)
MAX_UNEXPECTED_TOOL_DENIALS_PER_CASE = 6
MAX_REPEATED_UNEXPECTED_TOOL_CALLS = 2
E2E_ALL_SHELL_REVIEW_PATTERN = r"(?s)\A.*\Z"
APP_DEVELOPER_SCOPE_KEY = "issue35-app-implementation"
APP_QA_SCOPE_KEY = "issue35-app-qa"
INVESTMENT_RUN_DATE_PLACEHOLDER = "__ISSUE35_RUN_DATE__"
INVESTMENT_HORIZON_PLACEHOLDER = "__ISSUE35_HORIZON_YEARS__"
INVESTMENT_MAX_FACT_AGE_DAYS = 540
INVESTMENT_TICKERS = ("NVDA", "AMD", "AVGO")
INVESTMENT_REQUIRED_ARTIFACTS = (
    "investment_case/company_analysis.json",
    "investment_case/risk_analysis.json",
    "investment_case/report.md",
)
INVESTMENT_TICKER_ALIASES = {
    "NVDA": ("nvda", "nvidia"),
    "AMD": ("amd",),
    "AVGO": ("avgo", "broadcom"),
}
INVESTMENT_OFFICIAL_DOMAINS = {
    "NVDA": ("nvidia.com",),
    "AMD": ("amd.com",),
    "AVGO": ("broadcom.com",),
}
INVESTMENT_PERIOD_TOKEN_PATTERN = re.compile(
    r"(?:q[1-4]\s+(?:fy\s*20\d{2}|fiscal(?:\s+year)?\s+20\d{2}|20\d{2})"
    r"|(?:first|second|third|fourth)\s+quarter(?:\s+of)?\s+"
    r"(?:fy\s*20\d{2}|fiscal(?:\s+year)?\s+20\d{2}|20\d{2})"
    r"|(?:fy\s*20\d{2}|fiscal(?:\s+year)?\s+20\d{2}"
    r"|full\s+year\s+20\d{2}))",
    re.IGNORECASE,
)
INVESTMENT_PERIOD_EVIDENCE_PATTERN = re.compile(
    rf"(?<![a-z0-9])(?:{INVESTMENT_PERIOD_TOKEN_PATTERN.pattern})(?![a-z0-9])",
    re.IGNORECASE,
)
INVESTMENT_NUMBER_TOKEN = (
    r"(?:0(?:\.\d+)?|[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?|"
    r"[1-9]\d*(?:\.\d+)?)"
)
INVESTMENT_VALUE_TOKEN_PATTERN = re.compile(
    rf"(?:[$£€¥]\s*{INVESTMENT_NUMBER_TOKEN}"
    r"(?:\s+(?:million|billion|trillion|mn|bn))?"
    rf"|{INVESTMENT_NUMBER_TOKEN}%)",
    re.IGNORECASE,
)
INVESTMENT_FORWARD_LOOKING_PATTERN = re.compile(
    r"\b(?:guidance|outlook|forecast(?:ed|s|ing)?|estimate(?:d|s)?|"
    r"expect(?:ed|s|ing|ation|ations)?|project(?:ed|s|ing|ion|ions)?|"
    r"target(?:ed|s|ing)?|anticipat(?:e|ed|es|ing|ion|ions)|"
    r"approximately|about|will\s+(?:be|reach|grow|increase|decrease)|"
    r"(?:is|are)\s+expected)\b",
    re.IGNORECASE,
)
INVESTMENT_FORWARD_LOOKING_POSTFIX_PATTERN = re.compile(
    r"^\s*(?:[,:(\-]\s*)?(?:guidance|outlook|forecast|estimate)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    title: str
    org_id: str
    organization_name: str
    roles: tuple[dict[str, Any], ...]
    prompt: str
    required_artifacts: tuple[str, ...]


@dataclass
class CaseRun:
    spec: CaseSpec
    session_id: str
    ui_anchor_task_id: str
    started_at: str
    response: str = ""
    staffing_decisions: list[dict[str, Any]] = field(default_factory=list)
    tool_decisions: list[dict[str, Any]] = field(default_factory=list)
    feedback_checkpoint_id: str = ""
    resume_existing: bool = False
    staffing_recovery_checkpoint_id: str = ""
    staffing_recovery_run_id: str = ""


def _office_ui_root_metadata(spec: CaseSpec) -> dict[str, Any]:
    """Return the custom-company metadata persisted by Office session.create."""

    return {
        "exec_mode": "org",
        "company_profile": "custom",
        "preferred_agent": "native",
        "org_id": spec.org_id,
        "organization_id": spec.org_id,
        "mode": "org",
        "execution_mode": "company_mode",
        "origin_task_id": "",
        "task_mode_contract": "",
        "selected_execution_agent": "",
        "force_native_execution": False,
        "preferred_external_agent": "",
        "agent_selection": {},
    }


def _build_office_ui_root_task(
    run: CaseRun,
    *,
    project_id: str,
) -> Any:
    """Build the same pure root Task shape as Office's create-session service."""

    from opc.core.models import Task

    return Task(
        id=run.ui_anchor_task_id,
        title=run.spec.title,
        description="",
        project_id=project_id,
        session_id=run.session_id,
        metadata=_office_ui_root_metadata(run.spec),
        org_id=run.spec.org_id,
    )


def _validate_office_ui_root_task(
    task: Any,
    run: CaseRun,
    *,
    project_id: str,
) -> None:
    """Fail closed unless *task* remains a selectable pure Office UI root."""

    from opc.layer2_organization.company_runtime_identity import (
        is_pure_company_ui_anchor,
    )

    if str(getattr(task, "id", "") or "").strip() != run.ui_anchor_task_id:
        raise AssertionError(f"{run.spec.case_id}: UI root Task ID drifted")
    if str(getattr(task, "project_id", "") or "").strip() != project_id:
        raise AssertionError(f"{run.spec.case_id}: UI root Task crossed project scope")
    if str(getattr(task, "session_id", "") or "").strip() != run.session_id:
        raise AssertionError(f"{run.spec.case_id}: UI root Task session drifted")
    if str(getattr(task, "title", "") or "").strip() != run.spec.title:
        raise AssertionError(f"{run.spec.case_id}: UI root Task title drifted")
    if str(getattr(task, "description", "") or ""):
        raise AssertionError(
            f"{run.spec.case_id}: UI root Task no longer matches Office shape"
        )
    if str(getattr(task, "org_id", "") or "").strip() != run.spec.org_id:
        raise AssertionError(f"{run.spec.case_id}: UI root Task org drifted")
    if not is_pure_company_ui_anchor(task, run.session_id):
        raise AssertionError(
            f"{run.spec.case_id}: UI root Task was polluted by a company work-item link"
        )

    metadata = dict(getattr(task, "metadata", {}) or {})
    expected = _office_ui_root_metadata(run.spec)
    stable_keys = (
        "exec_mode",
        "company_profile",
        "preferred_agent",
        "org_id",
        "organization_id",
        "mode",
        "execution_mode",
        "task_mode_contract",
        "selected_execution_agent",
        "force_native_execution",
        "preferred_external_agent",
        "agent_selection",
    )
    if any(metadata.get(key) != expected[key] for key in stable_keys):
        raise AssertionError(
            f"{run.spec.case_id}: UI root Task execution configuration drifted"
        )
    origin_task_id = str(metadata.get("origin_task_id", "") or "").strip()
    if origin_task_id not in {"", run.ui_anchor_task_id}:
        raise AssertionError(f"{run.spec.case_id}: UI root Task origin drifted")


async def _persist_office_ui_root_task(
    store: Any,
    run: CaseRun,
    *,
    project_id: str,
) -> Any:
    """Persist exactly one fresh Office-shaped root, never a work-item surrogate."""

    if not run.ui_anchor_task_id:
        raise AssertionError(f"{run.spec.case_id}: missing journaled UI root Task ID")
    try:
        parsed_task_id = uuid.UUID(run.ui_anchor_task_id)
    except (TypeError, ValueError, AttributeError) as exc:
        raise AssertionError(
            f"{run.spec.case_id}: UI root Task ID is not a UUID"
        ) from exc
    if str(parsed_task_id) != run.ui_anchor_task_id:
        raise AssertionError(
            f"{run.spec.case_id}: UI root Task ID is not canonical"
        )

    tasks = await store.get_tasks(project_id=project_id)
    existing = next(
        (
            task
            for task in tasks
            if str(getattr(task, "id", "") or "").strip()
            == run.ui_anchor_task_id
        ),
        None,
    )
    session_tasks = [
        task
        for task in tasks
        if str(getattr(task, "session_id", "") or "").strip() == run.session_id
        or str(getattr(task, "parent_session_id", "") or "").strip()
        == run.session_id
    ]
    if existing is None:
        if run.resume_existing:
            raise AssertionError(
                f"{run.spec.case_id}: resume journal UI root Task is missing"
            )
        if session_tasks:
            raise AssertionError(
                f"{run.spec.case_id}: refusing to graft a UI root onto an existing runtime"
            )
        existing = _build_office_ui_root_task(run, project_id=project_id)
        await store.save_task(existing)
        existing = await store.get_task(run.ui_anchor_task_id)
        if existing is None:
            raise AssertionError(
                f"{run.spec.case_id}: persisted UI root Task cannot be reloaded"
            )

    _validate_office_ui_root_task(existing, run, project_id=project_id)
    pure_roots = []
    from opc.layer2_organization.company_runtime_identity import (
        is_pure_company_ui_anchor,
    )

    refreshed_tasks = await store.get_tasks(project_id=project_id)
    for task in refreshed_tasks:
        if is_pure_company_ui_anchor(task, run.session_id):
            pure_roots.append(str(getattr(task, "id", "") or "").strip())
    if pure_roots != [run.ui_anchor_task_id]:
        raise AssertionError(
            f"{run.spec.case_id}: expected one exact pure UI root, got {pure_roots}"
        )
    return existing


def _runtime_policy(
    *,
    downstream: tuple[str, ...] = (),
    review_role: str | None = None,
    turn_type: str = "work",
) -> dict[str, Any]:
    return {
        "execution_strategy": "native",
        "allowed_downstream_roles": list(downstream),
        "review_role": review_role,
        "default_turn_type": turn_type,
        "shell_timeout_override": None,
        "setup_env_type": None,
        "coordination_hints": {},
        "signal_capabilities": [],
        "parallelism_constraints": [],
        "gate_preferences": {},
    }


def _role(
    role_id: str,
    name: str,
    responsibility: str,
    *,
    reports_to: str,
    can_spawn: tuple[str, ...] = (),
    tools: tuple[str, ...] = (),
    review_role: str | None = None,
    turn_type: str = "work",
    role_type: str = "worker",
) -> dict[str, Any]:
    return {
        "id": role_id,
        "name": name,
        "responsibility": responsibility,
        "reports_to": reports_to,
        "icon": "leader" if role_type == "coordinator" else "work",
        "can_spawn": list(can_spawn),
        "tools": list(tools),
        "preferred_external_agent": None,
        "prompt_refs": [responsibility],
        "skill_refs": [],
        "runtime_policy": _runtime_policy(
            downstream=can_spawn,
            review_role=review_role,
            turn_type=turn_type,
        ),
        "capabilities": [],
        "role_type": role_type,
        "coordinator_policy": None,
    }


COORDINATION_TOOLS = (
    "file_read",
    "file_write",
    "file_edit",
    "file_search",
    "list_dir",
    "todo_write",
    "todo_read",
)
RESEARCH_TOOLS = COORDINATION_TOOLS + ("web_search", "shell_exec")
ENGINEERING_TOOLS = COORDINATION_TOOLS + ("shell_exec",)

INVESTMENT_ARTIFACT_BOUNDARY = (
    "Do not perform workspace-preparation writes or create the output directory "
    "separately. Never create `.gitkeep`, placeholder, scratch, preview, or any "
    "other extra file. Across the entire investment session, the only files any "
    "role may create or edit are investment_case/company_analysis.json, "
    "investment_case/risk_analysis.json, and investment_case/report.md. The first "
    "file_write to one of those required artifacts creates its parent directories "
    "as needed."
)


INVESTMENT_ROLES = (
    _role(
        "investment_lead",
        "Investment Lead",
        (
            "Own the investment mandate, delegate evidence and risk work, review both "
            "outputs, and deliver the final recommendation. Make the delegated briefs "
            "repeat the root mandate's exact analysis date, current-year "
            "official-search requirement, JSON schema, and critical-claim evidence rules. "
            "Ensure the final report's "
            "source table contains at least three literal complete http:// or https:// "
            "URLs, each paired with its retrieval date; use file_read to self-check the "
            "report and file_edit to correct omissions before delivery. Reuse only URLs "
            "validated in the child JSON files, and put the root mandate's exact analysis "
            "date and three horizon years in the report metadata lines. Include a Verified "
            "critical facts table with exactly six rows: the exact three claims from each "
            "child JSON. Paste the root mandate's exact three-object critical_claims example "
            "into both delegate briefs and acceptance criteria. Before approving either "
            "child, use file_read and explicitly read back all seven fields of each of its "
            "three claims; reject and rework any extra claim, extra field, forecast, guidance, "
            "future period, non-official URL, or multi-metric value_token. For every claim, "
            "confirm one single durable search hit contains the ticker/company alias, exact "
            "value_token, and the semantically equivalent strict period together, with the "
            "value in an actual-results clause rather than forward-looking language. Also "
            "enforce the root mandate's complete role-specific `company_profiles`, "
            "`ranked_recommendation`, `scenarios`, and `position_sizing_guardrails` schema "
            "and `risk_register`, `scenarios`, and `portfolio_guardrails` schema before "
            "approval. "
            f"{INVESTMENT_ARTIFACT_BOUNDARY}"
        ),
        reports_to="owner",
        can_spawn=("investment_analyst", "risk_analyst"),
        tools=RESEARCH_TOOLS,
        role_type="coordinator",
    ),
    _role(
        "investment_analyst",
        "Investment Analyst",
        (
            "Research current public evidence, compare the named companies, cite "
            "sources, persist the company-analysis note, and verify that note before "
            "handoff. Follow the root mandate's exact analysis_date/as_of/horizon_years "
            "JSON contract; search each ticker with the current year for its latest "
            "official result. Write exactly three critical_claims, one each for NVDA, AMD, "
            "and AVGO, all issuer-official and for already-ended reported periods. Each "
            "claim's ticker/company alias, value_token, and semantically equivalent strict "
            "period must occur together "
            "in one single search hit, and the selected value must be in an actual-results "
            "clause, not guidance/outlook/forecast/estimate/expectation/projection/target or "
            "approximately/about language. Each "
            "value_token is one currency amount or one percentage only; never put a sentence, "
            "multiple metrics, forecast, guidance, outlook, or future period in a claim. "
            "The JSON must also contain company_profiles for exactly NVDA/AMD/AVGO, each "
            "with a nonempty thesis, catalysts list, and valuation_caveats list; a nonempty "
            "ranked_recommendation; nonempty bear/base/bull scenarios; and a nonempty "
            "position_sizing_guardrails list. Before handoff, file_read the JSON and verify "
            "every field against the root example. "
            f"{INVESTMENT_ARTIFACT_BOUNDARY}"
        ),
        reports_to="investment_lead",
        tools=RESEARCH_TOOLS,
        review_role="investment_lead",
    ),
    _role(
        "risk_analyst",
        "Risk Analyst",
        (
            "Independently challenge the thesis, quantify downside scenarios, persist "
            "the risk-analysis note, and verify that note before handoff. Follow the root "
            "mandate's exact analysis_date/as_of/horizon_years JSON contract; independently "
            "search each ticker with the current year for its latest official result. Write "
            "exactly three critical_claims, one each for NVDA, AMD, and AVGO, all issuer-official "
            "and for already-ended reported periods. Each claim's ticker/company alias, "
            "value_token, and semantically equivalent strict period must occur together in one "
            "single search hit, and "
            "the selected value must be in an actual-results clause, not guidance/outlook/"
            "forecast/estimate/expectation/projection/target or approximately/about language. "
            "Each value_token is one currency amount or "
            "one percentage only; never put a sentence, multiple metrics, forecast, guidance, "
            "outlook, or future period in a claim. The JSON must also contain risk_register "
            "for exactly NVDA/AMD/AVGO, each with nonempty downside_risks and "
            "monitor_triggers lists and a nonempty sizing_guardrail; nonempty bear/base/bull "
            "scenarios; and a nonempty portfolio_guardrails list. Before handoff, file_read "
            "the JSON and verify every field against the root example. "
            f"{INVESTMENT_ARTIFACT_BOUNDARY}"
        ),
        reports_to="investment_lead",
        tools=RESEARCH_TOOLS,
        review_role="investment_lead",
        turn_type="verify",
    ),
)

APP_ROLES = (
    _role(
        "engineering_manager",
        "Engineering Manager",
        "Own scope and acceptance, delegate implementation and independent QA, review their evidence, and deliver the tested app.",
        reports_to="owner",
        can_spawn=("developer", "qa_engineer"),
        tools=ENGINEERING_TOOLS,
        role_type="coordinator",
    ),
    _role(
        "developer",
        "Frontend Developer",
        "Implement the complete dependency-free static app in the required files and run deterministic checks before handoff.",
        reports_to="engineering_manager",
        tools=ENGINEERING_TOOLS,
        review_role="engineering_manager",
    ),
    _role(
        "qa_engineer",
        "QA Engineer",
        "Independently inspect and test the app, report reproducible findings, and provide explicit acceptance evidence.",
        reports_to="engineering_manager",
        tools=ENGINEERING_TOOLS,
        review_role="engineering_manager",
        turn_type="verify",
    ),
)


CASES = (
    CaseSpec(
        case_id="investment",
        title="AI infrastructure investment analysis",
        org_id="issue35-investment-native",
        organization_name="Issue 35 Native Investment Team",
        roles=INVESTMENT_ROLES,
        prompt=(
            "ISSUE35 E2E INVESTMENT — Use company mode to produce a real investment-target analysis of "
            "NVIDIA, AMD, and Broadcom "
            "for a three-year AI-infrastructure allocation. The analysis date for this run is "
            f"{INVESTMENT_RUN_DATE_PLACEHOLDER}; use exactly that date rather than a model-knowledge date. "
            f"The three calendar horizon years are exactly {INVESTMENT_HORIZON_PLACEHOLDER}. "
            "The Investment Lead must delegate current-evidence "
            "research to investment_analyst and an independent downside review to risk_analyst, review both, "
            "and must copy the exact analysis date, current-year official-search requirement, JSON schema, "
            "critical-claim evidence rules, the complete three-object JSON example below, and the relevant "
            "role-specific substantive-section example into both "
            "delegate_work descriptions and acceptance criteria, "
            "then make one ranked recommendation. Use real web sources where available; distinguish sourced "
            "facts from assumptions. Each delegated analyst must run a successful, non-empty web_search for "
            "each of NVDA/NVIDIA, AMD, and AVGO/Broadcom whose query contains both the ticker or company name "
            f"and the four-digit year in {INVESTMENT_RUN_DATE_PLACEHOLDER}; search for the latest reported official "
            "financial result available on the analysis date. Both delegated analysts must invoke web_search before "
            "submitting their work. The investment_analyst must write its substantive structured note as valid JSON "
            "to investment_case/company_analysis.json and then use shell_exec with working_directory set to the "
            "project workplace to run exactly `python3 -m json.tool investment_case/company_analysis.json`. The "
            "risk_analyst must likewise write valid JSON to investment_case/risk_analysis.json and run exactly "
            "`python3 -m json.tool investment_case/risk_analysis.json`. "
            "Each shell_exec call must contain only one of those standalone commands. Never use wc, "
            "python -c, redirects, pipes, &&, echo, head, previews, command substitution, or any extra shell "
            "command. If inspection is needed, use file_read instead of shell_exec. A rejected permission "
            "request must be retried as a new ToolCall containing only the listed exact command. "
            "These checks are mandatory before each child submits work and exercise the child-agent permission path. "
            f"{INVESTMENT_ARTIFACT_BOUNDARY} "
            "Both child JSON files must use this exact top-level contract: `analysis_date` and `as_of` both equal "
            f"`{INVESTMENT_RUN_DATE_PLACEHOLDER}`; `horizon_years` is the three-integer calendar-year array; and "
            "`critical_claims` contains exactly three sourced facts: exactly one NVDA, one AMD, and one AVGO, "
            "with no fourth claim. Additional top-level enrichment is allowed, but these substantive sections "
            "are mandatory: company_analysis.json has `company_profiles` keyed exactly NVDA/AMD/AVGO, each "
            "with nonempty `thesis`, `catalysts` list, and `valuation_caveats` list; nonempty "
            "`ranked_recommendation`; `scenarios` keyed exactly `bear`, `base`, and `bull` with nonempty "
            "values; and a nonempty `position_sizing_guardrails` list. risk_analysis.json has "
            "`risk_register` keyed exactly NVDA/AMD/AVGO, each with nonempty `downside_risks` and "
            "`monitor_triggers` lists and nonempty `sizing_guardrail`; nonempty bear/base/bull `scenarios`; "
            "and a nonempty `portfolio_guardrails` list. The exact company-section example is "
            "`{\"company_profiles\":{\"NVDA\":{\"thesis\":\"<text>\","
            "\"catalysts\":[\"<text>\"],\"valuation_caveats\":[\"<text>\"]},"
            "\"AMD\":{\"thesis\":\"<text>\",\"catalysts\":[\"<text>\"],"
            "\"valuation_caveats\":[\"<text>\"]},\"AVGO\":{\"thesis\":\"<text>\","
            "\"catalysts\":[\"<text>\"],\"valuation_caveats\":[\"<text>\"]}},"
            "\"ranked_recommendation\":\"<text>\",\"scenarios\":{\"bear\":\"<text>\","
            "\"base\":\"<text>\",\"bull\":\"<text>\"},"
            "\"position_sizing_guardrails\":[\"<text>\"]}`. The exact risk-section example is "
            "`{\"risk_register\":{\"NVDA\":{\"downside_risks\":[\"<text>\"],"
            "\"monitor_triggers\":[\"<text>\"],\"sizing_guardrail\":\"<text>\"},"
            "\"AMD\":{\"downside_risks\":[\"<text>\"],\"monitor_triggers\":[\"<text>\"],"
            "\"sizing_guardrail\":\"<text>\"},\"AVGO\":{\"downside_risks\":[\"<text>\"],"
            "\"monitor_triggers\":[\"<text>\"],\"sizing_guardrail\":\"<text>\"}},"
            "\"scenarios\":{\"bear\":\"<text>\",\"base\":\"<text>\","
            "\"bull\":\"<text>\"},\"portfolio_guardrails\":[\"<text>\"]}`. Every critical "
            "claim must contain exactly these seven keys and no others: "
            "fields `ticker` (NVDA, AMD, or AVGO), `kind` (`sourced_fact`), `value_token`, `period_token`, "
            "`period_end` (YYYY-MM-DD), `source_url`, and `retrieval_date`. Every one of the three claims must use "
            "that ticker issuer's official domain (nvidia.com, amd.com, or broadcom.com, including subdomains); "
            "secondary and SEC sources may be used outside critical_claims but cannot satisfy one. Copy "
            "`value_token` verbatim from the selected web_search result title or snippet; the query text is not "
            "evidence. `period_token` must use the strict grammar below and its quarter ordinal, four-digit label "
            "year, and fiscal/non-fiscal meaning must equal one period phrase in that hit. `Q1` and `First "
            "Quarter`, plus optional `of` and `Year`, are semantically equivalent wording only. The "
            "ticker/company alias, exact value_token, and equivalent period for one claim must all occur together "
            "in that same single result hit; combining "
            "separate hits is forbidden. The chosen value occurrence must be in an actual reported-results sentence "
            "or clause, not guidance, outlook, forecast, estimate, expectation, projection, target, approximately, "
            "or about language. `value_token` must be an exact quantified "
            "single token: either one currency amount with an optional million/billion/trillion/mn/bn unit, or "
            "one percentage. It must not contain prose, multiple metrics, plus/minus ranges, or guidance. "
            "`period_token` must be an exact quarter, "
            "fiscal-year, or full-year phrase containing its four-digit year. `period_end` must be in the analysis "
            "calendar year, must not be after the analysis date, and must not be more than "
            f"{INVESTMENT_MAX_FACT_AGE_DAYS} days old. Set `retrieval_date` to the date on which "
            "that successful ToolResult was actually recorded; in this fail-closed run it must equal the analysis "
            "date. Every literal URL anywhere in each JSON must be "
            "returned by that same role's successful non-empty web_search and must sit in an object with that "
            "exact `retrieval_date`. Put assumptions outside `critical_claims`; assumptions never substitute for "
            "the required sourced facts. Forecasts, outlook, guidance, estimates, and any period that has not "
            "ended by the analysis date must stay outside critical_claims. Use exactly this JSON claim-array shape, "
            "replacing every angle-bracket instruction with one real value rather than copying the placeholder: "
            "`\"critical_claims\":["
            "{\"ticker\":\"NVDA\",\"kind\":\"sourced_fact\","
            "\"value_token\":\"<one exact amount or percentage>\","
            "\"period_token\":\"<one exact ended quarter/year phrase>\","
            "\"period_end\":\"<YYYY-MM-DD on or before analysis date>\","
            "\"source_url\":\"<nvidia.com URL returned by this role>\","
            f"\"retrieval_date\":\"{INVESTMENT_RUN_DATE_PLACEHOLDER}\"}},"
            "{\"ticker\":\"AMD\",\"kind\":\"sourced_fact\","
            "\"value_token\":\"<one exact amount or percentage>\","
            "\"period_token\":\"<one exact ended quarter/year phrase>\","
            "\"period_end\":\"<YYYY-MM-DD on or before analysis date>\","
            "\"source_url\":\"<amd.com URL returned by this role>\","
            f"\"retrieval_date\":\"{INVESTMENT_RUN_DATE_PLACEHOLDER}\"}},"
            "{\"ticker\":\"AVGO\",\"kind\":\"sourced_fact\","
            "\"value_token\":\"<one exact amount or percentage>\","
            "\"period_token\":\"<one exact ended quarter/year phrase>\","
            "\"period_end\":\"<YYYY-MM-DD on or before analysis date>\","
            "\"source_url\":\"<broadcom.com URL returned by this role>\","
            f"\"retrieval_date\":\"{INVESTMENT_RUN_DATE_PLACEHOLDER}\"}}]`. "
            "Before approving a child, the Investment Lead must file_read that JSON and explicitly audit all seven "
            "fields of all three entries. Any array length other than three, duplicate/missing ticker, extra key, "
            "non-official source, multi-metric token, guidance, or future period must be returned for rework. "
            "For every entry, explicitly locate one durable result hit that contains its ticker/company alias, "
            "value_token, and semantically equivalent period together and verify that value's local clause reports "
            "an actual result. "
            "Do not fabricate unavailable prices or multiples. Include catalysts, "
            "valuation caveats, bear/base/bull scenarios, and position-sizing guardrails. The final report's "
            "source table must contain at least three literal, complete `http://` or `https://` URLs; source "
            "IDs, citation IDs, or footnotes do not substitute for those URLs. Put a YYYY-MM-DD retrieval date "
            "in the same source-table row as every URL. Write the final report to "
            "investment_case/report.md in the project workplace. Before delivery, the Investment Lead must use "
            "file_read on investment_case/report.md to verify those literal URLs and their retrieval dates. If "
            "anything is missing, use file_edit to correct the report and file_read it again before delivery. The "
            "report may contain only URLs already present in one of the two validated child JSON files, and every "
            "report URL's row must reuse the exact durable ToolResult retrieval date. Near the top, include the "
            f"two exact standalone metadata lines `Analysis date: {INVESTMENT_RUN_DATE_PLACEHOLDER}` and "
            f"`Horizon years: {INVESTMENT_HORIZON_PLACEHOLDER}`. "
            "Include a Markdown section headed exactly `Verified critical facts`. In that section, add exactly "
            "six rows: all three claims from company_analysis.json followed by all three claims from "
            "risk_analysis.json; do not deduplicate identical claims. Each row must contain the child claim's exact ticker, "
            "`value_token`, `period_token`, `source_url`, and `retrieval_date` on the same physical line. "
            "Use the plain uppercase ticker as its own table cell, and put exactly one URL in each data row. "
            "Use exactly this visible six-column Markdown table header: "
            "`| Child | Ticker | Value | Period | URL | Retrieved |`, immediately followed by "
            "`| --- | --- | --- | --- | --- | --- |`. Do not put this section or table inside a code "
            "fence or HTML comment. "
            "This is research, not personalized financial advice. Do not ask the owner for intermediate business "
            "review; the only owner business-review checkpoint should be the final delivery feedback card."
        ),
        required_artifacts=(
            "investment_case/company_analysis.json",
            "investment_case/risk_analysis.json",
            "investment_case/report.md",
        ),
    ),
    CaseSpec(
        case_id="app",
        title="Static investment watchlist app",
        org_id="issue35-app-native",
        organization_name="Issue 35 Native App Team",
        roles=APP_ROLES,
        prompt=(
            "ISSUE35 E2E APP — Use company mode to build a complete dependency-free static investment "
            "watchlist app. The Engineering Manager should preferably call delegate_work once with both child "
            "items. It may "
            "instead use two sequential delegate_work calls, but both items must append to the same stable "
            "batch_id. The developer item must use the stable scope_key `issue35-app-implementation`, and the "
            "qa_engineer item must use scope_key `issue35-app-qa` plus the structured hard dependency "
            "`depends_on: [{\"scope_key\": \"issue35-app-implementation\"}]`. Never start or dispatch QA "
            "before the developer item completes, and do not remove or rewire that dependency. Review both "
            "results, and deliver only after the "
            "checks pass. Create exactly "
            "app_case/index.html, app_case/styles.css, and app_case/app.js in the project workplace. The app "
            "must support adding a ticker with thesis and risk level, deleting rows, filtering by risk, "
            "persisting data in localStorage, keyboard-accessible controls, responsive layout, and three seeded "
            "examples. The developer must use file_write/file_edit to create the app, and the delegated QA "
            "engineer must wait until all three required files durably exist, then use shell_exec with "
            "working_directory set to the project workplace to run exactly "
            "`node --check app_case/app.js`; perform any other source inspection with file_read. "
            "That shell_exec call must contain only the standalone node command. Never use wc, python -c, "
            "redirects, pipes, &&, echo, head, previews, command substitution, or any extra shell command. "
            "A rejected permission request must be retried as a new ToolCall containing only the listed exact "
            "command; use file_read for every other source inspection. "
            "Do not start a long-running server or install dependencies. Do not ask the owner for intermediate "
            "business review; the only owner business-review checkpoint should be the final delivery feedback card."
        ),
        required_artifacts=(
            "app_case/index.html",
            "app_case/styles.css",
            "app_case/app.js",
        ),
    ),
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--opc-home",
        type=Path,
        default=Path(os.environ.get("OPC_HOME", DEFAULT_OPC_HOME)),
        help="Isolated OPC_HOME containing real LLM configuration.",
    )
    parser.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    parser.add_argument(
        "--case-timeout-seconds",
        type=float,
        default=2700.0,
        help="Maximum wall time for each real company session.",
    )
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument(
        "--evidence-path",
        type=Path,
        default=None,
        help="JSON output path (default: OPC_HOME.parent/evidence-<token>.json).",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=None,
        help="Crash-safe run journal (default: OPC_HOME.parent/issue35-company-e2e-state.json).",
    )
    parser.add_argument(
        "--run-token",
        default="",
        help="Stable token used in both session IDs; useful for operator-owned reruns.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume session IDs from the state journal instead of starting a new run.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Required safety switch: make real LLM calls and approve test tool calls.",
    )
    return parser.parse_args()


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "value"):
        return value.value
    return str(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_ARTIFACT_URL_PATTERN = re.compile(r"https?://[^\s|<>()]+", re.IGNORECASE)
_ISO_DATE_PATTERN = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")


def _strict_iso_date(value: Any, *, label: str) -> date:
    raw = str(value or "").strip()
    try:
        parsed = date.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise AssertionError(f"{label} is not an exact YYYY-MM-DD date: {raw!r}") from exc
    if parsed.isoformat() != raw:
        raise AssertionError(f"{label} is not canonical YYYY-MM-DD: {raw!r}")
    return parsed


def _timestamp_date(value: Any, *, label: str) -> date:
    raw = str(value or "").strip()
    if not raw:
        raise AssertionError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AssertionError(f"{label} is not an ISO timestamp: {raw!r}") from exc
    return parsed.date()


def _render_case_prompt(spec: CaseSpec, started_at: str) -> str:
    """Inject run-scoped facts before the owner message reaches the model."""

    if spec.case_id != "investment":
        return spec.prompt
    analysis_date = _timestamp_date(
        started_at,
        label="investment run.started_at",
    ).isoformat()
    if INVESTMENT_RUN_DATE_PLACEHOLDER not in spec.prompt:
        raise AssertionError("investment prompt lost its run-date placeholder")
    if INVESTMENT_HORIZON_PLACEHOLDER not in spec.prompt:
        raise AssertionError("investment prompt lost its horizon placeholder")
    year = int(analysis_date[:4])
    horizon = f"{year}, {year + 1}, {year + 2}"
    rendered = spec.prompt.replace(
        INVESTMENT_RUN_DATE_PLACEHOLDER,
        analysis_date,
    ).replace(INVESTMENT_HORIZON_PLACEHOLDER, horizon)
    if (
        INVESTMENT_RUN_DATE_PLACEHOLDER in rendered
        or INVESTMENT_HORIZON_PLACEHOLDER in rendered
    ):
        raise AssertionError("investment prompt retained an unresolved run date")
    return rendered


def _canonical_evidence_url(raw_url: Any) -> str:
    """Return a strict canonical HTTP(S) URL, unwrapping legacy DDG links."""

    raw = html.unescape(str(raw_url or "")).strip().rstrip(".,;:!?]}`\"'")
    if not raw or any(character.isspace() for character in raw):
        raise AssertionError(f"invalid evidence URL: {raw!r}")
    if re.search(r"%(?![0-9A-Fa-f]{2})", raw):
        raise AssertionError(f"invalid percent escape in evidence URL: {raw!r}")
    candidate = f"https:{raw}" if raw.startswith("//") else raw
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise AssertionError(f"malformed evidence URL: {raw!r}") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise AssertionError(f"unsupported evidence URL: {raw!r}")
    if parsed.username is not None or parsed.password is not None:
        raise AssertionError(f"credential-bearing evidence URL: {raw!r}")

    normalized_host = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    if normalized_host in {"duckduckgo.com", "www.duckduckgo.com"}:
        values = parse_qs(parsed.query, keep_blank_values=True).get("uddg", [])
        if parsed.path.rstrip("/") != "/l" or len(values) != 1 or not values[0]:
            raise AssertionError(f"unusable DuckDuckGo redirect URL: {raw!r}")
        return _canonical_evidence_url(values[0])

    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    authority = normalized_host
    if ":" in authority and not authority.startswith("["):
        authority = f"[{authority}]"
    if port is not None and not default_port:
        authority = f"{authority}:{port}"
    path = quote(
        unquote(parsed.path or "/"),
        safe="/:@-._~!$&'()*+,;=",
    )
    return urlunsplit(
        (parsed.scheme.lower(), authority, path, parsed.query, "")
    )


def _literal_urls(text: str) -> list[str]:
    return [
        match.group(0).rstrip(".,;:!?]}`\"'")
        for match in _ARTIFACT_URL_PATTERN.finditer(html.unescape(text))
    ]


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def _investment_web_provenance(
    runtime_details: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build role-scoped evidence only from successful, non-empty search results."""

    roles = ("investment_analyst", "risk_analyst")
    hits_by_role: dict[str, dict[str, list[dict[str, Any]]]] = {
        role_id: {} for role_id in roles
    }
    calls_by_role: dict[str, list[dict[str, Any]]] = {role_id: [] for role_id in roles}
    for detail in runtime_details:
        role_id = str(detail.get("role_id", "") or "").strip()
        if role_id not in hits_by_role:
            continue
        calls = [
            call
            for call in list(detail.get("calls", []) or [])
            if str(call.get("tool_name", "") or "").strip() == "web_search"
        ]
        results = [
            result
            for result in list(detail.get("results", []) or [])
            if str(result.get("tool_name", "") or "").strip() == "web_search"
        ]
        for call in calls:
            call_id = str(call.get("tool_call_id", "") or "").strip()
            query = str(_mapping(call.get("arguments")).get("query", "") or "").strip()
            matches = [
                result
                for result in results
                if str(result.get("tool_call_id", "") or "").strip() == call_id
            ]
            if not call_id or not query or len(matches) != 1:
                continue
            result = matches[0]
            payload = _mapping(result.get("payload"))
            nested = payload.get("result")
            search_results = (
                list(nested.get("results", []) or [])
                if isinstance(nested, dict)
                else []
            )
            if payload.get("success") is not True or not search_results:
                continue
            retrieval_date = _timestamp_date(
                result.get("created_at"),
                label=f"web_search ToolResult {call_id}.created_at",
            ).isoformat()
            call_hits: list[dict[str, Any]] = []
            for search_result in search_results:
                if not isinstance(search_result, dict):
                    continue
                try:
                    source_url = _canonical_evidence_url(search_result.get("url"))
                except AssertionError:
                    continue
                hit = {
                    "source_url": source_url,
                    "title": html.unescape(
                        str(search_result.get("title", "") or "")
                    ).strip(),
                    "snippet": html.unescape(
                        str(search_result.get("snippet", "") or "")
                    ).strip(),
                    "retrieval_date": retrieval_date,
                    "tool_call_id": call_id,
                    "runtime_session_id": str(
                        detail.get("runtime_session_id", "") or ""
                    ),
                }
                call_hits.append(hit)
                hits_by_role[role_id].setdefault(source_url, []).append(hit)
            if call_hits:
                calls_by_role[role_id].append(
                    {
                        "tool_call_id": call_id,
                        "runtime_session_id": str(
                            detail.get("runtime_session_id", "") or ""
                        ),
                        "query": query,
                        "retrieval_date": retrieval_date,
                        "hit_count": len(call_hits),
                    }
                )
    return {"hits_by_role": hits_by_role, "calls_by_role": calls_by_role}


def _query_mentions_alias(query: str, aliases: tuple[str, ...]) -> bool:
    lowered = query.casefold()
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lowered)
        for alias in aliases
    )


def _json_url_date_occurrences(
    document: Any,
    *,
    artifact_name: str,
) -> list[dict[str, str]]:
    """Pair every JSON URL with retrieval_date from its immediate source object."""

    occurrences: list[dict[str, str]] = []

    def scalar_urls(value: Any) -> list[str]:
        if isinstance(value, str):
            return _literal_urls(value)
        if isinstance(value, list):
            return [url for item in value for url in scalar_urls(item)]
        return []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            local_urls = [
                raw_url
                for child in value.values()
                if not isinstance(child, dict)
                for raw_url in scalar_urls(child)
            ]
            if local_urls:
                retrieval_date = _strict_iso_date(
                    value.get("retrieval_date"),
                    label=f"{artifact_name}:{path}.retrieval_date",
                ).isoformat()
                for raw_url in local_urls:
                    occurrences.append(
                        {
                            "source_url": _canonical_evidence_url(raw_url),
                            "retrieval_date": retrieval_date,
                            "path": path,
                        }
                    )
            for key, child in value.items():
                if isinstance(child, dict):
                    visit(child, f"{path}.{key}")
                elif isinstance(child, list):
                    for index, item in enumerate(child):
                        if isinstance(item, (dict, list)):
                            visit(item, f"{path}.{key}[{index}]")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, (dict, list)):
                    visit(child, f"{path}[{index}]")
                elif _literal_urls(str(child or "")):
                    raise AssertionError(
                        f"{artifact_name}:{path}[{index}] URL lacks a source object"
                    )

    visit(document, "$")
    raw_urls = [_canonical_evidence_url(url) for url in _literal_urls(json.dumps(document))]
    paired_urls = [item["source_url"] for item in occurrences]
    if sorted(raw_urls) != sorted(paired_urls):
        raise AssertionError(
            f"{artifact_name}: every URL must have retrieval_date in its immediate object"
        )
    return occurrences


def _host_matches_official_domain(source_url: str, ticker: str) -> bool:
    hostname = str(urlsplit(source_url).hostname or "").lower().rstrip(".")
    return any(
        hostname == domain or hostname.endswith(f".{domain}")
        for domain in INVESTMENT_OFFICIAL_DOMAINS[ticker]
    )


def _normalized_evidence_text(value: Any) -> str:
    return " ".join(html.unescape(str(value or "")).casefold().split())


def _hit_has_clean_value_occurrence(
    hit: dict[str, Any],
    value_token: str,
) -> bool:
    """Require one exact value occurrence in a non-forward-looking local clause."""

    raw_evidence = html.unescape(
        f"{hit.get('title', '')}\n{hit.get('snippet', '')}"
    ).casefold()
    evidence = re.sub(r"[^\S\n]+", " ", raw_evidence)
    normalized_value = _normalized_evidence_text(value_token)
    suffix_guard = (
        r"(?!(?:\d|[.,]\d))"
        if normalized_value[-1:].isdigit()
        else r"(?!\w)"
    )
    occurrence_pattern = re.compile(
        rf"(?<![\d.,]){re.escape(normalized_value)}{suffix_guard}"
    )
    for occurrence in occurrence_pattern.finditer(evidence):
        clause_start = max(
            evidence.rfind(separator, 0, occurrence.start())
            for separator in (".", ";", "\n")
        ) + 1
        clause_ends = [
            location
            for separator in (".", ";", "\n")
            if (location := evidence.find(separator, occurrence.end())) >= 0
        ]
        clause_end = min(clause_ends, default=len(evidence))
        before = evidence[
            max(clause_start, occurrence.start() - 100) : occurrence.start()
        ]
        after = evidence[
            occurrence.end() : min(clause_end, occurrence.end() + 30)
        ]
        if (
            INVESTMENT_FORWARD_LOOKING_PATTERN.search(before) is None
            and INVESTMENT_FORWARD_LOOKING_POSTFIX_PATTERN.search(after) is None
        ):
            return True
    return False


def _canonical_investment_period(
    value: Any,
) -> tuple[str, int | None, int, bool] | None:
    """Return period semantics while ignoring harmless ``of``/``year`` wording."""

    token = _normalized_evidence_text(value)
    if INVESTMENT_PERIOD_TOKEN_PATTERN.fullmatch(token) is None:
        return None
    year_match = re.search(r"(?<!\d)(20\d{2})(?!\d)", token)
    if year_match is None:
        return None
    year = int(year_match.group(1))
    quarter: int | None = None
    short_quarter = re.match(r"q([1-4])\b", token)
    if short_quarter is not None:
        quarter = int(short_quarter.group(1))
    else:
        ordinal_match = re.match(
            r"(first|second|third|fourth)\s+quarter\b",
            token,
        )
        if ordinal_match is not None:
            quarter = {
                "first": 1,
                "second": 2,
                "third": 3,
                "fourth": 4,
            }[ordinal_match.group(1)]
    fiscal = bool(
        re.search(r"(?:^|\s)(?:fy(?=\s*20\d{2})|fiscal\b)", token)
    )
    return ("quarter" if quarter is not None else "year", quarter, year, fiscal)


def _investment_period_semantics_in_evidence(
    evidence_text: Any,
) -> set[tuple[str, int | None, int, bool]]:
    semantics: set[tuple[str, int | None, int, bool]] = set()
    text = html.unescape(str(evidence_text or ""))
    for match in INVESTMENT_PERIOD_EVIDENCE_PATTERN.finditer(text):
        canonical = _canonical_investment_period(match.group(0))
        if canonical is not None:
            semantics.add(canonical)
    return semantics


def _investment_period_is_semantically_evidenced(
    period_token: str,
    evidence_text: Any,
) -> bool:
    canonical = _canonical_investment_period(period_token)
    return canonical is not None and canonical in (
        _investment_period_semantics_in_evidence(evidence_text)
    )


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonempty_text_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(_nonempty_text(item) for item in value)
    )


def _nonempty_substantive_value(value: Any) -> bool:
    if _nonempty_text(value):
        return True
    if isinstance(value, list):
        return bool(value) and all(_nonempty_substantive_value(item) for item in value)
    if isinstance(value, dict):
        return bool(value) and all(
            _nonempty_text(key) and _nonempty_substantive_value(item)
            for key, item in value.items()
        )
    return False


def _validate_investment_substantive_schema(
    document: dict[str, Any],
    *,
    artifact_name: str,
) -> dict[str, Any]:
    """Validate role-specific analytical content independently of provenance."""

    scenarios = document.get("scenarios")
    if not isinstance(scenarios, dict) or set(scenarios) != {"bear", "base", "bull"}:
        raise AssertionError(
            f"{artifact_name}: scenarios must contain exactly bear, base, and bull"
        )
    if not all(_nonempty_substantive_value(scenarios[key]) for key in scenarios):
        raise AssertionError(f"{artifact_name}: every scenario must be nonempty")

    if artifact_name == "company_analysis.json":
        profiles = document.get("company_profiles")
        if not isinstance(profiles, dict) or set(profiles) != set(INVESTMENT_TICKERS):
            raise AssertionError(
                f"{artifact_name}: company_profiles must contain exactly NVDA, AMD, and AVGO"
            )
        for ticker in INVESTMENT_TICKERS:
            profile = profiles[ticker]
            if not isinstance(profile, dict):
                raise AssertionError(
                    f"{artifact_name}: company_profiles.{ticker} must be an object"
                )
            if not _nonempty_text(profile.get("thesis")):
                raise AssertionError(
                    f"{artifact_name}: company_profiles.{ticker}.thesis must be nonempty"
                )
            for field_name in ("catalysts", "valuation_caveats"):
                if not _nonempty_text_list(profile.get(field_name)):
                    raise AssertionError(
                        f"{artifact_name}: company_profiles.{ticker}.{field_name} "
                        "must be a nonempty text list"
                    )
        if not _nonempty_substantive_value(document.get("ranked_recommendation")):
            raise AssertionError(
                f"{artifact_name}: ranked_recommendation must be nonempty"
            )
        if not _nonempty_text_list(document.get("position_sizing_guardrails")):
            raise AssertionError(
                f"{artifact_name}: position_sizing_guardrails must be a nonempty text list"
            )
        return {
            "contract": "company_analysis",
            "company_profile_tickers": list(INVESTMENT_TICKERS),
            "scenario_keys": ["bear", "base", "bull"],
            "substantive_contract_valid": True,
        }

    if artifact_name == "risk_analysis.json":
        risk_register = document.get("risk_register")
        if not isinstance(risk_register, dict) or set(risk_register) != set(
            INVESTMENT_TICKERS
        ):
            raise AssertionError(
                f"{artifact_name}: risk_register must contain exactly NVDA, AMD, and AVGO"
            )
        for ticker in INVESTMENT_TICKERS:
            entry = risk_register[ticker]
            if not isinstance(entry, dict):
                raise AssertionError(
                    f"{artifact_name}: risk_register.{ticker} must be an object"
                )
            for field_name in ("downside_risks", "monitor_triggers"):
                if not _nonempty_text_list(entry.get(field_name)):
                    raise AssertionError(
                        f"{artifact_name}: risk_register.{ticker}.{field_name} "
                        "must be a nonempty text list"
                    )
            if not _nonempty_text(entry.get("sizing_guardrail")):
                raise AssertionError(
                    f"{artifact_name}: risk_register.{ticker}.sizing_guardrail "
                    "must be nonempty"
                )
        if not _nonempty_text_list(document.get("portfolio_guardrails")):
            raise AssertionError(
                f"{artifact_name}: portfolio_guardrails must be a nonempty text list"
            )
        return {
            "contract": "risk_analysis",
            "risk_register_tickers": list(INVESTMENT_TICKERS),
            "scenario_keys": ["bear", "base", "bull"],
            "substantive_contract_valid": True,
        }

    raise AssertionError(f"investment: unsupported note contract {artifact_name!r}")


def _validate_investment_note(
    document: Any,
    *,
    artifact_name: str,
    role_id: str,
    analysis_date: date,
    role_hits: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise AssertionError(f"{artifact_name}: top level must be an object")
    expected_date = analysis_date.isoformat()
    if document.get("analysis_date") != expected_date or document.get("as_of") != expected_date:
        raise AssertionError(
            f"{artifact_name}: analysis_date and as_of must equal {expected_date}"
        )
    expected_horizon = [
        analysis_date.year,
        analysis_date.year + 1,
        analysis_date.year + 2,
    ]
    if document.get("horizon_years") != expected_horizon:
        raise AssertionError(
            f"{artifact_name}: horizon_years must equal {expected_horizon!r}"
        )
    substantive_schema = _validate_investment_substantive_schema(
        document,
        artifact_name=artifact_name,
    )

    occurrences = _json_url_date_occurrences(
        document,
        artifact_name=artifact_name,
    )
    for occurrence in occurrences:
        matching_hits = role_hits.get(occurrence["source_url"], [])
        if not any(
            hit["retrieval_date"] == occurrence["retrieval_date"]
            for hit in matching_hits
        ):
            raise AssertionError(
                f"{artifact_name}: URL/date was not returned by {role_id}: "
                f"{occurrence['source_url']} @ {occurrence['retrieval_date']}"
            )

    claims = document.get("critical_claims")
    if not isinstance(claims, list) or len(claims) != len(INVESTMENT_TICKERS):
        raise AssertionError(
            f"{artifact_name}: critical_claims must contain exactly three entries"
        )
    required_fields = {
        "ticker",
        "kind",
        "value_token",
        "period_token",
        "period_end",
        "source_url",
        "retrieval_date",
    }
    claim_counts = {ticker: 0 for ticker in INVESTMENT_TICKERS}
    official_counts = {ticker: 0 for ticker in INVESTMENT_TICKERS}
    claim_evidence: list[dict[str, Any]] = []
    for index, raw_claim in enumerate(claims):
        if not isinstance(raw_claim, dict) or set(raw_claim) != required_fields:
            raise AssertionError(
                f"{artifact_name}: critical_claims[{index}] must contain exactly "
                "the seven required fields"
            )
        ticker = str(raw_claim.get("ticker", "") or "").strip().upper()
        if ticker not in INVESTMENT_TICKERS:
            raise AssertionError(
                f"{artifact_name}: critical_claims[{index}] has invalid ticker"
            )
        if str(raw_claim.get("kind", "") or "").strip() != "sourced_fact":
            raise AssertionError(
                f"{artifact_name}: assumptions cannot satisfy critical_claims"
            )
        value_token = str(raw_claim.get("value_token", "") or "").strip()
        period_token = str(raw_claim.get("period_token", "") or "").strip()
        if (
            INVESTMENT_VALUE_TOKEN_PATTERN.fullmatch(value_token) is None
            or INVESTMENT_PERIOD_TOKEN_PATTERN.fullmatch(period_token) is None
        ):
            raise AssertionError(
                f"{artifact_name}: critical_claims[{index}] must use one exact "
                "quantitative value_token and one ended-period token"
            )
        period_end = _strict_iso_date(
            raw_claim.get("period_end"),
            label=f"{artifact_name}:critical_claims[{index}].period_end",
        )
        age_days = (analysis_date - period_end).days
        if (
            age_days < 0
            or age_days > INVESTMENT_MAX_FACT_AGE_DAYS
            or period_end.year != analysis_date.year
        ):
            raise AssertionError(
                f"{artifact_name}: critical_claims[{index}] period_end must be "
                "ended, fresh, and in the analysis calendar year"
            )
        retrieval_date = _strict_iso_date(
            raw_claim.get("retrieval_date"),
            label=f"{artifact_name}:critical_claims[{index}].retrieval_date",
        ).isoformat()
        source_url = _canonical_evidence_url(raw_claim.get("source_url"))
        matching_hits = [
            hit
            for hit in role_hits.get(source_url, [])
            if hit["retrieval_date"] == retrieval_date
        ]

        def hit_supports_value(hit: dict[str, Any]) -> bool:
            return _hit_has_clean_value_occurrence(hit, value_token)

        token_hit = next(
            (
                hit
                for hit in matching_hits
                if (
                    hit_supports_value(hit)
                    and _investment_period_is_semantically_evidenced(
                        period_token,
                        f"{hit['title']} {hit['snippet']}",
                    )
                    and _query_mentions_alias(
                        f"{hit['title']} {hit['snippet']}",
                        INVESTMENT_TICKER_ALIASES[ticker],
                    )
                )
            ),
            None,
        )
        if token_hit is None:
            raise AssertionError(
                f"{artifact_name}: critical_claims[{index}] tokens are absent "
                "from its durable search hit title/snippet"
            )
        claim_counts[ticker] += 1
        official = _host_matches_official_domain(source_url, ticker)
        if not official:
            raise AssertionError(
                f"{artifact_name}: critical_claims[{index}] must use the "
                f"{ticker} issuer's official domain"
            )
        official_counts[ticker] += 1
        claim_evidence.append(
            {
                "ticker": ticker,
                "source_url": source_url,
                "retrieval_date": retrieval_date,
                "period_end": period_end.isoformat(),
                "age_days": age_days,
                "official_source": official,
                "value_token": value_token,
                "period_token": period_token,
                "tool_call_id": token_hit["tool_call_id"],
            }
        )
    expected_counts = {ticker: 1 for ticker in INVESTMENT_TICKERS}
    if claim_counts != expected_counts:
        raise AssertionError(
            f"{artifact_name}: critical_claims must contain exactly one claim per "
            f"ticker; actual={claim_counts!r}"
        )
    if official_counts != expected_counts:
        raise AssertionError(
            f"{artifact_name}: every critical claim must be issuer-official"
        )
    return {
        "role_id": role_id,
        "url_occurrence_count": len(occurrences),
        "distinct_url_count": len({item["source_url"] for item in occurrences}),
        "claim_count": len(claim_evidence),
        "claims_by_ticker": claim_counts,
        "official_claims_by_ticker": official_counts,
        "claims": claim_evidence,
        "urls": sorted({item["source_url"] for item in occurrences}),
        "substantive_schema": substantive_schema,
    }


def _investment_visible_report_lines(report: str) -> dict[int, str]:
    """Return visible Markdown lines under the strict report visibility rules."""

    visible_report_lines: dict[int, str] = {}
    fenced_marker: tuple[str, int] | None = None
    in_html_comment = False
    for line_number, raw_line in enumerate(report.splitlines(), start=1):
        stripped = raw_line.lstrip()
        fence_match = re.match(r"^(`{3,}|~{3,})", stripped)
        if fenced_marker is not None:
            marker_char, marker_length = fenced_marker
            if re.match(
                rf"^{re.escape(marker_char)}{{{marker_length},}}\s*$",
                stripped,
            ):
                fenced_marker = None
            elif _literal_urls(raw_line):
                raise AssertionError(
                    f"report.md:{line_number}: URLs in hidden Markdown content are forbidden"
                )
            continue
        if fence_match is not None:
            marker = fence_match.group(1)
            fenced_marker = (marker[0], len(marker))
            continue

        remaining = raw_line
        visible_parts: list[str] = []
        while remaining:
            if in_html_comment:
                comment_end = remaining.find("-->")
                if comment_end < 0:
                    remaining = ""
                    break
                remaining = remaining[comment_end + 3 :]
                in_html_comment = False
                continue
            comment_start = remaining.find("<!--")
            if comment_start < 0:
                visible_parts.append(remaining)
                break
            visible_parts.append(remaining[:comment_start])
            remaining = remaining[comment_start + 4 :]
            in_html_comment = True
        visible_line = "".join(visible_parts)
        raw_urls = _literal_urls(raw_line)
        visible_urls = _literal_urls(visible_line)
        if raw_urls != visible_urls:
            raise AssertionError(
                f"report.md:{line_number}: URLs in hidden Markdown content are forbidden"
            )
        visible_report_lines[line_number] = visible_line
    return visible_report_lines


def _canonical_report_evidence_url(
    raw_url: Any,
    *,
    line_number: int,
) -> str:
    """Canonicalize one report URL while preserving its repair owner."""

    try:
        return _canonical_evidence_url(raw_url)
    except (AssertionError, UnicodeError) as exc:
        detail = str(exc).strip() or type(exc).__name__
        raise AssertionError(f"report.md:{line_number}: {detail}") from exc


def _investment_data_quality_gate(
    workplace: Path,
    runtime_details: list[dict[str, Any]],
    run_started_at: str,
) -> dict[str, Any]:
    """Fail closed on freshness, role-scoped provenance, and claim support."""

    analysis_date = _timestamp_date(
        run_started_at,
        label="investment run.started_at",
    )
    provenance = _investment_web_provenance(runtime_details)
    calls_by_role = provenance["calls_by_role"]
    hits_by_role = provenance["hits_by_role"]
    current_year = str(analysis_date.year)
    current_query_evidence: dict[str, dict[str, str]] = {}
    for role_id in ("investment_analyst", "risk_analyst"):
        off_date_calls = [
            call["tool_call_id"]
            for call in calls_by_role[role_id]
            if call["retrieval_date"] != analysis_date.isoformat()
        ]
        if off_date_calls:
            raise AssertionError(
                f"investment: {role_id} web_search ToolResults crossed the "
                f"analysis-date boundary: {off_date_calls}"
            )
        current_query_evidence[role_id] = {}
        for ticker in INVESTMENT_TICKERS:
            matching_call = next(
                (
                    call
                    for call in calls_by_role[role_id]
                    if re.search(rf"(?<!\d){re.escape(current_year)}(?!\d)", call["query"])
                    and _query_mentions_alias(
                        call["query"], INVESTMENT_TICKER_ALIASES[ticker]
                    )
                ),
                None,
            )
            if matching_call is None:
                raise AssertionError(
                    f"investment: {role_id} lacks a successful non-empty current-year "
                    f"web_search for {ticker}"
                )
            current_query_evidence[role_id][ticker] = matching_call["tool_call_id"]

    note_contracts = (
        (
            "company_analysis.json",
            "investment_analyst",
            workplace / "investment_case/company_analysis.json",
        ),
        (
            "risk_analysis.json",
            "risk_analyst",
            workplace / "investment_case/risk_analysis.json",
        ),
    )
    note_evidence: dict[str, dict[str, Any]] = {}
    child_urls: set[str] = set()
    for artifact_name, role_id, path in note_contracts:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AssertionError(f"investment: invalid {artifact_name}") from exc
        note_evidence[artifact_name] = _validate_investment_note(
            document,
            artifact_name=artifact_name,
            role_id=role_id,
            analysis_date=analysis_date,
            role_hits=hits_by_role[role_id],
        )
        child_urls.update(note_evidence[artifact_name]["urls"])

    report_path = workplace / "investment_case/report.md"
    report = report_path.read_text(encoding="utf-8", errors="strict")
    expected_analysis_line = f"Analysis date: {analysis_date.isoformat()}"
    expected_horizon_line = (
        "Horizon years: "
        f"{analysis_date.year}, {analysis_date.year + 1}, {analysis_date.year + 2}"
    )
    report_lines = report.splitlines()

    visible_report_lines = _investment_visible_report_lines(report)

    analysis_lines = [
        line.strip()
        for line in visible_report_lines.values()
        if re.match(r"^\s*Analysis date\s*:", line, re.IGNORECASE)
    ]
    horizon_lines = [
        line.strip()
        for line in visible_report_lines.values()
        if re.match(r"^\s*Horizon years\s*:", line, re.IGNORECASE)
    ]
    if analysis_lines != [expected_analysis_line] or horizon_lines != [
        expected_horizon_line
    ]:
        raise AssertionError(
            "report.md: unique exact Analysis date and Horizon years metadata are required"
        )
    report_occurrences: list[dict[str, str]] = []
    for line_number, line in visible_report_lines.items():
        for raw_url in _literal_urls(line):
            source_url = _canonical_report_evidence_url(
                raw_url,
                line_number=line_number,
            )
            if source_url not in child_urls:
                raise AssertionError(
                    f"report.md:{line_number}: URL was not validated in a child JSON"
                )
            dates = set(_ISO_DATE_PATTERN.findall(line))
            union_hits = [
                hit
                for role_hits in hits_by_role.values()
                for hit in role_hits.get(source_url, [])
            ]
            durable_dates = {hit["retrieval_date"] for hit in union_hits}
            if len(dates) != 1 or not dates.issubset(durable_dates):
                raise AssertionError(
                    f"report.md:{line_number}: URL line must contain only its one "
                    "ToolResult date"
                )
            retrieval_date = next(iter(dates))
            report_occurrences.append(
                {
                    "source_url": source_url,
                    "retrieval_date": retrieval_date,
                    "line": str(line_number),
                }
            )
    if len({item["source_url"] for item in report_occurrences}) < 3:
        raise AssertionError("report.md: fewer than three validated source URLs")

    verified_headings: list[tuple[int, int]] = []
    for line_number, line in visible_report_lines.items():
        heading_match = re.match(
            r"^\s{0,3}(#{1,6})\s+Verified critical facts\s*$",
            line,
            re.IGNORECASE,
        )
        if heading_match is not None:
            verified_headings.append((line_number, len(heading_match.group(1))))
    if len(verified_headings) != 1:
        raise AssertionError(
            "report.md: exactly one Verified critical facts section is required"
        )
    verified_heading_line, verified_heading_level = verified_headings[0]
    verified_section_end = len(report_lines) + 1
    for line_number, line in visible_report_lines.items():
        if line_number <= verified_heading_line:
            continue
        next_heading = re.match(
            r"^\s{0,3}(#{1,6})\s+\S",
            line,
        )
        if (
            next_heading is not None
            and len(next_heading.group(1)) <= verified_heading_level
        ):
            verified_section_end = line_number
            break
    section_nonempty = [
        (line_number, line.strip())
        for line_number, line in visible_report_lines.items()
        if verified_heading_line < line_number < verified_section_end and line.strip()
    ]
    expected_header = ["Child", "Ticker", "Value", "Period", "URL", "Retrieved"]
    expected_separator = ["---"] * 6

    def table_cells(line: str) -> list[str] | None:
        if not (line.startswith("|") and line.endswith("|")):
            return None
        cells = [cell.strip() for cell in line[1:-1].split("|")]
        return cells if len(cells) == 6 else None

    if (
        len(section_nonempty) < 2
        or table_cells(section_nonempty[0][1]) != expected_header
        or table_cells(section_nonempty[1][1]) != expected_separator
    ):
        raise AssertionError(
            "report.md: Verified critical facts requires the exact visible table schema"
        )
    verified_table_rows: dict[int, list[str]] = {}
    for line_number, stripped_line in section_nonempty[2:]:
        cells = table_cells(stripped_line)
        if cells is None or len(_literal_urls(stripped_line)) != 1:
            continue
        verified_table_rows[line_number] = cells

    used_report_lines: set[int] = set()
    verified_critical_fact_rows: list[dict[str, Any]] = []
    for artifact_name, evidence in note_evidence.items():
        for claim_index, claim in enumerate(evidence["claims"]):
            if not claim["official_source"]:
                continue
            normalized_value = _normalized_evidence_text(claim["value_token"])
            normalized_period = _normalized_evidence_text(claim["period_token"])
            value_numbers = re.findall(r"\d+(?:[.,]\d+)*", normalized_value)
            matching_line: int | None = None
            for occurrence in report_occurrences:
                line_number = int(occurrence["line"])
                if line_number in used_report_lines:
                    continue
                row_cells = verified_table_rows.get(line_number)
                if (
                    row_cells is None
                    or row_cells[0] != artifact_name
                    or row_cells[1] != claim["ticker"]
                    or _normalized_evidence_text(row_cells[2])
                    != normalized_value
                    or row_cells[3] != claim["period_token"]
                    or _canonical_report_evidence_url(
                        row_cells[4],
                        line_number=line_number,
                    )
                    != claim["source_url"]
                    or row_cells[5] != claim["retrieval_date"]
                ):
                    continue
                if (
                    occurrence["source_url"] != claim["source_url"]
                    or occurrence["retrieval_date"] != claim["retrieval_date"]
                ):
                    continue
                line = visible_report_lines[line_number]
                normalized_line = _normalized_evidence_text(line)
                if (
                    normalized_value not in normalized_line
                    or normalized_period not in normalized_line
                    or not all(
                        re.search(
                            rf"(?<![\d.,]){re.escape(number)}(?![\d.,])",
                            normalized_line,
                        )
                        is not None
                        for number in value_numbers
                    )
                ):
                    continue
                matching_line = line_number
                break
            if matching_line is None:
                raise AssertionError(
                    "report.md: Verified critical facts lacks a unique exact row for "
                    f"{artifact_name} critical_claims[{claim_index}]"
                )
            used_report_lines.add(matching_line)
            verified_critical_fact_rows.append(
                {
                    "artifact": artifact_name,
                    "ticker": claim["ticker"],
                    "value_token": claim["value_token"],
                    "period_token": claim["period_token"],
                    "source_url": claim["source_url"],
                    "retrieval_date": claim["retrieval_date"],
                    "line": matching_line,
                }
            )
    if set(verified_table_rows) != used_report_lines:
        raise AssertionError(
            "report.md: Verified critical facts contains unmatched or extra data rows"
        )
    if len(verified_critical_fact_rows) != 2 * len(INVESTMENT_TICKERS):
        raise AssertionError(
            "report.md: Verified critical facts must contain exactly six rows"
        )

    return {
        "analysis_date": analysis_date.isoformat(),
        "horizon_years": [
            analysis_date.year,
            analysis_date.year + 1,
            analysis_date.year + 2,
        ],
        "max_fact_age_days": INVESTMENT_MAX_FACT_AGE_DAYS,
        "successful_nonempty_search_calls_by_role": {
            role_id: len(calls) for role_id, calls in calls_by_role.items()
        },
        "distinct_search_hits_by_role": {
            role_id: len(hits) for role_id, hits in hits_by_role.items()
        },
        "current_query_tool_calls": current_query_evidence,
        "notes": note_evidence,
        "report_url_occurrence_count": len(report_occurrences),
        "report_distinct_url_count": len(
            {item["source_url"] for item in report_occurrences}
        ),
        "report_sources": report_occurrences,
        "verified_critical_fact_rows": verified_critical_fact_rows,
        "role_scoped_provenance_closed": True,
        "critical_claims_supported": True,
    }


def _investment_issue_domains(issue: str) -> set[str]:
    """Classify a quality failure without weakening unknown fail-closed routing."""

    lowered = str(issue or "").casefold()
    if lowered.startswith("report.md:"):
        return {"report"}
    if lowered.startswith("company_analysis.json:"):
        return {"company"}
    if lowered.startswith("risk_analysis.json:"):
        return {"risk"}
    domains: set[str] = set()
    if "company_analysis" in lowered or "investment_analyst" in lowered:
        domains.add("company")
    if "risk_analysis" in lowered or "risk_analyst" in lowered:
        domains.add("risk")
    if any(
        marker in lowered
        for marker in (
            "report.md",
            "verified critical facts",
            "analysis date and horizon years metadata",
        )
    ):
        domains.add("report")
    return domains or {"unknown"}


def _investment_provenance_quality_issues(
    runtime_details: list[dict[str, Any]],
    analysis_date: date,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Collect every independently attributable role/query provenance issue."""

    try:
        provenance = _investment_web_provenance(runtime_details)
    except (AssertionError, OSError, UnicodeError) as exc:
        detail = str(exc).strip() or type(exc).__name__
        return None, [f"investment: invalid durable web provenance: {detail}"]

    issues: list[str] = []
    calls_by_role = provenance["calls_by_role"]
    current_year = str(analysis_date.year)
    for role_id in ("investment_analyst", "risk_analyst"):
        off_date_calls = [
            call["tool_call_id"]
            for call in calls_by_role[role_id]
            if call["retrieval_date"] != analysis_date.isoformat()
        ]
        if off_date_calls:
            issues.append(
                f"investment: {role_id} web_search ToolResults crossed the "
                f"analysis-date boundary: {off_date_calls}"
            )
        for ticker in INVESTMENT_TICKERS:
            matching_call = next(
                (
                    call
                    for call in calls_by_role[role_id]
                    if re.search(
                        rf"(?<!\d){re.escape(current_year)}(?!\d)",
                        call["query"],
                    )
                    and _query_mentions_alias(
                        call["query"], INVESTMENT_TICKER_ALIASES[ticker]
                    )
                ),
                None,
            )
            if matching_call is None:
                issues.append(
                    f"investment: {role_id} lacks a successful non-empty "
                    f"current-year web_search for {ticker}"
                )
    return provenance, issues


def _validate_investment_report_independent_contract(
    workplace: Path,
    analysis_date: date,
) -> None:
    """Validate only report facts that do not require valid child evidence."""

    report_path = workplace / "investment_case/report.md"
    try:
        report = report_path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as exc:
        raise AssertionError("investment: invalid report.md") from exc
    report_lines = report.splitlines()
    visible_report_lines = _investment_visible_report_lines(report)
    expected_analysis_line = f"Analysis date: {analysis_date.isoformat()}"
    expected_horizon_line = (
        "Horizon years: "
        f"{analysis_date.year}, {analysis_date.year + 1}, {analysis_date.year + 2}"
    )
    analysis_lines = [
        line.strip()
        for line in visible_report_lines.values()
        if re.match(r"^\s*Analysis date\s*:", line, re.IGNORECASE)
    ]
    horizon_lines = [
        line.strip()
        for line in visible_report_lines.values()
        if re.match(r"^\s*Horizon years\s*:", line, re.IGNORECASE)
    ]
    if analysis_lines != [expected_analysis_line] or horizon_lines != [
        expected_horizon_line
    ]:
        raise AssertionError(
            "report.md: unique exact Analysis date and Horizon years metadata are required"
        )

    verified_headings: list[tuple[int, int]] = []
    for line_number, line in visible_report_lines.items():
        heading_match = re.match(
            r"^\s{0,3}(#{1,6})\s+Verified critical facts\s*$",
            line,
            re.IGNORECASE,
        )
        if heading_match is not None:
            verified_headings.append((line_number, len(heading_match.group(1))))
    if len(verified_headings) != 1:
        raise AssertionError(
            "report.md: exactly one Verified critical facts section is required"
        )
    verified_heading_line, verified_heading_level = verified_headings[0]
    verified_section_end = len(report_lines) + 1
    for line_number, line in visible_report_lines.items():
        if line_number <= verified_heading_line:
            continue
        next_heading = re.match(r"^\s{0,3}(#{1,6})\s+\S", line)
        if (
            next_heading is not None
            and len(next_heading.group(1)) <= verified_heading_level
        ):
            verified_section_end = line_number
            break
    section_lines = [
        line.strip()
        for line_number, line in visible_report_lines.items()
        if verified_heading_line < line_number < verified_section_end
        and line.strip()
    ]

    def table_cells(line: str) -> list[str] | None:
        if not (line.startswith("|") and line.endswith("|")):
            return None
        cells = [cell.strip() for cell in line[1:-1].split("|")]
        return cells if len(cells) == 6 else None

    expected_header = ["Child", "Ticker", "Value", "Period", "URL", "Retrieved"]
    expected_separator = ["---"] * 6
    if (
        len(section_lines) < 2
        or table_cells(section_lines[0]) != expected_header
        or table_cells(section_lines[1]) != expected_separator
    ):
        raise AssertionError(
            "report.md: Verified critical facts requires the exact visible table schema"
        )
    rows = [
        cells
        for line in section_lines[2:]
        if (cells := table_cells(line)) is not None
        and len(_literal_urls(line)) == 1
    ]
    expected_children = {
        "company_analysis.json": len(INVESTMENT_TICKERS),
        "risk_analysis.json": len(INVESTMENT_TICKERS),
    }
    child_counts = {
        child: sum(row[0] == child for row in rows) for child in expected_children
    }
    if len(rows) != 2 * len(INVESTMENT_TICKERS) or child_counts != expected_children:
        raise AssertionError(
            "report.md: Verified critical facts must contain exactly three rows "
            "for each exact child artifact name"
        )
    for row in rows:
        if row[1] not in INVESTMENT_TICKERS or len(_ISO_DATE_PATTERN.findall(row[5])) != 1:
            raise AssertionError(
                "report.md: Verified critical facts contains an invalid ticker or date"
            )


def _investment_quality_issues(
    workplace: Path,
    runtime_details: list[dict[str, Any]],
    run_started_at: str,
) -> list[str]:
    """Aggregate one safe, attributable failure per independent quality domain."""

    try:
        analysis_date = _timestamp_date(
            run_started_at,
            label="investment run.started_at",
        )
    except (AssertionError, OSError, UnicodeError) as exc:
        detail = str(exc).strip() or type(exc).__name__
        return [f"investment: invalid analysis date: {detail}"]

    provenance, issues = _investment_provenance_quality_issues(
        runtime_details,
        analysis_date,
    )
    seen_domains = {
        domain for issue in issues for domain in _investment_issue_domains(issue)
    }
    note_contracts = (
        (
            "company_analysis.json",
            "investment_analyst",
            workplace / "investment_case/company_analysis.json",
        ),
        (
            "risk_analysis.json",
            "risk_analyst",
            workplace / "investment_case/risk_analysis.json",
        ),
    )
    for artifact_name, role_id, path in note_contracts:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            if provenance is None:
                if not isinstance(document, dict):
                    raise AssertionError(
                        f"{artifact_name}: top level must be an object"
                    )
                _validate_investment_substantive_schema(
                    document,
                    artifact_name=artifact_name,
                )
            else:
                _validate_investment_note(
                    document,
                    artifact_name=artifact_name,
                    role_id=role_id,
                    analysis_date=analysis_date,
                    role_hits=provenance["hits_by_role"][role_id],
                )
        except (AssertionError, OSError, UnicodeError, ValueError) as exc:
            issue = str(exc).strip() or f"investment: invalid {artifact_name}"
            if artifact_name not in issue:
                issue = f"investment: invalid {artifact_name}: {issue}"
            issues.append(issue)
            seen_domains.update(_investment_issue_domains(issue))

    try:
        _validate_investment_report_independent_contract(
            workplace,
            analysis_date,
        )
    except (AssertionError, OSError, UnicodeError) as exc:
        issue = str(exc).strip() or "investment: invalid report.md"
        issues.append(issue)
        seen_domains.add("report")

    # The original strict gate remains authoritative.  It captures dependent
    # report/child linkage failures when their prerequisite domains are valid.
    try:
        _investment_data_quality_gate(
            workplace,
            runtime_details,
            run_started_at,
        )
    except (AssertionError, OSError, UnicodeError) as exc:
        issue = str(exc).strip() or type(exc).__name__
        domains = _investment_issue_domains(issue)
        if issue not in issues and ("unknown" in domains or domains.isdisjoint(seen_domains)):
            issues.append(issue)

    return list(dict.fromkeys(issues))


def _validate_real_artifacts(spec: CaseSpec, workplace: Path) -> dict[str, Any]:
    """Run deterministic checks that are independent of the agents' claims."""

    if spec.case_id == "investment":
        report_path = workplace / "investment_case/report.md"
        report = report_path.read_text(encoding="utf-8", errors="replace")
        lowered = report.lower()
        source_urls = {
            url.rstrip(".,;:!?]\"'")
            for url in re.findall(r"https?://[^\s|<>()]+", report)
        }
        source_url_lines = [
            line for line in report.splitlines() if re.search(r"https?://", line)
        ]
        dated_source_urls = {
            url.rstrip(".,;:!?]\"'")
            for line in source_url_lines
            if re.search(r"\b20\d{2}[-/]\d{1,2}[-/]\d{1,2}\b", line)
            for url in re.findall(r"https?://[^\s|<>()]+", line)
        }
        note_texts = [
            (workplace / relative).read_text(encoding="utf-8", errors="replace")
            for relative in (
                "investment_case/company_analysis.json",
                "investment_case/risk_analysis.json",
            )
        ]
        parsed_notes: list[Any] = []
        notes_are_valid = True
        for note in note_texts:
            try:
                parsed_notes.append(json.loads(note))
            except (TypeError, ValueError):
                notes_are_valid = False
        checks = {
            "substantive_length": len(report) >= 1000,
            "structured_child_notes_valid": (
                notes_are_valid
                and len(parsed_notes) == 2
                and all(isinstance(note, (dict, list)) and bool(note) for note in parsed_notes)
            ),
            "structured_child_notes_substantive": all(
                len(note) >= 300 for note in note_texts
            ),
            "structured_child_notes_cite_sources": all(
                "http://" in note or "https://" in note for note in note_texts
            ),
            "all_targets_covered": all(
                name in lowered for name in ("nvidia", "amd", "broadcom")
            ),
            "scenario_analysis_present": all(
                term in lowered for term in ("bear", "base", "bull")
            ),
            "source_urls_present": len(source_urls) >= 3,
            "retrieval_dates_present": len(dated_source_urls) >= 3,
        }
    elif spec.case_id == "app":
        html_path = workplace / "app_case/index.html"
        css_path = workplace / "app_case/styles.css"
        js_path = workplace / "app_case/app.js"
        html = html_path.read_text(encoding="utf-8", errors="replace").lower()
        css = css_path.read_text(encoding="utf-8", errors="replace").lower()
        js = js_path.read_text(encoding="utf-8", errors="replace")
        js_lower = js.lower()
        node = shutil.which("node")
        if not node:
            raise AssertionError("app artifact validation requires node for syntax checking")
        syntax = subprocess.run(
            [node, "--check", str(js_path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        checks = {
            "substantive_files": (
                len(html) >= 300 and len(css) >= 200 and len(js) >= 500
            ),
            "assets_linked": "styles.css" in html and "app.js" in html,
            "accessible_controls_present": all(
                token in html for token in ("<form", "<input", "<select", "<button")
            ),
            "local_storage_present": "localstorage" in js_lower,
            "event_handling_present": "addeventlistener" in js_lower,
            "filtering_present": "filter" in js_lower,
            "deletion_present": any(
                token in js_lower for token in ("delete", "remove", "splice")
            ),
            "responsive_css_present": "@media" in css,
            "javascript_syntax_valid": syntax.returncode == 0,
        }
    else:
        raise AssertionError(f"unknown E2E artifact case: {spec.case_id}")

    failed = sorted(name for name, passed in checks.items() if not passed)
    if failed:
        raise AssertionError(
            f"{spec.case_id}: deterministic artifact checks failed: {failed}"
        )
    return checks


def _path_in_workplace(raw_path: Any, workplace: Path) -> Path:
    value = str(raw_path or ".").strip() or "."
    candidate = Path(value)
    resolved = (
        candidate.resolve()
        if candidate.is_absolute()
        else (workplace / candidate).resolve()
    )
    try:
        resolved.relative_to(workplace.resolve())
    except ValueError as exc:
        raise AssertionError(f"tool path escapes E2E workplace: {value}") from exc
    return resolved


def _validate_test_tool_call(
    spec: CaseSpec,
    workplace: Path,
    tool_call: dict[str, Any],
) -> None:
    """Fail closed unless a requested tool is necessary for this test case."""

    name = str(tool_call.get("name", "") or "").strip()
    arguments = dict(tool_call.get("arguments", {}) or {})
    allowed_write_paths = {
        (workplace / relative).resolve() for relative in spec.required_artifacts
    }

    if name in {"file_read", "list_dir"}:
        _path_in_workplace(arguments.get("path", "."), workplace)
        return
    if name == "file_search":
        _path_in_workplace(arguments.get("directory", "."), workplace)
        return
    if name in {"file_write", "file_edit"}:
        target = _path_in_workplace(arguments.get("path"), workplace)
        if target not in allowed_write_paths:
            raise AssertionError(
                f"{spec.case_id}: refusing unexpected write target: {target}"
            )
        return
    if name == "web_search" and spec.case_id == "investment":
        if not str(arguments.get("query", "") or "").strip():
            raise AssertionError("refusing empty web_search query")
        return
    if name == "shell_exec" and spec.case_id in {"investment", "app"}:
        unexpected_keys = set(arguments) - {
            "command", "working_directory", "timeout", "shell",
        }
        if unexpected_keys:
            raise AssertionError(
                f"refusing unexpected shell arguments: {sorted(unexpected_keys)}"
            )
        shell = str(arguments.get("shell", "") or "").strip().lower()
        if shell not in {"", "bash", "sh"}:
            raise AssertionError(f"refusing unsupported validation shell: {shell!r}")
        if "timeout" in arguments:
            timeout = arguments.get("timeout")
            if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 300:
                raise AssertionError(
                    f"refusing invalid validation-shell timeout: {timeout!r}"
                )
        raw_working_directory = str(
            arguments.get("working_directory", "") or ""
        ).strip()
        if raw_working_directory and not Path(raw_working_directory).is_absolute():
            raise AssertionError(
                "validation shell working_directory must be the absolute project workplace"
            )
        working_directory = (
            _path_in_workplace(raw_working_directory, workplace)
            if raw_working_directory
            else workplace.resolve()
        )
        if working_directory != workplace.resolve():
            raise AssertionError(
                "E2E validation shell must run from the project workplace root"
            )
        command = str(arguments.get("command", "") or "").strip()
        # The command is executed by a real shell.  Accept only the exact
        # validation grammar below; all unmodelled shell syntax fails closed.
        if (
            not command
            or "&" in command
            or any(
                marker in command
                for marker in (
                    "\n", "\r", ";", "||", "|", ">", "<", "`", "$",
                    "(", ")", "{", "}", "[", "]", "*", "?", "~", "!", "#",
                    "\\",
                )
            )
        ):
            raise AssertionError(f"refusing unmodelled shell syntax: {command!r}")
        investment_notes = {
            (workplace / "investment_case/company_analysis.json").resolve(),
            (workplace / "investment_case/risk_analysis.json").resolve(),
        }
        app_js = (workplace / "app_case/app.js").resolve()
        try:
            argv = shlex.split(command, posix=True)
        except ValueError as exc:
            raise AssertionError(
                f"refusing malformed shell quoting: {command!r}"
            ) from exc
        if (
            spec.case_id == "investment"
            and len(argv) == 4
            and argv[:3] == ["python3", "-m", "json.tool"]
            and _path_in_workplace(argv[3], working_directory) in investment_notes
        ):
            return
        if (
            spec.case_id == "app"
            and len(argv) == 3
            and argv[:2] == ["node", "--check"]
            and _path_in_workplace(argv[2], working_directory) == app_js
        ):
            return
        raise AssertionError(
            f"refusing non-whitelisted validation command: {command!r}"
        )

    raise AssertionError(
        f"{spec.case_id}: refusing unexpected permission request for tool {name!r}"
    )


def _modeled_tool_call_inputs_ready(
    spec: CaseSpec,
    workplace: Path,
    checkpoint: Any,
) -> bool:
    """Return false while an exact validation command's input is not durable yet.

    Company roles can run in parallel.  A QA role may therefore publish the
    exact, allowed validation command before its producer has atomically
    written the target artifact.  Keep that permission card pending and keep
    polling the company instead of approving a command that must fail.  An
    unexpected call is *not* deferred: it must be denied immediately.
    """

    payload = dict(getattr(checkpoint, "payload", {}) or {})
    tool_call = dict(payload.get("tool_call", {}) or {})
    try:
        _validate_test_tool_call(spec, workplace, tool_call)
    except AssertionError:
        return True
    if str(tool_call.get("name", "") or "").strip() != "shell_exec":
        return True
    arguments = dict(tool_call.get("arguments", {}) or {})
    command = str(arguments.get("command", "") or "").strip()
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        # The strict validator above already rejects malformed quoting.
        return True
    is_artifact_validation = (
        len(argv) == 4 and argv[:3] == ["python3", "-m", "json.tool"]
    ) or (len(argv) == 3 and argv[:2] == ["node", "--check"])
    if not is_artifact_validation:
        return True
    raw_working_directory = str(
        arguments.get("working_directory", "") or ""
    ).strip()
    working_directory = (
        _path_in_workplace(raw_working_directory, workplace)
        if raw_working_directory
        else workplace.resolve()
    )
    target = _path_in_workplace(argv[-1], working_directory)
    try:
        return target.is_file() and target.stat().st_size > 0
    except OSError:
        return False


def _required_exact_shell_commands(spec: CaseSpec) -> tuple[str, ...]:
    if spec.case_id == "investment":
        return (
            "python3 -m json.tool investment_case/company_analysis.json",
            "python3 -m json.tool investment_case/risk_analysis.json",
        )
    if spec.case_id == "app":
        return ("node --check app_case/app.js",)
    return ()


def _required_exact_shell_roles(spec: CaseSpec) -> dict[str, str]:
    if spec.case_id == "investment":
        return {
            "python3 -m json.tool investment_case/company_analysis.json": (
                "investment_analyst"
            ),
            "python3 -m json.tool investment_case/risk_analysis.json": (
                "risk_analyst"
            ),
        }
    if spec.case_id == "app":
        return {"node --check app_case/app.js": "qa_engineer"}
    return {}


def _required_exact_shell_role_pairs(
    spec: CaseSpec,
) -> frozenset[tuple[str, str]]:
    """Return the exact command/role pairs that must succeed for the case."""

    return frozenset(_required_exact_shell_roles(spec).items())


def _missing_required_exact_shell_role_pairs(
    spec: CaseSpec,
    approved_pairs: set[tuple[str, str]] | frozenset[tuple[str, str]],
) -> tuple[tuple[str, str], ...]:
    """Find required pairs without rejecting additional role revalidation."""

    return tuple(
        sorted(_required_exact_shell_role_pairs(spec) - set(approved_pairs))
    )


def _tool_call_signature(tool_call: dict[str, Any]) -> str:
    """Stable semantic key used only to bound repeated rejected requests."""

    encoded = json.dumps(
        {
            "name": str(tool_call.get("name", "") or "").strip(),
            "arguments": dict(tool_call.get("arguments", {}) or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _tool_rejection_feedback(
    spec: CaseSpec,
    tool_call: dict[str, Any],
    validation_reason: str,
) -> str:
    exact_commands = _required_exact_shell_commands(spec)
    command_hint = (
        "; ".join(f"`{command}`" for command in exact_commands)
        if exact_commands
        else "the exact tool calls stated in the task"
    )
    return (
        "Denied by the Issue #35 E2E safety policy because this is not an exact "
        f"modeled ToolCall ({validation_reason}). Retry as a new ToolCall. For "
        f"shell_exec, use only one listed standalone command: {command_hint}. "
        "Do not use wc, python -c, &&, pipes, redirects, command substitution, "
        "or any additional shell command; use file_read for inspection."
    )


def _decision_request_id(client_request_id: str, option_id: str) -> str:
    """Keep callers' IDs stable while giving harness decisions exact semantics."""

    value = str(client_request_id or "").strip()
    if value.startswith("issue35-e2e:"):
        prefix, separator, suffix = value.rpartition(":")
        if separator and suffix in {"approve_once", "deny"}:
            return f"{prefix}:{option_id}"
    return value


def _validate_permission_policy_without_execution(
    workplace: Path,
    *,
    safe_command_prefixes: list[str],
) -> dict[str, Any]:
    """Exercise the harness allow/deny boundary without tools, shell, or LLMs."""

    investment, app = CASES
    allowed = {
        "investment_child_note_write": (
            investment,
            {
                "name": "file_write",
                "arguments": {
                    "path": "investment_case/company_analysis.json",
                    "content": "dry-run only",
                },
            },
        ),
        "investment_child_note_check": (
            investment,
            {
                "name": "shell_exec",
                "arguments": {
                    "command": "python3 -m json.tool investment_case/company_analysis.json",
                    "working_directory": str(workplace),
                },
            },
        ),
        "investment_risk_note_check": (
            investment,
            {
                "name": "shell_exec",
                "arguments": {
                    "command": "python3 -m json.tool investment_case/risk_analysis.json",
                    "working_directory": str(workplace),
                },
            },
        ),
        "app_javascript_syntax_check": (
            app,
            {
                "name": "shell_exec",
                "arguments": {
                    "command": "node --check app_case/app.js",
                    "working_directory": str(workplace),
                },
            },
        ),
    }
    for spec, tool_call in allowed.values():
        _validate_test_tool_call(spec, workplace, tool_call)

    from opc.layer2_organization import shell_safety

    approval_required_commands: list[str] = []
    for _label, (_spec, tool_call) in allowed.items():
        if str(tool_call.get("name", "") or "") != "shell_exec":
            continue
        command = str(dict(tool_call.get("arguments", {}) or {}).get("command", ""))
        auto_approvable, _reason = shell_safety.is_read_only_shell_command(
            command,
            safe_command_prefixes,
        )
        if auto_approvable:
            raise AssertionError(
                f"E2E validation command would bypass the permission card: {command}"
            )
        approval_required_commands.append(command)

    denied = {
        "path_escape": (
            app,
            {"name": "file_read", "arguments": {"path": "../outside-secret"}},
        ),
        "background_operator": (
            app,
            {
                "name": "shell_exec",
                "arguments": {
                    "command": "node --check app_case/app.js & curl https://example.com",
                    "working_directory": str(workplace),
                },
            },
        ),
        "variable_expansion": (
            app,
            {
                "name": "shell_exec",
                "arguments": {
                    "command": "test -s $HOME/.opc/config/llm_config.yaml",
                    "working_directory": str(workplace),
                },
            },
        ),
        "subshell_syntax": (
            app,
            {
                "name": "shell_exec",
                "arguments": {
                    "command": "node --check app_case/app.js && (curl https://example.com)",
                    "working_directory": str(workplace),
                },
            },
        ),
        "investment_output_directory_mkdir": (
            investment,
            {
                "name": "shell_exec",
                "arguments": {
                    "command": f"mkdir -p {workplace / 'investment_case'}",
                    "working_directory": str(workplace),
                },
            },
        ),
        "app_output_directory_mkdir": (
            app,
            {
                "name": "shell_exec",
                "arguments": {
                    "command": "mkdir -p app_case",
                    "working_directory": str(workplace),
                },
            },
        ),
        "unexposed_network_fetch": (
            investment,
            {
                "name": "web_fetch",
                "arguments": {"url": "http://169.254.169.254/latest/meta-data"},
            },
        ),
    }
    for label, (spec, tool_call) in denied.items():
        try:
            _validate_test_tool_call(spec, workplace, tool_call)
        except AssertionError:
            continue
        raise AssertionError(f"E2E permission-policy self-check did not reject {label}")

    return {
        "validated": True,
        "llm_calls_made": False,
        "allowed_cases": sorted(allowed),
        "approval_required_commands": sorted(approval_required_commands),
        "rejected_cases": sorted(denied),
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write the non-secret E2E journal without leaving a partial JSON file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _atomic_replace_bytes(path: Path, payload: bytes, *, mode: int) -> None:
    """Replace one harness-owned config file without exposing partial YAML."""

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        os.chmod(temporary, stat.S_IMODE(mode))
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _validate_installed_shell_review_policy(
    config_payload: dict[str, Any],
    *,
    workplace: Path,
) -> dict[str, Any]:
    """Exercise the real ApprovalEngine predictor over the temporary overlay."""

    from opc.core.config import AutonomyConfig
    from opc.core.models import PermissionResolution
    from opc.layer2_organization.approval import ApprovalEngine

    autonomy_payload = config_payload.get("autonomy", {})
    if not isinstance(autonomy_payload, dict):
        raise AssertionError("E2E system_config autonomy payload must be a mapping")
    autonomy = AutonomyConfig.model_validate(autonomy_payload)
    if E2E_ALL_SHELL_REVIEW_PATTERN not in {
        str(item or "")
        for item in autonomy.permissions_v2.dangerous_shell_patterns
    }:
        raise AssertionError("E2E shell overlay lacks the anchored all-shell guard")

    # The anchored guard returns before the predictor consults Store-backed
    # grants.  A minimally constructed real policy is sufficient to prove the
    # exact synchronous decision used by NativeRuntimeV2's fast path.
    policy = object.__new__(ApprovalEngine)
    policy.config = autonomy
    policy._denial_counts = {}
    policy._recorded_denial_ids = set()
    policy._session_allowlist = {}
    policy.allowlist = None
    shell_tool = SimpleNamespace(
        name="shell_exec",
        requires_confirmation=False,
        read_only=False,
    )
    probe_task = SimpleNamespace(
        id="issue35-shell-overlay-probe",
        project_id="issue35-shell-overlay-probe",
        session_id="issue35-shell-overlay-probe",
        parent_session_id=None,
        assigned_to="probe",
        metadata={"target_output_dir": str(workplace)},
    )
    probes = {
        "unexpected_wc": "wc -l investment_case/company_analysis.json",
        "required_exact": (
            "python3 -m json.tool investment_case/company_analysis.json"
        ),
    }
    evidence: dict[str, Any] = {}
    for label, command in probes.items():
        decision = policy.predict(
            shell_tool,
            {
                "command": command,
                "working_directory": str(workplace),
            },
            task=probe_task,
        )
        resolution = getattr(decision.resolution, "value", str(decision.resolution))
        if (
            resolution != PermissionResolution.ASK.value
            or str(decision.source or "") != "shell_pattern"
        ):
            raise AssertionError(
                f"E2E all-shell guard did not route {label} through ASK: "
                f"resolution={resolution!r} source={decision.source!r}"
            )
        evidence[label] = {
            "command": command,
            "resolution": resolution,
            "source": str(decision.source or ""),
        }
    return {
        "installed": True,
        "allow_native_tool_auto_approval": (
            autonomy.allow_native_tool_auto_approval
        ),
        "all_shell_pattern": E2E_ALL_SHELL_REVIEW_PATTERN,
        "predictor_probes": evidence,
    }


async def _validate_installed_shell_async_authorization(
    config_dir: Path,
    *,
    workplace: Path,
) -> dict[str, Any]:
    """Prove configured shell matches enter the real async card boundary."""

    from opc.core.config import OPCConfig
    from opc.layer2_organization.approval import ApprovalEngine

    autonomy = OPCConfig.load(config_dir).autonomy
    policy = object.__new__(ApprovalEngine)
    policy.config = autonomy
    policy.interaction_coordinator = object()
    observed: list[dict[str, Any]] = []

    async def ask_user(
        self: Any,
        task: Any,
        action_kind: str,
        action_name: str,
        decision: Any,
        metadata: dict[str, Any],
    ) -> tuple[bool, Any]:
        del self, task
        observed.append(
            {
                "action_kind": action_kind,
                "action_name": action_name,
                "policy_source": str(decision.policy_source or ""),
                "command": str(
                    dict(metadata.get("arguments", {}) or {}).get(
                        "command",
                        "",
                    )
                    or ""
                ),
            }
        )
        return False, decision

    async def record(self: Any, *args: Any, **kwargs: Any) -> None:
        del self, args, kwargs

    policy._ask_user = MethodType(ask_user, policy)
    policy._record = MethodType(record, policy)
    task = SimpleNamespace(
        id="issue35-shell-overlay-async-probe",
        title="Issue 35 shell overlay async probe",
        project_id="issue35-shell-overlay-probe",
        session_id="issue35-shell-overlay-probe",
        parent_session_id=None,
        assigned_to="probe",
        metadata={"target_output_dir": str(workplace)},
    )
    probes = (
        "wc -l investment_case/company_analysis.json",
        "python3 -m json.tool investment_case/company_analysis.json",
    )
    for index, command in enumerate(probes):
        approved, decision = await policy.authorize_tool_call(
            task=task,
            tool_name="shell_exec",
            arguments={
                "command": command,
                "working_directory": str(workplace),
            },
            call_context={
                "id": f"issue35-shell-overlay-probe-{index}",
                "runtime_session_id": "rt_issue35_shell_overlay_probe",
            },
        )
        if approved or str(decision.policy_source or "") != "shell_pattern":
            raise AssertionError(
                "E2E shell overlay did not reach the async configured-pattern "
                f"card boundary for {command!r}"
            )
    if len(observed) != len(probes) or {
        item["command"] for item in observed
    } != set(probes):
        raise AssertionError(
            "E2E shell overlay async authorization did not request every probe"
        )
    return {
        "validated": True,
        "card_boundary_calls": observed,
    }


class _E2EShellReviewOverlay:
    """Process-crash-recoverable, atomically replaced E2E config overlay."""

    _LEGACY_JOURNAL_FIELDS = frozenset(
        {
            "version",
            "config_path",
            "original_base64",
            "original_sha256",
            "overlay_sha256",
            "original_mode",
        }
    )
    _JOURNAL_FIELDS = frozenset(
        {
            "version",
            "config_path",
            "original_base64",
            "original_sha256",
            "raw_overlay_base64",
            "raw_overlay_sha256",
            "normalized_overlay_base64",
            "normalized_overlay_sha256",
            "original_mode",
        }
    )

    def __init__(self, config_dir: Path, *, workplace: Path) -> None:
        self.path = config_dir / "system_config.yaml"
        self.journal_path = config_dir / ".issue35-e2e-shell-review-overlay.json"
        self.workplace = workplace
        self._original_bytes: bytes | None = None
        self._original_mode = 0
        self._installed = False

    @staticmethod
    def _digest(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """Reject ambiguous recovery journals with duplicate JSON members."""

        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(
                    f"duplicate JSON member in E2E overlay journal: {key!r}"
                )
            result[key] = value
        return result

    @staticmethod
    def _decode_journal_bytes(value: Any, *, label: str) -> bytes:
        if not isinstance(value, str):
            raise RuntimeError(f"invalid {label} bytes in E2E overlay journal")
        try:
            payload = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                f"invalid {label} bytes in E2E overlay journal"
            ) from exc
        if base64.b64encode(payload).decode("ascii") != value:
            raise RuntimeError(
                f"non-canonical {label} bytes in E2E overlay journal"
            )
        return payload

    @classmethod
    def _validate_journal_hash(
        cls,
        payload: bytes,
        value: Any,
        *,
        label: str,
    ) -> str:
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise RuntimeError(f"invalid {label} hash in E2E overlay journal")
        if cls._digest(payload) != value:
            raise RuntimeError(f"E2E overlay journal {label} hash mismatch")
        return value

    @staticmethod
    def _render_raw_overlay(original: bytes) -> tuple[dict[str, Any], bytes]:
        try:
            loaded = yaml.safe_load(original.decode("utf-8")) or {}
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise AssertionError("cannot parse E2E system config for shell overlay") from exc
        if not isinstance(loaded, dict):
            raise AssertionError("E2E system_config root must be a mapping")
        autonomy = loaded.setdefault("autonomy", {})
        if not isinstance(autonomy, dict):
            raise AssertionError("E2E system_config autonomy must be a mapping")
        permissions = autonomy.setdefault("permissions_v2", {})
        if not isinstance(permissions, dict):
            raise AssertionError(
                "E2E system_config autonomy.permissions_v2 must be a mapping"
            )
        raw_patterns = permissions.setdefault("dangerous_shell_patterns", [])
        if not isinstance(raw_patterns, list):
            raise AssertionError(
                "E2E dangerous_shell_patterns must be a list before overlay"
            )
        patterns = [str(item or "") for item in raw_patterns]
        if E2E_ALL_SHELL_REVIEW_PATTERN not in patterns:
            patterns.append(E2E_ALL_SHELL_REVIEW_PATTERN)
        permissions["dangerous_shell_patterns"] = patterns
        rendered = yaml.safe_dump(
            loaded,
            allow_unicode=True,
            sort_keys=False,
        ).encode("utf-8")
        return loaded, rendered

    @staticmethod
    def _render_normalized_overlay(config_payload: dict[str, Any]) -> bytes:
        """Render the exact ``OPCConfig.save`` system-config byte shape."""

        from opc.core.config import AutonomyConfig, CapabilityConfig, SystemConfig

        system_payload = config_payload.get("system", {})
        if isinstance(system_payload, dict):
            system_payload = dict(system_payload)
            if "mcp_servers" in config_payload:
                # OPCConfig.load folds the legacy top-level key into system
                # before model validation; save then emits it under system.
                system_payload["mcp_servers"] = config_payload["mcp_servers"]
        normalized = {
            "system": SystemConfig.model_validate(system_payload).model_dump(),
            "autonomy": AutonomyConfig.model_validate(
                config_payload.get("autonomy", {})
            ).model_dump(),
            "capabilities": CapabilityConfig.model_validate(
                config_payload.get("capabilities", {})
            ).model_dump(),
        }
        # OPCConfig.save delegates this exact payload to _atomic_write_yaml,
        # whose only serialization operation is this yaml.dump call.
        return yaml.dump(normalized, default_flow_style=False).encode("utf-8")

    @classmethod
    def _overlay_forms_from_original(
        cls,
        original: bytes,
    ) -> tuple[dict[str, Any], bytes, bytes]:
        config_payload, raw_overlay = cls._render_raw_overlay(original)
        normalized_overlay = cls._render_normalized_overlay(config_payload)
        return config_payload, raw_overlay, normalized_overlay

    def _read_journal(self) -> dict[str, Any]:
        try:
            journal = json.loads(
                self.journal_path.read_text(encoding="utf-8"),
                object_pairs_hook=self._strict_json_object,
            )
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(
                f"cannot read E2E shell overlay recovery journal: {self.journal_path}"
            ) from exc
        if not isinstance(journal, dict):
            raise RuntimeError("unsupported E2E shell overlay recovery journal")
        version = journal.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version not in {
            1,
            2,
        }:
            raise RuntimeError("unsupported E2E shell overlay recovery journal")
        expected_fields = (
            self._LEGACY_JOURNAL_FIELDS
            if version == 1
            else self._JOURNAL_FIELDS
        )
        if set(journal) != expected_fields:
            raise RuntimeError("incomplete E2E shell overlay recovery journal")
        if not isinstance(journal.get("config_path"), str) or journal["config_path"] != str(
            self.path
        ):
            raise RuntimeError(
                "E2E shell overlay recovery journal belongs to a different config path"
            )
        original_mode = journal.get("original_mode")
        if (
            isinstance(original_mode, bool)
            or not isinstance(original_mode, int)
            or original_mode < 0
            or original_mode > (0o177777 if version == 1 else 0o7777)
        ):
            raise RuntimeError("invalid original mode in E2E overlay journal")
        original = self._decode_journal_bytes(
            journal["original_base64"],
            label="original",
        )
        self._validate_journal_hash(
            original,
            journal["original_sha256"],
            label="original",
        )
        try:
            config_payload, reconstructed_raw, reconstructed_normalized = (
                self._overlay_forms_from_original(original)
            )
        except (AssertionError, ValueError, TypeError) as exc:
            raise RuntimeError(
                "cannot reconstruct E2E overlay from journaled original bytes"
            ) from exc

        if version == 1:
            # Run 29 may contain the v1 marker written immediately before this
            # protocol was introduced. Authenticate its deterministic raw
            # overlay against the old hash, then derive the sole canonical
            # OPCConfig.save form with today's serializer.
            self._validate_journal_hash(
                reconstructed_raw,
                journal["overlay_sha256"],
                label="raw overlay",
            )
            raw_overlay = reconstructed_raw
            normalized_overlay = reconstructed_normalized
        else:
            raw_overlay = self._decode_journal_bytes(
                journal["raw_overlay_base64"],
                label="raw overlay",
            )
            self._validate_journal_hash(
                raw_overlay,
                journal["raw_overlay_sha256"],
                label="raw overlay",
            )
            normalized_overlay = self._decode_journal_bytes(
                journal["normalized_overlay_base64"],
                label="normalized overlay",
            )
            self._validate_journal_hash(
                normalized_overlay,
                journal["normalized_overlay_sha256"],
                label="normalized overlay",
            )
            if raw_overlay != reconstructed_raw:
                raise RuntimeError(
                    "E2E overlay journal raw bytes do not match its original config"
                )
            if normalized_overlay != reconstructed_normalized:
                raise RuntimeError(
                    "E2E overlay journal normalized bytes do not match its raw overlay"
                )

        parsed = dict(journal)
        parsed["original_bytes"] = original
        parsed["raw_overlay_bytes"] = raw_overlay
        parsed["normalized_overlay_bytes"] = normalized_overlay
        parsed["config_payload"] = config_payload
        parsed["original_permission_mode"] = stat.S_IMODE(original_mode)
        return parsed

    def _clear_journal(self) -> None:
        self.journal_path.unlink()

    def _restore_original(self, journal: dict[str, Any]) -> None:
        original = bytes(journal["original_bytes"])
        mode = int(journal["original_permission_mode"])
        _atomic_replace_bytes(self.path, original, mode=mode)
        if (
            self.path.read_bytes() != original
            or stat.S_IMODE(self.path.stat().st_mode) != mode
        ):
            raise RuntimeError("E2E shell overlay recovery verification failed")
        self._clear_journal()

    def _recover_interrupted_install(self) -> None:
        if not self.journal_path.exists():
            return
        if not self.path.is_file():
            raise RuntimeError(
                "E2E shell overlay recovery found a missing system_config.yaml"
            )
        journal = self._read_journal()
        current = self.path.read_bytes()
        original = bytes(journal["original_bytes"])
        raw_overlay = bytes(journal["raw_overlay_bytes"])
        normalized_overlay = bytes(journal["normalized_overlay_bytes"])
        if current == original:
            # The process stopped after journaling but before atomic replace.
            self._restore_original(journal)
            return
        if current not in {raw_overlay, normalized_overlay}:
            current_hash = self._digest(current)
            raise RuntimeError(
                "E2E shell overlay recovery refused to overwrite externally "
                f"modified config: current_sha256={current_hash} "
                f"original_sha256={self._digest(original)} "
                f"raw_overlay_sha256={self._digest(raw_overlay)} "
                f"normalized_overlay_sha256={self._digest(normalized_overlay)}"
            )
        self._restore_original(journal)

    def install(self) -> dict[str, Any]:
        if self._installed:
            raise RuntimeError("E2E shell review overlay is already installed")
        self._recover_interrupted_install()
        if not self.path.is_file():
            raise FileNotFoundError(
                f"missing E2E system config for shell overlay: {self.path}"
            )
        original = self.path.read_bytes()
        try:
            loaded, rendered, normalized = self._overlay_forms_from_original(original)
        except AssertionError as exc:
            raise AssertionError(
                f"cannot construct E2E shell overlay from: {self.path}"
            ) from exc
        self._original_bytes = original
        self._original_mode = stat.S_IMODE(self.path.stat().st_mode)
        journal = {
            "version": 2,
            "config_path": str(self.path),
            "original_base64": base64.b64encode(original).decode("ascii"),
            "original_sha256": self._digest(original),
            "raw_overlay_base64": base64.b64encode(rendered).decode("ascii"),
            "raw_overlay_sha256": self._digest(rendered),
            "normalized_overlay_base64": base64.b64encode(normalized).decode("ascii"),
            "normalized_overlay_sha256": self._digest(normalized),
            "original_mode": self._original_mode,
        }
        _atomic_replace_bytes(
            self.journal_path,
            (
                json.dumps(journal, ensure_ascii=False, sort_keys=True) + "\n"
            ).encode("utf-8"),
            mode=0o600,
        )
        _atomic_replace_bytes(
            self.path,
            rendered,
            mode=self._original_mode,
        )
        self._installed = True
        try:
            return _validate_installed_shell_review_policy(
                loaded,
                workplace=self.workplace,
            )
        except BaseException as primary:
            try:
                self.restore()
            except BaseException as secondary:
                primary.add_note(
                    "Secondary E2E shell overlay restore failure during install: "
                    f"{type(secondary).__name__}: {secondary}"
                )
            raise

    def restore(self) -> None:
        if not self._installed:
            return
        original = self._original_bytes
        if original is None:
            raise RuntimeError("E2E shell overlay lost its original config bytes")
        journal = self._read_journal()
        current = self.path.read_bytes()
        raw_overlay = bytes(journal["raw_overlay_bytes"])
        normalized_overlay = bytes(journal["normalized_overlay_bytes"])
        if current not in {raw_overlay, normalized_overlay}:
            raise RuntimeError(
                "E2E shell overlay restore refused to overwrite externally "
                f"modified config: current_sha256={self._digest(current)} "
                f"raw_overlay_sha256={self._digest(raw_overlay)} "
                f"normalized_overlay_sha256={self._digest(normalized_overlay)}"
            )
        if bytes(journal["original_bytes"]) != original:
            raise RuntimeError("E2E shell overlay in-memory/journal original mismatch")
        if int(journal["original_permission_mode"]) != self._original_mode:
            raise RuntimeError("E2E shell overlay in-memory/journal mode mismatch")
        self._restore_original(journal)
        self._installed = False


def _checkpoint_root_session(checkpoint: Any, task: Any | None) -> str:
    payload = dict(getattr(checkpoint, "payload", {}) or {})
    interaction = dict(payload.get("interaction", {}) or {})
    ownership = dict(interaction.get("ownership", {}) or {})
    metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
    return str(
        ownership.get("root_session_id")
        or ownership.get("company_runtime_session_id")
        or ownership.get("ui_anchor_session_id")
        or metadata.get("company_runtime_root_session_id")
        or getattr(task, "parent_session_id", "")
        or payload.get("parent_session_id")
        or getattr(checkpoint, "session_id", "")
        or ""
    ).strip()


def _checkpoint_options(checkpoint: Any) -> set[str]:
    payload = dict(getattr(checkpoint, "payload", {}) or {})
    interaction = dict(payload.get("interaction", {}) or {})
    return {
        str(option.get("id", "") or "").strip()
        for option in list(interaction.get("options", []) or [])
        if isinstance(option, dict) and str(option.get("id", "") or "").strip()
    }


def _receipt_payload(receipt: Any) -> dict[str, Any]:
    if isinstance(receipt, dict):
        raw = dict(receipt)
    elif is_dataclass(receipt):
        raw = asdict(receipt)
    else:
        raw = {
            key: getattr(receipt, key)
            for key in (
                "accepted",
                "acknowledged",
                "deduplicated",
                "duplicate",
                "status",
                "outcome",
                "reason",
                "checkpoint_id",
                "checkpoint_type",
            )
            if hasattr(receipt, key)
        }
    checkpoint = raw.get("checkpoint")
    if checkpoint is not None:
        raw["checkpoint"] = {
            "checkpoint_id": str(getattr(checkpoint, "checkpoint_id", "") or ""),
            "checkpoint_type": str(getattr(checkpoint, "checkpoint_type", "") or ""),
            "status": str(getattr(checkpoint, "status", "") or ""),
        }
    return raw


def _receipt_acknowledged(receipt: dict[str, Any]) -> bool:
    outcome = str(receipt.get("outcome", "") or "").strip().lower()
    return bool(receipt.get("accepted") or receipt.get("acknowledged")) or outcome in {
        "accepted",
        "duplicate",
    }


def _canonical_interaction_decision_hash(decision: dict[str, Any]) -> str:
    """Mirror the durable interaction protocol's canonical decision hash.

    Keep this tiny primitive local to the standalone harness.  Importing the
    full coordinator just to hash a fixture makes offline validation depend on
    every coordinator type annotation and optional runtime dependency.
    """

    encoded = json.dumps(
        dict(decision or {}),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_exact_resumed_staffing_outcome_unknown(
    run: CaseRun,
    checkpoint: Any,
) -> bool:
    """Recognize only the product's fail-closed staffing crash-recovery shape.

    A staffing decision crosses a non-idempotent effect boundary before it
    starts the company dispatcher.  If that process dies, interaction
    recovery deliberately terminalizes the expired executing claim as
    ``outcome_unknown`` instead of replaying staffing and bootstrapping a
    second DelegationRun.  The already-created company run is resumed through
    its separate ``company_runtime_interrupted`` checkpoint.

    This exception is intentionally narrow: a fresh run, an unjournaled
    decision, a different request, or an incomplete execution/completion
    envelope remains a hard failure.
    """

    if (
        not run.resume_existing
        or str(getattr(checkpoint, "checkpoint_type", "") or "").strip()
        != STAFFING_CHECKPOINT_TYPE
        or str(getattr(checkpoint, "status", "") or "").strip()
        != "outcome_unknown"
        or str(getattr(checkpoint, "session_id", "") or "").strip()
        != run.session_id
        or len(run.staffing_decisions) != 1
    ):
        return False

    checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "").strip()
    expected_request_id = (
        f"issue35-e2e:{run.session_id}:{checkpoint_id}:native-staffing"
    )
    submission = dict(run.staffing_decisions[0] or {})
    receipt = dict(submission.get("receipt", {}) or {})
    if (
        str(submission.get("checkpoint_id", "") or "").strip()
        != checkpoint_id
        or str(submission.get("checkpoint_type", "") or "").strip()
        != STAFFING_CHECKPOINT_TYPE
        or str(submission.get("client_request_id", "") or "").strip()
        != expected_request_id
        or str(submission.get("root_session_id", "") or "").strip()
        != run.session_id
        or str(submission.get("company_profile", "") or "").strip()
        != "custom"
        or str(submission.get("org_id", "") or "").strip()
        != run.spec.org_id
        or submission.get("staffing_action") != "manual_approve"
        or submission.get("recruitment_agent") != "native"
        or not bool(receipt.get("accepted") or receipt.get("acknowledged"))
        or str(receipt.get("status", "") or "").strip() != "answered"
        or str(receipt.get("checkpoint_id", "") or "").strip()
        != checkpoint_id
        or str(receipt.get("checkpoint_type", "") or "").strip()
        != STAFFING_CHECKPOINT_TYPE
    ):
        return False

    expected_roles = {str(role["id"]) for role in run.spec.roles}
    submission_role_agents = dict(
        submission.get("recruitment_role_agents", {}) or {}
    )
    submission_selections = dict(submission.get("staffing_selections", {}) or {})
    if (
        set(submission_role_agents) != expected_roles
        or set(submission_role_agents.values()) != {"native"}
        or set(submission_selections) != expected_roles
        or any(
            dict(selection or {}) != {"kind": "fallback", "id": ""}
            for selection in submission_selections.values()
        )
    ):
        return False

    payload = dict(getattr(checkpoint, "payload", {}) or {})
    interaction = dict(payload.get("interaction", {}) or {})
    decision = dict(interaction.get("decision", {}) or {})
    decision_value = dict(decision.get("value", {}) or {})
    claim = dict(interaction.get("claim", {}) or {})
    execution = dict(interaction.get("execution", {}) or {})
    completion = dict(interaction.get("completion", {}) or {})
    claim_id = str(claim.get("claim_id", "") or "").strip()
    consumer_id = str(claim.get("consumer_id", "") or "").strip()
    try:
        canonical_decision_hash = _canonical_interaction_decision_hash(
            decision_value
        )
        claimed_at = datetime.fromisoformat(str(claim["claimed_at"]))
        lease_expires_at = datetime.fromisoformat(
            str(claim["lease_expires_at"])
        )
        execution_started_at = datetime.fromisoformat(
            str(execution["started_at"])
        )
        execution_detected_at = datetime.fromisoformat(
            str(execution["detected_at"])
        )
        completion_finished_at = datetime.fromisoformat(
            str(completion["finished_at"])
        )
        timestamps_are_ordered = (
            claimed_at.timestamp()
            <= lease_expires_at.timestamp()
            <= execution_detected_at.timestamp()
            <= completion_finished_at.timestamp()
            and claimed_at.timestamp()
            <= execution_started_at.timestamp()
            <= execution_detected_at.timestamp()
        )
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        return False
    return bool(
        str(payload.get("company_profile", "") or "").strip() == "custom"
        and str(payload.get("org_id", "") or "").strip() == run.spec.org_id
        and str(payload.get("primary_session_id", "") or "").strip()
        == run.session_id
        and
        str(interaction.get("kind", "") or "").strip()
        == STAFFING_CHECKPOINT_TYPE
        and str(interaction.get("domain_key", "") or "").strip()
        and dict(interaction.get("execution_scope", {}) or {})
        == {"company_profile": "custom", "org_id": run.spec.org_id}
        and str(decision.get("request_id", "") or "").strip()
        == expected_request_id
        and str(decision.get("decision_hash", "") or "").strip()
        == canonical_decision_hash
        and decision_value == {
            "staffing_action": "manual_approve",
            "staffing_selections": submission_selections,
            "recruitment_role_agents": submission_role_agents,
            "recruitment_agent": "native",
            "text": "approve",
        }
        and claim_id
        and consumer_id
        and str(execution.get("state", "") or "").strip()
        == "outcome_unknown"
        and str(execution.get("reason", "") or "").strip()
        == "execution_lease_expired"
        and str(execution.get("claim_id", "") or "").strip() == claim_id
        and str(execution.get("consumer_id", "") or "").strip()
        == consumer_id
        and str(completion.get("claim_id", "") or "").strip() == claim_id
        and str(completion.get("consumer_id", "") or "").strip()
        == consumer_id
        and str(completion.get("final_status", "") or "").strip()
        == "outcome_unknown"
        and timestamps_are_ordered
    )


def _staffing_checkpoint_is_accepted(run: CaseRun, checkpoint: Any) -> bool:
    return str(getattr(checkpoint, "status", "") or "").strip() == "resolved" or (
        _is_exact_resumed_staffing_outcome_unknown(run, checkpoint)
    )


async def _resumed_staffing_runtime_recovery_evidence(
    store: Any,
    run: CaseRun,
    checkpoint: Any,
    *,
    project_id: str,
    expected_checkpoint_id: str = "",
    expected_delegation_run_id: str = "",
    require_resolved: bool = False,
) -> dict[str, Any] | None:
    """Bind an unknown staffing effect to one exact interrupted company run.

    Before a resume message is sent, callers omit ``expected_checkpoint_id``;
    the function then requires exactly one active interruption card and returns
    its stable identity.  Final evidence passes that recorded identity with
    ``require_resolved=True``.  Resolved historical interruption cards are
    allowed, but can never stand in for the card selected by this invocation.
    """

    if not _is_exact_resumed_staffing_outcome_unknown(run, checkpoint):
        return None

    list_runs = getattr(store, "list_delegation_runs", None)
    if not callable(list_runs):
        raise RuntimeError(
            "staffing recovery evidence requires DelegationRun persistence"
        )
    delegation_runs = list(
        await list_runs(project_id=project_id, session_id=run.session_id)
    )
    if len(delegation_runs) != 1:
        raise AssertionError(
            f"{run.spec.case_id}: recovered staffing must bind to exactly one "
            f"DelegationRun; got {len(delegation_runs)}"
        )
    delegation_run = delegation_runs[0]
    delegation_run_id = str(
        getattr(delegation_run, "run_id", "") or ""
    ).strip()
    if (
        expected_delegation_run_id
        and delegation_run_id != expected_delegation_run_id
    ):
        raise AssertionError(
            f"{run.spec.case_id}: recorded staffing recovery DelegationRun "
            "identity drifted"
        )
    run_metadata = dict(getattr(delegation_run, "metadata", {}) or {})
    runtime_spec = dict(run_metadata.get("runtime_spec", {}) or {})
    runtime_spec_metadata = dict(runtime_spec.get("metadata", {}) or {})
    recovery_pointer = dict(
        getattr(delegation_run, "recovery_pointer", {}) or {}
    )
    staffing_payload = dict(getattr(checkpoint, "payload", {}) or {})
    staffing_interaction = dict(
        staffing_payload.get("interaction", {}) or {}
    )
    staffing_claim = dict(staffing_interaction.get("claim", {}) or {})
    staffing_claim_id = str(
        staffing_claim.get("claim_id", "") or ""
    ).strip()
    staffing_consumer_id = str(
        staffing_claim.get("consumer_id", "") or ""
    ).strip()
    expected_origin = {
        "checkpoint_id": str(
            getattr(checkpoint, "checkpoint_id", "") or ""
        ).strip(),
        "checkpoint_type": STAFFING_CHECKPOINT_TYPE,
        "project_id": project_id,
        "claim_id": staffing_claim_id,
        "consumer_id": staffing_consumer_id,
    }
    expected_lifecycle_statuses = (
        {"awaiting_owner"}
        if require_resolved
        else {"active", "awaiting_owner"}
    )
    if (
        not delegation_run_id
        or str(getattr(delegation_run, "project_id", "") or "").strip()
        != project_id
        or str(getattr(delegation_run, "session_id", "") or "").strip()
        != run.session_id
        or str(
            getattr(delegation_run, "company_profile", "") or ""
        ).strip()
        != "custom"
        or str(
            getattr(delegation_run, "execution_model", "") or ""
        ).strip()
        != "actor_runtime"
        or str(
            getattr(delegation_run, "final_decider_role_id", "") or ""
        ).strip()
        != str(run.spec.roles[0]["id"])
        or str(getattr(delegation_run, "status", "") or "").strip()
        != "running"
        or str(
            getattr(delegation_run, "lifecycle_status", "") or ""
        ).strip()
        not in expected_lifecycle_statuses
        or str(runtime_spec_metadata.get("organization_id", "") or "").strip()
        != run.spec.org_id
        or dict(run_metadata.get("origin_owner_interaction", {}) or {})
        != expected_origin
        or not staffing_claim_id
        or not staffing_consumer_id
        or str(recovery_pointer.get("project_id", "") or "").strip()
        != project_id
        or str(recovery_pointer.get("session_id", "") or "").strip()
        != run.session_id
    ):
        raise AssertionError(
            f"{run.spec.case_id}: recovered DelegationRun/origin scope drifted"
        )
    if not expected_checkpoint_id and str(
        getattr(delegation_run, "controller_owner_token", "") or ""
    ).strip():
        raise AssertionError(
            f"{run.spec.case_id}: recovered DelegationRun already has a live "
            "controller before interruption resume"
        )

    interrupted_rows = await _find_case_checkpoints(
        store,
        project_id=project_id,
        session_id=run.session_id,
        checkpoint_types=("company_runtime_interrupted",),
        statuses=None,
    )
    active_interrupted_rows = [
        row
        for row in interrupted_rows
        if str(getattr(row, "status", "") or "").strip()
        in {"pending", "resuming"}
    ]
    if expected_checkpoint_id:
        matching_rows = [
            row
            for row in interrupted_rows
            if str(getattr(row, "checkpoint_id", "") or "").strip()
            == expected_checkpoint_id
        ]
        if len(matching_rows) != 1:
            raise AssertionError(
                f"{run.spec.case_id}: recorded company runtime recovery "
                f"checkpoint {expected_checkpoint_id!r} is missing or duplicated"
            )
        interrupted = matching_rows[0]
        if require_resolved and active_interrupted_rows:
            raise AssertionError(
                f"{run.spec.case_id}: final evidence still has an active "
                "company_runtime_interrupted checkpoint"
            )
    else:
        if len(active_interrupted_rows) != 1:
            raise AssertionError(
                f"{run.spec.case_id}: recovered staffing requires exactly one "
                "active company_runtime_interrupted checkpoint; "
                f"got {len(active_interrupted_rows)}"
            )
        interrupted = active_interrupted_rows[0]
        if str(getattr(interrupted, "status", "") or "").strip() != "pending":
            raise AssertionError(
                f"{run.spec.case_id}: company runtime recovery is already "
                "resuming; refusing to start a second resume effect"
            )

    interrupted_status = str(
        getattr(interrupted, "status", "") or ""
    ).strip()
    allowed_statuses = {"resolved"} if require_resolved else {
        "pending",
        "resuming",
    }
    if interrupted_status not in allowed_statuses:
        raise AssertionError(
            f"{run.spec.case_id}: recorded company runtime recovery checkpoint "
            f"has status {interrupted_status!r}; expected "
            f"{sorted(allowed_statuses)}"
        )
    interrupted_payload = dict(getattr(interrupted, "payload", {}) or {})
    interrupted_task_id = str(
        getattr(interrupted, "task_id", "") or ""
    ).strip()
    interrupted_task = (
        await store.get_task(interrupted_task_id) if interrupted_task_id else None
    )
    interrupted_task_metadata = dict(
        getattr(interrupted_task, "metadata", {}) or {}
    )
    root_work_item_id = str(
        run_metadata.get("root_work_item_id", "") or ""
    ).strip()
    list_work_items = getattr(store, "list_delegation_work_items", None)
    if not root_work_item_id or not callable(list_work_items):
        raise RuntimeError(
            "staffing recovery evidence requires the durable root WorkItem"
        )
    work_items = list(await list_work_items(delegation_run_id))
    root_work_items = [
        item
        for item in work_items
        if str(getattr(item, "work_item_id", "") or "").strip()
        == root_work_item_id
    ]
    root_work_item_metadata = (
        dict(getattr(root_work_items[0], "metadata", {}) or {})
        if len(root_work_items) == 1
        else {}
    )
    if (
        str(getattr(interrupted, "project_id", "") or "").strip()
        != project_id
        or str(
            getattr(interrupted, "checkpoint_type", "") or ""
        ).strip()
        != "company_runtime_interrupted"
        or str(getattr(interrupted, "session_id", "") or "").strip()
        != run.session_id
        or interrupted_payload.get("checkpoint_type")
        != "company_runtime_interrupted"
        or int(interrupted_payload.get("version", 0) or 0) != 2
        or str(interrupted_payload.get("reason", "") or "").strip()
        != "startup_recovery"
        or str(interrupted_payload.get("project_id", "") or "").strip()
        != project_id
        or str(interrupted_payload.get("run_id", "") or "").strip()
        != delegation_run_id
        or str(interrupted_payload.get("company_profile", "") or "").strip()
        != "custom"
        or str(interrupted_payload.get("parent_session_id", "") or "").strip()
        != run.session_id
        or str(interrupted_payload.get("session_id", "") or "").strip()
        != run.session_id
        or (
            interrupted_payload.get("root_session_id") is not None
            and str(
                interrupted_payload.get("root_session_id", "") or ""
            ).strip()
            != run.session_id
        )
        or not interrupted_task_id
        or interrupted_task is None
        or str(interrupted_payload.get("origin_task_id", "") or "").strip()
        != interrupted_task_id
        or interrupted_task_id
        not in {
            str(task_id or "").strip()
            for task_id in list(interrupted_payload.get("task_ids", []) or [])
        }
        or str(getattr(interrupted_task, "project_id", "") or "").strip()
        != project_id
        or str(
            getattr(interrupted_task, "parent_session_id", "") or ""
        ).strip()
        != run.session_id
        or str(
            interrupted_task_metadata.get("delegation_run_id", "") or ""
        ).strip()
        != delegation_run_id
        or dict(
            interrupted_task_metadata.get("origin_owner_interaction", {}) or {}
        )
        != expected_origin
        or len(root_work_items) != 1
        or dict(
            root_work_item_metadata.get("origin_owner_interaction", {}) or {}
        )
        != expected_origin
        or _checkpoint_root_session(interrupted, interrupted_task)
        != run.session_id
        or not str(interrupted_payload.get("basis_hash", "") or "").strip()
    ):
        raise AssertionError(
            f"{run.spec.case_id}: company_runtime_interrupted scope does not "
            "bind exactly to the recovered DelegationRun/root session"
        )

    if require_resolved:
        try:
            suspend_started_at = datetime.fromisoformat(
                str(interrupted_payload["suspend_started_at"])
            )
            suspend_finalized_at = datetime.fromisoformat(
                str(interrupted_payload["suspend_finalized_at"])
            )
            resume_handoff_at = datetime.fromisoformat(
                str(interrupted_payload["resume_handoff_at"])
            )
            resume_resolved_at = datetime.fromisoformat(
                str(interrupted_payload["resume_resolved_at"])
            )
            resume_started_at = datetime.fromisoformat(
                str(interrupted_payload["resume_started_at"])
            )
            resume_generation = int(
                interrupted_payload["resume_controller_lease_generation"]
            )
        except (KeyError, TypeError, ValueError, OSError, OverflowError) as exc:
            raise AssertionError(
                f"{run.spec.case_id}: resolved runtime recovery lacks canonical "
                "resume timestamps"
            ) from exc
        if (
            str(interrupted_payload.get("resume_state", "") or "").strip()
            != "handoff_complete"
            or not (
                suspend_started_at.timestamp()
                <= suspend_finalized_at.timestamp()
                <= resume_started_at.timestamp()
                <= resume_handoff_at.timestamp()
                <= resume_resolved_at.timestamp()
            )
            or resume_generation <= 0
            or str(
                interrupted_payload.get("ui_anchor_task_id", "") or ""
            ).strip()
            != run.ui_anchor_task_id
        ):
            raise AssertionError(
                f"{run.spec.case_id}: resolved runtime recovery handoff drifted"
            )

    return {
        "delegation_run_id": delegation_run_id,
        "delegation_run_session_id": run.session_id,
        "checkpoint_id": str(
            getattr(interrupted, "checkpoint_id", "") or ""
        ).strip(),
        "checkpoint_type": "company_runtime_interrupted",
        "checkpoint_status": interrupted_status,
        "origin_task_id": interrupted_task_id,
        "project_id": project_id,
        "root_session_id": run.session_id,
        "staffing_checkpoint_id": expected_origin["checkpoint_id"],
        "staffing_claim_id": staffing_claim_id,
        "staffing_consumer_id": staffing_consumer_id,
    }


def _org_payload(spec: CaseSpec) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "kind": "opc_org_architecture",
        "organization_id": spec.org_id,
        "organization_name": spec.organization_name,
        "company": {
            "name": spec.organization_name,
            "topology": "Three-role issue #35 E2E organization",
            "company_profile": "custom",
            "execution_model": "actor_runtime",
            "final_decider_role_id": spec.roles[0]["id"],
            "company_profiles": ["corporate", "custom"],
        },
        "roles": [dict(role) for role in spec.roles],
        "employees": [],
        "escalation_rules": [],
        "runtime_policies": {},
        "talent_templates": [],
        "teams": [],
        "team_runtime": {},
        "installed_packages": [],
        "role_serial_queue_enabled": True,
        "metadata": {"source": "issue35_company_e2e"},
    }


def _validate_setup_without_execution(
    opc_home: Path,
    *,
    project_id: str,
) -> dict[str, Any]:
    """Validate the isolated project and both native orgs without LLM calls.

    This deliberately validates organization payloads in memory.  A dry run
    must not create or replace saved organizations merely to prove that the
    harness is pointed at the right project.
    """

    opc_home = opc_home.expanduser().resolve()
    if opc_home.name != ".opc":
        raise AssertionError(f"OPC_HOME must name a .opc directory: {opc_home}")
    config_dir = opc_home / "config"
    if not config_dir.is_dir():
        raise FileNotFoundError(f"missing test config directory: {config_dir}")
    if not (config_dir / "llm_config.yaml").is_file():
        raise FileNotFoundError(f"missing real LLM config: {config_dir / 'llm_config.yaml'}")

    project_root = opc_home.parent
    if not (project_root / "pyproject.toml").is_file():
        raise AssertionError(
            f"OPC_HOME must be .opc under a test project root with pyproject.toml: {opc_home}"
        )

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from opc.core.config import OPCConfig, get_project_workplace
    from opc.core.org_config import (
        apply_org_config_payload_to_config,
        validate_runnable_org_config,
    )
    from opc.layer5_memory.approval_allowlist import ApprovalAllowlistManager
    from opc.project_id import is_valid_project_id

    if not is_valid_project_id(project_id):
        raise AssertionError(f"invalid E2E project ID: {project_id!r}")

    base = OPCConfig.load(config_dir)
    if not bool(base.autonomy.enabled):
        raise AssertionError("E2E config must enable the autonomy approval policy")
    if not bool(base.autonomy.tool_first_use_approval):
        raise AssertionError(
            "E2E config must require first-use tool approval to exercise issue #35"
        )
    if not bool(base.autonomy.permissions_v2.enabled):
        raise AssertionError("E2E config must enable deterministic tool permissions v2")
    allow_tools = {
        str(name or "").strip() for name in base.autonomy.permissions_v2.allow_tools
    }
    deny_tools = {
        str(name or "").strip() for name in base.autonomy.permissions_v2.deny_tools
    }
    exemptions = {
        str(name or "").strip() for name in base.autonomy.tool_approval_exemptions
    }
    if "shell_exec" in allow_tools | deny_tools | exemptions:
        raise AssertionError(
            "E2E config must route shell_exec through normal permission prediction"
        )
    allowlist = ApprovalAllowlistManager(opc_home)
    if allowlist.list_patterns("tool", "shell_exec", project_id=project_id):
        raise AssertionError(
            "E2E project/global shell allowlist must be empty so permission cards are real"
        )
    organizations: list[dict[str, Any]] = []
    for spec in CASES:
        loaded = apply_org_config_payload_to_config(base, _org_payload(spec))
        validate_runnable_org_config(loaded, organization_id=spec.org_id)
        roles = list(loaded.org.roles or [])
        role_evidence = [
            {
                "role_id": role.id,
                "execution_strategy": role.runtime_policy.execution_strategy,
                "preferred_external_agent": role.preferred_external_agent,
            }
            for role in roles
        ]
        if len(role_evidence) != 3:
            raise AssertionError(
                f"{spec.org_id}: expected 3 roles, got {len(role_evidence)}"
            )
        if any(item["execution_strategy"] != "native" for item in role_evidence):
            raise AssertionError(f"{spec.org_id}: every role must use native execution")
        if any(item["preferred_external_agent"] for item in role_evidence):
            raise AssertionError(
                f"{spec.org_id}: external-agent preference leaked into E2E org"
            )
        organizations.append(
            {
                "organization_id": spec.org_id,
                "company_profile": loaded.org.company_profile,
                "execution_model": loaded.org.execution_model,
                "final_decider_role_id": loaded.org.final_decider_role_id,
                "roles": role_evidence,
            }
        )

    previous_cwd = Path.cwd()
    try:
        os.chdir(project_root)
        workplace = get_project_workplace(project_id).resolve()
    finally:
        os.chdir(previous_cwd)
    expected_workplace = (
        project_root.parent / f"{project_root.name}_workplace" / project_id
    ).resolve()
    if workplace != expected_workplace:
        raise AssertionError(
            f"workplace resolver mismatch: expected {expected_workplace}, got {workplace}"
        )
    permission_policy = _validate_permission_policy_without_execution(
        workplace,
        safe_command_prefixes=list(base.autonomy.safe_command_prefixes),
    )

    return {
        "validated": True,
        "llm_calls_made": False,
        "opc_home": str(opc_home),
        "project_root": str(project_root),
        "project_id": project_id,
        "workplace": str(workplace),
        "approval_policy": {
            "autonomy_enabled": True,
            "tool_first_use_approval": True,
            "permissions_v2_enabled": True,
            "shell_allowlist_empty": True,
        },
        "permission_policy_self_check": permission_policy,
        "organizations": organizations,
    }


def _prepare_saved_orgs(opc_home: Path) -> list[dict[str, Any]]:
    from opc.core.org_config import (
        apply_org_config_payload_to_config,
        load_org_config_payload,
        validate_runnable_org_config,
        write_org_config_payload,
    )
    from opc.core.config import OPCConfig

    config_dir = opc_home / "config"
    base = OPCConfig.load(config_dir)
    evidence: list[dict[str, Any]] = []
    for spec in CASES:
        path = write_org_config_payload(config_dir, spec.org_id, _org_payload(spec))
        loaded_payload, loaded_path = load_org_config_payload(config_dir, spec.org_id)
        loaded = apply_org_config_payload_to_config(
            base,
            loaded_payload,
            source_path=loaded_path,
        )
        validate_runnable_org_config(loaded, organization_id=spec.org_id)
        roles = list(loaded.org.roles or [])
        role_evidence = [
            {
                "role_id": role.id,
                "execution_strategy": role.runtime_policy.execution_strategy,
                "preferred_external_agent": role.preferred_external_agent,
            }
            for role in roles
        ]
        if len(role_evidence) != 3:
            raise AssertionError(f"{spec.org_id}: expected 3 roles, got {len(role_evidence)}")
        if any(item["execution_strategy"] != "native" for item in role_evidence):
            raise AssertionError(f"{spec.org_id}: every role must use native execution")
        if any(item["preferred_external_agent"] for item in role_evidence):
            raise AssertionError(f"{spec.org_id}: external-agent preference leaked into E2E org")
        evidence.append(
            {
                "organization_id": spec.org_id,
                "company_profile": loaded.org.company_profile,
                "execution_model": loaded.org.execution_model,
                "final_decider_role_id": loaded.org.final_decider_role_id,
                "organization_config_file": str(loaded.org.organization_config_file),
                "saved_path": str(path),
                "saved_sha256": _sha256(path),
                "roles": role_evidence,
            }
        )
    return evidence


async def _find_case_checkpoints(
    store: Any,
    *,
    project_id: str,
    session_id: str,
    checkpoint_types: tuple[str, ...],
    statuses: tuple[str, ...] | None,
) -> list[Any]:
    checkpoints = await store.get_execution_checkpoints(
        project_id=project_id,
        checkpoint_types=list(checkpoint_types),
        statuses=list(statuses or ()),
    )
    matched: list[Any] = []
    for checkpoint in checkpoints:
        task = await store.get_task(checkpoint.task_id) if checkpoint.task_id else None
        if _checkpoint_root_session(checkpoint, task) == session_id:
            matched.append(checkpoint)
    return matched


async def _single_pending_final_feedback(
    store: Any,
    *,
    project_id: str,
    session_id: str,
    case_id: str,
    allow_none: bool,
) -> Any | None:
    """Fail closed on duplicate active final-review cards in one owner scope."""

    rows = await _find_case_checkpoints(
        store,
        project_id=project_id,
        session_id=session_id,
        checkpoint_types=(FINAL_CHECKPOINT_TYPE,),
        statuses=None,
    )
    active = [
        checkpoint
        for checkpoint in rows
        if str(checkpoint.status or "").strip()
        in ACTIVE_OWNER_CHECKPOINT_STATUSES
    ]
    pending = [
        checkpoint
        for checkpoint in active
        if str(checkpoint.status or "").strip() == "pending"
    ]
    if len(active) > 1 or len(pending) > 1:
        raise AssertionError(
            f"{case_id}: expected at most one active pending "
            f"{FINAL_CHECKPOINT_TYPE}; active="
            f"{[(item.checkpoint_id, item.status) for item in active]}"
        )
    if active and not pending:
        raise AssertionError(
            f"{case_id}: final feedback is active but not pending: "
            f"{[(item.checkpoint_id, item.status) for item in active]}"
        )
    if not pending:
        if allow_none:
            return None
        raise AssertionError(
            f"{case_id}: expected exactly one pending {FINAL_CHECKPOINT_TYPE}"
        )
    return pending[0]


async def _final_owner_interaction_frontier(
    store: Any,
    *,
    project_id: str,
    session_id: str,
    case_id: str,
) -> tuple[Any, list[dict[str, str]]]:
    """Require the final card to be the scope's only active owner wait."""

    from opc.core.interaction_protocol import OWNER_INTERACTION_CHECKPOINT_TYPES

    rows = await _find_case_checkpoints(
        store,
        project_id=project_id,
        session_id=session_id,
        checkpoint_types=tuple(sorted(OWNER_INTERACTION_CHECKPOINT_TYPES)),
        statuses=tuple(sorted(ACTIVE_OWNER_CHECKPOINT_STATUSES)),
    )
    frontier = [
        {
            "checkpoint_id": str(row.checkpoint_id or ""),
            "checkpoint_type": str(row.checkpoint_type or ""),
            "status": str(row.status or ""),
            "task_id": str(row.task_id or ""),
        }
        for row in rows
    ]
    if len(rows) != 1:
        raise AssertionError(
            f"{case_id}: final owner frontier must contain exactly one active "
            f"checkpoint, got {frontier}"
        )
    checkpoint = rows[0]
    if (
        str(checkpoint.checkpoint_type or "") != FINAL_CHECKPOINT_TYPE
        or str(checkpoint.status or "") != "pending"
    ):
        raise AssertionError(
            f"{case_id}: final owner frontier is not one pending "
            f"{FINAL_CHECKPOINT_TYPE}: {frontier}"
        )
    return checkpoint, frontier


async def _approve_native_staffing_checkpoint(
    engine: Any,
    checkpoint: Any,
    *,
    spec: CaseSpec,
    session_id: str,
    client_request_id: str,
) -> dict[str, Any]:
    """Select role-only staffing with native execution through the public API."""

    if str(checkpoint.checkpoint_type or "") != STAFFING_CHECKPOINT_TYPE:
        raise AssertionError("unexpected checkpoint passed to staffing decision")
    if checkpoint.task_id:
        raise AssertionError("company staffing selection must be taskless")
    if str(checkpoint.session_id or "").strip() != session_id:
        raise AssertionError("company staffing selection belongs to another session")

    payload = dict(checkpoint.payload or {})
    interaction = dict(payload.get("interaction", {}) or {})
    execution_scope = dict(interaction.get("execution_scope", {}) or {})
    expected_scope = {"company_profile": "custom", "org_id": spec.org_id}
    if execution_scope != expected_scope:
        raise AssertionError(
            f"{spec.case_id}: staffing checkpoint durable scope drifted: "
            f"{execution_scope!r}"
        )
    if str(payload.get("company_profile", "") or "").strip() != "custom":
        raise AssertionError(f"{spec.case_id}: staffing checkpoint is not custom")
    if str(payload.get("org_id", "") or "").strip() != spec.org_id:
        raise AssertionError(f"{spec.case_id}: staffing checkpoint has wrong org_id")
    if str(payload.get("primary_session_id", "") or "").strip() != session_id:
        raise AssertionError(
            f"{spec.case_id}: staffing checkpoint has wrong primary session"
        )

    expected_role_ids = {str(role["id"]) for role in spec.roles}
    staffing_roles = list(payload.get("staffing_roles", []) or [])
    role_ids = [
        str(role.get("role_id", "") or "").strip()
        for role in staffing_roles
        if isinstance(role, dict)
    ]
    if (
        len(role_ids) != len(expected_role_ids)
        or len(set(role_ids)) != len(role_ids)
        or set(role_ids) != expected_role_ids
    ):
        raise AssertionError(
            f"{spec.case_id}: staffing roles do not match the saved organization"
        )
    for role in staffing_roles:
        selected_agent = str(
            role.get("selected_agent") or role.get("default_agent") or ""
        ).strip()
        default_agent = str(role.get("default_agent", "") or "").strip()
        if selected_agent != "native" or default_agent != "native":
            raise AssertionError(
                f"{spec.case_id}: refusing non-native staffing default for "
                f"{role.get('role_id')}: selected={selected_agent!r}, "
                f"default={default_agent!r}"
            )

    selections = {
        role_id: {"kind": "fallback", "id": ""}
        for role_id in sorted(expected_role_ids)
    }
    role_agents = {role_id: "native" for role_id in sorted(expected_role_ids)}
    decision = {
        "staffing_action": "manual_approve",
        "staffing_selections": selections,
        "recruitment_role_agents": role_agents,
        "recruitment_agent": "native",
        "text": "approve",
    }
    submit = getattr(engine, "submit_checkpoint_decision", None)
    if not callable(submit):
        raise RuntimeError(
            "OPCEngine.submit_checkpoint_decision is unavailable; "
            "company staffing cannot be driven canonically"
        )
    receipt = _receipt_payload(
        await submit(
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_type=checkpoint.checkpoint_type,
            decision=decision,
            client_request_id=client_request_id,
            requester_session_id=session_id,
        )
    )
    if not _receipt_acknowledged(receipt):
        raise AssertionError(
            f"staffing checkpoint {checkpoint.checkpoint_id} was not accepted: "
            f"{receipt}"
        )
    return {
        "checkpoint_id": checkpoint.checkpoint_id,
        "checkpoint_type": checkpoint.checkpoint_type,
        "root_session_id": session_id,
        "company_profile": "custom",
        "org_id": spec.org_id,
        "staffing_action": "manual_approve",
        "staffing_selections": selections,
        "recruitment_role_agents": role_agents,
        "recruitment_agent": "native",
        "client_request_id": client_request_id,
        "receipt": receipt,
    }


async def _approve_tool_checkpoint(
    engine: Any,
    checkpoint: Any,
    *,
    spec: CaseSpec,
    workplace: Path,
    session_id: str,
    ui_anchor_task_id: str,
    client_request_id: str,
    prior_decisions: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    decision_started: Any | None = None,
) -> dict[str, Any]:
    checkpoint_id = str(checkpoint.checkpoint_id or "").strip()
    prior_match = next(
        (
            dict(prior)
            for prior in prior_decisions
            if str(prior.get("checkpoint_id", "") or "").strip()
            == checkpoint_id
        ),
        None,
    )

    options = _checkpoint_options(checkpoint)
    payload = dict(checkpoint.payload or {})
    interaction = dict(payload.get("interaction", {}) or {})
    ownership = dict(interaction.get("ownership", {}) or {})
    tool_call = dict(payload.get("tool_call", {}) or {})
    missing_identity = [
        key
        for key, value in (
            ("tool_call.id", tool_call.get("id")),
            ("tool_call.name", tool_call.get("name")),
            ("tool_call.fingerprint", tool_call.get("fingerprint")),
            ("tool_call.runtime_session_id", tool_call.get("runtime_session_id")),
            ("ownership.waiting_task_id", ownership.get("waiting_task_id")),
            ("ownership.root_session_id", ownership.get("root_session_id")),
        )
        if not str(value or "").strip()
    ]
    if missing_identity:
        raise AssertionError(
            f"tool checkpoint {checkpoint.checkpoint_id} lacks exact durable identity: "
            f"{', '.join(missing_identity)}"
        )
    waiting_task_id = str(ownership.get("waiting_task_id", "") or "").strip()
    if str(checkpoint.task_id or "").strip() != waiting_task_id:
        raise AssertionError(
            f"tool checkpoint {checkpoint.checkpoint_id} waiting-task identity mismatch"
        )
    waiting_task = await engine.store.get_task(waiting_task_id)
    if waiting_task is None:
        raise AssertionError(
            f"tool checkpoint {checkpoint.checkpoint_id} waiting task is missing"
        )
    if str(waiting_task.project_id or "").strip() != str(checkpoint.project_id or "").strip():
        raise AssertionError(
            f"tool checkpoint {checkpoint.checkpoint_id} crosses project scope"
        )
    allowed_roles = {str(role["id"]) for role in spec.roles}
    if str(waiting_task.assigned_to or "").strip() not in allowed_roles:
        raise AssertionError(
            f"tool checkpoint {checkpoint.checkpoint_id} belongs to an unexpected role"
        )
    from opc.layer2_organization.company_runtime_identity import (
        load_company_runtime_identity_index,
    )

    identity_index = await load_company_runtime_identity_index(
        engine.store,
        str(checkpoint.project_id or "default").strip() or "default",
    )
    identity = identity_index.resolve(task_id=waiting_task_id)
    if identity is None:
        raise AssertionError(
            f"tool checkpoint {checkpoint.checkpoint_id} has no canonical company identity"
        )
    canonical_root_session_id = str(identity.runtime_session_id or "").strip()
    canonical_anchor_task_id = str(identity.ui_anchor_task_id or "").strip()
    recorded_root_session_id = str(
        ownership.get("root_session_id")
        or ownership.get("company_runtime_session_id")
        or ""
    ).strip()
    recorded_anchor_task_id = str(
        ownership.get("ui_anchor_task_id", "") or ""
    ).strip()
    if canonical_root_session_id != session_id:
        raise AssertionError(
            f"tool checkpoint {checkpoint.checkpoint_id} canonical root session "
            "does not match the E2E session"
        )
    if not ui_anchor_task_id or canonical_anchor_task_id != ui_anchor_task_id:
        raise AssertionError(
            f"tool checkpoint {checkpoint.checkpoint_id} canonical UI anchor "
            "does not match the journaled pure UI root"
        )
    if recorded_root_session_id != canonical_root_session_id:
        raise AssertionError(
            f"tool checkpoint {checkpoint.checkpoint_id} recorded root session "
            "drifted from canonical company identity"
        )
    if recorded_anchor_task_id != canonical_anchor_task_id:
        raise AssertionError(
            f"tool checkpoint {checkpoint.checkpoint_id} recorded UI anchor "
            "drifted from canonical company identity"
        )
    if str(tool_call.get("name", "") or "") == "shell_exec":
        waiting_metadata = dict(waiting_task.metadata or {})
        for source_key in ("inherited_environment", "environment_manifest"):
            source = dict(waiting_metadata.get(source_key, {}) or {})
            if str(source.get("shell_prefix", "") or "").strip() or str(
                source.get("shell_prefix_win", "") or ""
            ).strip():
                raise AssertionError(
                    f"tool checkpoint {checkpoint.checkpoint_id} has an unreviewed shell prefix"
                )
    validation_reason = ""
    try:
        _validate_test_tool_call(spec, workplace, tool_call)
    except AssertionError as exc:
        validation_reason = str(exc).strip() or "unmodelled_tool_call"

    option_id = "deny" if validation_reason else "approve_once"
    if option_id not in options:
        raise AssertionError(
            f"tool checkpoint {checkpoint.checkpoint_id} lacks {option_id}: "
            f"{sorted(options)}"
        )
    signature = _tool_call_signature(tool_call)
    if prior_match is not None:
        prior_option = str(
            prior_match.get("decision", "") or "approve_once"
        ).strip()
        stable_identity = (
            str(prior_match.get("tool_call_id", "") or "")
            == str(tool_call.get("id", "") or "")
            and str(prior_match.get("tool_call_fingerprint", "") or "")
            == str(tool_call.get("fingerprint", "") or "")
            and str(prior_match.get("tool_runtime_session_id", "") or "")
            == str(tool_call.get("runtime_session_id", "") or "")
        )
        if not stable_identity or prior_option != option_id:
            raise AssertionError(
                f"{spec.case_id}: journaled ToolCall decision identity drifted for "
                f"checkpoint {checkpoint_id}"
            )
        if _receipt_acknowledged(dict(prior_match.get("receipt", {}) or {})):
            # A pending duplicate observed before the checkpoint status
            # projection catches up must never consume another decision.
            return {**prior_match, "deduplicated_by_harness": True}
    if option_id == "deny":
        rejected = [
            item
            for item in prior_decisions
            if str(item.get("decision", "") or "").strip() == "deny"
            and str(item.get("checkpoint_id", "") or "").strip()
            != checkpoint_id
        ]
        if len(rejected) >= MAX_UNEXPECTED_TOOL_DENIALS_PER_CASE:
            raise AssertionError(
                f"{spec.case_id}: exceeded the bounded unexpected ToolCall denial "
                f"limit ({MAX_UNEXPECTED_TOOL_DENIALS_PER_CASE})"
            )
        repeated = sum(
            1
            for item in rejected
            if str(item.get("tool_call_signature", "") or "").strip()
            == signature
        )
        if repeated >= MAX_REPEATED_UNEXPECTED_TOOL_CALLS:
            raise AssertionError(
                f"{spec.case_id}: native agent repeated the same rejected ToolCall "
                f"more than {MAX_REPEATED_UNEXPECTED_TOOL_CALLS} times"
            )
        decision_text = _tool_rejection_feedback(
            spec,
            tool_call,
            validation_reason,
        )
    else:
        decision_text = "approve_once"

    requester_task_id = canonical_anchor_task_id
    submit = getattr(engine, "submit_checkpoint_decision", None)
    if not callable(submit):
        raise RuntimeError(
            "OPCEngine.submit_checkpoint_decision is unavailable; core issue #35 fix is not active"
        )
    canonical_request_id = _decision_request_id(client_request_id, option_id)
    decision_record = {
        "checkpoint_id": checkpoint.checkpoint_id,
        "checkpoint_type": checkpoint.checkpoint_type,
        "waiting_task_id": checkpoint.task_id,
        "root_session_id": canonical_root_session_id,
        "ui_anchor_task_id": requester_task_id,
        "tool_name": str(tool_call.get("name", "") or ""),
        "tool_call_id": str(tool_call.get("id", "") or ""),
        "tool_call_fingerprint": str(tool_call.get("fingerprint", "") or ""),
        "tool_runtime_session_id": str(
            tool_call.get("runtime_session_id", "") or ""
        ),
        "tool_arguments": dict(tool_call.get("arguments", {}) or {}),
        "tool_command": str(
            dict(tool_call.get("arguments", {}) or {}).get("command", "") or ""
        ),
        "tool_call_signature": signature,
        "decision": option_id,
        "exact_modeled_call": option_id == "approve_once",
        "rejected": option_id == "deny",
        "rejection_reason": validation_reason,
        "decision_text": decision_text,
        "client_request_id": canonical_request_id,
        "receipt": {},
        "submission_state": "planned",
    }
    if callable(decision_started):
        decision_started(dict(decision_record))
    receipt = _receipt_payload(
        await submit(
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_type=checkpoint.checkpoint_type,
            decision={"option_id": option_id, "text": decision_text},
            client_request_id=canonical_request_id,
            requester_task_id=requester_task_id,
            requester_session_id=canonical_root_session_id,
        )
    )
    if not _receipt_acknowledged(receipt):
        raise AssertionError(
            f"checkpoint {checkpoint.checkpoint_id} was not accepted: {receipt}"
        )
    return {
        **decision_record,
        "receipt": receipt,
        "submission_state": "acknowledged",
    }


async def _reconcile_planned_tool_decisions(
    engine: Any,
    run: CaseRun,
    *,
    project_id: str,
    state_changed: Any,
) -> None:
    """Recover only a journal intent that exactly matches durable truth."""

    planned = [
        item
        for item in run.tool_decisions
        if not _receipt_acknowledged(dict(item.get("receipt", {}) or {}))
    ]
    if not planned:
        return
    rows = await _find_case_checkpoints(
        engine.store,
        project_id=project_id,
        session_id=run.session_id,
        checkpoint_types=TOOL_CHECKPOINT_TYPES,
        statuses=None,
    )
    by_id = {str(row.checkpoint_id or "").strip(): row for row in rows}
    for item in planned:
        checkpoint_id = str(item.get("checkpoint_id", "") or "").strip()
        checkpoint = by_id.get(checkpoint_id)
        if checkpoint is None:
            raise AssertionError(
                f"{run.spec.case_id}: planned tool decision lost checkpoint "
                f"{checkpoint_id}"
            )
        payload = dict(checkpoint.payload or {})
        tool_call = dict(payload.get("tool_call", {}) or {})
        stable_identity = all(
            str(tool_call.get(durable_key, "") or "")
            == str(item.get(journal_key, "") or "")
            for durable_key, journal_key in (
                ("id", "tool_call_id"),
                ("fingerprint", "tool_call_fingerprint"),
                ("runtime_session_id", "tool_runtime_session_id"),
            )
        )
        if not stable_identity:
            raise AssertionError(
                f"{run.spec.case_id}: planned tool decision identity drifted for "
                f"checkpoint {checkpoint_id}"
            )
        interaction = dict(payload.get("interaction", {}) or {})
        durable_decision = interaction.get("decision")
        status = str(checkpoint.status or "").strip()
        if not isinstance(durable_decision, dict):
            if status == "pending":
                # Crash occurred before submit; deterministic resubmission of
                # the journaled intent happens in the normal pending loop.
                continue
            raise AssertionError(
                f"{run.spec.case_id}: terminal tool checkpoint {checkpoint_id} "
                "has no durable decision"
            )
        expected_value = {
            "option_id": str(item.get("decision", "") or "").strip(),
            "text": str(item.get("decision_text", "") or ""),
        }
        encoded = json.dumps(
            expected_value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        if (
            str(durable_decision.get("request_id", "") or "").strip()
            != str(item.get("client_request_id", "") or "").strip()
            or durable_decision.get("value") != expected_value
            or str(durable_decision.get("decision_hash", "") or "").strip()
            != hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        ):
            raise AssertionError(
                f"{run.spec.case_id}: durable tool decision conflicts with "
                f"journal intent for checkpoint {checkpoint_id}"
            )
        item["receipt"] = {
            "accepted": True,
            "deduplicated": True,
            "status": status,
            "outcome": "recovered_exact_durable_decision",
            "checkpoint_id": checkpoint_id,
            "checkpoint_type": str(checkpoint.checkpoint_type or ""),
        }
        item["submission_state"] = "recovered_after_submit"
        state_changed()


async def _denied_tool_checkpoint_statuses(
    store: Any,
    *,
    project_id: str,
    session_id: str,
    case_id: str,
    checkpoint_ids: set[str],
) -> dict[str, str]:
    """Take one non-blocking snapshot of acknowledged denial settlement."""

    if not checkpoint_ids:
        return {}
    failure_statuses = {
        "failed",
        "invalid",
        "outcome_unknown",
        "stale",
        "cancelled",
        "superseded",
    }
    active_statuses = {"pending", "answered", "consuming", "resuming"}
    rows = await _find_case_checkpoints(
        store,
        project_id=project_id,
        session_id=session_id,
        checkpoint_types=TOOL_CHECKPOINT_TYPES,
        statuses=None,
    )
    by_id = {
        str(row.checkpoint_id or "").strip(): row
        for row in rows
        if str(row.checkpoint_id or "").strip() in checkpoint_ids
    }
    missing = sorted(checkpoint_ids - set(by_id))
    if missing:
        raise AssertionError(
            f"{case_id}: denied tool checkpoints disappeared: {missing}"
        )
    statuses: dict[str, str] = {}
    for checkpoint_id in sorted(checkpoint_ids):
        status = str(by_id[checkpoint_id].status or "").strip()
        if status in failure_statuses:
            raise AssertionError(
                f"{case_id}: denied tool checkpoint {checkpoint_id} settled as "
                f"{status}"
            )
        if status != "resolved" and status not in active_statuses:
            raise AssertionError(
                f"{case_id}: denied tool checkpoint {checkpoint_id} has unknown "
                f"status {status!r}"
            )
        statuses[checkpoint_id] = status
    return statuses


def _metadata_flag_true(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _is_final_authoritative_delivery_work_item(work_item: Any) -> bool:
    """Identify the real owner-facing final delivery, never an auxiliary."""

    metadata = dict(getattr(work_item, "metadata", {}) or {})
    if str(metadata.get("feedback_scope", "") or "").strip().lower() != "final":
        return False
    if not _metadata_flag_true(metadata.get("authoritative_output", False)):
        return False
    if any(
        _metadata_flag_true(metadata.get(key, False))
        for key in (
            "attention_work_item",
            "self_evolution_work_item",
            "runtime_auxiliary_task",
            "company_runtime_auxiliary_task",
            "report_execution_work_item",
            "review_execution_work_item",
        )
    ):
        return False
    turn_kinds = {
        str(value or "").strip().lower()
        for value in (
            getattr(work_item, "kind", ""),
            metadata.get("work_item_turn_type"),
            metadata.get("work_kind"),
            metadata.get("delegation_turn_kind"),
        )
        if str(value or "").strip()
    }
    if turn_kinds & {
        "attention",
        "report",
        "review",
        "runtime_aux",
        "runtime_auxiliary",
        "self-evolution",
        "self_evolution",
    }:
        return False
    return bool(turn_kinds & {"deliver", "delivery"})


def _failed_delivery_work_items(work_items: list[Any]) -> list[Any]:
    failed: list[Any] = []
    for work_item in work_items:
        phase = str(
            getattr(
                getattr(work_item, "phase", ""),
                "value",
                getattr(work_item, "phase", ""),
            )
            or ""
        ).strip().lower()
        if phase not in {"failed", "cancelled"}:
            continue
        if _is_final_authoritative_delivery_work_item(work_item):
            failed.append(work_item)
    return failed


def _closed_impossible_final_delivery_frontiers(
    delegation_run: Any,
    work_items: list[Any],
) -> list[dict[str, Any]]:
    """Return final-delivery frontiers that cannot advance without a controller.

    A failed child alone is not terminal: a live company controller may still
    run failure triage or rebuild it.  The E2E run is closed-impossible only
    when the explicit final, authoritative delivery card is nonterminal, its
    controller lease has been released, and a default-hard dependency is both
    terminally failed and still recorded on the delivery's waiting frontier.
    """

    if str(
        getattr(delegation_run, "controller_owner_token", "") or ""
    ).strip():
        return []

    work_item_by_id = {
        str(getattr(item, "work_item_id", "") or "").strip(): item
        for item in work_items
        if str(getattr(item, "work_item_id", "") or "").strip()
    }
    terminal_phases = {"approved", "failed", "cancelled"}
    failed_phases = {"failed", "cancelled"}
    impossible: list[dict[str, Any]] = []

    for delivery in work_items:
        if not _is_final_authoritative_delivery_work_item(delivery):
            continue
        metadata = dict(getattr(delivery, "metadata", {}) or {})

        raw_phase = getattr(delivery, "phase", "")
        delivery_phase = str(
            getattr(raw_phase, "value", raw_phase) or ""
        ).strip().lower()
        if delivery_phase in terminal_phases:
            continue

        dependency_ids = [
            str(item).strip()
            for item in list(metadata.get("dependency_work_item_ids", []) or [])
            if str(item).strip()
        ]
        waiting_ids = {
            str(item).strip()
            for item in list(metadata.get("waiting_on_work_item_ids", []) or [])
            if str(item).strip()
        }
        dependency_classes = dict(metadata.get("dependency_classes", {}) or {})
        failed_dependencies: list[dict[str, str]] = []
        for dependency_id in dependency_ids:
            dependency_class = str(
                dependency_classes.get(dependency_id, "hard") or "hard"
            ).strip().lower()
            if dependency_class in {"soft", "info"}:
                continue
            if dependency_id not in waiting_ids:
                continue
            dependency = work_item_by_id.get(dependency_id)
            raw_dependency_phase = getattr(dependency, "phase", "")
            dependency_phase = str(
                getattr(raw_dependency_phase, "value", raw_dependency_phase) or ""
            ).strip().lower()
            if dependency_phase in failed_phases:
                failed_dependencies.append(
                    {
                        "work_item_id": dependency_id,
                        "phase": dependency_phase,
                    }
                )
        if failed_dependencies:
            impossible.append(
                {
                    "delivery_work_item_id": str(
                        getattr(delivery, "work_item_id", "") or ""
                    ),
                    "delivery_projection_id": str(
                        getattr(delivery, "projection_id", "") or ""
                    ),
                    "delivery_phase": delivery_phase,
                    "failed_dependencies": failed_dependencies,
                }
            )
    return impossible


async def _raise_if_case_terminally_failed(
    store: Any,
    *,
    project_id: str,
    session_id: str,
    case_id: str,
) -> None:
    """Fail the harness as soon as durable company state is terminal."""

    list_runs = getattr(store, "list_delegation_runs", None)
    if not callable(list_runs):
        return
    runs = await list_runs(project_id=project_id, session_id=session_id)
    list_work_items = getattr(store, "list_delegation_work_items", None)
    for delegation_run in runs:
        run_id = str(getattr(delegation_run, "run_id", "") or "").strip()
        status = str(getattr(delegation_run, "status", "") or "").strip()
        lifecycle_status = str(
            getattr(delegation_run, "lifecycle_status", "") or ""
        ).strip()
        if status == "failed" or lifecycle_status == "closed_failed":
            failure = dict(getattr(delegation_run, "metadata", {}) or {}).get(
                "run_failure", {}
            )
            raise AssertionError(
                f"{case_id}: delegation run {run_id or '<unknown>'} reached "
                f"terminal failure status={status!r} "
                f"lifecycle_status={lifecycle_status!r}: {failure}"
            )
        if not run_id or not callable(list_work_items):
            continue
        work_items = await list_work_items(run_id)
        failed_deliveries = _failed_delivery_work_items(work_items)
        if failed_deliveries:
            identities = [
                {
                    "work_item_id": str(
                        getattr(item, "work_item_id", "") or ""
                    ),
                    "projection_id": str(
                        getattr(item, "projection_id", "") or ""
                    ),
                    "blocked_reason": str(
                        getattr(item, "blocked_reason", "") or ""
                    ),
                    "phase": str(
                        getattr(
                            getattr(item, "phase", ""),
                            "value",
                            getattr(item, "phase", ""),
                        )
                        or ""
                    ).strip().lower(),
                }
                for item in failed_deliveries
            ]
            raise AssertionError(
                f"{case_id}: final delivery entered a terminal failed/cancelled "
                f"phase before final feedback: {identities}"
            )
        impossible_frontiers = _closed_impossible_final_delivery_frontiers(
            delegation_run,
            work_items,
        )
        if impossible_frontiers:
            # The run and its WorkItems are separate reads.  A replacement
            # controller may acquire the released lease between them, so the
            # original ownerless run object is not sufficient evidence for a
            # closed frontier.  Re-read the exact run after the WorkItem scan.
            get_run = getattr(store, "get_delegation_run", None)
            if callable(get_run):
                fresh_run = await get_run(run_id)
            else:
                fresh_runs = await list_runs(
                    project_id=project_id,
                    session_id=session_id,
                )
                fresh_run = next(
                    (
                        candidate
                        for candidate in fresh_runs
                        if str(getattr(candidate, "run_id", "") or "").strip()
                        == run_id
                    ),
                    None,
                )
            if fresh_run is None:
                continue
            impossible_frontiers = _closed_impossible_final_delivery_frontiers(
                fresh_run,
                work_items,
            )
        if impossible_frontiers:
            raise AssertionError(
                f"{case_id}: delegation run {run_id} reached a closed impossible "
                f"final-delivery frontier before final feedback (controller "
                f"lease released): "
                f"{impossible_frontiers}"
            )


async def _case_has_durable_runtime_snapshot(
    store: Any,
    run: CaseRun,
    *,
    project_id: str,
) -> bool:
    """Distinguish a started case from a merely journaled UI root on resume."""

    list_runs = getattr(store, "list_delegation_runs", None)
    delegation_runs = (
        list(
            await list_runs(
                project_id=project_id,
                session_id=run.session_id,
            )
        )
        if callable(list_runs)
        else []
    )
    if len(delegation_runs) > 1:
        raise AssertionError(
            f"{run.spec.case_id}: resume found duplicate DelegationRuns for "
            f"{run.session_id}"
        )
    delegation_run_ids = {
        str(getattr(item, "run_id", "") or "").strip()
        for item in delegation_runs
        if str(getattr(item, "run_id", "") or "").strip()
    }

    tasks = list(await store.get_tasks(project_id=project_id))
    runtime_tasks = [
        task
        for task in tasks
        if str(getattr(task, "id", "") or "").strip()
        != run.ui_anchor_task_id
        and (
            str(getattr(task, "parent_session_id", "") or "").strip()
            == run.session_id
            or (
                bool(delegation_run_ids)
                and str(
                    dict(getattr(task, "metadata", {}) or {}).get(
                        "delegation_run_id", ""
                    )
                    or ""
                ).strip()
                in delegation_run_ids
            )
        )
    ]
    runtime_checkpoints = await _find_case_checkpoints(
        store,
        project_id=project_id,
        session_id=run.session_id,
        checkpoint_types=(
            STAFFING_CHECKPOINT_TYPE,
            FINAL_CHECKPOINT_TYPE,
            "company_runtime_interrupted",
            "company_runtime_suspended",
            *TOOL_CHECKPOINT_TYPES,
        ),
        statuses=None,
    )
    durable_started = bool(
        delegation_runs or runtime_tasks or runtime_checkpoints
    )
    journal_started = bool(
        run.staffing_decisions
        or run.tool_decisions
        or run.feedback_checkpoint_id
    )
    if journal_started and not durable_started:
        raise AssertionError(
            f"{run.spec.case_id}: resume journal records runtime activity but "
            "the durable company snapshot is missing"
        )
    return durable_started


async def _refresh_case_resume_routing(
    store: Any,
    runs: list[CaseRun],
    *,
    project_id: str,
) -> None:
    """Recompute resume-vs-fresh routing independently for every case."""

    for run in runs:
        run.resume_existing = await _case_has_durable_runtime_snapshot(
            store,
            run,
            project_id=project_id,
        )


def _case_input_content(run: CaseRun, workplace: Path) -> str:
    """Render either an exact resume control or the untouched case request."""

    if run.resume_existing:
        return "continue"
    return (
        f"{_render_case_prompt(run.spec, run.started_at)}\n\n"
        "The absolute project workplace for tool working_directory is: "
        f"{workplace}"
    )


async def _drive_case(
    engine: Any,
    run: CaseRun,
    *,
    project_id: str,
    workplace: Path,
    poll_seconds: float,
    timeout_seconds: float,
    state_changed: Any,
) -> None:
    if bool(run.staffing_recovery_checkpoint_id) != bool(
        run.staffing_recovery_run_id
    ):
        raise AssertionError(
            f"{run.spec.case_id}: partial staffing recovery identity"
        )
    ui_root = await engine.store.get_task(run.ui_anchor_task_id)
    if ui_root is None:
        raise AssertionError(
            f"{run.spec.case_id}: process_message cannot run without its UI root Task"
        )
    _validate_office_ui_root_task(ui_root, run, project_id=project_id)
    await _raise_if_case_terminally_failed(
        engine.store,
        project_id=project_id,
        session_id=run.session_id,
        case_id=run.spec.case_id,
    )

    staffing_rows_at_start = await _find_case_checkpoints(
        engine.store,
        project_id=project_id,
        session_id=run.session_id,
        checkpoint_types=(STAFFING_CHECKPOINT_TYPE,),
        statuses=None,
    )
    if len(staffing_rows_at_start) > 1:
        raise AssertionError(
            f"{run.spec.case_id}: expected exactly one staffing checkpoint"
        )
    if run.resume_existing and run.staffing_decisions and not staffing_rows_at_start:
        raise AssertionError(
            f"{run.spec.case_id}: resume journaled a staffing decision but its "
            "durable checkpoint is missing"
        )

    existing_feedback = await _single_pending_final_feedback(
        engine.store,
        project_id=project_id,
        session_id=run.session_id,
        case_id=run.spec.case_id,
        allow_none=True,
    )
    staffing_recovery: dict[str, Any] | None = None
    recovery_already_resolved = False
    if staffing_rows_at_start:
        staffing_at_start = staffing_rows_at_start[0]
        staffing_status_at_start = str(
            getattr(staffing_at_start, "status", "") or ""
        ).strip()
        if staffing_status_at_start == "outcome_unknown":
            if (
                existing_feedback is not None
                and run.staffing_recovery_checkpoint_id
                and run.staffing_recovery_run_id
            ):
                interruption_rows = await _find_case_checkpoints(
                    engine.store,
                    project_id=project_id,
                    session_id=run.session_id,
                    checkpoint_types=("company_runtime_interrupted",),
                    statuses=None,
                )
                recorded_rows = [
                    row
                    for row in interruption_rows
                    if str(getattr(row, "checkpoint_id", "") or "").strip()
                    == run.staffing_recovery_checkpoint_id
                ]
                if (
                    len(recorded_rows) == 1
                    and str(
                        getattr(recorded_rows[0], "status", "") or ""
                    ).strip()
                    == "resolved"
                ):
                    staffing_recovery = (
                        await _resumed_staffing_runtime_recovery_evidence(
                            engine.store,
                            run,
                            staffing_at_start,
                            project_id=project_id,
                            expected_checkpoint_id=(
                                run.staffing_recovery_checkpoint_id
                            ),
                            expected_delegation_run_id=(
                                run.staffing_recovery_run_id
                            ),
                            require_resolved=True,
                        )
                    )
                    if (
                        staffing_recovery is None
                        or str(staffing_recovery["delegation_run_id"])
                        != run.staffing_recovery_run_id
                    ):
                        raise AssertionError(
                            f"{run.spec.case_id}: resolved staffing recovery "
                            "identity drifted before final feedback resume"
                        )
                    recovery_already_resolved = True

            if not recovery_already_resolved:
                # Fail closed before process_message can mutate anything.  A
                # crash-terminal staffing effect is resumable only through the
                # one active interruption card bound to its exact company run.
                staffing_recovery = (
                    await _resumed_staffing_runtime_recovery_evidence(
                        engine.store,
                        run,
                        staffing_at_start,
                        project_id=project_id,
                    )
                )
                if staffing_recovery is None:
                    raise AssertionError(
                        f"{run.spec.case_id}: staffing outcome is unknown and "
                        "has no exact same-run interruption recovery"
                    )
                recovered_checkpoint_id = str(staffing_recovery["checkpoint_id"])
                recovered_run_id = str(staffing_recovery["delegation_run_id"])
                if run.staffing_recovery_checkpoint_id and (
                    run.staffing_recovery_checkpoint_id != recovered_checkpoint_id
                    or run.staffing_recovery_run_id != recovered_run_id
                ):
                    raise AssertionError(
                        f"{run.spec.case_id}: journaled staffing recovery identity "
                        "drifted before resume"
                    )
                run.staffing_recovery_checkpoint_id = recovered_checkpoint_id
                run.staffing_recovery_run_id = recovered_run_id
                state_changed()
        elif staffing_status_at_start in {
            "failed",
            "invalid",
            "stale",
            "cancelled",
        }:
            raise AssertionError(
                f"{run.spec.case_id}: staffing continuation ended as "
                f"{staffing_status_at_start} before resume"
            )

    staffing_resolved_at_start = bool(
        staffing_rows_at_start
        and str(
            getattr(staffing_rows_at_start[0], "status", "") or ""
        ).strip()
        == "resolved"
    )
    if existing_feedback is not None and (
        staffing_resolved_at_start or recovery_already_resolved
    ):
        run.feedback_checkpoint_id = existing_feedback.checkpoint_id
        run.response = "<resumed: final delivery feedback was already pending>"
        state_changed()
        return

    pending_staffing_at_start = await _find_case_checkpoints(
        engine.store,
        project_id=project_id,
        session_id=run.session_id,
        checkpoint_types=(STAFFING_CHECKPOINT_TYPE,),
        statuses=("pending",),
    )
    if len(pending_staffing_at_start) > 1:
        raise AssertionError(
            f"{run.spec.case_id}: multiple pending staffing checkpoints"
        )

    process_task: asyncio.Task[str] | None = None
    # On resume, answer an already-published staffing card directly. Sending
    # the word "continue" first would route through a legacy text adapter and
    # would no longer prove the explicit checkpoint API path.
    if not (run.resume_existing and pending_staffing_at_start):
        content = _case_input_content(run, workplace)
        metadata = {
            "issue35_e2e": True,
            "issue35_case_id": run.spec.case_id,
            "expected_agent": "native",
        }
        if run.resume_existing:
            metadata["ui_force_resume"] = True
        if run.staffing_recovery_checkpoint_id:
            metadata.update(
                {
                    "response_to_checkpoint_id": (
                        run.staffing_recovery_checkpoint_id
                    ),
                    "response_to_checkpoint_type": (
                        "company_runtime_interrupted"
                    ),
                }
            )
        process_task = asyncio.create_task(
            engine.process_message(
                content,
                project_id=project_id,
                session_id=run.session_id,
                mode="org",
                org_id=run.spec.org_id,
                preferred_agent="native",
                domains=["finance", "research"]
                if run.spec.case_id == "investment"
                else ["coding", "frontend", "testing"],
                company_profile="custom",
                origin_task_id=run.ui_anchor_task_id,
                message_metadata=metadata,
            ),
            name=f"issue35-{run.spec.case_id}-{run.session_id}",
        )
    seen_staffing_checkpoints = {
        str(item.get("checkpoint_id", "") or "")
        for item in run.staffing_decisions
    }
    seen_tool_checkpoints = {
        str(item.get("checkpoint_id", "") or "")
        for item in run.tool_decisions
        if _receipt_acknowledged(dict(item.get("receipt", {}) or {}))
    }
    reported_denied_checkpoints = {
        str(item.get("checkpoint_id", "") or "")
        for item in run.tool_decisions
        if bool(item.get("resolution_reported", False))
    }

    def upsert_tool_decision(decision: dict[str, Any]) -> None:
        checkpoint_id = str(decision.get("checkpoint_id", "") or "").strip()
        for index, current in enumerate(run.tool_decisions):
            if str(current.get("checkpoint_id", "") or "").strip() == checkpoint_id:
                run.tool_decisions[index] = dict(decision)
                state_changed()
                return
        run.tool_decisions.append(dict(decision))
        state_changed()

    deadline = time.monotonic() + timeout_seconds
    unresolved_denied_statuses: dict[str, str] = {}
    try:
        while time.monotonic() < deadline:
            await _reconcile_planned_tool_decisions(
                engine,
                run,
                project_id=project_id,
                state_changed=state_changed,
            )
            await _raise_if_case_terminally_failed(
                engine.store,
                project_id=project_id,
                session_id=run.session_id,
                case_id=run.spec.case_id,
            )
            pending_staffing = await _find_case_checkpoints(
                engine.store,
                project_id=project_id,
                session_id=run.session_id,
                checkpoint_types=(STAFFING_CHECKPOINT_TYPE,),
                statuses=("pending",),
            )
            if len(pending_staffing) > 1:
                raise AssertionError(
                    f"{run.spec.case_id}: multiple pending staffing checkpoints"
                )
            for checkpoint in pending_staffing:
                if checkpoint.checkpoint_id in seen_staffing_checkpoints:
                    continue
                decision = await _approve_native_staffing_checkpoint(
                    engine,
                    checkpoint,
                    spec=run.spec,
                    session_id=run.session_id,
                    client_request_id=(
                        f"issue35-e2e:{run.session_id}:"
                        f"{checkpoint.checkpoint_id}:native-staffing"
                    ),
                )
                seen_staffing_checkpoints.add(checkpoint.checkpoint_id)
                run.staffing_decisions.append(decision)
                state_changed()
                print(
                    f"[{run.spec.case_id}] approved native staffing checkpoint "
                    f"{checkpoint.checkpoint_id}",
                    flush=True,
                )

            pending_tools = await _find_case_checkpoints(
                engine.store,
                project_id=project_id,
                session_id=run.session_id,
                checkpoint_types=TOOL_CHECKPOINT_TYPES,
                statuses=("pending",),
            )
            # A NativeRuntimeV2 parallel tool batch may publish more than one
            # permission card before waiting for the whole batch. Submit every
            # currently visible decision first; waiting for the first denial
            # while a sibling card is still pending deadlocks that batch.
            for checkpoint in sorted(
                pending_tools,
                key=lambda item: str(item.checkpoint_id or ""),
            ):
                if checkpoint.checkpoint_id in seen_tool_checkpoints:
                    continue
                if not _modeled_tool_call_inputs_ready(
                    run.spec,
                    workplace,
                    checkpoint,
                ):
                    # Do not approve an exact validator until the producer's
                    # atomic file write is visible.  Other pending cards in
                    # this snapshot must still be drained.
                    continue
                decision = await _approve_tool_checkpoint(
                    engine,
                    checkpoint,
                    spec=run.spec,
                    workplace=workplace,
                    session_id=run.session_id,
                    ui_anchor_task_id=run.ui_anchor_task_id,
                    client_request_id=(
                        f"issue35-e2e:{run.session_id}:{checkpoint.checkpoint_id}:approve_once"
                    ),
                    prior_decisions=run.tool_decisions,
                    decision_started=upsert_tool_decision,
                )
                seen_tool_checkpoints.add(checkpoint.checkpoint_id)
                upsert_tool_decision(decision)
                if decision["decision"] != "deny":
                    print(
                        f"[{run.spec.case_id}] approved exact "
                        f"{decision['tool_name'] or 'tool'} checkpoint "
                        f"{checkpoint.checkpoint_id}",
                        flush=True,
                    )
            denied_by_id = {
                str(item.get("checkpoint_id", "") or "").strip(): item
                for item in run.tool_decisions
                if str(item.get("decision", "") or "").strip() == "deny"
                and _receipt_acknowledged(dict(item.get("receipt", {}) or {}))
            }
            denied_statuses = await _denied_tool_checkpoint_statuses(
                engine.store,
                project_id=project_id,
                session_id=run.session_id,
                case_id=run.spec.case_id,
                checkpoint_ids=set(denied_by_id),
            )
            unresolved_denied_statuses = {
                checkpoint_id: status
                for checkpoint_id, status in denied_statuses.items()
                if status != "resolved"
            }
            newly_resolved = sorted(
                checkpoint_id
                for checkpoint_id, status in denied_statuses.items()
                if status == "resolved"
                and checkpoint_id not in reported_denied_checkpoints
            )
            for checkpoint_id in newly_resolved:
                decision = dict(denied_by_id[checkpoint_id])
                print(
                    f"[{run.spec.case_id}] safely denied unexpected "
                    f"{decision['tool_name'] or 'tool'} checkpoint "
                    f"{checkpoint_id}: {decision['rejection_reason']}",
                    flush=True,
                )
                reported_denied_checkpoints.add(checkpoint_id)
                decision["resolution_reported"] = True
                upsert_tool_decision(decision)

            feedback = await _single_pending_final_feedback(
                engine.store,
                project_id=project_id,
                session_id=run.session_id,
                case_id=run.spec.case_id,
                allow_none=True,
            )
            if feedback is not None:
                if unresolved_denied_statuses:
                    await asyncio.sleep(poll_seconds)
                    continue
                feedback_staffing = await _find_case_checkpoints(
                    engine.store,
                    project_id=project_id,
                    session_id=run.session_id,
                    checkpoint_types=(STAFFING_CHECKPOINT_TYPE,),
                    statuses=None,
                )
                if len(feedback_staffing) != 1:
                    raise AssertionError(
                        f"{run.spec.case_id}: final feedback appeared without "
                        "exactly one staffing checkpoint"
                    )
                staffing_status = str(
                    feedback_staffing[0].status or ""
                ).strip()
                recovered_staffing = staffing_status == "outcome_unknown"
                if recovered_staffing and (
                    not run.staffing_recovery_checkpoint_id
                    or not run.staffing_recovery_run_id
                    or not _is_exact_resumed_staffing_outcome_unknown(
                        run,
                        feedback_staffing[0],
                    )
                ):
                    raise AssertionError(
                        f"{run.spec.case_id}: final feedback appeared without the "
                        "journaled exact staffing recovery identity"
                    )
                if staffing_status != "resolved" and not recovered_staffing:
                    if staffing_status in {
                        "failed",
                        "invalid",
                        "outcome_unknown",
                        "stale",
                        "cancelled",
                    }:
                        raise AssertionError(
                            f"{run.spec.case_id}: staffing continuation ended as "
                            f"{staffing_status}"
                        )
                    await asyncio.sleep(poll_seconds)
                    continue
                if process_task is not None:
                    run.response = await asyncio.wait_for(
                        process_task,
                        timeout=120.0,
                    )
                    process_task = None
                elif not run.response:
                    run.response = (
                        "<continued through explicit native staffing decision>"
                    )
                if recovered_staffing:
                    staffing_recovery = (
                        await _resumed_staffing_runtime_recovery_evidence(
                            engine.store,
                            run,
                            feedback_staffing[0],
                            project_id=project_id,
                            expected_checkpoint_id=(
                                run.staffing_recovery_checkpoint_id
                            ),
                            expected_delegation_run_id=(
                                run.staffing_recovery_run_id
                            ),
                            require_resolved=True,
                        )
                    )
                    if (
                        staffing_recovery is None
                        or str(staffing_recovery["delegation_run_id"])
                        != run.staffing_recovery_run_id
                    ):
                        raise AssertionError(
                            f"{run.spec.case_id}: resolved staffing recovery "
                            "identity drifted"
                        )
                # Final delivery is committed by the company runtime before
                # the outer staffing-decision consumer returns.  On crash
                # recovery, await that consumer first so the exact recorded
                # interruption card crosses its resolved handoff fence.
                frontier_feedback, _frontier = (
                    await _final_owner_interaction_frontier(
                        engine.store,
                        project_id=project_id,
                        session_id=run.session_id,
                        case_id=run.spec.case_id,
                    )
                )
                if frontier_feedback.checkpoint_id != feedback.checkpoint_id:
                    raise AssertionError(
                        f"{run.spec.case_id}: final feedback identity changed "
                        "between pending and owner-frontier reads"
                    )
                run.feedback_checkpoint_id = feedback.checkpoint_id
                state_changed()
                return

            staffing_rows = await _find_case_checkpoints(
                engine.store,
                project_id=project_id,
                session_id=run.session_id,
                checkpoint_types=(STAFFING_CHECKPOINT_TYPE,),
                statuses=None,
            )
            if len(staffing_rows) > 1:
                raise AssertionError(
                    f"{run.spec.case_id}: expected exactly one staffing checkpoint"
                )
            for staffing in staffing_rows:
                status = str(staffing.status or "").strip()
                if status == "resolved":
                    continue
                if status == "outcome_unknown" and (
                    run.staffing_recovery_checkpoint_id
                    and run.staffing_recovery_run_id
                    and _is_exact_resumed_staffing_outcome_unknown(run, staffing)
                ):
                    # The exact recovery was authorized and journaled before
                    # process_message.  Do not re-select an interruption card
                    # while that one transitions pending -> resuming -> resolved.
                    continue
                if status in {
                    "failed",
                    "invalid",
                    "outcome_unknown",
                    "stale",
                    "cancelled",
                }:
                    error = dict(staffing.payload or {}).get("interaction_error", {})
                    raise AssertionError(
                        f"{run.spec.case_id}: staffing continuation ended as "
                        f"{status}: {error}"
                    )

            if process_task is not None and process_task.done():
                # The first call normally returns after publishing the
                # staffing pause.  Once that card has been submitted, the
                # durable interaction consumer owns the real company run, so
                # keep polling instead of treating the pause response as
                # completion.
                run.response = process_task.result()
                process_task = None
                if not staffing_rows and not run.staffing_decisions:
                    raise AssertionError(
                        f"{run.spec.case_id} completed without pending "
                        f"{FINAL_CHECKPOINT_TYPE} or a staffing continuation"
                    )
            await asyncio.sleep(poll_seconds)
        unresolved_detail = (
            f"; unresolved denied tool checkpoints={unresolved_denied_statuses}"
            if unresolved_denied_statuses
            else ""
        )
        raise TimeoutError(
            f"{run.spec.case_id} did not reach final feedback within "
            f"{timeout_seconds:.0f}s{unresolved_detail}"
        )
    finally:
        if process_task is not None and not process_task.done():
            process_task.cancel()
            await asyncio.gather(process_task, return_exceptions=True)


def _task_native_evidence(task: Any) -> dict[str, Any]:
    metadata = dict(task.metadata or {})
    selected = str(
        metadata.get("selected_execution_agent")
        or metadata.get("work_item_execution_agent")
        or ""
    ).strip()
    strategy = str(metadata.get("work_item_execution_strategy", "") or "").strip()
    return {
        "task_id": task.id,
        "session_id": task.session_id,
        "parent_session_id": task.parent_session_id,
        "assigned_to": task.assigned_to,
        "status": getattr(task.status, "value", str(task.status)),
        "delegation_run_id": str(metadata.get("delegation_run_id", "") or ""),
        "work_item_id": str(getattr(task, "linked_work_item_id", "") or ""),
        "work_item_projection_id": str(metadata.get("work_item_projection_id", "") or ""),
        "work_item_turn_type": str(metadata.get("work_item_turn_type", "") or ""),
        "work_item_execution_strategy": strategy,
        "selected_execution_agent": selected,
        "assigned_external_agent": task.assigned_external_agent,
        "org_id": str(metadata.get("org_id", "") or metadata.get("organization_id", "") or ""),
        "organization_config_file": str(metadata.get("organization_config_file", "") or ""),
    }


def _is_native_runtime_v2_execution_session(row: dict[str, Any]) -> bool:
    """Distinguish NativeRuntimeV2 executions from role/member projections.

    ``runtime_sessions`` also stores company member-session projections for a
    Task.  Those rows commonly have identifiers such as ``role-session::*``
    and may be updated to ``idle`` after the actual NativeRuntimeV2 row has
    completed.  The production NativeRuntimeV2 namespace is ``rt_*``.  The
    metadata fallback recognizes older/custom ids only when the completed
    runtime artifact identifies itself and carries a RuntimeV2 ledger field.
    """

    runtime_session_id = str(
        row.get("runtime_session_id", "") or ""
    ).strip()
    if runtime_session_id.startswith("rt_"):
        return True
    metadata = dict(row.get("metadata", {}) or {})
    runtime_v2_ledger_fields = {
        "active_subagents",
        "artifact_manifest",
        "compaction_boundaries",
        "permission_requests",
        "prefetch_hits",
        "resume_cursor",
        "task_ledger",
        "verification",
    }
    return bool(
        runtime_session_id
        and str(metadata.get("runtime_session_id", "") or "").strip()
        == runtime_session_id
        and runtime_v2_ledger_fields.intersection(metadata)
    )


def _single_completed_native_execution_runtime(
    sessions: list[dict[str, Any]],
    *,
    case_id: str,
    task_id: str,
) -> dict[str, Any]:
    """Select the latest completed attempt after every older attempt settled.

    Executive or deterministic pre-delivery rework deliberately reuses the
    projected Task, so a healthy Task may own more than one ``rt_*`` row.  The
    immutable runtime ``created_at`` value is the attempt order.  Missing or
    tied timestamps and any nonterminal attempt are ambiguous and therefore
    fail closed.
    """

    native_executions = [
        row
        for row in sessions
        if _is_native_runtime_v2_execution_session(row)
    ]
    all_rows = [
        (
            str(row.get("runtime_session_id", "") or ""),
            str(row.get("status", "") or ""),
        )
        for row in sessions
    ]
    native_rows = [
        (
            str(row.get("runtime_session_id", "") or ""),
            str(row.get("status", "") or ""),
        )
        for row in native_executions
    ]
    if not native_executions:
        raise AssertionError(
            f"{case_id}: native Task {task_id} has no "
            "NativeRuntimeV2 execution runtime; "
            f"native={native_rows} all_rows={all_rows}"
        )
    dated_executions = [
        (
            _durable_ledger_created_at(
                row,
                evidence_label=(
                    f"{case_id}: native Task {task_id} runtime "
                    f"{str(row.get('runtime_session_id', '') or '').strip()}"
                ),
            )[0],
            row,
        )
        for row in native_executions
    ]
    latest_created_at = max(created_at for created_at, _row in dated_executions)
    latest_rows = [
        row
        for created_at, row in dated_executions
        if created_at == latest_created_at
    ]
    if len(latest_rows) != 1:
        raise AssertionError(
            f"{case_id}: native Task {task_id} has ambiguous latest "
            "NativeRuntimeV2 attempts with the same created_at; "
            f"native={native_rows} all_rows={all_rows}"
        )
    terminal_statuses = {"completed", "failed", "cancelled", "suspended"}
    unsettled = [
        (
            str(row.get("runtime_session_id", "") or ""),
            str(row.get("status", "") or ""),
        )
        for row in native_executions
        if str(row.get("status", "") or "").strip().lower()
        not in terminal_statuses
    ]
    if unsettled:
        raise AssertionError(
            f"{case_id}: native Task {task_id} has unsettled "
            f"NativeRuntimeV2 attempts: {unsettled}; all_rows={all_rows}"
        )
    execution = latest_rows[0]
    if str(execution.get("status", "") or "").strip() != "completed":
        raise AssertionError(
            f"{case_id}: terminal native Task {task_id} latest "
            "NativeRuntimeV2 attempt is not completed: "
            f"native={native_rows} all_rows={all_rows}"
        )
    return execution


def _durable_ledger_created_at(
    row: dict[str, Any],
    *,
    evidence_label: str,
) -> tuple[datetime, str]:
    """Read one persisted ledger timestamp in a comparison-safe form."""

    raw = row.get("created_at")
    rendered = raw.isoformat() if isinstance(raw, datetime) else str(raw or "").strip()
    if not rendered:
        raise AssertionError(f"{evidence_label} lacks durable created_at")
    try:
        parsed = (
            raw
            if isinstance(raw, datetime)
            else datetime.fromisoformat(rendered.replace("Z", "+00:00"))
        )
    except (TypeError, ValueError) as exc:
        raise AssertionError(
            f"{evidence_label} has invalid durable created_at: {rendered!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed, rendered


def _work_item_projection_id(task: Any) -> str:
    return str(
        dict(getattr(task, "metadata", {}) or {}).get(
            "work_item_projection_id", ""
        )
        or ""
    ).strip()


def _native_runtime_attempt_records(
    sessions: list[dict[str, Any]],
    *,
    case_id: str,
    task_id: str,
) -> list[dict[str, str]]:
    records: list[tuple[datetime, dict[str, str]]] = []
    for row in sessions:
        if not _is_native_runtime_v2_execution_session(row):
            continue
        runtime_session_id = str(
            row.get("runtime_session_id", "") or ""
        ).strip()
        created_at, rendered_created_at = _durable_ledger_created_at(
            row,
            evidence_label=(
                f"{case_id}: native Task {task_id} runtime {runtime_session_id}"
            ),
        )
        records.append(
            (
                created_at,
                {
                    "runtime_session_id": runtime_session_id,
                    "status": str(row.get("status", "") or "").strip(),
                    "created_at": rendered_created_at,
                    "updated_at": str(row.get("updated_at", "") or "").strip(),
                },
            )
        )
    records.sort(key=lambda item: (item[0], item[1]["runtime_session_id"]))
    return [record for _created_at, record in records]


async def _investment_runtime_details_from_store(
    store: Any,
    *,
    project_id: str,
    role_tasks: dict[str, Any],
    case_id: str,
) -> list[dict[str, Any]]:
    list_sessions = getattr(store, "list_runtime_sessions", None)
    list_calls = getattr(store, "list_runtime_tool_calls", None)
    list_results = getattr(store, "list_runtime_tool_results", None)
    if not all(callable(item) for item in (list_sessions, list_calls, list_results)):
        raise RuntimeError(
            "investment pre-delivery validation requires the durable native ledger"
        )

    details: list[dict[str, Any]] = []
    for role_id in ("investment_analyst", "risk_analyst"):
        task = role_tasks.get(role_id)
        if task is None:
            raise AssertionError(
                f"investment: durable {role_id} execute projection is missing"
            )
        task_id = str(getattr(task, "id", "") or "").strip()
        projection_id = _work_item_projection_id(task)
        if not task_id or not projection_id:
            raise AssertionError(
                f"investment: durable {role_id} execute identity is incomplete"
            )
        sessions = list(
            await list_sessions(
                project_id=project_id,
                task_id=task_id,
                limit=100,
            )
        )
        selected = _single_completed_native_execution_runtime(
            sessions,
            case_id=f"{case_id} {role_id}",
            task_id=task_id,
        )
        runtime_session_id = str(
            selected.get("runtime_session_id", "") or ""
        ).strip()
        _created_at, rendered_created_at = _durable_ledger_created_at(
            selected,
            evidence_label=(
                f"{case_id}: selected {role_id} runtime {runtime_session_id}"
            ),
        )
        details.append(
            {
                "task_id": task_id,
                "role_id": role_id,
                "work_item_turn_type": str(
                    dict(getattr(task, "metadata", {}) or {}).get(
                        "work_item_turn_type", ""
                    )
                    or ""
                ).strip(),
                "work_item_projection_id": projection_id,
                "runtime_session_id": runtime_session_id,
                "runtime_created_at": rendered_created_at,
                "runtime_attempts": _native_runtime_attempt_records(
                    sessions,
                    case_id=case_id,
                    task_id=task_id,
                ),
                "calls": list(await list_calls(runtime_session_id)),
                "results": list(await list_results(runtime_session_id)),
            }
        )
    return details


def _investment_execute_role_tasks(
    durable_tasks: list[Any],
    *,
    case_id: str,
) -> dict[str, Any]:
    """Select the two canonical analyst execute projections for evidence."""

    role_tasks: dict[str, Any] = {}
    for role_id in ("investment_analyst", "risk_analyst"):
        matches = [
            task
            for task in durable_tasks
            if str(getattr(task, "assigned_to", "") or "").strip()
            == role_id
            and str(
                dict(getattr(task, "metadata", {}) or {}).get(
                    "work_item_turn_type", ""
                )
                or ""
            ).strip()
            == "execute"
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"{case_id}: expected one durable {role_id} execute "
                f"projection, got {len(matches)}"
            )
        role_tasks[role_id] = matches[0]
    return role_tasks


async def _investment_execute_runtime_details_from_store(
    store: Any,
    *,
    project_id: str,
    durable_tasks: list[Any],
    case_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load the canonical analyst evidence scope from the durable ledger."""

    role_tasks = _investment_execute_role_tasks(
        durable_tasks,
        case_id=case_id,
    )
    runtime_details = await _investment_runtime_details_from_store(
        store,
        project_id=project_id,
        role_tasks=role_tasks,
        case_id=case_id,
    )
    return role_tasks, runtime_details


def _investment_artifact_hashes(workplace: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for relative_path in INVESTMENT_REQUIRED_ARTIFACTS:
        path = workplace / relative_path
        if not path.is_file():
            raise AssertionError(
                f"investment: required artifact is missing: {relative_path}"
            )
        hashes[relative_path] = _sha256(path)
    return hashes


def _investment_artifact_snapshot_state(workplace: Path) -> dict[str, str]:
    """Content-address valid and invalid artifact states for race detection."""

    state: dict[str, str] = {}
    for relative_path in INVESTMENT_REQUIRED_ARTIFACTS:
        path = workplace / relative_path
        try:
            state[relative_path] = f"sha256:{_sha256(path)}"
        except OSError as exc:
            errno = getattr(exc, "errno", None)
            state[relative_path] = f"error:{type(exc).__name__}:{errno}"
    return state


def _investment_consumed_web_ledger(
    runtime_details: list[dict[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    provenance = _investment_web_provenance(runtime_details)
    consumed_call_ids = {
        (
            role_id,
            str(call.get("runtime_session_id", "") or "").strip(),
            str(call.get("tool_call_id", "") or "").strip(),
        )
        for role_id, calls in dict(provenance["calls_by_role"]).items()
        for call in list(calls or [])
    }
    consumed_calls: list[dict[str, str]] = []
    consumed_results: list[dict[str, str]] = []
    seen_calls: set[tuple[str, str]] = set()
    seen_results: set[tuple[str, str]] = set()
    for detail in sorted(
        runtime_details,
        key=lambda item: (
            str(item.get("role_id", "") or ""),
            str(item.get("runtime_session_id", "") or ""),
        ),
    ):
        role_id = str(detail.get("role_id", "") or "").strip()
        runtime_session_id = str(
            detail.get("runtime_session_id", "") or ""
        ).strip()
        calls = [
            call
            for call in list(detail.get("calls", []) or [])
            if str(call.get("tool_name", "") or "").strip() == "web_search"
        ]
        results = [
            result
            for result in list(detail.get("results", []) or [])
            if str(result.get("tool_name", "") or "").strip() == "web_search"
        ]
        call_ids: set[str] = set()
        for call in calls:
            tool_call_id = str(call.get("tool_call_id", "") or "").strip()
            if (
                role_id,
                runtime_session_id,
                tool_call_id,
            ) not in consumed_call_ids:
                continue
            if not runtime_session_id or not tool_call_id:
                raise AssertionError(
                    f"investment: {role_id} durable web_search ToolCall identity is incomplete"
                )
            identity = (runtime_session_id, tool_call_id)
            if identity in seen_calls:
                raise AssertionError(
                    f"investment: duplicate durable web_search ToolCall identity {identity!r}"
                )
            seen_calls.add(identity)
            call_ids.add(tool_call_id)
            consumed_calls.append(
                {
                    "role_id": role_id,
                    "runtime_session_id": runtime_session_id,
                    "tool_call_id": tool_call_id,
                }
            )
        for result in results:
            tool_call_id = str(result.get("tool_call_id", "") or "").strip()
            if tool_call_id not in call_ids:
                continue
            result_record_id = str(
                result.get("result_record_id", "") or ""
            ).strip()
            if not result_record_id:
                raise AssertionError(
                    f"investment: {role_id} durable web_search ToolResult identity is incomplete"
                )
            identity = (runtime_session_id, result_record_id)
            if identity in seen_results:
                raise AssertionError(
                    f"investment: duplicate durable web_search ToolResult identity {identity!r}"
                )
            seen_results.add(identity)
            consumed_results.append(
                {
                    "role_id": role_id,
                    "runtime_session_id": runtime_session_id,
                    "tool_call_id": tool_call_id,
                    "result_record_id": result_record_id,
                }
            )
    consumed_calls.sort(
        key=lambda item: (
            item["role_id"],
            item["runtime_session_id"],
            item["tool_call_id"],
        )
    )
    consumed_results.sort(
        key=lambda item: (
            item["role_id"],
            item["runtime_session_id"],
            item["tool_call_id"],
            item["result_record_id"],
        )
    )
    return {
        "tool_calls": consumed_calls,
        "tool_results": consumed_results,
    }


def _investment_quality_snapshot(
    workplace: Path,
    runtime_details: list[dict[str, Any]],
    run_started_at: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Run the gate against one stable, content-addressed artifact snapshot."""

    runtime_details = _investment_evidence_runtime_scope(runtime_details)
    before = _investment_artifact_hashes(workplace)
    quality_gate = _investment_data_quality_gate(
        workplace,
        runtime_details,
        run_started_at,
    )
    after = _investment_artifact_hashes(workplace)
    if before != after:
        raise RuntimeError(
            "investment artifact content changed during deterministic "
            "pre-delivery validation"
        )
    return quality_gate, after


def _investment_evidence_runtime_scope(
    runtime_details: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate and order the exact analyst execute evidence boundary."""

    expected_roles = ("investment_analyst", "risk_analyst")
    if len(runtime_details) != len(expected_roles):
        raise AssertionError(
            "investment: pre-delivery evidence requires exactly two analyst "
            f"execute runtimes, got {len(runtime_details)}"
        )
    details_by_role: dict[str, dict[str, Any]] = {}
    for detail in runtime_details:
        role_id = str(detail.get("role_id", "") or "").strip()
        if role_id not in expected_roles or role_id in details_by_role:
            raise AssertionError(
                "investment: pre-delivery evidence has an unexpected or "
                f"duplicate runtime role: {role_id!r}"
            )
        if (
            str(detail.get("work_item_turn_type", "") or "").strip()
            != "execute"
        ):
            raise AssertionError(
                "investment: pre-delivery evidence contains a non-execute "
                f"runtime for {role_id}"
            )
        identity = {
            "task_id": str(detail.get("task_id", "") or "").strip(),
            "work_item_projection_id": str(
                detail.get("work_item_projection_id", "") or ""
            ).strip(),
            "runtime_session_id": str(
                detail.get("runtime_session_id", "") or ""
            ).strip(),
        }
        if not all(identity.values()):
            raise AssertionError(
                "investment: pre-delivery evidence runtime identity is "
                f"incomplete for {role_id}: {identity}"
            )
        details_by_role[role_id] = detail
    if set(details_by_role) != set(expected_roles):
        raise AssertionError(
            "investment: pre-delivery evidence lacks one canonical analyst "
            "execute runtime"
        )
    ordered = [details_by_role[role_id] for role_id in expected_roles]
    for identity_key in (
        "task_id",
        "work_item_projection_id",
        "runtime_session_id",
    ):
        identities = {
            str(detail.get(identity_key, "") or "").strip()
            for detail in ordered
        }
        if len(identities) != len(expected_roles):
            raise AssertionError(
                "investment: pre-delivery evidence aliases analyst runtime "
                f"identity field {identity_key}"
            )
    return ordered


def _investment_pre_delivery_evidence(
    *,
    project_id: str,
    delegation_run_id: str,
    root_session_id: str,
    run_started_at: str,
    workplace: Path,
    runtime_details: list[dict[str, Any]],
    quality_gate: dict[str, Any],
    artifact_sha256: dict[str, str] | None = None,
    consumed_web_ledger: dict[str, list[dict[str, str]]] | None = None,
) -> dict[str, Any]:
    runtime_details = _investment_evidence_runtime_scope(runtime_details)
    canonical_consumed_web_ledger = _investment_consumed_web_ledger(
        runtime_details
    )
    if (
        consumed_web_ledger is not None
        and dict(consumed_web_ledger) != canonical_consumed_web_ledger
    ):
        raise AssertionError(
            "investment: supplied consumed web ledger does not match the "
            "canonical analyst execute scope"
        )
    return {
        "validator_id": "issue35_investment_data_quality",
        "schema_version": 1,
        "scope": "investment",
        "project_id": project_id,
        "delegation_run_id": delegation_run_id,
        "root_session_id": root_session_id,
        "run_started_at": run_started_at,
        "artifact_sha256": dict(
            artifact_sha256
            if artifact_sha256 is not None
            else _investment_artifact_hashes(workplace)
        ),
        "consumed_web_ledger": canonical_consumed_web_ledger,
        "runtime_inputs": [
            {
                "role_id": str(detail.get("role_id", "") or ""),
                "task_id": str(detail.get("task_id", "") or ""),
                "work_item_projection_id": str(
                    detail.get("work_item_projection_id", "") or ""
                ),
                "runtime_session_id": str(
                    detail.get("runtime_session_id", "") or ""
                ),
                "runtime_created_at": str(
                    detail.get("runtime_created_at", "") or ""
                ),
                "runtime_attempts": list(
                    detail.get("runtime_attempts", []) or []
                ),
            }
            for detail in runtime_details
        ],
        "quality_gate": quality_gate,
    }


def _investment_rework_targets_for_issue(
    issue: str,
    *,
    role_projection_ids: dict[str, str],
    delivery_projection_id: str,
) -> list[str]:
    domains = _investment_issue_domains(issue)
    targets: list[str] = []
    if "report" in domains:
        targets.append(delivery_projection_id)
    elif "company" in domains:
        targets.append(role_projection_ids.get("investment_analyst", ""))
    if "report" not in domains and "risk" in domains:
        targets.append(role_projection_ids.get("risk_analyst", ""))
    if "unknown" in domains:
        targets.extend(
            role_projection_ids.get(role_id, "")
            for role_id in ("investment_analyst", "risk_analyst")
        )
    normalized = list(
        dict.fromkeys(str(item or "").strip() for item in targets if str(item or "").strip())
    )
    return normalized or ([delivery_projection_id] if delivery_projection_id else [])


def _investment_rework_targets_for_issues(
    issues: list[str],
    *,
    role_projection_ids: dict[str, str],
    delivery_projection_id: str,
) -> list[str]:
    """Union all domain targets so one validator callback causes one rework round."""

    targets = [
        projection_id
        for issue in issues
        for projection_id in _investment_rework_targets_for_issue(
            issue,
            role_projection_ids=role_projection_ids,
            delivery_projection_id=delivery_projection_id,
        )
    ]
    return list(dict.fromkeys(target for target in targets if target))


def _investment_rework_issues_by_projection_id(
    issues: list[str],
    *,
    role_projection_ids: dict[str, str],
    delivery_projection_id: str,
) -> dict[str, list[str]]:
    """Keep each deterministic blocker scoped to the owner that can fix it."""

    routed: dict[str, list[str]] = {}
    for issue in issues:
        for projection_id in _investment_rework_targets_for_issue(
            issue,
            role_projection_ids=role_projection_ids,
            delivery_projection_id=delivery_projection_id,
        ):
            routed.setdefault(projection_id, [])
            if issue not in routed[projection_id]:
                routed[projection_id].append(issue)
    return routed


class _Issue35PreDeliveryQualityValidator:
    """E2E-only deterministic gate injected into every custom child Engine."""

    def __init__(self, *, workplace: Path, project_id: str) -> None:
        self.workplace = workplace
        self.project_id = str(project_id or "default").strip() or "default"
        self.store: Any | None = None
        self.runs_by_session: dict[str, CaseRun] = {}

    def bind_store(self, store: Any) -> None:
        self.store = store

    def register_runs(self, runs: list[CaseRun]) -> None:
        self.runs_by_session = {run.session_id: run for run in runs}

    def _registered_run(self, delivery_task: Any) -> CaseRun:
        parent_session_id = str(
            getattr(delivery_task, "parent_session_id", "") or ""
        ).strip()
        delivery_session_id = str(
            getattr(delivery_task, "session_id", "") or ""
        ).strip()
        matches = [
            run
            for session_id, run in self.runs_by_session.items()
            if parent_session_id == session_id
            or delivery_session_id == session_id
            or delivery_session_id.startswith(f"{session_id}:")
        ]
        if len(matches) != 1:
            raise AssertionError(
                "issue35: delivery does not map to exactly one registered E2E session"
            )
        return matches[0]

    async def __call__(
        self,
        delivery_task: Any,
        plan: Any,
        tasks: list[Any],
        delivery_package: dict[str, Any],
    ) -> dict[str, Any]:
        del plan, delivery_package
        delivery_metadata = dict(
            getattr(delivery_task, "metadata", {}) or {}
        )
        org_id = str(
            delivery_metadata.get("org_id")
            or delivery_metadata.get("organization_id")
            or ""
        ).strip()
        registered_run = self._registered_run(delivery_task)
        if registered_run.spec.case_id != "investment":
            if (
                registered_run.spec.case_id != "app"
                or org_id != registered_run.spec.org_id
            ):
                raise AssertionError(
                    "issue35: non-investment delivery identity drifted from its "
                    "registered E2E case"
                )
            return {
                "valid": True,
                "evidence": {
                    "validator_id": "issue35_investment_data_quality",
                    "schema_version": 1,
                    "scope": "not_applicable",
                    "org_id": org_id,
                },
                "issues": [],
                "rework_target_projection_ids": [],
            }

        delivery_projection_id = _work_item_projection_id(delivery_task)
        role_projection_ids = {
            str(getattr(task, "assigned_to", "") or "").strip():
            _work_item_projection_id(task)
            for task in tasks
            if str(
                dict(getattr(task, "metadata", {}) or {}).get(
                    "work_item_turn_type", ""
                )
                or ""
            ).strip()
            == "execute"
        }
        evidence_context: dict[str, Any] = {
            "validator_id": "issue35_investment_data_quality",
            "schema_version": 1,
            "scope": "investment",
            "project_id": self.project_id,
        }
        if org_id != registered_run.spec.org_id:
            raise RuntimeError(
                "investment: delivery org_id must equal the registered "
                f"organization {registered_run.spec.org_id!r}; got {org_id!r}"
            )
        if self.store is None:
            raise RuntimeError(
                "investment pre-delivery validator is not bound to the durable Store"
            )
        task_project_id = str(
            getattr(delivery_task, "project_id", "") or ""
        ).strip()
        if task_project_id != self.project_id:
            raise RuntimeError(
                "investment: delivery crossed the configured E2E project scope"
            )
        root_session_id = registered_run.session_id
        run_started_at = registered_run.started_at
        delegation_run_id = str(
            delivery_metadata.get("delegation_run_id", "") or ""
        ).strip()
        if not delegation_run_id:
            raise RuntimeError(
                "investment: delivery lacks a durable delegation_run_id"
            )
        evidence_context.update(
            {
                "delegation_run_id": delegation_run_id,
                "root_session_id": root_session_id,
                "run_started_at": run_started_at,
            }
        )
        get_tasks = getattr(self.store, "get_tasks", None)
        if not callable(get_tasks):
            raise RuntimeError(
                "investment pre-delivery validator cannot query durable Tasks"
            )
        durable_tasks = [
            task
            for task in list(await get_tasks(project_id=self.project_id))
            if str(
                dict(getattr(task, "metadata", {}) or {}).get(
                    "delegation_run_id", ""
                )
                or ""
            ).strip()
            == delegation_run_id
        ]
        durable_role_tasks, runtime_details = (
            await _investment_execute_runtime_details_from_store(
                self.store,
                project_id=self.project_id,
                durable_tasks=durable_tasks,
                case_id="investment pre-delivery",
            )
        )
        for role_id in ("investment_analyst", "risk_analyst"):
            role_projection_ids[role_id] = _work_item_projection_id(
                durable_role_tasks[role_id]
            )
        consumed_web_ledger = _investment_consumed_web_ledger(runtime_details)
        try:
            quality_gate, artifact_sha256 = _investment_quality_snapshot(
                self.workplace,
                runtime_details,
                run_started_at,
            )
            evidence = _investment_pre_delivery_evidence(
                project_id=self.project_id,
                delegation_run_id=delegation_run_id,
                root_session_id=root_session_id,
                run_started_at=run_started_at,
                workplace=self.workplace,
                runtime_details=runtime_details,
                quality_gate=quality_gate,
                artifact_sha256=artifact_sha256,
                consumed_web_ledger=consumed_web_ledger,
            )
            return {
                "valid": True,
                "evidence": evidence,
                "issues": [],
                "rework_target_projection_ids": [],
            }
        except (AssertionError, OSError, UnicodeError) as exc:
            original_issue = str(exc).strip() or type(exc).__name__
            snapshot_before = _investment_artifact_snapshot_state(
                self.workplace
            )
            issues = _investment_quality_issues(
                self.workplace,
                runtime_details,
                run_started_at,
            )
            snapshot_after = _investment_artifact_snapshot_state(
                self.workplace
            )
            if snapshot_before != snapshot_after:
                raise RuntimeError(
                    "investment artifact content changed during deterministic "
                    "pre-delivery failure aggregation"
                ) from exc
            if not issues:
                issues = [original_issue]
            rework_issues_by_projection_id = (
                _investment_rework_issues_by_projection_id(
                    issues,
                    role_projection_ids=role_projection_ids,
                    delivery_projection_id=delivery_projection_id,
                )
            )
            return {
                "valid": False,
                "evidence": {
                    **evidence_context,
                    "quality_failure": issues[0],
                    "quality_failures": issues,
                },
                "issues": issues,
                "rework_target_projection_ids": list(
                    rework_issues_by_projection_id
                ),
                "rework_issues_by_projection_id": (
                    rework_issues_by_projection_id
                ),
            }


def _assert_investment_pre_delivery_evidence_matches(
    persisted_evidence: dict[str, Any],
    *,
    project_id: str,
    delegation_run_id: str,
    root_session_id: str,
    run_started_at: str,
    workplace: Path,
    runtime_details: list[dict[str, Any]],
    quality_gate: dict[str, Any],
) -> dict[str, Any]:
    expected = _investment_pre_delivery_evidence(
        project_id=project_id,
        delegation_run_id=delegation_run_id,
        root_session_id=root_session_id,
        run_started_at=run_started_at,
        workplace=workplace,
        runtime_details=runtime_details,
        quality_gate=quality_gate,
    )
    if persisted_evidence != expected:
        raise AssertionError(
            "investment: persisted pre-delivery quality evidence no longer "
            "matches the late durable-ledger and artifact recomputation"
        )
    return {
        "matched": True,
        "artifact_sha256": dict(expected["artifact_sha256"]),
        "consumed_tool_call_count": len(
            expected["consumed_web_ledger"]["tool_calls"]
        ),
        "consumed_tool_result_count": len(
            expected["consumed_web_ledger"]["tool_results"]
        ),
    }


def _matching_native_tool_results(
    detail: dict[str, Any],
    call: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        result
        for result in list(detail.get("results", []) or [])
        if str(result.get("tool_call_id", "") or "").strip()
        == str(call.get("tool_call_id", "") or "").strip()
        and str(result.get("tool_name", "") or "").strip()
        == str(call.get("tool_name", "") or "").strip()
    ]


def _native_tool_result_succeeded(result: dict[str, Any]) -> bool:
    payload = dict(result.get("payload", {}) or {})
    nested = payload.get("result")
    return payload.get("success") is True and not (
        isinstance(nested, dict) and nested.get("success") is False
    )


def _native_tool_ledger_closure(
    spec: CaseSpec,
    *,
    runtime_details: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Prove every ToolCall in every native attempt has one exact ToolResult."""

    seen_runtime_sessions: set[str] = set()
    seen_calls: set[tuple[str, str, str]] = set()
    seen_results: set[tuple[str, str]] = set()
    evidence: list[dict[str, str]] = []
    for detail in runtime_details:
        runtime_session_id = str(
            detail.get("runtime_session_id", "") or ""
        ).strip()
        if not runtime_session_id or runtime_session_id in seen_runtime_sessions:
            raise AssertionError(
                f"{spec.case_id}: native attempt details contain a missing or "
                f"duplicate runtime_session_id: {runtime_session_id!r}"
            )
        seen_runtime_sessions.add(runtime_session_id)
        calls = list(detail.get("calls", []) or [])
        results = list(detail.get("results", []) or [])
        call_keys: set[tuple[str, str]] = set()
        for call in calls:
            tool_call_id = str(call.get("tool_call_id", "") or "").strip()
            tool_name = str(call.get("tool_name", "") or "").strip()
            key = (runtime_session_id, tool_call_id, tool_name)
            if not tool_call_id or not tool_name or key in seen_calls:
                raise AssertionError(
                    f"{spec.case_id}: native ToolCall identity is missing or "
                    f"duplicated across attempts: {key!r}"
                )
            seen_calls.add(key)
            call_keys.add((tool_call_id, tool_name))
            matching_results = [
                result
                for result in results
                if str(result.get("tool_call_id", "") or "").strip()
                == tool_call_id
                and str(result.get("tool_name", "") or "").strip()
                == tool_name
            ]
            if len(matching_results) != 1:
                raise AssertionError(
                    f"{spec.case_id}: native ToolCall {key!r} must have exactly "
                    f"one durable ToolResult, got {len(matching_results)}"
                )
            result_record_id = str(
                matching_results[0].get("result_record_id", "") or ""
            ).strip()
            result_identity = (runtime_session_id, result_record_id)
            if not result_record_id or result_identity in seen_results:
                raise AssertionError(
                    f"{spec.case_id}: native ToolResult identity is missing or "
                    f"duplicated across attempts: {result_identity!r}"
                )
            seen_results.add(result_identity)
            evidence.append(
                {
                    "runtime_session_id": runtime_session_id,
                    "task_id": str(detail.get("task_id", "") or ""),
                    "role_id": str(detail.get("role_id", "") or ""),
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "result_record_id": result_record_id,
                }
            )
        orphan_results = [
            (
                str(result.get("tool_call_id", "") or "").strip(),
                str(result.get("tool_name", "") or "").strip(),
                str(result.get("result_record_id", "") or "").strip(),
            )
            for result in results
            if (
                str(result.get("tool_call_id", "") or "").strip(),
                str(result.get("tool_name", "") or "").strip(),
            )
            not in call_keys
        ]
        if orphan_results:
            raise AssertionError(
                f"{spec.case_id}: native attempt {runtime_session_id} has "
                f"orphan durable ToolResults: {orphan_results}"
            )
    return evidence


def _native_successful_file_mutations(
    spec: CaseSpec,
    workplace: Path,
    *,
    runtime_details: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collect every successful native file mutation and reject extra targets."""

    allowed_targets = {
        (workplace / relative).resolve(): relative
        for relative in spec.required_artifacts
    }
    evidence: list[dict[str, Any]] = []
    for detail in runtime_details:
        runtime_session_id = str(
            detail.get("runtime_session_id", "") or ""
        ).strip()
        for call in list(detail.get("calls", []) or []):
            tool_name = str(call.get("tool_name", "") or "").strip()
            if tool_name not in {"file_write", "file_edit"}:
                continue
            tool_call_id = str(call.get("tool_call_id", "") or "").strip()
            matches = _matching_native_tool_results(detail, call)
            if len(matches) != 1:
                raise AssertionError(
                    f"{spec.case_id}: native {tool_name} ToolCall "
                    f"{(runtime_session_id, tool_call_id)!r} must have exactly "
                    f"one durable ToolResult, got {len(matches)}"
                )
            result = matches[0]
            if not _native_tool_result_succeeded(result):
                continue
            target = _path_in_workplace(
                dict(call.get("arguments", {}) or {}).get("path"),
                workplace,
            )
            relative = allowed_targets.get(target)
            if relative is None:
                raise AssertionError(
                    f"{spec.case_id}: successful native {tool_name} wrote an "
                    f"unexpected artifact outside required_artifacts: {target}"
                )
            call_at, call_created_at = _durable_ledger_created_at(
                call,
                evidence_label=(
                    f"{spec.case_id} native {tool_name} ToolCall {tool_call_id}"
                ),
            )
            result_at, result_created_at = _durable_ledger_created_at(
                result,
                evidence_label=(
                    f"{spec.case_id} native {tool_name} ToolResult {tool_call_id}"
                ),
            )
            if result_at < call_at:
                raise AssertionError(
                    f"{spec.case_id}: native {tool_name} ToolResult "
                    f"{tool_call_id!r} predates its ToolCall"
                )
            evidence.append(
                {
                    "runtime_session_id": runtime_session_id,
                    "task_id": str(detail.get("task_id", "") or ""),
                    "role_id": str(detail.get("role_id", "") or ""),
                    "tool_call_id": tool_call_id,
                    "result_record_id": str(
                        result.get("result_record_id", "") or ""
                    ),
                    "tool_name": tool_name,
                    "relative_path": relative,
                    "call_created_at": call_created_at,
                    "result_created_at": result_created_at,
                }
            )
    return evidence


def _app_native_tool_contract(
    spec: CaseSpec,
    workplace: Path,
    *,
    runtime_details: list[dict[str, Any]],
    successful_file_mutations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Prove developer writes precede one successful native QA syntax check."""

    if spec.case_id != "app":
        raise AssertionError("app native tool contract requires the app case")
    mutations = (
        list(successful_file_mutations)
        if successful_file_mutations is not None
        else _native_successful_file_mutations(
            spec,
            workplace,
            runtime_details=runtime_details,
        )
    )
    developer_by_artifact: dict[str, list[dict[str, Any]]] = {}
    for item in mutations:
        if str(item.get("role_id", "") or "") != "developer":
            continue
        developer_by_artifact.setdefault(
            str(item.get("relative_path", "") or ""),
            [],
        ).append(item)
    missing = sorted(
        set(spec.required_artifacts) - set(developer_by_artifact)
    )
    if missing:
        raise AssertionError(
            f"{spec.case_id}: developer lacks successful native write ledger "
            f"entries for {missing}"
        )

    latest_developer_writes: dict[str, dict[str, Any]] = {}
    for relative in spec.required_artifacts:
        latest_developer_writes[relative] = max(
            developer_by_artifact[relative],
            key=lambda item: _durable_ledger_created_at(
                {"created_at": item.get("result_created_at")},
                evidence_label=(
                    f"{spec.case_id} developer write result for {relative}"
                ),
            )[0],
        )
    last_developer_write = max(
        latest_developer_writes.values(),
        key=lambda item: _durable_ledger_created_at(
            {"created_at": item.get("result_created_at")},
            evidence_label="app final developer write result",
        )[0],
    )
    last_write_at, last_write_created_at = _durable_ledger_created_at(
        {"created_at": last_developer_write.get("result_created_at")},
        evidence_label="app final developer write result",
    )

    command = "node --check app_case/app.js"
    qa_successes: list[dict[str, Any]] = []
    for detail in runtime_details:
        if str(detail.get("role_id", "") or "") != "qa_engineer":
            continue
        runtime_session_id = str(
            detail.get("runtime_session_id", "") or ""
        ).strip()
        for call in list(detail.get("calls", []) or []):
            if str(call.get("tool_name", "") or "").strip() != "shell_exec":
                continue
            arguments = dict(call.get("arguments", {}) or {})
            if str(arguments.get("command", "") or "").strip() != command:
                continue
            if not runtime_session_id.startswith("rt_"):
                raise AssertionError(
                    f"{spec.case_id}: required QA validation did not run in a "
                    f"true rt_* NativeRuntimeV2 session: {runtime_session_id!r}"
                )
            _validate_test_tool_call(
                spec,
                workplace,
                {"name": "shell_exec", "arguments": arguments},
            )
            tool_call_id = str(call.get("tool_call_id", "") or "").strip()
            matches = _matching_native_tool_results(detail, call)
            if len(matches) != 1:
                raise AssertionError(
                    f"{spec.case_id}: QA exact validation ToolCall "
                    f"{tool_call_id!r} must have exactly one durable ToolResult, "
                    f"got {len(matches)}"
                )
            result = matches[0]
            if not _native_tool_result_succeeded(result):
                raise AssertionError(
                    f"{spec.case_id}: QA exact validation ToolCall "
                    f"{tool_call_id!r} did not succeed"
                )
            call_at, call_created_at = _durable_ledger_created_at(
                call,
                evidence_label=f"{spec.case_id} QA validation ToolCall {tool_call_id}",
            )
            result_at, result_created_at = _durable_ledger_created_at(
                result,
                evidence_label=f"{spec.case_id} QA validation ToolResult {tool_call_id}",
            )
            if result_at < call_at:
                raise AssertionError(
                    f"{spec.case_id}: QA validation ToolResult "
                    f"{tool_call_id!r} predates its ToolCall"
                )
            qa_successes.append(
                {
                    "runtime_session_id": runtime_session_id,
                    "task_id": str(detail.get("task_id", "") or ""),
                    "role_id": "qa_engineer",
                    "tool_call_id": tool_call_id,
                    "result_record_id": str(
                        result.get("result_record_id", "") or ""
                    ),
                    "command": command,
                    "call_created_at": call_created_at,
                    "result_created_at": result_created_at,
                    "_call_at": call_at,
                }
            )
    if len(qa_successes) != 1:
        raise AssertionError(
            f"{spec.case_id}: expected exactly one successful qa_engineer "
            f"{command!r} call in a true rt_* runtime, got {len(qa_successes)}"
        )
    qa = qa_successes[0]
    if qa["_call_at"] <= last_write_at:
        raise AssertionError(
            f"{spec.case_id}: QA validation began at "
            f"{qa['call_created_at']!r} before the final developer write "
            f"ToolResult at {last_write_created_at!r}"
        )
    qa.pop("_call_at", None)
    qa["after_last_developer_write"] = True
    qa["last_developer_write_result_created_at"] = last_write_created_at
    return {
        "successful_file_mutations": mutations,
        "developer_artifact_writes": latest_developer_writes,
        "qa_javascript_validation": qa,
    }


def _app_delegation_dependency_contract(
    work_items: list[Any],
) -> dict[str, Any]:
    """Prove the durable QA item remains hard-dependent on implementation."""

    developer_items = [
        item
        for item in work_items
        if str(getattr(item, "role_id", "") or "") == "developer"
        and str(dict(getattr(item, "metadata", {}) or {}).get("scope_key", "") or "")
        == APP_DEVELOPER_SCOPE_KEY
    ]
    qa_items = [
        item
        for item in work_items
        if str(getattr(item, "role_id", "") or "") == "qa_engineer"
        and str(dict(getattr(item, "metadata", {}) or {}).get("scope_key", "") or "")
        == APP_QA_SCOPE_KEY
    ]
    if len(developer_items) != 1 or len(qa_items) != 1:
        raise AssertionError(
            "app: expected exactly one durable developer/QA item with the "
            f"required stable scope keys; developer={len(developer_items)} "
            f"qa={len(qa_items)}"
        )
    developer = developer_items[0]
    qa = qa_items[0]
    developer_id = str(getattr(developer, "work_item_id", "") or "").strip()
    qa_id = str(getattr(qa, "work_item_id", "") or "").strip()
    developer_metadata = dict(getattr(developer, "metadata", {}) or {})
    qa_metadata = dict(getattr(qa, "metadata", {}) or {})
    developer_batch = str(getattr(developer, "batch_id", "") or "").strip()
    qa_batch = str(getattr(qa, "batch_id", "") or "").strip()
    developer_scope = (
        str(getattr(developer, "run_id", "") or "").strip(),
        developer_batch,
        str(getattr(developer, "parent_work_item_id", "") or "").strip(),
        str(getattr(developer, "source_seat_id", "") or "").strip(),
    )
    qa_scope = (
        str(getattr(qa, "run_id", "") or "").strip(),
        qa_batch,
        str(getattr(qa, "parent_work_item_id", "") or "").strip(),
        str(getattr(qa, "source_seat_id", "") or "").strip(),
    )

    def _required_nonnegative_integer(
        metadata: dict[str, Any],
        key: str,
    ) -> int:
        value = metadata.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AssertionError(
                f"app: {key} must be a non-negative durable integer; "
                f"developer={developer_metadata!r} qa={qa_metadata!r}"
            )
        return value

    developer_sequence = _required_nonnegative_integer(
        developer_metadata,
        "delegate_sequence_index",
    )
    qa_sequence = _required_nonnegative_integer(
        qa_metadata,
        "delegate_sequence_index",
    )
    developer_invocation_id = str(
        developer_metadata.get("delegate_invocation_id", "") or ""
    ).strip()
    qa_invocation_id = str(
        qa_metadata.get("delegate_invocation_id", "") or ""
    ).strip()
    developer_invocation_index = _required_nonnegative_integer(
        developer_metadata,
        "delegate_invocation_index",
    )
    qa_invocation_index = _required_nonnegative_integer(
        qa_metadata,
        "delegate_invocation_index",
    )
    sequencing_mode = (
        "same_invocation"
        if developer_invocation_id == qa_invocation_id
        else "split_append"
    )
    dependency_ids = [
        str(item or "").strip()
        for item in list(qa_metadata.get("dependency_work_item_ids", []) or [])
        if str(item or "").strip()
    ]
    resolved = [
        dict(item)
        for item in list(qa_metadata.get("resolved_dependencies", []) or [])
        if isinstance(item, dict)
    ]
    if (
        not developer_id
        or not qa_id
        or any(not component for component in developer_scope)
        or developer_scope != qa_scope
        or developer_metadata.get("created_by_delegate_work") is not True
        or qa_metadata.get("created_by_delegate_work") is not True
        or not developer_invocation_id
        or not qa_invocation_id
        or developer_sequence >= qa_sequence
        or (
            sequencing_mode == "same_invocation"
            and developer_invocation_index >= qa_invocation_index
        )
        or dependency_ids != [developer_id]
        or len(resolved) != 1
        or str(resolved[0].get("work_item_id", "") or "") != developer_id
        or str(resolved[0].get("resolved_by", "") or "") != "scope_key"
    ):
        raise AssertionError(
            "app: QA must remain a later same-scope durable hard dependency "
            f"of {APP_DEVELOPER_SCOPE_KEY}; developer={developer_id!r} "
            f"qa={qa_id!r} scopes={(developer_scope, qa_scope)!r} "
            f"sequences={(developer_sequence, qa_sequence)!r} "
            f"invocations={(developer_invocation_id, qa_invocation_id)!r} "
            f"dependencies={dependency_ids!r} resolved={resolved!r}"
        )
    return {
        "developer_work_item_id": developer_id,
        "developer_scope_key": APP_DEVELOPER_SCOPE_KEY,
        "qa_work_item_id": qa_id,
        "qa_scope_key": APP_QA_SCOPE_KEY,
        "batch_id": developer_batch,
        "manager_sequence_scope": {
            "run_id": developer_scope[0],
            "batch_id": developer_scope[1],
            "parent_work_item_id": developer_scope[2],
            "source_seat_id": developer_scope[3],
        },
        "sequencing_mode": sequencing_mode,
        "developer_delegate_invocation_id": developer_invocation_id,
        "developer_delegate_invocation_index": developer_invocation_index,
        "developer_delegate_sequence_index": developer_sequence,
        "qa_delegate_invocation_id": qa_invocation_id,
        "qa_delegate_invocation_index": qa_invocation_index,
        "qa_delegate_sequence_index": qa_sequence,
        "dependency_work_item_ids": dependency_ids,
        "resolved_by": "scope_key",
        "validated": True,
    }


def _native_shell_ledger_closure(
    spec: CaseSpec,
    workplace: Path,
    *,
    runtime_details: list[dict[str, Any]],
    tool_checkpoint_evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Close every native shell ledger row against the E2E allow/deny model."""

    checkpoint_by_call: dict[tuple[str, str, str], dict[str, Any]] = {}
    for checkpoint in tool_checkpoint_evidence:
        if str(checkpoint.get("tool_name", "") or "").strip() != "shell_exec":
            continue
        key = (
            str(checkpoint.get("tool_runtime_session_id", "") or "").strip(),
            str(checkpoint.get("tool_call_id", "") or "").strip(),
            "shell_exec",
        )
        if not key[0] or not key[1] or key in checkpoint_by_call:
            raise AssertionError(
                f"{spec.case_id}: shell permission checkpoints have an invalid "
                f"or duplicate native ToolCall identity: {key!r}"
            )
        checkpoint_by_call[key] = checkpoint

    evidence: list[dict[str, Any]] = []
    seen_calls: set[tuple[str, str, str]] = set()
    matched_checkpoints: set[tuple[str, str, str]] = set()
    for detail in runtime_details:
        runtime_session_id = str(
            detail.get("runtime_session_id", "") or ""
        ).strip()
        for call in list(detail.get("calls", []) or []):
            tool_name = str(call.get("tool_name", "") or "").strip()
            if tool_name != "shell_exec":
                continue
            tool_call_id = str(call.get("tool_call_id", "") or "").strip()
            key = (runtime_session_id, tool_call_id, tool_name)
            if not runtime_session_id or not tool_call_id or key in seen_calls:
                raise AssertionError(
                    f"{spec.case_id}: native shell ledger has an invalid or "
                    f"duplicate ToolCall identity: {key!r}"
                )
            seen_calls.add(key)
            arguments = dict(call.get("arguments", {}) or {})
            matching_results = [
                result
                for result in list(detail.get("results", []) or [])
                if str(result.get("tool_call_id", "") or "").strip()
                == tool_call_id
                and str(result.get("tool_name", "") or "").strip()
                == tool_name
            ]
            if len(matching_results) != 1:
                raise AssertionError(
                    f"{spec.case_id}: native shell ToolCall {key!r} must have "
                    f"exactly one ToolResult, got {len(matching_results)}"
                )
            result = matching_results[0]
            result_payload = dict(result.get("payload", {}) or {})
            nested_result = result_payload.get("result")
            succeeded = result_payload.get("success") is True and not (
                isinstance(nested_result, dict)
                and nested_result.get("success") is False
            )
            failed = result_payload.get("success") is False and not (
                isinstance(nested_result, dict)
                and nested_result.get("success") is True
            )
            checkpoint = checkpoint_by_call.get(key)
            try:
                _validate_test_tool_call(
                    spec,
                    workplace,
                    {"name": tool_name, "arguments": arguments},
                )
            except AssertionError as exc:
                modeled = False
                model_reason = str(exc)
            else:
                modeled = True
                model_reason = ""

            if modeled:
                if not succeeded:
                    raise AssertionError(
                        f"{spec.case_id}: modeled native shell ToolCall {key!r} "
                        "did not have one successful ToolResult"
                    )
                if checkpoint is None:
                    raise AssertionError(
                        f"{spec.case_id}: modeled native shell ToolCall {key!r} "
                        "bypassed the execute-only all-shell permission guard"
                    )
                checkpoint_result = dict(checkpoint.get("tool_result", {}) or {})
                if (
                    str(checkpoint.get("decision", "") or "") != "approve_once"
                    or not bool(checkpoint.get("exact_modeled_call", False))
                    or dict(checkpoint.get("tool_arguments", {}) or {})
                    != arguments
                    or checkpoint_result.get("success") is not True
                    or str(
                        checkpoint_result.get("permission_resolution", "") or ""
                    ).strip()
                    != "allow"
                    or not bool(
                        checkpoint_result.get(
                            "checkpoint_tool_result_persisted",
                            False,
                        )
                    )
                    or str(
                        checkpoint_result.get("checkpoint_execution_state", "")
                        or ""
                    ).strip()
                    != "result_persisted"
                    or str(
                        checkpoint_result.get("checkpoint_completion_status", "")
                        or ""
                    ).strip()
                    != "resolved"
                    or (
                        str(checkpoint_result.get("result_record_id", "") or "")
                        and str(result.get("result_record_id", "") or "")
                        != str(checkpoint_result.get("result_record_id", "") or "")
                    )
                ):
                    raise AssertionError(
                        f"{spec.case_id}: modeled native shell ToolCall {key!r} "
                        "was not canonically approved with an atomic ToolResult"
                    )
            else:
                if succeeded:
                    raise AssertionError(
                        f"{spec.case_id}: unexpected native shell ToolCall "
                        f"{key!r} succeeded outside the E2E model: {model_reason}"
                    )
                if checkpoint is None:
                    raise AssertionError(
                        f"{spec.case_id}: unexpected native shell ToolCall "
                        f"{key!r} lacks a canonical deny checkpoint: {model_reason}"
                    )
                if (
                    str(checkpoint.get("decision", "") or "") != "deny"
                    or not bool(checkpoint.get("rejected", False))
                    or bool(checkpoint.get("exact_modeled_call", True))
                    or dict(checkpoint.get("tool_arguments", {}) or {}) != arguments
                ):
                    raise AssertionError(
                        f"{spec.case_id}: unexpected native shell ToolCall "
                        f"{key!r} does not match its canonical deny decision"
                    )
                checkpoint_result = dict(checkpoint.get("tool_result", {}) or {})
                if (
                    not failed
                    or checkpoint_result.get("success") is not False
                    or str(
                        checkpoint_result.get("permission_resolution", "") or ""
                    ).strip()
                    != "deny"
                    or not bool(
                        checkpoint_result.get(
                            "checkpoint_tool_result_persisted",
                            False,
                        )
                    )
                    or str(
                        checkpoint_result.get("checkpoint_execution_state", "")
                        or ""
                    ).strip()
                    != "result_persisted"
                    or str(
                        checkpoint_result.get("checkpoint_completion_status", "")
                        or ""
                    ).strip()
                    != "resolved"
                    or (
                        str(checkpoint_result.get("result_record_id", "") or "")
                        and str(result.get("result_record_id", "") or "")
                        != str(checkpoint_result.get("result_record_id", "") or "")
                    )
                ):
                    raise AssertionError(
                        f"{spec.case_id}: unexpected native shell ToolCall "
                        f"{key!r} lacks its unique canonical denied ToolResult"
                    )
                matched_checkpoints.add(key)
            if checkpoint is not None:
                matched_checkpoints.add(key)
            evidence.append(
                {
                    "runtime_session_id": runtime_session_id,
                    "task_id": str(detail.get("task_id", "") or ""),
                    "role_id": str(detail.get("role_id", "") or ""),
                    "tool_call_id": tool_call_id,
                    "command": str(arguments.get("command", "") or ""),
                    "modeled": modeled,
                    "outcome": "approved_success"
                    if modeled
                    else "canonical_deny",
                    "checkpoint_id": str(
                        (checkpoint or {}).get("checkpoint_id", "") or ""
                    ),
                    "result_record_id": str(
                        result.get("result_record_id", "") or ""
                    ),
                }
            )

    unmatched = sorted(set(checkpoint_by_call) - matched_checkpoints)
    if unmatched:
        raise AssertionError(
            f"{spec.case_id}: shell permission checkpoints do not map to true "
            f"NativeRuntimeV2 execution calls: {unmatched}"
        )
    return evidence


def _pre_delivery_assessment_failure_kind(
    feedback_task_metadata: Mapping[str, Any],
) -> str:
    """Return any top-level or nested executive-assessment failure.

    New runtimes mirror the nested result onto the Task-level audit fields,
    but resumed/legacy rows may predate that projection.  Evidence collection
    must reject either representation so a stale missing mirror cannot turn an
    unavailable assessment into a successful E2E result.
    """

    metadata = dict(feedback_task_metadata or {})
    top_level = str(
        metadata.get("pre_delivery_assessment_failure_kind", "") or ""
    ).strip()
    if top_level:
        return top_level
    assessment = dict(metadata.get("ceo_pre_delivery_assessment", {}) or {})
    nested_failure = str(
        assessment.get("assessment_failure_kind", "") or ""
    ).strip()
    if nested_failure:
        return nested_failure
    if str(assessment.get("assessment_status", "") or "").strip() == "unavailable":
        return "assessment_unavailable"
    if bool(assessment.get("assessment_infrastructure_failure", False)):
        return "assessment_infrastructure_failure"
    return ""


async def _collect_case_evidence(
    engine: Any,
    run: CaseRun,
    *,
    project_id: str,
    workplace: Path,
) -> dict[str, Any]:
    runs = await engine.store.list_delegation_runs(
        project_id=project_id,
        session_id=run.session_id,
    )
    if len(runs) != 1:
        raise AssertionError(
            f"{run.spec.case_id}: expected one delegation run for {run.session_id}, got {len(runs)}"
        )
    delegation_run = runs[0]
    expected_run_contract = {
        "company_profile": "custom",
        "execution_model": "actor_runtime",
        "final_decider_role_id": str(run.spec.roles[0]["id"]),
    }
    actual_run_contract = {
        "company_profile": str(delegation_run.company_profile or "").strip(),
        "execution_model": str(delegation_run.execution_model or "").strip(),
        "final_decider_role_id": str(
            delegation_run.final_decider_role_id or ""
        ).strip(),
    }
    if actual_run_contract != expected_run_contract:
        raise AssertionError(
            f"{run.spec.case_id}: DelegationRun organization contract drifted; "
            f"expected={expected_run_contract!r} actual={actual_run_contract!r}"
        )
    if str(delegation_run.lifecycle_status or "").strip() != "awaiting_owner":
        raise AssertionError(
            f"{run.spec.case_id}: delegation run did not reach awaiting_owner; "
            f"lifecycle_status={delegation_run.lifecycle_status!r}"
        )
    all_tasks = await engine.store.get_tasks(project_id=project_id)
    runtime_tasks = [
        task
        for task in all_tasks
        if str(dict(task.metadata or {}).get("delegation_run_id", "") or "")
        == delegation_run.run_id
    ]
    task_evidence = [_task_native_evidence(task) for task in runtime_tasks]
    execution_tasks = [
        item
        for item in task_evidence
        if item["work_item_projection_id"] or item["work_item_id"]
    ]
    if not execution_tasks:
        raise AssertionError(f"{run.spec.case_id}: no company execution tasks were persisted")
    non_native = [
        item
        for item in execution_tasks
        if item["work_item_execution_strategy"] != "native"
        or item["selected_execution_agent"] != "native"
        or item["assigned_external_agent"] not in {None, ""}
    ]
    if non_native:
        raise AssertionError(
            f"{run.spec.case_id}: non-native company task evidence: {non_native}"
        )

    list_runtime_sessions = getattr(engine.store, "list_runtime_sessions", None)
    list_transcript = getattr(
        engine.store,
        "list_runtime_transcript_entries",
        None,
    )
    list_runtime_events = getattr(engine.store, "list_runtime_events", None)
    list_tool_calls = getattr(engine.store, "list_runtime_tool_calls", None)
    list_tool_results = getattr(engine.store, "list_runtime_tool_results", None)
    if not all(
        callable(method)
        for method in (
            list_runtime_sessions,
            list_transcript,
            list_runtime_events,
            list_tool_calls,
            list_tool_results,
        )
    ):
        raise RuntimeError("E2E evidence requires the complete native runtime ledger")

    runtime_task_by_id = {
        str(task.id or ""): task
        for task in runtime_tasks
        if str(task.id or "") in {item["task_id"] for item in execution_tasks}
    }
    native_runtime_evidence: list[dict[str, Any]] = []
    all_native_runtime_details: list[dict[str, Any]] = []
    # Latest-attempt view: deterministic quality/provenance must consume only
    # the artifact-producing attempt after rework.
    native_runtime_details: list[dict[str, Any]] = []
    for task_id, task in runtime_task_by_id.items():
        sessions = list(
            await list_runtime_sessions(
                project_id=project_id,
                task_id=task_id,
                limit=100,
            )
        )
        execution_session = _single_completed_native_execution_runtime(
            sessions,
            case_id=run.spec.case_id,
            task_id=task_id,
        )
        selected_runtime_session_id = str(
            execution_session.get("runtime_session_id", "") or ""
        ).strip()
        attempt_records = _native_runtime_attempt_records(
            sessions,
            case_id=run.spec.case_id,
            task_id=task_id,
        )
        attempt_rows_by_id = {
            str(row.get("runtime_session_id", "") or "").strip(): row
            for row in sessions
            if _is_native_runtime_v2_execution_session(row)
        }
        attempt_evidence: list[dict[str, Any]] = []
        selected_detail: dict[str, Any] | None = None
        selected_assistant_turn_count = 0
        selected_completed_turn_count = 0
        for attempt_record in attempt_records:
            runtime_session_id = attempt_record["runtime_session_id"]
            attempt_row = attempt_rows_by_id[runtime_session_id]
            transcript = list(await list_transcript(runtime_session_id))
            runtime_events = list(
                await list_runtime_events(runtime_session_id, limit=1000)
            )
            completed_turn_events = [
                event
                for event in runtime_events
                if str(event.get("event_type", "") or "").strip()
                == "turn_completed"
            ]
            assistant_turns = [
                entry
                for entry in transcript
                if str(entry.get("role", "") or "").strip()
                == "assistant"
                and str(entry.get("entry_type", "") or "").strip()
                == "message"
            ]
            calls = list(await list_tool_calls(runtime_session_id))
            results = list(await list_tool_results(runtime_session_id))
            attempt_detail = {
                "task_id": task_id,
                "role_id": str(getattr(task, "assigned_to", "") or ""),
                "work_item_projection_id": _work_item_projection_id(task),
                "runtime_session_id": runtime_session_id,
                "runtime_created_at": attempt_record["created_at"],
                "runtime_status": str(
                    attempt_row.get("status", "") or ""
                ).strip(),
                "calls": calls,
                "results": results,
            }
            all_native_runtime_details.append(attempt_detail)
            attempt_evidence.append(
                {
                    **attempt_record,
                    "assistant_llm_turn_count": len(assistant_turns),
                    "turn_completed_event_count": len(completed_turn_events),
                    "tool_call_count": len(calls),
                    "tool_result_count": len(results),
                    "selected_latest": (
                        runtime_session_id == selected_runtime_session_id
                    ),
                }
            )
            if runtime_session_id == selected_runtime_session_id:
                selected_detail = {
                    **attempt_detail,
                    "runtime_attempts": attempt_records,
                }
                selected_assistant_turn_count = len(assistant_turns)
                selected_completed_turn_count = len(completed_turn_events)
        if selected_detail is None:
            raise AssertionError(
                f"{run.spec.case_id}: selected native runtime "
                f"{selected_runtime_session_id!r} disappeared from Task {task_id}"
            )
        if not selected_assistant_turn_count or not selected_completed_turn_count:
            raise AssertionError(
                f"{run.spec.case_id}: native Task {task_id} latest completed "
                "attempt has no durable completed LLM turn"
            )
        native_runtime_details.append(selected_detail)
        native_runtime_evidence.append(
            {
                "task_id": task_id,
                "role_id": selected_detail["role_id"],
                "runtime_session_id": selected_runtime_session_id,
                "status": "completed",
                "attempt_count": len(attempt_evidence),
                "runtime_attempts": attempt_evidence,
                "assistant_llm_turn_count": selected_assistant_turn_count,
                "turn_completed_event_count": selected_completed_turn_count,
                "tool_call_count": len(selected_detail["calls"]),
                "tool_result_count": len(selected_detail["results"]),
            }
        )

    investment_runtime_details: list[dict[str, Any]] | None = None
    if run.spec.case_id == "investment":
        _role_tasks, investment_runtime_details = (
            await _investment_execute_runtime_details_from_store(
                engine.store,
                project_id=project_id,
                durable_tasks=runtime_tasks,
                case_id="investment late evidence",
            )
        )

    def successful_ledger_call(
        detail: dict[str, Any],
        call: dict[str, Any],
    ) -> bool:
        matches = [
            result
            for result in detail["results"]
            if str(result.get("tool_call_id", "") or "")
            == str(call.get("tool_call_id", "") or "")
            and str(result.get("tool_name", "") or "")
            == str(call.get("tool_name", "") or "")
        ]
        if len(matches) != 1:
            return False
        result_payload = dict(matches[0].get("payload", {}) or {})
        nested = result_payload.get("result")
        return result_payload.get("success") is True and not (
            isinstance(nested, dict) and nested.get("success") is False
        )

    role_tool_contract_evidence: dict[str, Any] = {}
    role_tool_contract_evidence["all_attempt_tool_ledger"] = (
        _native_tool_ledger_closure(
            run.spec,
            runtime_details=all_native_runtime_details,
        )
    )
    successful_file_mutations = _native_successful_file_mutations(
        run.spec,
        workplace,
        runtime_details=all_native_runtime_details,
    )
    role_tool_contract_evidence["successful_file_mutations"] = (
        successful_file_mutations
    )
    if run.spec.case_id == "investment":
        if investment_runtime_details is None:
            raise AssertionError(
                "investment: canonical analyst runtime evidence is missing"
            )
        for role_id in ("investment_analyst", "risk_analyst"):
            successful_web_calls = [
                {
                    "runtime_session_id": detail["runtime_session_id"],
                    "tool_call_id": str(call.get("tool_call_id", "") or ""),
                    "query": str(
                        dict(call.get("arguments", {}) or {}).get("query", "")
                        or ""
                    ),
                }
                for detail in investment_runtime_details
                if detail["role_id"] == role_id
                for call in detail["calls"]
                if str(call.get("tool_name", "") or "") == "web_search"
                and str(
                    dict(call.get("arguments", {}) or {}).get("query", "") or ""
                ).strip()
                and successful_ledger_call(detail, call)
            ]
            if not successful_web_calls:
                raise AssertionError(
                    f"{run.spec.case_id}: {role_id} has no successful native "
                    "web_search ToolCall/ToolResult pair"
                )
            role_tool_contract_evidence[f"{role_id}_web_search"] = (
                successful_web_calls
            )
    elif run.spec.case_id == "app":
        app_tool_contract = _app_native_tool_contract(
            run.spec,
            workplace,
            runtime_details=all_native_runtime_details,
            successful_file_mutations=successful_file_mutations,
        )
        role_tool_contract_evidence.update(app_tool_contract)

    work_items = await engine.store.list_delegation_work_items(delegation_run.run_id)
    if run.spec.case_id == "app":
        role_tool_contract_evidence["app_delegation_dependency"] = (
            _app_delegation_dependency_contract(work_items)
        )
    expected_roles = {str(role["id"]) for role in run.spec.roles}
    successful_attempts = []
    for item in work_items:
        metadata = dict(item.metadata or {})
        try:
            attempt_seq = int(metadata.get("attempt_seq", 0) or 0)
        except (TypeError, ValueError):
            attempt_seq = 0
        attempt_outcome = str(metadata.get("attempt_outcome", "") or "").strip().lower()
        phase = str(
            getattr(getattr(item, "phase", None), "value", item.phase) or ""
        ).strip().lower()
        if (
            attempt_seq > 0
            and bool(metadata.get("attempt_settled", False))
            and attempt_outcome not in {"crashed", "interrupted", "failed", "cancelled"}
            and phase not in {"failed", "cancelled"}
        ):
            successful_attempts.append(item)
    executed_roles = {
        str(item.role_id or "")
        for item in successful_attempts
        if str(item.role_id or "")
    }
    if not expected_roles.issubset(executed_roles):
        raise AssertionError(
            f"{run.spec.case_id}: expected every org role to finish a real agent attempt; "
            f"expected={sorted(expected_roles)} actual={sorted(executed_roles)}"
        )

    feedback, owner_frontier = await _final_owner_interaction_frontier(
        engine.store,
        project_id=project_id,
        session_id=run.session_id,
        case_id=run.spec.case_id,
    )
    if feedback.checkpoint_id != run.feedback_checkpoint_id:
        raise AssertionError(
            f"{run.spec.case_id}: journaled final feedback identity drifted"
        )
    feedback_payload = dict(feedback.payload or {})
    feedback_interaction = dict(feedback_payload.get("interaction", {}) or {})
    feedback_execution_scope = dict(
        feedback_interaction.get("execution_scope", {}) or {}
    )
    if (
        str(feedback_execution_scope.get("company_profile", "") or "").strip()
        != "custom"
        or str(feedback_execution_scope.get("org_id", "") or "").strip()
        != run.spec.org_id
    ):
        raise AssertionError(
            f"{run.spec.case_id}: final checkpoint durable execution scope drifted: "
            f"{feedback_execution_scope!r}"
        )
    if str(feedback_payload.get("feedback_scope", "") or "").lower() != "final":
        raise AssertionError(f"{run.spec.case_id}: feedback checkpoint is not final scope")
    if str(feedback_payload.get("review_level", "") or "").lower() != "human":
        raise AssertionError(f"{run.spec.case_id}: final feedback is not human review")

    staffing_rows = await _find_case_checkpoints(
        engine.store,
        project_id=project_id,
        session_id=run.session_id,
        checkpoint_types=(STAFFING_CHECKPOINT_TYPE,),
        statuses=None,
    )
    if len(staffing_rows) != 1:
        raise AssertionError(
            f"{run.spec.case_id}: expected one resolved native staffing checkpoint "
            "or its exact journaled crash-recovery terminal"
        )
    if len(run.staffing_decisions) != 1:
        raise AssertionError(
            f"{run.spec.case_id}: expected one staffing decision submitted by the harness"
        )
    staffing = staffing_rows[0]
    staffing_status = str(staffing.status or "").strip()
    staffing_runtime_recovery: dict[str, Any] | None = None
    if staffing_status == "outcome_unknown":
        if (
            not run.staffing_recovery_checkpoint_id
            or not run.staffing_recovery_run_id
        ):
            raise AssertionError(
                f"{run.spec.case_id}: recovered staffing lacks its journaled "
                "runtime recovery identity"
            )
        staffing_runtime_recovery = (
            await _resumed_staffing_runtime_recovery_evidence(
                engine.store,
                run,
                staffing,
                project_id=project_id,
                expected_checkpoint_id=run.staffing_recovery_checkpoint_id,
                expected_delegation_run_id=run.staffing_recovery_run_id,
                require_resolved=True,
            )
        )
    if staffing_status != "resolved" and staffing_runtime_recovery is None:
        raise AssertionError(
            f"{run.spec.case_id}: staffing checkpoint did not resolve and lacks "
            "an exact same-run interruption recovery chain"
        )
    staffing_recovered_outcome_unknown = staffing_runtime_recovery is not None
    staffing_submission = dict(run.staffing_decisions[0])
    expected_staffing_request_id = (
        f"issue35-e2e:{run.session_id}:{staffing.checkpoint_id}:native-staffing"
    )
    if (
        staffing_submission.get("checkpoint_id") != staffing.checkpoint_id
        or staffing_submission.get("client_request_id")
        != expected_staffing_request_id
        or staffing_submission.get("staffing_action") != "manual_approve"
        or staffing_submission.get("recruitment_agent") != "native"
        or set(staffing_submission.get("recruitment_role_agents", {}).values())
        != {"native"}
        or not _receipt_acknowledged(
            dict(staffing_submission.get("receipt", {}) or {})
        )
    ):
        raise AssertionError(
            f"{run.spec.case_id}: staffing decision lacks canonical native evidence"
        )
    staffing_payload = dict(staffing.payload or {})
    staffing_interaction = dict(staffing_payload.get("interaction", {}) or {})
    staffing_scope = dict(staffing_interaction.get("execution_scope", {}) or {})
    staffing_decision_record = dict(
        staffing_interaction.get("decision", {}) or {}
    )
    staffing_decision_value = dict(
        staffing_decision_record.get("value", {}) or {}
    )
    if staffing_scope != {"company_profile": "custom", "org_id": run.spec.org_id}:
        raise AssertionError(
            f"{run.spec.case_id}: staffing durable execution scope drifted"
        )
    if (
        staffing_decision_value.get("staffing_action") != "manual_approve"
        or staffing_decision_value.get("recruitment_agent") != "native"
        or set(
            dict(
                staffing_decision_value.get("recruitment_role_agents", {}) or {}
            ).values()
        )
        != {"native"}
    ):
        raise AssertionError(
            f"{run.spec.case_id}: durable staffing decision is not all-native"
        )

    tool_rows = await _find_case_checkpoints(
        engine.store,
        project_id=project_id,
        session_id=run.session_id,
        checkpoint_types=TOOL_CHECKPOINT_TYPES,
        statuses=None,
    )
    if not tool_rows:
        raise AssertionError(
            f"{run.spec.case_id}: no typed tool_permission checkpoint occurred"
        )
    unresolved_tools = [
        checkpoint.checkpoint_id
        for checkpoint in tool_rows
        if str(checkpoint.status or "") != "resolved"
    ]
    if unresolved_tools:
        raise AssertionError(
            f"{run.spec.case_id}: tool permission checkpoints not resolved: {unresolved_tools}"
        )

    submitted_by_checkpoint: dict[str, dict[str, Any]] = {}
    for submission in run.tool_decisions:
        checkpoint_id = str(submission.get("checkpoint_id", "") or "").strip()
        if not checkpoint_id or checkpoint_id in submitted_by_checkpoint:
            raise AssertionError(
                f"{run.spec.case_id}: invalid or duplicate submitted tool decision "
                f"for checkpoint {checkpoint_id!r}"
            )
        if str(submission.get("root_session_id", "") or "").strip() != run.session_id:
            raise AssertionError(
                f"{run.spec.case_id}: submitted tool decision has the wrong root session"
            )
        submitted_option = str(
            submission.get("decision", "") or "approve_once"
        ).strip()
        if submitted_option not in {"approve_once", "deny"}:
            raise AssertionError(
                f"{run.spec.case_id}: unsupported submitted tool decision "
                f"{submitted_option!r}"
            )
        expected_request_id = (
            f"issue35-e2e:{run.session_id}:{checkpoint_id}:{submitted_option}"
        )
        if str(submission.get("client_request_id", "") or "") != expected_request_id:
            raise AssertionError(
                f"{run.spec.case_id}: tool decision lacks the harness API request identity"
            )
        if not _receipt_acknowledged(dict(submission.get("receipt", {}) or {})):
            raise AssertionError(
                f"{run.spec.case_id}: tool decision receipt was not acknowledged"
            )
        submitted_by_checkpoint[checkpoint_id] = submission
    checkpoint_ids = {checkpoint.checkpoint_id for checkpoint in tool_rows}
    if set(submitted_by_checkpoint) != checkpoint_ids:
        raise AssertionError(
            f"{run.spec.case_id}: durable tool checkpoints do not exactly match "
            "decisions submitted through OPCEngine.submit_checkpoint_decision; "
            f"checkpoints={sorted(checkpoint_ids)} "
            f"submissions={sorted(submitted_by_checkpoint)}"
        )

    tool_checkpoint_evidence: list[dict[str, Any]] = []
    delegated_child_tool_seen = False
    approved_exact_shell_role_pairs: set[tuple[str, str]] = set()
    organization_role_ids = {
        str(role.get("id", "") or "").strip() for role in run.spec.roles
    }
    runtime_tool_results_cache: dict[str, list[dict[str, Any]]] = {}
    for checkpoint in tool_rows:
        payload = dict(checkpoint.payload or {})
        interaction = dict(payload.get("interaction", {}) or {})
        execution_scope = dict(interaction.get("execution_scope", {}) or {})
        if (
            str(execution_scope.get("company_profile", "") or "").strip()
            != "custom"
            or str(execution_scope.get("org_id", "") or "").strip()
            != run.spec.org_id
        ):
            raise AssertionError(
                f"{run.spec.case_id}: tool checkpoint {checkpoint.checkpoint_id} "
                f"durable execution scope drifted: {execution_scope!r}"
            )
        ownership = dict(interaction.get("ownership", {}) or {})
        tool_call = dict(payload.get("tool_call", {}) or {})
        decision_record = dict(interaction.get("decision", {}) or {})
        decision_value = dict(decision_record.get("value", {}) or {})
        durable_option = str(
            decision_value.get("option_id", "") or ""
        ).strip()
        if durable_option not in {"approve_once", "deny"}:
            raise AssertionError(
                f"{run.spec.case_id}: tool checkpoint {checkpoint.checkpoint_id} "
                f"has unsupported durable decision {durable_option!r}"
            )
        missing_identity = [
            key
            for key, value in (
                ("id", tool_call.get("id")),
                ("name", tool_call.get("name")),
                ("fingerprint", tool_call.get("fingerprint")),
                ("runtime_session_id", tool_call.get("runtime_session_id")),
            )
            if not str(value or "").strip()
        ]
        if missing_identity:
            raise AssertionError(
                f"{run.spec.case_id}: resolved tool checkpoint "
                f"{checkpoint.checkpoint_id} lacks {missing_identity}"
            )
        submission = submitted_by_checkpoint[checkpoint.checkpoint_id]
        submitted_option = str(
            submission.get("decision", "") or "approve_once"
        ).strip()
        if submitted_option != durable_option:
            raise AssertionError(
                f"{run.spec.case_id}: submitted and durable tool decisions differ "
                f"for checkpoint {checkpoint.checkpoint_id}"
            )
        for evidence_key, submitted_key in (
            ("id", "tool_call_id"),
            ("name", "tool_name"),
            ("fingerprint", "tool_call_fingerprint"),
            ("runtime_session_id", "tool_runtime_session_id"),
        ):
            if str(tool_call.get(evidence_key, "") or "") != str(
                submission.get(submitted_key, "") or ""
            ):
                raise AssertionError(
                    f"{run.spec.case_id}: submitted decision identity changed for "
                    f"checkpoint {checkpoint.checkpoint_id}"
                )
        runtime_session_id = str(tool_call.get("runtime_session_id", "") or "")
        if runtime_session_id not in runtime_tool_results_cache:
            list_results = getattr(
                engine.store,
                "list_runtime_tool_results",
                None,
            )
            if not callable(list_results):
                raise RuntimeError(
                    "E2E evidence requires the durable runtime ToolResult ledger"
                )
            runtime_tool_results_cache[runtime_session_id] = list(
                await list_results(runtime_session_id)
            )
        matching_results = [
            result
            for result in runtime_tool_results_cache[runtime_session_id]
            if str(result.get("tool_call_id", "") or "")
            == str(tool_call.get("id", "") or "")
            and str(result.get("tool_name", "") or "")
            == str(tool_call.get("name", "") or "")
        ]
        if len(matching_results) != 1:
            raise AssertionError(
                f"{run.spec.case_id}: tool checkpoint {checkpoint.checkpoint_id} "
                f"does not map one-to-one to a durable ToolResult; "
                f"matches={len(matching_results)}"
            )
        tool_result = matching_results[0]
        result_payload = dict(tool_result.get("payload", {}) or {})
        result_metadata = dict(tool_result.get("metadata", {}) or {})
        approval_result = dict(payload.get("approval_result", {}) or {})
        execution_result = dict(interaction.get("execution", {}) or {})
        completion_result = dict(interaction.get("completion", {}) or {})
        if (
            not bool(approval_result.get("tool_result_persisted", False))
            or str(execution_result.get("state", "") or "").strip()
            != "result_persisted"
            or str(completion_result.get("final_status", "") or "").strip()
            != "resolved"
        ):
            raise AssertionError(
                f"{run.spec.case_id}: tool checkpoint {checkpoint.checkpoint_id} "
                "resolved without an atomic durable ToolResult completion"
            )
        waiting_task_id = str(
            ownership.get("waiting_task_id") or checkpoint.task_id or ""
        ).strip()
        waiting_task = (
            await engine.store.get_task(waiting_task_id) if waiting_task_id else None
        )
        waiting_role = str(getattr(waiting_task, "assigned_to", "") or "").strip()
        if (
            durable_option == "approve_once"
            and waiting_role
            and waiting_role != run.spec.roles[0]["id"]
        ):
            delegated_child_tool_seen = True
        if durable_option == "approve_once":
            # Re-run the strict validator over durable data. This proves every
            # approved call was exact, independently of the harness journal.
            _validate_test_tool_call(run.spec, workplace, tool_call)
            if waiting_role not in organization_role_ids:
                raise AssertionError(
                    f"{run.spec.case_id}: exact ToolCall "
                    f"{checkpoint.checkpoint_id} was approved for non-org role "
                    f"{waiting_role!r}"
                )
            nested_result = result_payload.get("result")
            if (
                result_payload.get("success") is not True
                or (
                    isinstance(nested_result, dict)
                    and nested_result.get("success") is False
                )
                or not bool(approval_result.get("approved", False))
            ):
                raise AssertionError(
                    f"{run.spec.case_id}: approved exact ToolCall "
                    f"{checkpoint.checkpoint_id} did not complete successfully"
                )
            if str(tool_call.get("name", "") or "").strip() == "shell_exec":
                approved_command = str(
                    dict(tool_call.get("arguments", {}) or {}).get(
                        "command", ""
                    )
                    or ""
                ).strip()
                approved_exact_shell_role_pairs.add(
                    (approved_command, waiting_role)
                )
        else:
            try:
                _validate_test_tool_call(run.spec, workplace, tool_call)
            except AssertionError:
                pass
            else:
                raise AssertionError(
                    f"{run.spec.case_id}: harness denied an exact modeled ToolCall "
                    f"at checkpoint {checkpoint.checkpoint_id}"
                )
            if not str(submission.get("rejection_reason", "") or "").strip():
                raise AssertionError(
                    f"{run.spec.case_id}: rejected checkpoint "
                    f"{checkpoint.checkpoint_id} lacks a recorded reason"
                )
            denial_approval = dict(result_payload.get("approval", {}) or {})
            permission_decision = dict(
                result_metadata.get("permission_decision", {}) or {}
            )
            if (
                result_payload.get("success") is not False
                or "denied this exact toolcall"
                not in str(result_payload.get("error", "") or "").lower()
                or str(denial_approval.get("human_reply", "") or "").strip()
                != "deny"
                or str(permission_decision.get("resolution", "") or "").strip()
                != "deny"
                or bool(approval_result.get("approved", True))
                or (
                    isinstance(result_payload.get("result"), dict)
                    and result_payload["result"].get("success") is True
                )
            ):
                raise AssertionError(
                    f"{run.spec.case_id}: rejected checkpoint "
                    f"{checkpoint.checkpoint_id} did not deliver the canonical "
                    "denied ToolResult to the same native runtime"
                )
        tool_checkpoint_evidence.append(
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "status": checkpoint.status,
                "waiting_task_id": waiting_task_id,
                "waiting_role_id": waiting_role,
                "tool_name": str(tool_call.get("name", "") or ""),
                "tool_call_id": str(tool_call.get("id", "") or ""),
                "tool_call_fingerprint": str(tool_call.get("fingerprint", "") or ""),
                "tool_runtime_session_id": str(
                    tool_call.get("runtime_session_id", "") or ""
                ),
                "tool_arguments": dict(tool_call.get("arguments", {}) or {}),
                "tool_command": str(
                    dict(tool_call.get("arguments", {}) or {}).get("command", "")
                    or ""
                ),
                "decision": durable_option,
                "exact_modeled_call": durable_option == "approve_once",
                "rejected": durable_option == "deny",
                "rejection_reason": str(
                    submission.get("rejection_reason", "") or ""
                ),
                "tool_result": {
                    "result_record_id": str(
                        tool_result.get("result_record_id", "") or ""
                    ),
                    "runtime_session_id": str(
                        tool_result.get("runtime_session_id", "") or ""
                    ),
                    "tool_call_id": str(
                        tool_result.get("tool_call_id", "") or ""
                    ),
                    "tool_name": str(tool_result.get("tool_name", "") or ""),
                    "success": result_payload.get("success"),
                    "permission_resolution": str(
                        result_metadata.get("permission_decision", {}).get(
                            "resolution", ""
                        )
                        or ""
                    ),
                    "checkpoint_tool_result_persisted": bool(
                        approval_result.get("tool_result_persisted", False)
                    ),
                    "checkpoint_execution_state": str(
                        execution_result.get("state", "") or ""
                    ),
                    "checkpoint_completion_status": str(
                        completion_result.get("final_status", "") or ""
                    ),
                },
                "submitted_via_engine_api": True,
                "execution_scope": execution_scope,
            }
        )
    if not delegated_child_tool_seen:
        raise AssertionError(
            f"{run.spec.case_id}: tool approval path was not exercised by a delegated child role"
        )
    missing_required_shell_role_pairs = (
        _missing_required_exact_shell_role_pairs(
            run.spec,
            approved_exact_shell_role_pairs,
        )
    )
    if missing_required_shell_role_pairs:
        raise AssertionError(
            f"{run.spec.case_id}: required exact shell command/role pairs were "
            f"not approved successfully: {list(missing_required_shell_role_pairs)}; "
            f"approved_pairs={sorted(approved_exact_shell_role_pairs)}"
        )
    native_shell_calls = _native_shell_ledger_closure(
        run.spec,
        workplace,
        runtime_details=all_native_runtime_details,
        tool_checkpoint_evidence=tool_checkpoint_evidence,
    )

    artifacts: list[dict[str, Any]] = []
    for relative in run.spec.required_artifacts:
        path = workplace / relative
        if not path.is_file() or path.stat().st_size < 40:
            raise AssertionError(
                f"{run.spec.case_id}: required real artifact missing or empty: {path}"
            )
        artifacts.append(
            {
                "relative_path": relative,
                "absolute_path": str(path),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    artifact_validation = _validate_real_artifacts(run.spec, workplace)
    investment_data_quality: dict[str, Any] | None = None
    if run.spec.case_id == "investment":
        if investment_runtime_details is None:
            raise AssertionError(
                "investment: canonical analyst runtime evidence is missing"
            )
        investment_data_quality, _late_artifact_hashes = (
            _investment_quality_snapshot(
                workplace,
                investment_runtime_details,
                run.started_at,
            )
        )
        role_tool_contract_evidence["investment_data_quality"] = (
            investment_data_quality
        )

    feedback_task = await engine.store.get_task(feedback.task_id) if feedback.task_id else None
    feedback_task_meta = dict(getattr(feedback_task, "metadata", {}) or {})
    if feedback_task is None:
        raise AssertionError(f"{run.spec.case_id}: final feedback waiting task is missing")
    deterministic_validation = dict(
        feedback_task_meta.get("pre_delivery_validation", {}) or {}
    )
    if (
        deterministic_validation.get("status") != "passed"
        or deterministic_validation.get("valid") is not True
    ):
        raise AssertionError(
            f"{run.spec.case_id}: final card lacks a passed deterministic "
            "pre-delivery validation record"
        )
    deterministic_evidence = dict(
        deterministic_validation.get("evidence", {}) or {}
    )
    if run.spec.case_id == "investment":
        if investment_data_quality is None or investment_runtime_details is None:
            raise AssertionError(
                "investment: late quality-gate recomputation is missing"
            )
        role_tool_contract_evidence["pre_delivery_evidence_recomputed"] = (
            _assert_investment_pre_delivery_evidence_matches(
                deterministic_evidence,
                project_id=project_id,
                delegation_run_id=delegation_run.run_id,
                root_session_id=run.session_id,
                run_started_at=run.started_at,
                workplace=workplace,
                runtime_details=investment_runtime_details,
                quality_gate=investment_data_quality,
            )
        )
    elif deterministic_evidence != {
        "validator_id": "issue35_investment_data_quality",
        "schema_version": 1,
        "scope": "not_applicable",
        "org_id": run.spec.org_id,
    }:
        raise AssertionError(
            f"{run.spec.case_id}: investment-only pre-delivery validator "
            "did not record the exact not-applicable evidence"
        )
    work_item_phase_evidence = [
        {
            "work_item_id": str(item.work_item_id or ""),
            "role_id": str(item.role_id or ""),
            "kind": str(item.kind or ""),
            "phase": str(
                getattr(getattr(item, "phase", None), "value", item.phase) or ""
            ),
        }
        for item in work_items
    ]
    non_final_phases = [
        item
        for item in work_item_phase_evidence
        if item["phase"] not in {"approved", "awaiting_human"}
    ]
    awaiting_human_items = [
        item
        for item in work_item_phase_evidence
        if item["phase"] == "awaiting_human"
    ]
    feedback_work_item_id = str(
        getattr(feedback_task, "linked_work_item_id", "")
        or feedback_task_meta.get("work_item_id")
        or feedback_task_meta.get("delegation_work_item_id")
        or ""
    ).strip()
    if non_final_phases:
        raise AssertionError(
            f"{run.spec.case_id}: nonterminal WorkItems remain at final review: "
            f"{non_final_phases}"
        )
    if (
        len(awaiting_human_items) != 1
        or not feedback_work_item_id
        or awaiting_human_items[0]["work_item_id"] != feedback_work_item_id
    ):
        raise AssertionError(
            f"{run.spec.case_id}: the sole awaiting_human WorkItem must be the "
            f"final feedback target; feedback_work_item={feedback_work_item_id!r} "
            f"awaiting={awaiting_human_items!r}"
        )
    from opc.layer2_organization.company_runtime_identity import (
        load_company_runtime_identity_index,
    )

    identity_index = await load_company_runtime_identity_index(
        engine.store,
        project_id,
    )
    feedback_identity = identity_index.resolve(task_id=str(feedback_task.id or ""))
    if feedback_identity is None:
        raise AssertionError(
            f"{run.spec.case_id}: final feedback has no canonical company runtime identity"
        )
    owner_task_id = str(feedback_identity.ui_anchor_task_id or "").strip()
    owner_session_id = str(feedback_identity.runtime_session_id or "").strip()
    if (
        owner_task_id != run.ui_anchor_task_id
        or owner_session_id != run.session_id
    ):
        raise AssertionError(
            f"{run.spec.case_id}: final feedback owner identity mismatch: "
            f"task={owner_task_id!r} session={owner_session_id!r}"
        )
    execution_task_ids = {str(item["task_id"] or "") for item in execution_tasks}
    if owner_task_id in execution_task_ids:
        raise AssertionError(
            f"{run.spec.case_id}: company work-item Task was reused as the UI anchor"
        )
    owner_task = await engine.store.get_task(owner_task_id)
    if owner_task is None:
        raise AssertionError(
            f"{run.spec.case_id}: owner UI anchor task does not exist"
        )
    owner_task_title = str(owner_task.title or "").strip()
    if not owner_task_title:
        raise AssertionError(
            f"{run.spec.case_id}: owner UI anchor task has no selectable title"
        )
    _validate_office_ui_root_task(owner_task, run, project_id=project_id)
    owner_authorized = await engine.can_answer_checkpoint(
        feedback,
        requester_task_id=owner_task_id,
        requester_session_id=owner_session_id,
    )
    if not owner_authorized:
        raise AssertionError(
            f"{run.spec.case_id}: pending final feedback is not visible/answerable "
            "from its owner session"
        )
    if _pre_delivery_assessment_failure_kind(feedback_task_meta):
        raise AssertionError(
            f"{run.spec.case_id}: final card came from a failed pre-delivery assessment"
        )
    if feedback_task_meta.get("pre_delivery_rework_cap_reached"):
        raise AssertionError(
            f"{run.spec.case_id}: final card came from an exhausted rework fallback"
        )
    pre_delivery = dict(feedback_task_meta.get("ceo_pre_delivery_assessment", {}) or {})
    if pre_delivery and not bool(pre_delivery.get("deliverable", False)):
        raise AssertionError(
            f"{run.spec.case_id}: executive assessment did not mark delivery ready"
        )
    org_id = str(
        feedback_task_meta.get("org_id")
        or feedback_task_meta.get("organization_id")
        or ""
    ).strip()
    if org_id != run.spec.org_id:
        raise AssertionError(
            f"{run.spec.case_id}: delivery org identity mismatch: {org_id!r}"
        )

    return {
        "case_id": run.spec.case_id,
        "title": run.spec.title,
        "session_id": run.session_id,
        "org_id": run.spec.org_id,
        "delegation_run": {
            "run_id": delegation_run.run_id,
            "session_id": delegation_run.session_id,
            "company_profile": delegation_run.company_profile,
            "execution_model": delegation_run.execution_model,
            "final_decider_role_id": delegation_run.final_decider_role_id,
            "status": delegation_run.status,
            "lifecycle_status": delegation_run.lifecycle_status,
            "organization_contract_matches_case": (
                actual_run_contract == expected_run_contract
            ),
        },
        "owner_feedback_authorized": owner_authorized,
        "owner_ui_anchor_task_id": owner_task_id,
        "owner_ui_anchor_session_id": owner_session_id,
        "owner_ui_anchor_title": owner_task_title,
        "owner_ui_anchor_is_pure": True,
        "owner_ui_anchor_is_work_item": False,
        "pre_delivery_validation": deterministic_validation,
        "native_agent_tasks": task_evidence,
        "native_runtime_ledgers": native_runtime_evidence,
        "native_shell_calls": native_shell_calls,
        "all_native_shell_calls_modeled": True,
        "role_tool_contract_evidence": role_tool_contract_evidence,
        "work_items": [
            {
                "work_item_id": item.work_item_id,
                "role_id": item.role_id,
                "kind": item.kind,
                "projection_id": item.projection_id,
                "phase": getattr(item.phase, "value", str(item.phase)),
                "parent_work_item_id": item.parent_work_item_id,
                "manager_role_id": item.manager_role_id,
                "handoff_status": item.handoff_status,
                "attempt_seq": int(dict(item.metadata or {}).get("attempt_seq", 0) or 0),
                "attempt_settled": bool(
                    dict(item.metadata or {}).get("attempt_settled", False)
                ),
                "attempt_outcome": str(
                    dict(item.metadata or {}).get("attempt_outcome", "") or ""
                ),
            }
            for item in work_items
        ],
        "staffing_decision": staffing_submission,
        "staffing_checkpoint": {
            "checkpoint_id": staffing.checkpoint_id,
            "checkpoint_type": staffing.checkpoint_type,
            "status": staffing.status,
            "recovered_outcome_unknown": staffing_recovered_outcome_unknown,
            "runtime_interruption_recovery": staffing_runtime_recovery,
            "company_profile": "custom",
            "org_id": run.spec.org_id,
            "execution_scope": staffing_scope,
            "staffing_action": staffing_decision_value.get("staffing_action"),
            "recruitment_agent": staffing_decision_value.get(
                "recruitment_agent"
            ),
            "recruitment_role_agents": staffing_decision_value.get(
                "recruitment_role_agents"
            ),
            "submitted_via_engine_api": True,
        },
        "tool_permission_decisions": run.tool_decisions,
        "tool_permission_checkpoint_count": len(tool_rows),
        "tool_permission_checkpoints": tool_checkpoint_evidence,
        "required_exact_shell_role_pairs": [
            {"command": command, "role_id": role_id}
            for command, role_id in sorted(
                _required_exact_shell_role_pairs(run.spec)
            )
        ],
        "approved_exact_shell_role_pairs": [
            {"command": command, "role_id": role_id}
            for command, role_id in sorted(approved_exact_shell_role_pairs)
        ],
        "missing_required_exact_shell_role_pairs": [],
        "active_owner_interaction_frontier": owner_frontier,
        "final_feedback_checkpoint": {
            "checkpoint_id": feedback.checkpoint_id,
            "checkpoint_type": feedback.checkpoint_type,
            "status": feedback.status,
            "task_id": feedback.task_id,
            "feedback_scope": feedback_payload.get("feedback_scope"),
            "review_level": feedback_payload.get("review_level"),
            "review_target_role_id": feedback_payload.get("review_target_role_id"),
            "organization_id": org_id,
            "execution_scope": feedback_execution_scope,
            "organization_config_file": str(
                feedback_task_meta.get("organization_config_file", "") or ""
            ),
            "pending_count_in_session": 1,
            "active_count_in_session": 1,
            "linked_work_item_id": feedback_work_item_id,
            "sole_awaiting_human_work_item": awaiting_human_items[0],
        },
        "response": {
            "chars": len(run.response),
            "sha256": hashlib.sha256(run.response.encode("utf-8")).hexdigest(),
            "preview": run.response[:240],
        },
        "artifacts": artifacts,
        "artifact_validation": artifact_validation,
        "investment_data_quality": investment_data_quality,
    }


async def _run_with_installed_shell_review(
    args: argparse.Namespace,
    *,
    opc_home: Path,
    setup: dict[str, Any],
    shell_review_policy: dict[str, Any],
) -> dict[str, Any]:
    config_dir = opc_home / "config"
    project_root = Path(setup["project_root"])
    workplace = Path(setup["workplace"])
    if not args.resume:
        stale_artifacts = [
            str(workplace / relative)
            for spec in CASES
            for relative in spec.required_artifacts
            if (workplace / relative).exists()
        ]
        if stale_artifacts:
            raise AssertionError(
                "fresh E2E run refuses pre-existing required artifacts; use --resume "
                f"for the journaled run or a fresh --project-id: {stale_artifacts}"
            )

    os.environ["OPC_HOME"] = str(opc_home)
    os.chdir(project_root)
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    from opc.core.config import OPCConfig, get_project_workplace
    from opc.engine import OPCEngine

    org_evidence = _prepare_saved_orgs(opc_home)
    config = OPCConfig.load(config_dir)
    pre_delivery_validator = _Issue35PreDeliveryQualityValidator(
        workplace=workplace,
        project_id=args.project_id,
    )
    engine = OPCEngine(
        config=config,
        opc_home=opc_home,
        project_id=args.project_id,
        pre_delivery_validator=pre_delivery_validator,
    )
    started_at = datetime.now().isoformat()
    state_path = (
        args.state_path.expanduser().resolve()
        if args.state_path is not None
        else opc_home.parent / "issue35-company-e2e-state.json"
    )
    if args.resume:
        if not state_path.is_file():
            raise FileNotFoundError(f"cannot resume without state journal: {state_path}")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if int(state.get("schema_version", 0) or 0) != 2:
            raise AssertionError(
                "resume journal predates the pure Office UI root contract; "
                "start a fresh isolated E2E run"
            )
        if str(state.get("opc_home", "")) != str(opc_home):
            raise AssertionError("resume journal belongs to a different OPC_HOME")
        if str(state.get("project_id", "")) != str(args.project_id):
            raise AssertionError("resume journal belongs to a different project")
        token = str(state.get("run_token", "") or "").strip()
        state_cases = {
            str(item.get("case_id", "")): dict(item)
            for item in list(state.get("cases", []) or [])
            if isinstance(item, dict)
        }
        if {spec.case_id for spec in CASES} != set(state_cases):
            raise AssertionError("resume journal does not contain exactly the two E2E cases")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", token):
            raise AssertionError("resume journal contains an invalid run token")
        for spec in CASES:
            state_case = state_cases[spec.case_id]
            if str(state_case.get("org_id", "") or "") != spec.org_id:
                raise AssertionError(
                    f"resume journal org mismatch for case {spec.case_id}"
                )
            expected_session_id = f"issue35-{spec.case_id}-{token}"
            if str(state_case.get("session_id", "") or "") != expected_session_id:
                raise AssertionError(
                    f"resume journal session mismatch for case {spec.case_id}"
                )
            try:
                ui_anchor_task_id = str(
                    uuid.UUID(
                        str(state_case.get("ui_anchor_task_id", "") or "")
                    )
                )
            except (TypeError, ValueError, AttributeError) as exc:
                raise AssertionError(
                    f"resume journal UI root Task ID is invalid for {spec.case_id}"
                ) from exc
            if ui_anchor_task_id != str(
                state_case.get("ui_anchor_task_id", "") or ""
            ):
                raise AssertionError(
                    f"resume journal UI root Task ID is not canonical for {spec.case_id}"
                )
        runs = [
            CaseRun(
                spec=spec,
                session_id=str(state_cases[spec.case_id].get("session_id", "") or ""),
                ui_anchor_task_id=str(
                    state_cases[spec.case_id].get("ui_anchor_task_id", "") or ""
                ),
                started_at=str(
                    state_cases[spec.case_id].get("started_at", "") or started_at
                ),
                staffing_decisions=list(
                    state_cases[spec.case_id].get("staffing_decisions", []) or []
                ),
                tool_decisions=list(
                    state_cases[spec.case_id].get("tool_decisions", []) or []
                ),
                feedback_checkpoint_id=str(
                    state_cases[spec.case_id].get("feedback_checkpoint_id", "") or ""
                ),
                staffing_recovery_checkpoint_id=str(
                    state_cases[spec.case_id].get(
                        "staffing_recovery_checkpoint_id", ""
                    )
                    or ""
                ),
                staffing_recovery_run_id=str(
                    state_cases[spec.case_id].get(
                        "staffing_recovery_run_id", ""
                    )
                    or ""
                ),
                resume_existing=True,
            )
            for spec in CASES
        ]
        for run in runs:
            if bool(run.staffing_recovery_checkpoint_id) != bool(
                run.staffing_recovery_run_id
            ):
                raise AssertionError(
                    f"resume journal has a partial staffing recovery identity "
                    f"for {run.spec.case_id}"
                )
        if not token or any(not run.session_id for run in runs):
            raise AssertionError("resume journal has an incomplete token or session ID")
    else:
        token = str(args.run_token or "").strip() or (
            datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", token):
            raise AssertionError(
                "--run-token must be 1-80 characters using letters, digits, dot, "
                "underscore, or hyphen, and must start with a letter or digit"
            )
        runs = [
            CaseRun(
                spec=spec,
                session_id=f"issue35-{spec.case_id}-{token}",
                ui_anchor_task_id=str(uuid.uuid4()),
                started_at=datetime.now().isoformat(),
            )
            for spec in CASES
        ]

    if len({run.session_id for run in runs}) != len(CASES):
        raise AssertionError("the two E2E cases must use distinct session IDs")
    if len({run.ui_anchor_task_id for run in runs}) != len(CASES):
        raise AssertionError("the two E2E cases must use distinct UI root Task IDs")
    from opc.layer5_memory.approval_allowlist import ApprovalAllowlistManager

    allowlist = ApprovalAllowlistManager(opc_home)
    session_shell_grants = [
        run.session_id
        for run in runs
        if list(
            dict(allowlist.session_scope(run.session_id).get("tool", {}) or {}).get(
                "shell_exec", []
            )
            or []
        )
    ]
    if session_shell_grants:
        raise AssertionError(
            "E2E sessions have pre-existing shell approvals and cannot prove a fresh "
            f"permission card: {session_shell_grants}"
        )

    def persist_state() -> None:
        _atomic_write_json(
            state_path,
            {
                "schema_version": 2,
                "issue": 35,
                "opc_home": str(opc_home),
                "project_id": args.project_id,
                "run_token": token,
                "updated_at": datetime.now().isoformat(),
                "cases": [
                    {
                        "case_id": run.spec.case_id,
                        "org_id": run.spec.org_id,
                        "session_id": run.session_id,
                        "ui_anchor_task_id": run.ui_anchor_task_id,
                        "started_at": run.started_at,
                        "feedback_checkpoint_id": run.feedback_checkpoint_id,
                        "staffing_recovery_checkpoint_id": (
                            run.staffing_recovery_checkpoint_id
                        ),
                        "staffing_recovery_run_id": run.staffing_recovery_run_id,
                        "staffing_decisions": run.staffing_decisions,
                        "tool_decisions": run.tool_decisions,
                    }
                    for run in runs
                ],
            },
        )

    pre_delivery_validator.register_runs(runs)
    persist_state()
    try:
        await engine.initialize()
        pre_delivery_validator.bind_store(engine.store)
        if args.resume:
            await _refresh_case_resume_routing(
                engine.store,
                runs,
                project_id=args.project_id,
            )
        for run in runs:
            await _persist_office_ui_root_task(
                engine.store,
                run,
                project_id=args.project_id,
            )
        persist_state()
        for run in runs:
            print(
                f"[{run.spec.case_id}] starting real native company/org session {run.session_id}",
                flush=True,
            )
            await _drive_case(
                engine,
                run,
                project_id=args.project_id,
                workplace=workplace,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.case_timeout_seconds,
                state_changed=persist_state,
            )
            print(
                f"[{run.spec.case_id}] reached pending final delivery feedback "
                f"{run.feedback_checkpoint_id}",
                flush=True,
            )

        workplace = get_project_workplace(args.project_id).resolve()
        case_evidence = [
            await _collect_case_evidence(
                engine,
                run,
                project_id=args.project_id,
                workplace=workplace,
            )
            for run in runs
        ]
        feedback_ids = {
            case["final_feedback_checkpoint"]["checkpoint_id"] for case in case_evidence
        }
        if len(feedback_ids) != len(CASES):
            raise AssertionError("the two sessions did not create distinct final feedback checkpoints")
        owner_anchor_ids = {
            case["owner_ui_anchor_task_id"] for case in case_evidence
        }
        owner_anchor_titles = {
            case["owner_ui_anchor_title"] for case in case_evidence
        }
        acceptance = {
            "two_distinct_sessions": len({run.session_id for run in runs}) == 2,
            "all_roles_configured_native": all(
                role["execution_strategy"] == "native"
                and not role["preferred_external_agent"]
                for organization in org_evidence
                for role in organization["roles"]
            ),
            "all_executed_agents_native": all(
                task["selected_execution_agent"] == "native"
                and task["assigned_external_agent"] in {None, ""}
                for case in case_evidence
                for task in case["native_agent_tasks"]
                if task["work_item_projection_id"] or task["work_item_id"]
            ),
            "every_native_task_has_completed_runtime_and_llm_turn": all(
                {
                    ledger["task_id"]
                    for ledger in case["native_runtime_ledgers"]
                }
                == {
                    task["task_id"]
                    for task in case["native_agent_tasks"]
                    if task["work_item_projection_id"] or task["work_item_id"]
                }
                and all(
                    ledger["status"] == "completed"
                    and ledger["assistant_llm_turn_count"] >= 1
                    and ledger["turn_completed_event_count"] >= 1
                    and ledger["attempt_count"]
                    == len(ledger["runtime_attempts"])
                    and sum(
                        1
                        for attempt in ledger["runtime_attempts"]
                        if attempt["selected_latest"]
                    )
                    == 1
                    and all(
                        attempt["status"]
                        in {"completed", "failed", "cancelled", "suspended"}
                        for attempt in ledger["runtime_attempts"]
                    )
                    for ledger in case["native_runtime_ledgers"]
                )
                for case in case_evidence
            ),
            "all_native_shell_calls_modeled": all(
                case["all_native_shell_calls_modeled"]
                for case in case_evidence
            ),
            "role_specific_native_tool_contracts_verified": all(
                (
                    run.spec.case_id == "investment"
                    and all(
                        case["role_tool_contract_evidence"].get(
                            f"{role_id}_web_search"
                        )
                        for role_id in ("investment_analyst", "risk_analyst")
                    )
                )
                or (
                    run.spec.case_id == "app"
                    and set(
                        case["role_tool_contract_evidence"].get(
                            "developer_artifact_writes", {}
                        )
                    )
                    == set(run.spec.required_artifacts)
                    and bool(
                        case["role_tool_contract_evidence"].get(
                            "qa_javascript_validation", {}
                        ).get("after_last_developer_write", False)
                    )
                    and bool(
                        case["role_tool_contract_evidence"].get(
                            "app_delegation_dependency", {}
                        ).get("validated", False)
                    )
                )
                for run, case in zip(runs, case_evidence)
            ),
            "delegation_runs_match_custom_actor_runtime_contract": all(
                case["delegation_run"]["company_profile"] == "custom"
                and case["delegation_run"]["execution_model"]
                == "actor_runtime"
                and case["delegation_run"]["final_decider_role_id"]
                == run.spec.roles[0]["id"]
                and case["delegation_run"][
                    "organization_contract_matches_case"
                ]
                for run, case in zip(runs, case_evidence)
            ),
            "native_staffing_submitted_via_engine_api": all(
                (
                    case["staffing_checkpoint"]["status"] == "resolved"
                    or case["staffing_checkpoint"].get(
                        "recovered_outcome_unknown", False
                    )
                )
                and case["staffing_checkpoint"]["staffing_action"]
                == "manual_approve"
                and case["staffing_checkpoint"]["recruitment_agent"]
                == "native"
                and set(
                    dict(
                        case["staffing_checkpoint"][
                            "recruitment_role_agents"
                        ]
                        or {}
                    ).values()
                )
                == {"native"}
                and case["staffing_checkpoint"]["submitted_via_engine_api"]
                for case in case_evidence
            ),
            "delegated_child_tool_permission_exercised_in_each_session": all(
                any(
                    row["decision"] == "approve_once"
                    and row["exact_modeled_call"]
                    and
                    row["waiting_role_id"]
                    and row["waiting_role_id"] != run.spec.roles[0]["id"]
                    for row in case["tool_permission_checkpoints"]
                )
                for run, case in zip(runs, case_evidence)
            ),
            "tool_permissions_submitted_via_engine_api": all(
                row["decision"] in {"approve_once", "deny"}
                and row["submitted_via_engine_api"]
                for case in case_evidence
                for row in case["tool_permission_checkpoints"]
            ),
            "all_approved_tool_calls_are_exact": all(
                row["decision"] != "approve_once"
                or row["exact_modeled_call"]
                for case in case_evidence
                for row in case["tool_permission_checkpoints"]
            ),
            "all_unexpected_tool_calls_are_denied": all(
                row["exact_modeled_call"]
                or (
                    row["decision"] == "deny"
                    and row["rejected"]
                    and bool(row["rejection_reason"])
                )
                for case in case_evidence
                for row in case["tool_permission_checkpoints"]
            ),
            "durable_interaction_scopes_match_saved_orgs": all(
                case["final_feedback_checkpoint"]["execution_scope"]
                == {"company_profile": "custom", "org_id": run.spec.org_id}
                and all(
                    row["execution_scope"]
                    == {"company_profile": "custom", "org_id": run.spec.org_id}
                    for row in case["tool_permission_checkpoints"]
                )
                for run, case in zip(runs, case_evidence)
            ),
            "two_pending_final_delivery_feedback_checkpoints": (
                len(feedback_ids) == len(CASES)
                and all(
                    case["final_feedback_checkpoint"]["status"] == "pending"
                    and case["final_feedback_checkpoint"][
                        "pending_count_in_session"
                    ]
                    == 1
                    and case["final_feedback_checkpoint"][
                        "active_count_in_session"
                    ]
                    == 1
                    and case["owner_feedback_authorized"]
                    for case in case_evidence
                )
            ),
            "final_owner_frontiers_are_exclusive": all(
                case["active_owner_interaction_frontier"]
                == [
                    {
                        "checkpoint_id": case["final_feedback_checkpoint"][
                            "checkpoint_id"
                        ],
                        "checkpoint_type": FINAL_CHECKPOINT_TYPE,
                        "status": "pending",
                        "task_id": case["final_feedback_checkpoint"]["task_id"],
                    }
                ]
                and case["final_feedback_checkpoint"]["linked_work_item_id"]
                == case["final_feedback_checkpoint"][
                    "sole_awaiting_human_work_item"
                ]["work_item_id"]
                and all(
                    item["phase"] in {"approved", "awaiting_human"}
                    for item in case["work_items"]
                )
                for case in case_evidence
            ),
            "two_distinct_ui_owner_anchors": (
                len(owner_anchor_ids) == len(CASES)
                and len(owner_anchor_titles) == len(CASES)
                and owner_anchor_ids
                == {run.ui_anchor_task_id for run in runs}
                and all(
                    case["owner_ui_anchor_is_pure"]
                    and not case["owner_ui_anchor_is_work_item"]
                    for case in case_evidence
                )
            ),
            "real_artifacts_verified": all(
                all(case["artifact_validation"].values())
                for case in case_evidence
            ),
            "investment_data_quality_verified": all(
                case["case_id"] != "investment"
                or (
                    bool(case.get("investment_data_quality"))
                    and case["investment_data_quality"].get(
                        "role_scoped_provenance_closed"
                    )
                    is True
                    and case["investment_data_quality"].get(
                        "critical_claims_supported"
                    )
                    is True
                    and all(
                        set(
                            case["investment_data_quality"].get(
                                "current_query_tool_calls", {}
                            ).get(role_id, {})
                        )
                        == set(INVESTMENT_TICKERS)
                        for role_id in (
                            "investment_analyst",
                            "risk_analyst",
                        )
                    )
                    and all(
                        note.get("claim_count") == len(INVESTMENT_TICKERS)
                        and note.get("claims_by_ticker")
                        == {ticker: 1 for ticker in INVESTMENT_TICKERS}
                        and note.get("official_claims_by_ticker")
                        == {ticker: 1 for ticker in INVESTMENT_TICKERS}
                        and set(
                            note.get("official_claims_by_ticker", {})
                        )
                        == set(INVESTMENT_TICKERS)
                        for note in case["investment_data_quality"].get(
                            "notes", {}
                        ).values()
                    )
                    and len(
                        case["investment_data_quality"].get(
                            "verified_critical_fact_rows", []
                        )
                    )
                    == 2 * len(INVESTMENT_TICKERS)
                    and len(
                        {
                            row.get("line")
                            for row in case["investment_data_quality"].get(
                                "verified_critical_fact_rows", []
                            )
                        }
                    )
                    == len(
                        case["investment_data_quality"].get(
                            "verified_critical_fact_rows", []
                        )
                    )
                )
                for case in case_evidence
            ),
        }
        failed_acceptance = sorted(
            name for name, accepted in acceptance.items() if not accepted
        )
        if failed_acceptance:
            raise AssertionError(
                f"issue #35 E2E acceptance failed: {failed_acceptance}"
            )
        return {
            "success": True,
            "issue": 35,
            "started_at": started_at,
            "completed_at": datetime.now().isoformat(),
            "repository_under_test": str(REPO_ROOT),
            "opc_home": str(opc_home),
            "project_root": str(project_root),
            "project_id": args.project_id,
            "workplace": str(workplace),
            "run_token": token,
            "state_path": str(state_path),
            "resumed": bool(args.resume),
            "session_execution": "sequential (permission monitor concurrent with each session)",
            "e2e_shell_review_policy": shell_review_policy,
            "organizations": org_evidence,
            "cases": case_evidence,
            "acceptance": acceptance,
            "ui_verification_required": (
                "Open each owner_ui_anchor_task_id in Office UI and assert a visible "
                "DeliveryFeedbackPanel; this backend harness does not claim DOM rendering."
            ),
        }
    finally:
        await engine.shutdown()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    """Install the execute-only shell guard around the entire real E2E run."""

    opc_home = args.opc_home.expanduser().resolve()
    setup = _validate_setup_without_execution(
        opc_home,
        project_id=args.project_id,
    )
    overlay = _E2EShellReviewOverlay(
        opc_home / "config",
        workplace=Path(setup["workplace"]),
    )
    shell_review_policy = overlay.install()
    try:
        shell_review_policy["async_authorization"] = (
            await _validate_installed_shell_async_authorization(
                opc_home / "config",
                workplace=Path(setup["workplace"]),
            )
        )
        result = await _run_with_installed_shell_review(
            args,
            opc_home=opc_home,
            setup=setup,
            shell_review_policy=shell_review_policy,
        )
    except BaseException as primary:
        # The inner runner shuts down the Store-owning Engine and every custom
        # child first. Only then may later processes observe the original
        # project policy again.
        try:
            overlay.restore()
        except BaseException as secondary:
            primary.add_note(
                "Secondary E2E shell overlay restore failure after the run: "
                f"{type(secondary).__name__}: {secondary}"
            )
        raise
    else:
        overlay.restore()
        return result


def main() -> int:
    args = _parse_args()
    opc_home = args.opc_home.expanduser().resolve()
    if not args.execute:
        setup = _validate_setup_without_execution(
            opc_home,
            project_id=args.project_id,
        )
        plan = {
            "execute": False,
            "message": "Dry run only. Re-run with --execute to make real LLM calls.",
            "setup_validation": setup,
            "cases": [
                {
                    "case_id": spec.case_id,
                    "org_id": spec.org_id,
                    "roles": [role["id"] for role in spec.roles],
                    "required_artifacts": list(spec.required_artifacts),
                    "ui_root_task_contract": {
                        "created_before_process_message": True,
                        "one_fresh_uuid_per_session": True,
                        "parent_id": None,
                        "parent_session_id": None,
                        "linked_work_item_id": "",
                        "metadata": _office_ui_root_metadata(spec),
                        "process_message_origin_task_id": "<same persisted Task.id>",
                    },
                }
                for spec in CASES
            ],
        }
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    evidence_path = args.evidence_path
    try:
        evidence = asyncio.run(_run(args))
    except BaseException as exc:
        failure = {
            "success": False,
            "issue": 35,
            "failed_at": datetime.now().isoformat(),
            "error_type": type(exc).__name__,
            "error": str(exc),
            "opc_home": str(opc_home),
            "project_id": args.project_id,
        }
        if evidence_path is None:
            evidence_path = opc_home.parent / "issue35-company-e2e-failure.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(
            json.dumps(failure, ensure_ascii=False, indent=2, default=_json_default) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        print(f"failure evidence: {evidence_path}", file=sys.stderr)
        return 1

    if evidence_path is None:
        evidence_path = opc_home.parent / "issue35-company-e2e-evidence.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2, default=_json_default)
    evidence_path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    print(f"evidence: {evidence_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
