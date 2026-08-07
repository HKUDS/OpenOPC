from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import yaml

from opc.core.config import EmployeeConfig, OPCConfig, RoleConfig, TalentTemplateConfig
from opc.core.events import EventBus
from opc.core.models import (
    ExecutionMode,
    RouterDecision,
    Task,
    WorkItemExecutionStrategy,
)
from opc.database.store import OPCStore
from opc.engine import OPCEngine
from opc.layer2_organization.company_mode import CompanyRuntimeSpecBuilder
from opc.layer2_organization.org_engine import (
    OrgEngine,
    TASK_MODE_GENERAL_ROLE_ID,
)
from opc.layer2_organization.recruiter import CompanyRecruiter, build_recruitment_plan_from_payload
from opc.layer2_organization.task_graph import TaskGraphScheduler
from opc.layer2_organization.talent_market import TalentMarket
from opc.layer5_memory.memory_manager import MemoryManager
from tests._temp_paths import WorkspaceTemporaryDirectory, workspace_path

_REAL_TEMPORARY_DIRECTORY = tempfile.TemporaryDirectory


def setUpModule() -> None:
    tempfile.TemporaryDirectory = WorkspaceTemporaryDirectory  # type: ignore[assignment]


def tearDownModule() -> None:
    tempfile.TemporaryDirectory = _REAL_TEMPORARY_DIRECTORY  # type: ignore[assignment]


class DummyRecruiterLLM:
    def __init__(self, responses: list[object] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, object]] = []

    async def simple_chat(self, prompt: str, system: str | None = None, task_type: str | None = None) -> str:
        payload = json.loads(prompt)
        self.calls.append({
            "payload": payload,
            "system": system,
            "task_type": task_type,
        })
        if "category_catalog" in payload:
            request_text = str(payload.get("user_request", "") or "").lower()
            feedback_text = " ".join(payload.get("recruiter_feedback", [])).lower()
            categories = [item["category"] for item in payload.get("category_catalog", [])]
            roles = list(payload.get("roles", []) or [])
            if not categories:
                if roles:
                    return json.dumps(
                        {
                            "roles": [
                                {
                                    "role_id": role.get("role_id", ""),
                                    "action": "direct_role_execution",
                                    "categories": [],
                                    "rationale": "No staffing catalog is available, so ordinary role execution is sufficient",
                                }
                                for role in roles
                            ]
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {
                        "action": "direct_role_execution",
                        "categories": [],
                        "rationale": "No staffing catalog is available, so ordinary role execution is sufficient",
                    },
                    ensure_ascii=False,
                )
            if roles:
                return json.dumps(
                    {
                        "roles": [
                            self._triage_role_payload(
                                role,
                                request_text=request_text,
                                feedback_text=feedback_text,
                                categories=categories,
                            )
                            for role in roles
                        ]
                    },
                    ensure_ascii=False,
            )

            need = payload.get("need", {}) or {}
            role_text = str(need.get("role_responsibility", "") or "").lower()
            legacy_request_text = str(need.get("user_request", "") or "").lower()
            combined = (role_text + " " + legacy_request_text).strip()
            if combined in {"hi", "hello", "你好", "您好"}:
                return json.dumps(
                    {
                        "action": "direct_role_execution",
                        "categories": [],
                        "rationale": "Simple greeting; ordinary role execution is sufficient",
                    },
                    ensure_ascii=False,
                )
            # Match a role to a category only when the role's own responsibility
            # carries the keyword. The user request is contextual, not enough
            # on its own to flag every role as engineering-relevant.
            preferred = []
            if any(token in feedback_text for token in ("test", "testing", "qa", "validation")) and "testing" in categories:
                preferred.append("testing")
            if any(
                token in role_text
                for token in ("backend", "api", "code", "implementation", "implement", "technical")
            ) and "engineering" in categories:
                preferred.append("engineering")
            if any(
                token in role_text
                for token in ("finance", "financial", "investment", "portfolio", "valuation")
            ) and "finance" in categories:
                preferred.append("finance")
            if any(token in role_text for token in ("test", "qa", "validation")) and "testing" in categories:
                preferred.append("testing")
            if not preferred:
                return json.dumps(
                    {
                        "action": "direct_role_execution",
                        "categories": [],
                        "rationale": "Role does not match any staffing category",
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {
                    "action": "category_screening",
                    "categories": preferred[:3],
                    "rationale": "Selected the closest talent categories for this staffing need",
                },
                ensure_ascii=False,
            )
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            if callable(response):
                response = response(payload)
            return str(response)
        if "roles" in payload:
            proposals: list[dict[str, object]] = []
            candidate_pool = list(payload.get("candidate_pool", []) or [])
            for role in payload.get("roles", []):
                role_id = str(role.get("role_id", "") or "")
                existing = list(role.get("existing_employees", []) or [])
                selected_categories = [
                    str(category or "").strip().lower()
                    for category in list(role.get("selected_categories", []) or [])
                    if str(category or "").strip()
                ]
                candidates: list[dict[str, object]] = []
                seen_candidates: set[str] = set()
                for category in selected_categories:
                    for candidate in candidate_pool:
                        candidate_id = str(candidate.get("template_id", "") or "")
                        candidate_category = str(candidate.get("category", "") or "").strip().lower()
                        if candidate_id and candidate_id not in seen_candidates and candidate_category == category:
                            seen_candidates.add(candidate_id)
                            candidates.append(candidate)
                if existing:
                    first_existing = existing[0]
                    proposals.append(
                        {
                            "role_id": role_id,
                            "employee_id": first_existing["employee_id"],
                            "template_id": "",
                            "proposed_employee_name": "",
                            "rationale": "Existing employee has the strongest relevant experience",
                            "status": "existing_staff",
                        }
                    )
                    continue
                if candidates:
                    first = candidates[0]
                    proposals.append(
                        {
                            "role_id": role_id,
                            "employee_id": "",
                            "template_id": first["template_id"],
                            "proposed_employee_name": first["name"],
                            "rationale": "Best fit based on category, name, and description",
                            "status": "proposed_hire",
                        }
                    )
                    continue
                if str(role.get("triage_action", "") or "") == "direct_role_execution":
                    proposals.append(
                        {
                            "role_id": role_id,
                            "employee_id": "",
                            "template_id": "",
                            "proposed_employee_name": "",
                            "rationale": "Ordinary role execution is sufficient",
                            "status": "direct_role_execution",
                        }
                    )
                    continue
                proposals.append(
                    {
                        "role_id": role_id,
                        "employee_id": "",
                        "template_id": "",
                        "proposed_employee_name": "",
                        "rationale": "No credible imported candidate was available for this role need",
                        "status": "fallback_role_only",
                    }
                )
            return json.dumps({"proposals": proposals}, ensure_ascii=False)
        if payload.get("existing_employees"):
            first_existing = payload["existing_employees"][0]
            return json.dumps(
                {
                    "employee_id": first_existing["employee_id"],
                    "template_id": "",
                    "proposed_employee_name": "",
                    "rationale": "Existing employee has the strongest relevant experience",
                    "status": "existing_staff",
                },
                ensure_ascii=False,
            )
        if not payload.get("new_candidates"):
            return json.dumps(
                {
                    "employee_id": "",
                    "template_id": "",
                    "proposed_employee_name": "",
                    "rationale": "No credible imported candidate was available for this role need",
                    "status": "fallback_role_only",
                },
                ensure_ascii=False,
            )
        first = payload["new_candidates"][0]
        return json.dumps(
            {
                "employee_id": "",
                "template_id": first["template_id"],
                "proposed_employee_name": first["name"],
                "rationale": "Best fit based on category, name, and description",
                "status": "proposed_hire",
            },
            ensure_ascii=False,
        )

    def _triage_role_payload(
        self,
        role: dict[str, object],
        *,
        request_text: str,
        feedback_text: str,
        categories: list[str],
    ) -> dict[str, object]:
        role_text = str(role.get("role_responsibility", "") or "").lower()
        combined = (role_text + " " + request_text).strip()
        if combined in {"hi", "hello", "你好", "您好"}:
            return {
                "role_id": role.get("role_id", ""),
                "action": "direct_role_execution",
                "categories": [],
                "rationale": "Simple greeting; ordinary role execution is sufficient",
            }
        preferred = []
        if any(token in feedback_text for token in ("test", "testing", "qa", "validation")) and "testing" in categories:
            preferred.append("testing")
        if any(
            token in role_text
            for token in ("backend", "api", "code", "implementation", "implement", "technical")
        ) and "engineering" in categories:
            preferred.append("engineering")
        if any(
            token in f"{role_text} {request_text}"
            for token in ("finance", "financial", "investment", "portfolio", "valuation")
        ) and "finance" in categories:
            preferred.append("finance")
        if any(token in role_text for token in ("test", "qa", "validation")) and "testing" in categories:
            preferred.append("testing")
        if not preferred:
            return {
                "role_id": role.get("role_id", ""),
                "action": "direct_role_execution",
                "categories": [],
                "rationale": "Role does not match any staffing category",
            }
        return {
            "role_id": role.get("role_id", ""),
            "action": "category_screening",
            "categories": preferred[:3],
            "rationale": "Selected the closest talent categories for this staffing need",
        }


class DummyCompanyExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[object, list[object]]] = []

    async def execute(self, runtime_plan, tasks):
        self.calls.append((runtime_plan, tasks))
        return "runtime executed"


class CompanyRecruiterFlowTests(unittest.IsolatedAsyncioTestCase):
    def _make_template(self, *, template_id: str, name: str, category: str, domains: list[str], description: str) -> TalentTemplateConfig:
        return TalentTemplateConfig(
            id=template_id,
            name=name,
            description=description,
            category=category,
            domains=domains,
            tags=list(domains),
            prompt_ref=f"prompts/talent/{template_id}.md",
            source_repo=str(workspace_path("agency-agents", base="recruiter-fixtures")),
            source_path=f"{category}/{template_id}.md",
            source_revision="local",
        )

    async def _build_engine(self, root: Path, config: OPCConfig, llm: DummyRecruiterLLM) -> tuple[OPCEngine, OPCStore]:
        self._write_talent_catalog(root, list(config.org.talent_templates))
        store = OPCStore(root / "tasks.db")
        await store.initialize()
        engine = OPCEngine(config=config, opc_home=root, project_id="proj1")
        engine.store = store
        engine.memory = MemoryManager(root, "proj1", store=store)
        engine.org_engine = OrgEngine(config, root)
        engine.talent_market = TalentMarket(root, config)
        engine.task_scheduler = TaskGraphScheduler(store, EventBus())
        engine.company_runtime_spec_builder = CompanyRuntimeSpecBuilder(engine.org_engine)
        engine.company_recruiter = CompanyRecruiter(llm, engine.org_engine, engine.talent_market)
        engine.company_executor = DummyCompanyExecutor()
        return engine, store

    def _write_talent_catalog(self, root: Path, templates: list[TalentTemplateConfig]) -> None:
        talent_dir = root / "prompts" / "talent"
        talent_dir.mkdir(parents=True, exist_ok=True)
        for template in templates:
            payload = {
                "id": template.id,
                "name": template.name,
                "description": template.description,
                "category": template.category,
                "domains": list(template.domains),
                "tags": list(template.tags),
            }
            body = f"# {template.name}\n\n{template.description}\n"
            (talent_dir / f"{template.id}.md").write_text(
                "---\n"
                + yaml.dump(payload, default_flow_style=False, sort_keys=False, allow_unicode=True)
                + "---\n\n"
                + body,
                encoding="utf-8",
            )

    def _company_decision(self) -> RouterDecision:
        decision = RouterDecision(
            mode=ExecutionMode.COMPANY_MODE,
            company_profile="corporate",
            domains=[],
        )
        return decision

    def _task_decision(self) -> RouterDecision:
        return RouterDecision(
            mode=ExecutionMode.TASK_MODE,
            company_profile="corporate",
            domains=[],
        )

    async def test_execute_decision_opens_staffing_preflight_before_initial_recruitment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)

            response = await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-1",
            )

            checkpoint = await store.get_latest_pending_checkpoint("proj1", "sess-1")
            tasks = await store.get_tasks(project_id="proj1")
            self.assertIn("pending manual staffing selection", response)
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint.checkpoint_type, "company_staffing_selection")
            self.assertEqual(checkpoint.payload["staffing_strategy"], "initial_recruitment")
            self.assertEqual(checkpoint.payload["recommended_action"], "auto_recruit")
            self.assertEqual(checkpoint.payload["staffing_defaults"]["source"], "system")
            self.assertTrue(all(role["default_selection"]["kind"] == "fallback" for role in checkpoint.payload["staffing_roles"]))
            # Card defaults become per-role overrides on approve, so they must
            # reflect what will actually run (OBS-11): the role template's
            # external preference when it can run, native otherwise.
            agents_by_role = {
                role["role_id"]: role["selected_agent"]
                for role in checkpoint.payload["staffing_roles"]
            }
            self.assertTrue(all(
                role["selected_agent"] == role["default_agent"]
                for role in checkpoint.payload["staffing_roles"]
            ))
            self.assertEqual(agents_by_role.get("ceo"), "native")
            self.assertEqual(agents_by_role.get("senior_engineer"), "codex")
            self.assertEqual(llm.calls, [])
            self.assertEqual(tasks, [])
            await store.close()

    async def test_execute_decision_pauses_for_manual_staffing_when_existing_employee_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.employees = [
                EmployeeConfig(
                    employee_id="engineering-existing-specialist",
                    template_id="engineering-existing-specialist",
                    name="Existing Specialist",
                    role_id="senior_engineer",
                    description="Existing engineering employee.",
                    category="engineering",
                    domains=["backend"],
                    prompt_refs=[],
                )
            ]
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)

            response = await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-manual-1",
            )

            checkpoint = await store.get_latest_pending_checkpoint("proj1", "sess-manual-1")
            tasks = await store.get_tasks(project_id="proj1")
            self.assertIn("pending manual staffing selection", response)
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint.checkpoint_type, "company_staffing_selection")
            self.assertEqual(llm.calls, [])
            self.assertEqual(tasks, [])
            employee_ids = {
                item["employee_id"]
                for item in checkpoint.payload["staffing_pool"]["employees"]
            }
            template_ids = {
                item["template_id"]
                for item in checkpoint.payload["staffing_pool"]["templates"]
            }
            self.assertIn("engineering-existing-specialist", employee_ids)
            self.assertIn("engineering-backend-architect", template_ids)
            senior_role = next(role for role in checkpoint.payload["staffing_roles"] if role["role_id"] == "senior_engineer")
            self.assertEqual(
                senior_role["default_selection"],
                {
                    "kind": "employee",
                    "id": "engineering-existing-specialist",
                    "employee_id": "engineering-existing-specialist",
                },
            )
            self.assertEqual(senior_role["selected_agent"], "codex")
            await store.close()

    async def test_org_hire_existing_canonical_employee_staffs_requested_role(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-fullstack-specialist",
                    name="Fullstack Specialist",
                    category="engineering",
                    domains=["frontend", "backend"],
                    description="Builds and reviews fullstack product features.",
                )
            ]
            self._write_talent_catalog(root, list(config.org.talent_templates))
            market = TalentMarket(root, config)

            market.hire_template("engineering-fullstack-specialist", "senior_engineer")
            employee = market.hire_template("engineering-fullstack-specialist", "qa_analyst")

            real_employees = [
                item for item in config.org.employees
                if item.employee_id == "engineering-fullstack-specialist"
            ]
            self.assertEqual(len(real_employees), 1)
            self.assertEqual(employee.employee_id, "engineering-fullstack-specialist")
            self.assertEqual(employee.role_id, "senior_engineer")
            self.assertIn("senior_engineer", employee.metadata.get("staffed_role_ids", []))
            self.assertIn("qa_analyst", employee.metadata.get("staffed_role_ids", []))

            org_engine = OrgEngine(config, root)
            qa_employees = org_engine.list_employees(role_id="qa_analyst")
            self.assertEqual([item.employee_id for item in qa_employees], ["engineering-fullstack-specialist"])
            qa_assignment = org_engine.resolve_employee_for_work_item("qa_analyst", project_id="proj1")
            self.assertIsNotNone(qa_assignment)
            assert qa_assignment is not None
            self.assertEqual(qa_assignment["employee_id"], "engineering-fullstack-specialist")
            self.assertEqual(qa_assignment["role_id"], "qa_analyst")

            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            response = await engine._execute_decision(
                self._company_decision(),
                "Review the fullstack implementation quality",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-org-hire",
            )
            checkpoint = await store.get_latest_pending_checkpoint("proj1", "sess-org-hire")
            self.assertIn("pending manual staffing selection", response)
            self.assertIsNotNone(checkpoint)
            assert checkpoint is not None
            qa_role = next(role for role in checkpoint.payload["staffing_roles"] if role["role_id"] == "qa_analyst")
            self.assertEqual(
                qa_role["default_selection"],
                {
                    "kind": "employee",
                    "id": "engineering-fullstack-specialist",
                    "employee_id": "engineering-fullstack-specialist",
                },
            )
            await store.close()

    async def test_org_hire_beats_stale_project_fallback_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.employees = [
                EmployeeConfig(
                    employee_id="engineering-existing-specialist",
                    template_id="engineering-existing-specialist",
                    name="Existing Specialist",
                    role_id="senior_engineer",
                    description="Existing engineering employee.",
                    category="engineering",
                    domains=["backend"],
                    prompt_refs=[],
                )
            ]
            engine, store = await self._build_engine(root, config, DummyRecruiterLLM())
            decision = self._company_decision()
            engine._save_project_company_staffing_defaults(
                decision,
                company_profile="corporate",
                role_ids={"senior_engineer"},
                staffing_overrides={},
                staffing_experience_modes={},
                fallback_role_ids={"senior_engineer"},
                role_agent_overrides={},
            )
            runtime_spec = engine.company_runtime_spec_builder.build_spec(
                decision,
                original_message="Build another backend API",
            )

            payload = engine._build_manual_staffing_checkpoint_payload(
                decision,
                "Build another backend API",
                runtime_spec,
                session_id="sess-defaults-org-hire",
                origin_channel="cli",
                origin_chat_id="",
                origin_thread_id="",
            )

            self.assertIsNotNone(payload)
            assert payload is not None
            senior_role = next(role for role in payload["staffing_roles"] if role["role_id"] == "senior_engineer")
            self.assertEqual(
                senior_role["default_selection"],
                {
                    "kind": "employee",
                    "id": "engineering-existing-specialist",
                    "employee_id": "engineering-existing-specialist",
                },
            )
            self.assertEqual(senior_role["default_source"], "org")
            await store.close()

    async def test_company_preflight_manual_creates_staffing_checkpoint_without_employees(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.employees = []
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-frontend-developer",
                    name="Frontend Developer",
                    category="engineering",
                    domains=["frontend", "game"],
                    description="Frontend implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            decision = self._company_decision()
            decision.metadata["company_preflight"] = "manual"
            decision.preferred_agent = "opencode"

            response = await engine._execute_decision(
                decision,
                "Build a browser game",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-manual-empty",
            )

            checkpoint = await store.get_latest_pending_checkpoint("proj1", "sess-manual-empty")
            tasks = await store.get_tasks(project_id="proj1")
            self.assertIn("pending manual staffing selection", response)
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint.checkpoint_type, "company_staffing_selection")
            self.assertEqual(checkpoint.payload["staffing_strategy"], "initial_recruitment")
            self.assertEqual(checkpoint.payload["recommended_action"], "auto_recruit")
            self.assertEqual(checkpoint.payload["staffing_pool"]["employees"], [])
            self.assertGreater(len(checkpoint.payload["staffing_roles"]), 0)
            self.assertTrue(all(role["default_selection"]["kind"] == "fallback" for role in checkpoint.payload["staffing_roles"]))
            # Card defaults become per-role overrides on approve, so they must
            # reflect what will actually run (OBS-11): the role template's
            # external preference when it can run, native otherwise.
            agents_by_role = {
                role["role_id"]: role["selected_agent"]
                for role in checkpoint.payload["staffing_roles"]
            }
            self.assertTrue(all(
                role["selected_agent"] == role["default_agent"]
                for role in checkpoint.payload["staffing_roles"]
            ))
            # decision.preferred_agent="opencode" is an explicit session-level
            # choice and outranks role template preferences.
            self.assertEqual(agents_by_role.get("ceo"), "opencode")
            self.assertEqual(agents_by_role.get("senior_engineer"), "opencode")
            template_ids = {item["template_id"] for item in checkpoint.payload["staffing_pool"]["templates"]}
            self.assertIn("engineering-frontend-developer", template_ids)
            self.assertEqual(llm.calls, [])
            self.assertEqual(tasks, [])
            await store.close()

    async def test_manual_role_agent_override_beats_default_native_preferred_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            engine, store = await self._build_engine(root, config, DummyRecruiterLLM())
            decision = self._company_decision()
            decision.preferred_agent = "native"

            topology = engine.org_engine.build_runtime_delegation_topology()
            enriched = engine._enrich_runtime_delegation_topology(
                runtime_topology=topology,
                decision=decision,
                project_id="proj1",
                fallback_role_ids={"ceo", "senior_engineer"},
                role_agent_overrides={"ceo": "codex", "senior_engineer": "opencode"},
            )
            seats_by_role = {
                seat["role_id"]: seat
                for seat in enriched["seats"]
                if seat.get("role_id") in {"ceo", "senior_engineer"}
            }

            self.assertEqual(seats_by_role["ceo"]["selected_execution_agent"], "codex")
            self.assertEqual(seats_by_role["ceo"]["preferred_external_agent"], "codex")
            self.assertFalse(seats_by_role["ceo"]["force_native_execution"])
            self.assertEqual(seats_by_role["senior_engineer"]["selected_execution_agent"], "opencode")
            self.assertEqual(seats_by_role["senior_engineer"]["preferred_external_agent"], "opencode")
            self.assertFalse(seats_by_role["senior_engineer"]["force_native_execution"])
            await store.close()

    async def test_project_company_staffing_defaults_persist_latest_manual_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.employees = [
                EmployeeConfig(
                    employee_id="engineering-existing-specialist",
                    template_id="engineering-existing-specialist",
                    name="Existing Specialist",
                    role_id="senior_engineer",
                    description="Existing engineering employee.",
                    category="engineering",
                    domains=["backend"],
                    prompt_refs=[],
                )
            ]
            engine, store = await self._build_engine(root, config, DummyRecruiterLLM())
            decision = self._company_decision()
            await engine._execute_decision(
                decision,
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-defaults-1",
            )

            result = await engine._maybe_resume_checkpoint(
                "approve",
                "sess-defaults-1",
                reply_metadata={
                    "staffing_action": "manual_approve",
                    "staffing_selections": {
                        "senior_engineer": {
                            "kind": "employee",
                            "id": "engineering-existing-specialist",
                        }
                    },
                    "recruitment_role_agents": {
                        "senior_engineer": "opencode",
                    },
                },
            )

            self.assertIn("runtime executed", result)
            defaults_path = root / "projects" / "proj1" / "company_staffing_defaults.json"
            self.assertTrue(defaults_path.exists())
            runtime_spec = engine.company_runtime_spec_builder.build_spec(
                self._company_decision(),
                original_message="Build another backend API",
            )
            payload = engine._build_manual_staffing_checkpoint_payload(
                self._company_decision(),
                "Build another backend API",
                runtime_spec,
                session_id="sess-defaults-2",
                origin_channel="cli",
                origin_chat_id="",
                origin_thread_id="",
            )
            self.assertIsNotNone(payload)
            assert payload is not None
            self.assertEqual(payload["staffing_defaults"]["source"], "project")
            senior_role = next(role for role in payload["staffing_roles"] if role["role_id"] == "senior_engineer")
            self.assertEqual(
                senior_role["default_selection"],
                {
                    "kind": "employee",
                    "id": "engineering-existing-specialist",
                    "employee_id": "engineering-existing-specialist",
                },
            )
            self.assertEqual(senior_role["selected_agent"], "opencode")
            ceo_role = next(role for role in payload["staffing_roles"] if role["role_id"] == "ceo")
            self.assertEqual(ceo_role["default_selection"], {"kind": "fallback", "id": ""})
            self.assertEqual(ceo_role["selected_agent"], "native")
            await store.close()

    async def test_confirmed_session_reuses_staffing_defaults_without_recruiter_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.employees = [
                EmployeeConfig(
                    employee_id="engineering-existing-specialist",
                    template_id="engineering-existing-specialist",
                    name="Existing Specialist",
                    role_id="senior_engineer",
                    description="Existing engineering employee.",
                    category="engineering",
                    domains=["backend"],
                    prompt_refs=[],
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            decision = self._company_decision()
            await engine._execute_decision(
                decision,
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-defaults-reuse",
            )

            first_result = await engine._maybe_resume_checkpoint(
                "approve",
                "sess-defaults-reuse",
                reply_metadata={
                    "staffing_action": "manual_approve",
                    "staffing_selections": {
                        "senior_engineer": {
                            "kind": "employee",
                            "id": "engineering-existing-specialist",
                        }
                    },
                    "recruitment_role_agents": {
                        "senior_engineer": "opencode",
                    },
                },
            )
            self.assertIn("runtime executed", first_result)
            self.assertEqual(llm.calls, [])

            second_result = await engine._execute_decision(
                decision,
                "Build another backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-defaults-reuse",
            )

            tasks = await store.get_tasks(project_id="proj1")
            checkpoints = await store.get_execution_checkpoints(
                project_id="proj1",
                session_id="sess-defaults-reuse",
            )
            latest_task = max(tasks, key=lambda task: task.created_at)
            self.assertIn("runtime executed", second_result)
            self.assertEqual(llm.calls, [])
            self.assertEqual(
                latest_task.metadata["recruitment_staffing_overrides"]["senior_engineer"],
                "engineering-existing-specialist",
            )
            self.assertEqual(
                latest_task.metadata["delegation_playbook"]["recruitment_staffing_experience_modes"]["senior_engineer"],
                "with_experience",
            )
            self.assertEqual(
                latest_task.metadata["recruitment_role_agent_overrides"]["senior_engineer"],
                "opencode",
            )
            self.assertFalse(
                [
                    checkpoint
                    for checkpoint in checkpoints
                    if checkpoint.checkpoint_type == "company_recruitment_confirmation"
                    and checkpoint.status == "pending"
                ]
            )
            await store.close()

    async def test_manual_staffing_template_selection_hires_and_executes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.employees = [
                EmployeeConfig(
                    employee_id="engineering-existing-specialist",
                    template_id="engineering-existing-specialist",
                    name="Existing Specialist",
                    role_id="senior_engineer",
                    description="Existing engineering employee.",
                    category="engineering",
                    domains=["backend"],
                    prompt_refs=[],
                )
            ]
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-manual-2",
            )

            result = await engine._maybe_resume_checkpoint(
                "approve",
                "sess-manual-2",
                reply_metadata={
                    "staffing_action": "manual_approve",
                    "staffing_selections": {
                        "senior_engineer": {
                            "kind": "template",
                            "id": "engineering-backend-architect",
                        }
                    },
                    "recruitment_role_agents": {"senior_engineer": "codex"},
                },
            )

            tasks = await store.get_tasks(project_id="proj1")
            employee_ids = {employee.employee_id for employee in config.org.employees}
            self.assertIn("Approved manual staffing", result)
            self.assertIn("runtime executed", result)
            self.assertIn("engineering-backend-architect", employee_ids)
            self.assertIn("engineering-existing-specialist", employee_ids)
            self.assertEqual(
                tasks[0].metadata["recruitment_staffing_overrides"]["senior_engineer"],
                "engineering-backend-architect",
            )
            self.assertIn("cto", tasks[0].metadata["recruitment_fallback_role_ids"])
            self.assertEqual(
                tasks[0].metadata["recruitment_role_agent_overrides"]["senior_engineer"],
                "codex",
            )
            await store.close()

    async def test_manual_staffing_auto_recruit_continues_to_recruitment_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.employees = [
                EmployeeConfig(
                    employee_id="engineering-existing-specialist",
                    template_id="engineering-existing-specialist",
                    name="Existing Specialist",
                    role_id="senior_engineer",
                    description="Existing engineering employee.",
                    category="engineering",
                    domains=["backend"],
                    prompt_refs=[],
                )
            ]
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-manual-auto",
            )

            response = await engine._maybe_resume_checkpoint(
                "auto recruit",
                "sess-manual-auto",
                reply_metadata={
                    "staffing_action": "auto_recruit",
                    "recruitment_role_agents": {"senior_engineer": "cursor"},
                },
            )

            checkpoint = await store.get_latest_pending_checkpoint("proj1", "sess-manual-auto")
            self.assertIn("pending staffing decision", response)
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint.checkpoint_type, "company_recruitment_confirmation")
            recruitment_plan = build_recruitment_plan_from_payload(checkpoint.payload["recruitment_plan"])
            senior_proposal = next(
                proposal
                for proposal in recruitment_plan.proposals
                if proposal.role_id == "senior_engineer"
            )
            self.assertEqual(senior_proposal.metadata["selected_execution_agent"], "cursor")
            self.assertEqual(checkpoint.payload["recruitment_role_agents"]["senior_engineer"], "cursor")
            from opc.plugins.office_ui.event_adapter import EventAdapter
            from opc.plugins.office_ui.ws_handler import WSHandler

            meta = WSHandler(engine, MagicMock(), MagicMock(), EventAdapter())._build_recruitment_meta(
                checkpoint,
                engine=engine,
            )
            from opc.plugins.office_ui.snapshot_builder import _build_snapshot_checkpoint_meta

            snapshot_meta = await _build_snapshot_checkpoint_meta(
                engine,
                SimpleNamespace(session_id="sess-manual-auto", metadata={}),
            )
            self.assertIn("staffing_roles", meta)
            self.assertIn("staffing_pool", meta)
            self.assertIn("staffing_selections", meta)
            senior_role = next(role for role in meta["staffing_roles"] if role["role_id"] == "senior_engineer")
            self.assertEqual(senior_role["default_selection"]["kind"], "employee")
            self.assertEqual(senior_role["default_selection"]["id"], "engineering-existing-specialist")
            self.assertEqual(senior_role["selected_agent"], "cursor")
            self.assertEqual(meta["recruitment_role_agents"]["senior_engineer"], "cursor")
            senior_meta_proposal = next(proposal for proposal in meta["proposals"] if proposal["role_id"] == "senior_engineer")
            self.assertEqual(senior_meta_proposal["selected_agent"], "cursor")
            self.assertIsNotNone(snapshot_meta)
            assert snapshot_meta is not None
            self.assertEqual(snapshot_meta["recruitment_role_agents"]["senior_engineer"], "cursor")
            snapshot_role = next(role for role in snapshot_meta["staffing_roles"] if role["role_id"] == "senior_engineer")
            self.assertEqual(snapshot_role["selected_agent"], "cursor")
            self.assertTrue(meta["recruitment_rationales"][0]["rationale"])
            self.assertGreater(len(llm.calls), 0)
            await store.close()

    async def test_manual_staffing_auto_recruit_does_not_silently_execute_no_hire_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.roles = [
                RoleConfig(
                    id="managing_partner",
                    name="Managing Partner",
                    responsibility="Owns investment mandate and final company selection.",
                    reports_to="owner",
                    tools=[],
                )
            ]
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            await engine._execute_decision(
                self._company_decision(),
                "Find the top VC investments",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-auto-no-hire",
            )

            response = await engine._maybe_resume_checkpoint(
                "auto recruit",
                "sess-auto-no-hire",
                reply_metadata={"staffing_action": "auto_recruit"},
            )

            checkpoint = await store.get_latest_pending_checkpoint("proj1", "sess-auto-no-hire")
            tasks = await store.get_tasks(project_id="proj1")
            self.assertIn("pending staffing decision", response)
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint.checkpoint_type, "company_recruitment_confirmation")
            self.assertEqual(tasks, [])
            await store.close()

    async def test_recruitment_feedback_reruns_recruiter_and_updates_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                ),
                self._make_template(
                    template_id="engineering-api-tester",
                    name="API Tester",
                    category="testing",
                    domains=["api", "qa"],
                    description="API quality and backend validation specialist.",
                ),
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-2",
            )
            await engine._maybe_resume_checkpoint(
                "auto recruit",
                "sess-2",
                reply_metadata={
                    "staffing_action": "auto_recruit",
                    "recruitment_role_agents": {"senior_engineer": "cursor"},
                },
            )
            old_checkpoint = await store.get_latest_pending_checkpoint("proj1", "sess-2")
            assert old_checkpoint is not None

            revised = await engine._maybe_resume_checkpoint("Use someone more testing-oriented", "sess-2")

            checkpoint = await store.get_latest_pending_checkpoint("proj1", "sess-2")
            assert checkpoint is not None
            recruitment_plan = build_recruitment_plan_from_payload(checkpoint.payload["recruitment_plan"])
            checkpoints = await store.get_execution_checkpoints(project_id="proj1", session_id="sess-2")
            statuses = {item.checkpoint_id: item.status for item in checkpoints}
            from opc.plugins.office_ui.event_adapter import EventAdapter
            from opc.plugins.office_ui.ws_handler import WSHandler

            meta = WSHandler(engine, MagicMock(), MagicMock(), EventAdapter())._build_recruitment_meta(
                checkpoint,
                engine=engine,
            )
            self.assertIn("Recruiter feedback so far", revised)
            self.assertNotEqual(checkpoint.checkpoint_id, old_checkpoint.checkpoint_id)
            self.assertEqual(statuses[old_checkpoint.checkpoint_id], "superseded")
            self.assertEqual(statuses[checkpoint.checkpoint_id], "pending")
            self.assertEqual(checkpoint.payload["previous_checkpoint_id"], old_checkpoint.checkpoint_id)
            self.assertEqual(checkpoint.payload["recruitment_revision"], 2)
            testing_proposals = [
                proposal
                for proposal in recruitment_plan.proposals
                if proposal.candidate and proposal.candidate.template_id == "engineering-api-tester"
            ]
            self.assertTrue(testing_proposals)
            self.assertEqual(recruitment_plan.recruiter_feedback, ["Use someone more testing-oriented"])
            first_role = testing_proposals[0].role_id
            senior_proposal = next(
                proposal
                for proposal in recruitment_plan.proposals
                if proposal.role_id == "senior_engineer"
            )
            self.assertEqual(senior_proposal.metadata["selected_execution_agent"], "cursor")
            self.assertEqual(checkpoint.payload["recruitment_role_agents"]["senior_engineer"], "cursor")
            self.assertEqual(
                meta["staffing_selections"][first_role]["id"],
                testing_proposals[0].candidate.template_id,
            )
            self.assertTrue(meta["recruitment_rationales"][0]["rationale"])
            await store.close()

    async def test_approve_recruitment_persists_hires_and_executes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-3",
            )
            await engine._maybe_resume_checkpoint(
                "auto recruit",
                "sess-3",
                reply_metadata={"staffing_action": "auto_recruit"},
            )

            result = await engine._maybe_resume_checkpoint("approve", "sess-3")

            tasks = await store.get_tasks(project_id="proj1")
            self.assertIn("Approved recruitment plan", result)
            self.assertIn("runtime executed", result)
            employee_ids = {employee.employee_id for employee in config.org.employees}
            self.assertIn("engineering-backend-architect", employee_ids)
            org_payload = yaml.safe_load(
                (root / "config" / "company_orgs" / "org_corporate_config.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(org_payload["employees"], [])
            self.assertEqual(org_payload["talent_templates"], [])
            self.assertTrue(
                (root / "company_state" / "corporate" / "employees" / "engineering-backend-architect.yaml").exists()
            )
            self.assertFalse(
                (
                    root
                    / "company_state"
                    / "corporate"
                    / "employees"
                    / "senior_engineer-engineering-backend-architect.yaml"
                ).exists()
            )
            self.assertEqual(tasks[0].metadata["employee_assignment"]["employee_id"], "ceo-default-employee")
            self.assertEqual(
                tasks[0].metadata["recruitment_staffing_overrides"]["senior_engineer"],
                "engineering-backend-architect",
            )
            self.assertEqual(
                tasks[0].metadata["delegation_playbook"]["recruitment_role_agent_overrides"]["senior_engineer"],
                "codex",
            )
            session = await store.get_session("sess-3")
            self.assertTrue(dict(session.metadata).get("recruitment_confirmation_completed"))
            self.assertIsNone(await store.get_latest_pending_checkpoint("proj1", "sess-3"))
            await store.close()

    async def test_approve_recruitment_uses_adjusted_staffing_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                ),
                self._make_template(
                    template_id="engineering-api-tester",
                    name="API Tester",
                    category="testing",
                    domains=["api", "qa"],
                    description="API quality and backend validation specialist.",
                ),
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-3-adjusted",
            )
            await engine._maybe_resume_checkpoint(
                "auto recruit",
                "sess-3-adjusted",
                reply_metadata={"staffing_action": "auto_recruit"},
            )

            result = await engine._maybe_resume_checkpoint(
                "approve",
                "sess-3-adjusted",
                reply_metadata={
                    "staffing_selections": {
                        "senior_engineer": {
                            "kind": "template",
                            "id": "engineering-api-tester",
                        }
                    }
                },
            )

            tasks = await store.get_tasks(project_id="proj1")
            employee_ids = {employee.employee_id for employee in config.org.employees}
            self.assertIn("runtime executed", result)
            self.assertIn("engineering-api-tester", employee_ids)
            self.assertIn("engineering-backend-architect", employee_ids)
            self.assertEqual(
                tasks[0].metadata["recruitment_staffing_overrides"]["senior_engineer"],
                "engineering-api-tester",
            )
            await store.close()

    async def test_approve_recruitment_applies_role_agent_override_to_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-3-agent-override",
            )

            result = await engine._maybe_resume_checkpoint(
                "approve",
                "sess-3-agent-override",
                reply_metadata={"recruitment_role_agents": {"senior_engineer": "codex"}},
            )

            tasks = await store.get_tasks(project_id="proj1")
            self.assertIn("runtime executed", result)
            self.assertEqual(tasks[0].metadata["recruitment_role_agent_overrides"]["senior_engineer"], "codex")
            self.assertEqual(
                tasks[0].metadata["delegation_playbook"]["recruitment_role_agent_overrides"]["senior_engineer"],
                "codex",
            )
            await store.close()

    async def test_approve_recruitment_replaces_stale_role_employee_with_approved_hire(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-3-already-hired",
            )
            await engine._maybe_resume_checkpoint(
                "auto recruit",
                "sess-3-already-hired",
                reply_metadata={"staffing_action": "auto_recruit"},
            )
            config.org.employees.append(
                EmployeeConfig(
                    employee_id="engineering-existing-specialist",
                    template_id="engineering-existing-specialist",
                    name="Existing Specialist",
                    role_id="senior_engineer",
                    description="Already approved for this role.",
                    category="engineering",
                    domains=["backend"],
                    prompt_refs=[],
                )
            )

            result = await engine._maybe_resume_checkpoint("approve", "sess-3-already-hired")

            tasks = await store.get_tasks(project_id="proj1")
            employee_ids = {employee.employee_id for employee in config.org.employees}
            self.assertIn("runtime executed", result)
            self.assertIn("engineering-existing-specialist", employee_ids)
            self.assertIn("engineering-backend-architect", employee_ids)
            self.assertEqual(
                tasks[0].metadata["recruitment_staffing_overrides"]["senior_engineer"],
                "engineering-backend-architect",
            )
            await store.close()

    async def test_sync_origin_task_execution_context_persists_workspace_and_comms_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)

            origin_task = Task(
                id="origin-root",
                title="Root Session",
                project_id="proj1",
                session_id="sess-root",
                metadata={"exec_mode": "company"},
            )
            await store.save_task(origin_task)

            workspace_contract = {
                "workspace_root": str(root / "workspace"),
                "output_root": str(root / "workspace" / "deliverables"),
                "comms_workspace_root": str(root / "workspace"),
                "comms_root": str(root / "workspace" / ".opc-comms"),
            }
            await engine._sync_origin_task_execution_context(
                "origin-root",
                session_id="sess-root",
                decision=self._company_decision(),
                workspace_contract=workspace_contract,
                original_message="Build the backend API",
            )

            refreshed = await store.get_task("origin-root")
            assert refreshed is not None
            self.assertEqual(refreshed.metadata["workspace_root"], str(root / "workspace"))
            self.assertEqual(refreshed.metadata["target_output_dir"], str(root / "workspace" / "deliverables"))
            self.assertEqual(refreshed.metadata["comms_workspace_root"], str(root / "workspace"))
            self.assertEqual(refreshed.metadata["comms_root"], str(root / "workspace" / ".opc-comms"))
            self.assertEqual(refreshed.metadata["execution_mode"], ExecutionMode.COMPANY_MODE.value)
            self.assertEqual(refreshed.metadata["company_profile"], "corporate")
            self.assertEqual(refreshed.metadata["origin_task_id"], "origin-root")
            await store.close()

    async def test_same_project_new_session_still_prompts_after_previous_session_completed_recruitment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)

            await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-prev",
            )
            await engine._maybe_resume_checkpoint("approve", "sess-prev")

            response = await engine._execute_decision(
                self._company_decision(),
                "Build the backend API again",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-new",
            )

            checkpoint = await store.get_latest_pending_checkpoint("proj1", "sess-new")
            self.assertIn("pending manual staffing selection", response)
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint.checkpoint_type, "company_staffing_selection")
            await store.close()

    async def test_same_session_skips_recruitment_prompt_after_previous_confirmation_completed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)

            await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-sticky",
            )
            await engine._maybe_resume_checkpoint("approve", "sess-sticky")

            response = await engine._execute_decision(
                self._company_decision(),
                "Build another backend API in the same session",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-sticky",
            )

            checkpoint = await store.get_latest_pending_checkpoint("proj1", "sess-sticky")
            session = await store.get_session("sess-sticky")
            self.assertIn("runtime executed", response)
            self.assertIsNone(checkpoint)
            self.assertTrue(dict(session.metadata).get("recruitment_confirmation_completed"))
            await store.close()

    async def test_new_project_new_session_still_prompts_after_other_project_completed_recruitment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)

            await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-proj1",
            )
            await engine._maybe_resume_checkpoint("approve", "sess-proj1")

            engine.project_id = "proj2"
            assert engine.memory
            engine.memory.set_project("proj2")

            response = await engine._execute_decision(
                self._company_decision(),
                "Build the backend API for project two",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-proj2",
            )

            checkpoint = await store.get_latest_pending_checkpoint("proj2", "sess-proj2")
            self.assertIn("pending manual staffing selection", response)
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint.checkpoint_type, "company_staffing_selection")
            self.assertEqual(checkpoint.payload["staffing_defaults"]["source"], "system")
            # Card defaults become per-role overrides on approve, so they must
            # reflect what will actually run (OBS-11): the role template's
            # external preference when it can run, native otherwise.
            agents_by_role = {
                role["role_id"]: role["selected_agent"]
                for role in checkpoint.payload["staffing_roles"]
            }
            self.assertTrue(all(
                role["selected_agent"] == role["default_agent"]
                for role in checkpoint.payload["staffing_roles"]
            ))
            self.assertEqual(agents_by_role.get("ceo"), "native")
            self.assertEqual(agents_by_role.get("senior_engineer"), "codex")
            await store.close()

    async def test_deny_recruitment_cancels_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-deny-1",
            )
            await engine._maybe_resume_checkpoint(
                "auto recruit",
                "sess-deny-1",
                reply_metadata={"staffing_action": "auto_recruit"},
            )

            result = await engine._maybe_resume_checkpoint("deny", "sess-deny-1")

            self.assertEqual(result, "Recruitment was cancelled. Execution will not continue.")
            self.assertIsNone(await store.get_latest_pending_checkpoint("proj1", "sess-deny-1"))
            self.assertEqual(await store.get_tasks(project_id="proj1"), [])
            await store.close()

    async def test_recruitment_keyword_with_extra_text_becomes_feedback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-feedback-1",
            )
            await engine._maybe_resume_checkpoint(
                "auto recruit",
                "sess-feedback-1",
                reply_metadata={"staffing_action": "auto_recruit"},
            )
            old_checkpoint = await store.get_latest_pending_checkpoint("proj1", "sess-feedback-1")
            assert old_checkpoint is not None

            result = await engine._maybe_resume_checkpoint("deny because this role should stay unstaffed", "sess-feedback-1")

            checkpoint = await store.get_latest_pending_checkpoint("proj1", "sess-feedback-1")
            assert checkpoint is not None
            recruitment_plan = build_recruitment_plan_from_payload(checkpoint.payload["recruitment_plan"])
            checkpoints = await store.get_execution_checkpoints(project_id="proj1", session_id="sess-feedback-1")
            statuses = {item.checkpoint_id: item.status for item in checkpoints}
            self.assertIn("Recruiter feedback so far", result)
            self.assertNotEqual(checkpoint.checkpoint_id, old_checkpoint.checkpoint_id)
            self.assertEqual(statuses[old_checkpoint.checkpoint_id], "superseded")
            self.assertEqual(checkpoint.payload["previous_checkpoint_id"], old_checkpoint.checkpoint_id)
            self.assertEqual(checkpoint.payload["recruitment_revision"], 2)
            self.assertEqual(
                recruitment_plan.recruiter_feedback,
                ["deny because this role should stay unstaffed"],
            )
            await store.close()

    async def test_task_mode_executes_directly_without_recruitment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                ),
                self._make_template(
                    template_id="marketing-content-strategist",
                    name="Content Strategist",
                    category="marketing",
                    domains=["writing"],
                    description="Brand messaging and editorial planning specialist.",
                ),
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)

            async def _fake_execute_single_agent(tasks, use_external=None):
                _ = (tasks, use_external)
                return "runtime executed"

            engine._execute_single_agent = _fake_execute_single_agent  # type: ignore[method-assign]

            response = await engine._execute_decision(
                self._task_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-task-1",
            )
            checkpoint = await store.get_latest_pending_checkpoint("proj1", "sess-task-1")
            tasks = await store.get_tasks(project_id="proj1")
            self.assertIsNone(checkpoint)
            self.assertEqual(llm.calls, [])
            self.assertIn("runtime executed", response)
            self.assertEqual(len(config.org.employees), 0)
            self.assertEqual(tasks[0].assigned_to, TASK_MODE_GENERAL_ROLE_ID)
            self.assertIsNone(tasks[0].assigned_external_agent)
            self.assertTrue(tasks[0].metadata["force_native_execution"])
            self.assertEqual(
                tasks[0].metadata["work_item_execution_strategy"],
                WorkItemExecutionStrategy.NATIVE.value,
            )
            self.assertEqual(
                tasks[0].metadata["task_mode_contract"],
                "single_full_capability_main_agent",
            )
            self.assertNotIn("employee_assignment", tasks[0].metadata)
            self.assertNotIn("employee_prompt_context", tasks[0].metadata)
            self.assertNotIn("employee_delta_context", tasks[0].metadata)
            await store.close()

    async def test_task_mode_simple_greeting_skips_recruitment_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)

            async def _fake_execute_single_agent(tasks, use_external=None):
                _ = (tasks, use_external)
                return "runtime executed"

            engine._execute_single_agent = _fake_execute_single_agent  # type: ignore[method-assign]

            result = await engine._execute_decision(
                self._task_decision(),
                "你好",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-task-hello",
            )

            checkpoint = await store.get_latest_pending_checkpoint("proj1", "sess-task-hello")
            tasks = await store.get_tasks(project_id="proj1")
            self.assertIsNone(checkpoint)
            self.assertEqual(len(tasks), 1)
            self.assertEqual(llm.calls, [])
            self.assertIn("runtime executed", result)
            self.assertEqual(tasks[0].assigned_to, TASK_MODE_GENERAL_ROLE_ID)
            self.assertNotIn("employee_assignment", tasks[0].metadata)
            await store.close()

    async def test_no_candidate_templates_falls_back_to_role_only_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            engine.talent_market.list_available_templates = lambda: []  # type: ignore[method-assign]
            result = await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-4",
            )

            checkpoint = await store.get_latest_pending_checkpoint("proj1", "sess-4")
            tasks = await store.get_tasks(project_id="proj1")
            employee_ids = {employee.employee_id for employee in config.org.employees}
            self.assertIsNotNone(checkpoint)
            self.assertEqual(checkpoint.checkpoint_type, "company_staffing_selection")
            self.assertEqual(checkpoint.payload["staffing_strategy"], "role_only_fallback")
            self.assertEqual(checkpoint.payload["recommended_action"], "manual_approve")
            result = await engine._maybe_resume_checkpoint(
                "approve",
                "sess-4",
                reply_metadata={"staffing_action": "manual_approve"},
            )
            tasks = await store.get_tasks(project_id="proj1")
            employee_ids = {employee.employee_id for employee in config.org.employees}
            self.assertIn("ceo-fallback-empty-employee", employee_ids)
            self.assertIn("runtime executed", result)
            self.assertEqual(tasks[0].metadata["employee_assignment"]["employee_id"], "ceo-fallback-empty-employee")
            self.assertEqual(tasks[0].metadata["recruitment_staffing_overrides"], {})
            await store.close()

    def test_fallback_role_uses_empty_employee_instead_of_existing_staff(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.employees = [
                EmployeeConfig(
                    employee_id="engineering-architect",
                    template_id="engineering-architect",
                    name="Existing Architect",
                    role_id="cto",
                    description="Existing technical architecture employee.",
                    category="engineering",
                    prompt_refs=["Architecture persona prompt."],
                )
            ]
            engine = OPCEngine(config=config, opc_home=root, project_id="proj1")
            engine.org_engine = OrgEngine(config, None)
            topology = {
                "seats": [
                    {
                        "seat_id": "seat-cto",
                        "role_id": "cto",
                        "metadata": {},
                    }
                ]
            }

            ordinary = engine._enrich_runtime_delegation_topology(
                runtime_topology=topology,
                decision=self._company_decision(),
                project_id="proj1",
            )
            fallback = engine._enrich_runtime_delegation_topology(
                runtime_topology=topology,
                decision=self._company_decision(),
                project_id="proj1",
                fallback_role_ids={"cto"},
            )
            ordinary_after_fallback = engine._enrich_runtime_delegation_topology(
                runtime_topology=topology,
                decision=self._company_decision(),
                project_id="proj1",
            )

            ordinary_seat = ordinary["seats"][0]
            fallback_seat = fallback["seats"][0]
            self.assertEqual(ordinary_seat["employee_id"], "engineering-architect")
            self.assertEqual(ordinary_after_fallback["seats"][0]["employee_id"], "engineering-architect")
            self.assertEqual(fallback_seat["employee_id"], "cto-fallback-empty-employee")
            self.assertEqual(fallback_seat["employee_assignment"]["prompt_context"], "")
            self.assertEqual(fallback_seat["employee_assignment"]["delta_context"], "")
            self.assertEqual(fallback_seat["metadata"]["employee_prompt_context"], "")
            self.assertTrue(fallback_seat["employee_assignment"]["metadata"]["is_fallback_employee"])
            self.assertEqual(
                fallback_seat["employee_assignment"]["metadata"]["employee_origin"],
                "recruitment_fallback",
            )
            self.assertNotIn(
                "prompt_context_suppressed",
                fallback_seat["employee_assignment"]["metadata"],
            )

    def test_configured_fallback_employee_prompt_refs_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.employees = [
                EmployeeConfig(
                    employee_id="engineering-architect",
                    template_id="engineering-architect",
                    name="Existing Architect",
                    role_id="cto",
                    description="Existing technical architecture employee.",
                    category="engineering",
                    prompt_refs=["Architecture persona prompt."],
                ),
                EmployeeConfig(
                    employee_id="cto-fallback-empty-employee",
                    template_id="fallback-empty-employee",
                    name="CTO Fallback Empty Employee",
                    role_id="cto",
                    description="Configured fallback placeholder.",
                    category="fallback",
                    prompt_refs=["Custom fallback prompt."],
                    metadata={
                        "is_fallback_employee": True,
                        "auto_created_for_role": "cto",
                        "employee_origin": "recruitment_fallback",
                    },
                ),
            ]
            engine = OPCEngine(config=config, opc_home=root, project_id="proj1")
            engine.org_engine = OrgEngine(config, root)
            topology = {
                "seats": [
                    {
                        "seat_id": "seat-cto",
                        "role_id": "cto",
                        "metadata": {},
                    }
                ]
            }

            fallback = engine._enrich_runtime_delegation_topology(
                runtime_topology=topology,
                decision=self._company_decision(),
                project_id="proj1",
                fallback_role_ids={"cto"},
            )

            fallback_seat = fallback["seats"][0]
            self.assertEqual(fallback_seat["employee_id"], "cto-fallback-empty-employee")
            self.assertEqual(
                fallback_seat["employee_assignment"]["prompt_context"],
                "Custom fallback prompt.",
            )
            self.assertEqual(
                fallback_seat["metadata"]["employee_prompt_context"],
                "Custom fallback prompt.",
            )

    async def test_task_mode_prefers_default_executor_employee_over_specialized_executor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.roles = [
                RoleConfig(
                    id="executor",
                    name="Executive Secretariat",
                    responsibility="Concrete task execution",
                    reports_to="owner",
                    tools=[],
                )
            ]
            config.org.employees = [
                EmployeeConfig(
                    employee_id="specialized-agents-orchestrator",
                    template_id="specialized-agents-orchestrator",
                    name="Agents Orchestrator",
                    role_id="executor",
                    description="Autonomous pipeline manager that orchestrates the runtime.",
                    category="specialized",
                    domains=["specialized", "engineering"],
                    prompt_refs=[],
                    preferred_external_agent="codex",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)

            async def _fake_execute_single_agent(tasks, use_external=None):
                _ = (tasks, use_external)
                return "runtime executed"

            engine._execute_single_agent = _fake_execute_single_agent  # type: ignore[method-assign]

            response = await engine._execute_decision(
                self._task_decision(),
                "hello",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-task-default",
            )

            tasks = await store.get_tasks(project_id="proj1")
            employee_ids = {employee.employee_id for employee in config.org.employees}
            self.assertIn("runtime executed", response)
            self.assertEqual(tasks[0].assigned_to, TASK_MODE_GENERAL_ROLE_ID)
            self.assertIsNone(tasks[0].assigned_external_agent)
            self.assertTrue(tasks[0].metadata["force_native_execution"])
            self.assertIn("specialized-agents-orchestrator", employee_ids)
            self.assertNotIn("executor-default-employee", employee_ids)
            self.assertNotIn("employee_assignment", tasks[0].metadata)
            await store.close()

    def test_task_mode_generalist_role_filters_company_only_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            org_engine = OrgEngine(OPCConfig(), root)
            org_engine.configure_task_mode_tools([
                "file_read",
                "web_search",
                "send_dm",
                "request_user_input",
                "browser_navigate",
            ])

            role = org_engine.get_task_mode_role()
            assignment = org_engine.resolve_employee_for_work_item(
                TASK_MODE_GENERAL_ROLE_ID,
                [],
                project_id="proj1",
                prefer_default_employee=True,
            )

            self.assertEqual(role.role_id, TASK_MODE_GENERAL_ROLE_ID)
            self.assertEqual(role.prompt_refs, [])
            self.assertIn("file_read", role.tools)
            self.assertIn("web_search", role.tools)
            self.assertIn("browser_navigate", role.tools)
            self.assertIn("request_user_input", role.tools)
            self.assertNotIn("send_dm", role.tools)
            self.assertIsNone(assignment)

    async def test_recruiter_compares_existing_employee_and_new_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.employees = [
                EmployeeConfig(
                    employee_id="engineering-senior-backend",
                    template_id="engineering-senior-backend",
                    name="Existing Senior Engineer",
                    role_id="senior_engineer",
                    description="A proven backend specialist",
                    category="engineering",
                    domains=["backend", "api"],
                    prompt_refs=[],
                )
            ]
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            engine.org_engine.employee_evolution.save_evolution_profile(
                {
                    "employees": {
                        "engineering-senior-backend": {
                            "successes": 4,
                            "roles": {"senior_engineer": {"successes": 3}},
                            "domains": {"backend": {"successes": 2}, "api": {"successes": 2}},
                            "learned_skill_refs": ["existing-senior-engineer-backend-playbook"],
                        }
                    }
                },
                project_id="proj1",
            )
            self.assertEqual(engine.org_engine.employee_evolution.preferences.load_project("proj1"), {})

            manual_response = await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-5",
            )
            self.assertIn("pending manual staffing selection", manual_response)

            response = await engine._maybe_resume_checkpoint(
                "auto recruit",
                "sess-5",
                reply_metadata={"staffing_action": "auto_recruit"},
            )
            checkpoint = await store.get_latest_pending_checkpoint("proj1", "sess-5")
            assert checkpoint is not None
            category_payload = next(
                call["payload"]
                for call in llm.calls
                if "category_catalog" in call["payload"]
            )
            payload = next(
                call["payload"]
                for call in llm.calls
                if call["payload"].get("roles") and "category_catalog" not in call["payload"]
            )
            senior_payload = next(role for role in payload["roles"] if role["role_id"] == "senior_engineer")
            recruitment_plan = build_recruitment_plan_from_payload(checkpoint.payload["recruitment_plan"])

            self.assertEqual(category_payload["category_catalog"][0]["category"], "engineering")
            self.assertGreater(len(payload["roles"]), 1)
            self.assertEqual(payload["employee_pool"][0]["employee_id"], "engineering-senior-backend")
            self.assertIn("role_responsibility", senior_payload)
            self.assertEqual(senior_payload["existing_employees"][0]["employee_id"], "engineering-senior-backend")
            self.assertEqual(payload["candidate_pool"][0]["template_id"], "engineering-backend-architect")
            self.assertEqual(senior_payload["selected_categories"], ["engineering"])
            self.assertNotIn("new_candidates", senior_payload)
            self.assertNotIn("domains", payload["candidate_pool"][0])
            senior_proposal = next(
                proposal
                for proposal in recruitment_plan.proposals
                if proposal.role_id == "senior_engineer"
            )
            self.assertEqual(senior_proposal.status, "existing_staff")
            self.assertEqual(senior_proposal.existing_employee.employee_id, "engineering-senior-backend")
            self.assertIn("keep existing employee", response)
            await store.close()

    async def test_global_recruiter_accepts_same_template_for_multiple_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.company_profile = "custom"
            config.org.roles = [
                RoleConfig(
                    id="api_owner",
                    name="API Owner",
                    responsibility="Own backend API architecture and implementation choices.",
                ),
                RoleConfig(
                    id="integration_owner",
                    name="Integration Owner",
                    responsibility="Own backend API integration code and implementation details.",
                    reports_to="api_owner",
                ),
            ]
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]

            def duplicate_global_response(payload: dict[str, object]) -> str:
                roles = list(payload.get("roles", []) or [])
                return json.dumps(
                    {
                        "proposals": [
                            {
                                "role_id": role["role_id"],
                                "employee_id": "",
                                "template_id": "engineering-backend-architect",
                                "proposed_employee_name": f"{role['role_id']} Backend Architect",
                                "rationale": "This template fits this role's backend API responsibility.",
                                "status": "proposed_hire",
                            }
                            for role in roles
                        ]
                    },
                    ensure_ascii=False,
                )

            llm = DummyRecruiterLLM(responses=[duplicate_global_response])
            engine, store = await self._build_engine(root, config, llm)
            decision = RouterDecision(
                mode=ExecutionMode.COMPANY_MODE,
                company_profile="custom",
                org_id="test-org",
                domains=[],
            )
            runtime_spec = engine.company_runtime_spec_builder.build_spec(
                decision,
                original_message="Build the backend API",
            )

            plan = await engine.company_recruiter.build_recruitment_plan(
                runtime_spec,
                domains=[],
                project_id="proj1",
            )

            category_calls = [call for call in llm.calls if "category_catalog" in call["payload"]]
            global_payload = next(
                call["payload"]
                for call in llm.calls
                if call["payload"].get("roles") and "category_catalog" not in call["payload"]
            )
            self.assertEqual(len(category_calls), 1)
            self.assertEqual(len(global_payload["roles"]), 2)
            self.assertEqual(global_payload["final_decider_role_id"], "api_owner")
            self.assertEqual(global_payload["org_graph"], {"api_owner": ["integration_owner"]})
            self.assertEqual(
                {role["role_responsibility"] for role in global_payload["roles"]},
                {
                    "Own backend API architecture and implementation choices.",
                    "Own backend API integration code and implementation details.",
                },
            )
            self.assertEqual(
                [proposal.candidate.template_id for proposal in plan.proposals if proposal.candidate],
                ["engineering-backend-architect", "engineering-backend-architect"],
            )
            self.assertTrue(all(proposal.status == "proposed_hire" for proposal in plan.proposals))
            await store.close()

    async def test_recruiter_uses_injected_recruitment_llm_backend(self) -> None:
        class FailingNativeRecruiterLLM(DummyRecruiterLLM):
            async def simple_chat(self, prompt: str, system: str | None = None, task_type: str | None = None) -> str:
                raise AssertionError("native recruiter llm should not be used")

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.company_profile = "custom"
            config.org.roles = [
                RoleConfig(
                    id="finance_owner",
                    name="Finance Owner",
                    responsibility="Analyze investment portfolios, valuation, and financial performance.",
                ),
            ]
            config.org.talent_templates = [
                self._make_template(
                    template_id="finance-investment-researcher",
                    name="Investment Researcher",
                    category="finance",
                    domains=["investment"],
                    description="Investment research and portfolio comparison specialist.",
                )
            ]
            native_llm = FailingNativeRecruiterLLM()
            external_like_llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, native_llm)
            decision = RouterDecision(
                mode=ExecutionMode.COMPANY_MODE,
                company_profile="custom",
                org_id="test-org",
                domains=[],
            )
            runtime_spec = engine.company_runtime_spec_builder.build_spec(
                decision,
                original_message="Compare investment performance",
            )

            plan = await engine.company_recruiter.build_recruitment_plan(
                runtime_spec,
                domains=[],
                project_id="proj1",
                recruitment_llm=external_like_llm,
                recruitment_agent="opencode",
            )

            global_payload = next(
                call["payload"]
                for call in external_like_llm.calls
                if call["payload"].get("roles") and "category_catalog" not in call["payload"]
            )
            self.assertEqual(native_llm.calls, [])
            self.assertEqual(plan.metadata["recruitment_agent"], "opencode")
            self.assertEqual(global_payload["candidate_pool"][0]["template_id"], "finance-investment-researcher")
            self.assertEqual(plan.proposals[0].candidate.template_id, "finance-investment-researcher")
            await store.close()

    async def test_global_triage_uses_request_context_for_generic_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            talent_dir = root / "prompts" / "talent"
            talent_dir.mkdir(parents=True, exist_ok=True)
            (talent_dir / "finance-finance-investment-researcher.md").write_text(
                "---\n"
                "name: Investment Researcher\n"
                "description: Investment research, portfolio comparison, and valuation analysis.\n"
                "---\n"
                "# Investment Researcher\n",
                encoding="utf-8",
            )
            config = OPCConfig()
            config.org.company_profile = "custom"
            config.org.roles = [
                RoleConfig(
                    id="chao",
                    name="Chao",
                    responsibility="leader",
                ),
                RoleConfig(
                    id="zongwei",
                    name="Zongwei",
                    responsibility="student",
                    reports_to="chao",
                ),
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            decision = RouterDecision(
                mode=ExecutionMode.COMPANY_MODE,
                company_profile="custom",
                org_id="test-org",
                domains=[],
            )
            runtime_spec = engine.company_runtime_spec_builder.build_spec(
                decision,
                original_message="Compare investment performance between Cathie Wood and Warren Buffett",
            )

            await engine.company_recruiter.build_recruitment_plan(
                runtime_spec,
                domains=[],
                project_id="proj1",
            )

            category_calls = [call for call in llm.calls if "category_catalog" in call["payload"]]
            global_payload = next(
                call["payload"]
                for call in llm.calls
                if call["payload"].get("roles") and "category_catalog" not in call["payload"]
            )

            self.assertEqual(len(category_calls), 1)
            self.assertEqual(len(category_calls[0]["payload"]["roles"]), 2)
            self.assertEqual(
                {role["role_id"]: role["selected_categories"] for role in global_payload["roles"]},
                {"chao": ["finance"], "zongwei": ["finance"]},
            )
            self.assertEqual(
                [candidate["template_id"] for candidate in global_payload["candidate_pool"]],
                ["finance-finance-investment-researcher"],
            )
            self.assertTrue(all("new_candidates" not in role for role in global_payload["roles"]))
            await store.close()

    async def test_global_recruiter_payload_includes_minimal_org_graph(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.company_profile = "corporate"
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            runtime_spec = engine.company_runtime_spec_builder.build_spec(
                self._company_decision(),
                original_message="Build and validate a small web app",
            )

            await engine.company_recruiter.build_recruitment_plan(
                runtime_spec,
                domains=[],
                project_id="proj1",
            )

            global_call = next(
                call for call in llm.calls
                if call["payload"].get("roles") and "category_catalog" not in call["payload"]
            )
            payload = global_call["payload"]
            self.assertEqual(payload["final_decider_role_id"], "ceo")
            self.assertEqual(
                payload["org_graph"],
                {
                    "ceo": ["cto", "cmo", "coo"],
                    "cto": ["env_engineer", "senior_engineer", "devops_engineer"],
                    "cmo": ["content_specialist", "designer"],
                    "coo": ["acquisition_specialist", "qa_analyst"],
                },
            )
            system_prompt = str(global_call["system"] or "")
            self.assertIn("Use org_graph as the reporting/delegation structure.", system_prompt)
            self.assertNotIn(
                "It is allowed to choose the same existing employee or template for multiple roles",
                system_prompt,
            )
            await store.close()

    async def test_auto_recruiter_uses_all_local_templates_in_selected_categories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            talent_dir = root / "prompts" / "talent"
            talent_dir.mkdir(parents=True, exist_ok=True)
            finance_template_ids = []
            for index in range(9):
                template_id = f"finance-finance-specialist-{index}"
                finance_template_ids.append(template_id)
                (talent_dir / f"{template_id}.md").write_text(
                    "---\n"
                    f"name: Finance Specialist {index}\n"
                    f"description: Investment portfolio and valuation analysis specialist {index}.\n"
                    "---\n"
                    f"# Finance Specialist {index}\n",
                    encoding="utf-8",
                )
            (talent_dir / "marketing-growth-strategist.md").write_text(
                "---\n"
                "name: Growth Strategist\n"
                "description: Audience growth and campaign planning.\n"
                "---\n"
                "# Growth Strategist\n",
                encoding="utf-8",
            )
            config = OPCConfig()
            config.org.company_profile = "custom"
            config.org.roles = [
                RoleConfig(
                    id="finance_owner",
                    name="Finance Owner",
                    responsibility="Analyze investment portfolios, valuation, and financial performance.",
                ),
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            decision = RouterDecision(
                mode=ExecutionMode.COMPANY_MODE,
                company_profile="custom",
                org_id="test-org",
                domains=[],
            )
            runtime_spec = engine.company_runtime_spec_builder.build_spec(
                decision,
                original_message="Compare investment performance",
            )

            plan = await engine.company_recruiter.build_recruitment_plan(
                runtime_spec,
                domains=[],
                project_id="proj1",
            )

            category_payload = next(call["payload"] for call in llm.calls if "category_catalog" in call["payload"])
            global_payload = next(
                call["payload"]
                for call in llm.calls
                if call["payload"].get("roles") and "category_catalog" not in call["payload"]
            )
            role_payload = global_payload["roles"][0]
            candidate_ids = {candidate["template_id"] for candidate in global_payload["candidate_pool"]}
            finance_catalog = next(
                item for item in category_payload["category_catalog"]
                if item["category"] == "finance"
            )

            self.assertEqual(role_payload["selected_categories"], ["finance"])
            self.assertGreaterEqual(finance_catalog["template_count"], 9)
            self.assertTrue(set(finance_template_ids).issubset(candidate_ids))
            self.assertNotIn("marketing-growth-strategist", candidate_ids)
            self.assertEqual(config.org.talent_templates, [])
            self.assertIn(plan.proposals[0].candidate.template_id, candidate_ids)
            await store.close()

    async def test_approve_existing_employee_choice_is_used_as_staffing_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.employees = [
                EmployeeConfig(
                    employee_id="engineering-senior-backend",
                    template_id="engineering-senior-backend",
                    name="Existing Senior Engineer",
                    role_id="senior_engineer",
                    description="A proven backend specialist",
                    category="engineering",
                    domains=["backend", "api"],
                    prompt_refs=[],
                )
            ]
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            engine.org_engine.employee_evolution.save_evolution_profile(
                {
                    "employees": {
                        "engineering-senior-backend": {
                            "successes": 5,
                            "roles": {"senior_engineer": {"successes": 4}},
                            "domains": {"backend": {"successes": 3}, "api": {"successes": 2}},
                            "learned_skill_refs": ["existing-senior-engineer-backend-playbook"],
                        }
                    }
                },
                project_id="proj1",
            )
            self.assertEqual(engine.org_engine.employee_evolution.preferences.load_project("proj1"), {})
            await engine._execute_decision(
                self._company_decision(),
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-6",
            )

            result = await engine._maybe_resume_checkpoint(
                "approve",
                "sess-6",
                reply_metadata={
                    "staffing_action": "manual_approve",
                    "staffing_selections": {
                        "senior_engineer": {
                            "kind": "employee",
                            "id": "engineering-senior-backend",
                        }
                    },
                },
            )

            tasks = await store.get_tasks(project_id="proj1")
            employee_ids = {employee.employee_id for employee in config.org.employees}
            self.assertIn("engineering-senior-backend", employee_ids)
            self.assertEqual(tasks[0].metadata["employee_assignment"]["employee_id"], "ceo-fallback-empty-employee")
            self.assertEqual(
                tasks[0].metadata["recruitment_staffing_overrides"]["senior_engineer"],
                "engineering-senior-backend",
            )
            self.assertIn("runtime executed", result)
            await store.close()

    async def test_approve_recruitment_handles_string_subtasks_from_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                self._make_template(
                    template_id="engineering-backend-architect",
                    name="Backend Architect",
                    category="engineering",
                    domains=["backend", "api"],
                    description="Backend architecture and API implementation specialist.",
                )
            ]
            llm = DummyRecruiterLLM()
            engine, store = await self._build_engine(root, config, llm)
            decision = self._company_decision()
            decision.sub_tasks = [
                "Plan the implementation",
                "Build the backend API",
                "Validate the deliverable",
            ]

            await engine._execute_decision(
                decision,
                "Build the backend API",
                context=SimpleNamespace(default_channel="cli", origin_chat_id="", origin_thread_id=""),
                session_id="sess-7",
            )

            result = await engine._maybe_resume_checkpoint("approve", "sess-7")

            tasks = await store.get_tasks(project_id="proj1")
            self.assertIn("runtime executed", result)
            self.assertGreater(len(tasks), 0)
            self.assertIn("## Global Intent Summary", tasks[0].description)
            self.assertIn("## Requested Subtasks", tasks[0].description)
            self.assertIn("Build the backend API", tasks[0].description)
            await store.close()

class EmployeeRegistryStorageTests(unittest.TestCase):
    def test_config_save_moves_real_employees_to_registry_and_migrates_evolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_dir = root / "config"
            config = OPCConfig()
            config.org.employees = [
                EmployeeConfig(
                    employee_id="analyst-finance-investment-analyst",
                    template_id="finance-investment-analyst",
                    name="Investment Analyst",
                    role_id="analyst",
                    description="Investment research specialist.",
                    category="finance",
                ),
                EmployeeConfig(
                    employee_id="analyst-fallback-empty-employee",
                    template_id="fallback-empty-employee",
                    name="Analyst Fallback Empty Employee",
                    role_id="analyst",
                    metadata={
                        "is_fallback_employee": True,
                        "auto_created_for_role": "analyst",
                    },
                ),
            ]
            evolution_path = root / "evolution" / "employees.json"
            evolution_path.parent.mkdir(parents=True)
            evolution_path.write_text(
                json.dumps(
                    {
                        "employees": {
                            "analyst-finance-investment-analyst": {
                                "successes": 2,
                                "learned_skill_refs": ["investment-playbook"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            config.save(config_dir)

            registry_path = root / "company_state" / "corporate" / "employees" / "finance-investment-analyst.yaml"
            self.assertTrue(registry_path.exists())
            registry_payload = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
            employee_payload = registry_payload["employee"]
            self.assertEqual(employee_payload["employee_id"], "finance-investment-analyst")
            self.assertEqual(employee_payload["metadata"]["legacy_employee_ids"], ["analyst-finance-investment-analyst"])
            self.assertEqual(employee_payload["metadata"]["staffed_role_ids"], ["analyst"])
            self.assertFalse((root / "company_state" / "corporate" / "employees" / "analyst-fallback-empty-employee.yaml").exists())

            org_payload = yaml.safe_load(
                (root / "config" / "company_orgs" / "org_corporate_config.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(org_payload["employees"], [])
            evolution = json.loads(evolution_path.read_text(encoding="utf-8"))
            self.assertIn("finance-investment-analyst", evolution["employees"])
            self.assertNotIn("analyst-finance-investment-analyst", evolution["employees"])

            loaded = OPCConfig.load(config_dir)
            org_engine = OrgEngine(loaded, root)
            self.assertEqual(org_engine.get_employee("finance-investment-analyst").employee_id, "finance-investment-analyst")
            self.assertEqual(org_engine.get_employee("analyst-finance-investment-analyst").employee_id, "finance-investment-analyst")

    def test_template_only_assignment_skips_employee_delta_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.employees = [
                EmployeeConfig(
                    employee_id="finance-investment-analyst",
                    template_id="finance-investment-analyst",
                    name="Investment Analyst",
                    role_id="analyst",
                    description="Investment research specialist.",
                    category="finance",
                )
            ]
            org_engine = OrgEngine(config, root)
            org_engine.employee_evolution.save_evolution_profile(
                {
                    "employees": {
                        "finance-investment-analyst": {
                            "projects_reflected": 1,
                            "delta_profile": {
                                "working_patterns": ["Compare investment claims with evidence."],
                            },
                        }
                    }
                },
                project_id="proj1",
            )
            employee = org_engine.get_employee("finance-investment-analyst")
            assert employee is not None

            experienced = org_engine._build_employee_assignment(
                employee,
                role_id="analyst",
                domains=[],
                project_id="proj1",
                experience_mode="with_experience",
            )
            template_only = org_engine._build_employee_assignment(
                employee,
                role_id="analyst",
                domains=[],
                project_id="proj1",
                experience_mode="template_only",
            )

            self.assertIn("Compare investment claims", experienced["delta_context"])
            self.assertEqual(experienced["experience_mode"], "with_experience")
            self.assertEqual(template_only["delta_context"], "")
            self.assertEqual(template_only["experience_mode"], "template_only")


class TalentMarketParsingTests(unittest.TestCase):
    def _write_template(self, root: Path, template: TalentTemplateConfig) -> None:
        talent_dir = root / "prompts" / "talent"
        talent_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": template.id,
            "name": template.name,
            "description": template.description,
            "category": template.category,
            "domains": list(template.domains),
            "tags": list(template.tags),
        }
        body = str(template.prompt_ref or f"# {template.name}\n").rstrip()
        (talent_dir / f"{template.id}.md").write_text(
            "---\n"
            + yaml.dump(payload, default_flow_style=False, sort_keys=False, allow_unicode=True)
            + "---\n\n"
            + body
            + "\n",
            encoding="utf-8",
        )

    def test_scan_local_talent_infers_root_category_from_filename_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            talent_dir = root / "prompts" / "talent"
            talent_dir.mkdir(parents=True, exist_ok=True)
            (talent_dir / "product-product-manager.md").write_text(
                "# Product Manager\n\nGeneral product leadership.\n",
                encoding="utf-8",
            )

            market = TalentMarket(root, OPCConfig())
            templates = {template.id: template for template in market.scan_local_talent()}

            self.assertIn("product-product-manager", templates)
            self.assertEqual(templates["product-product-manager"].category, "product")

    def test_scan_local_talent_prefers_longest_category_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            talent_dir = root / "prompts" / "talent"
            talent_dir.mkdir(parents=True, exist_ok=True)
            (talent_dir / "project-management-project-shepherd.md").write_text(
                "# Project Shepherd\n\nCoordinates project delivery.\n",
                encoding="utf-8",
            )

            market = TalentMarket(root, OPCConfig())
            templates = {template.id: template for template in market.scan_local_talent()}

            self.assertIn("project-management-project-shepherd", templates)
            self.assertEqual(
                templates["project-management-project-shepherd"].category,
                "project-management",
            )

    def test_scan_local_talent_recognizes_finance_category_from_flat_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            talent_dir = root / "prompts" / "talent"
            talent_dir.mkdir(parents=True, exist_ok=True)
            (talent_dir / "finance-finance-investment-researcher.md").write_text(
                "---\n"
                "name: Investment Researcher\n"
                "description: Investment analysis and valuation.\n"
                "---\n"
                "# Investment Researcher\n",
                encoding="utf-8",
            )

            market = TalentMarket(root, OPCConfig())
            templates = {template.id: template for template in market.scan_local_talent()}

            self.assertIn("finance-finance-investment-researcher", templates)
            self.assertNotIn("general-finance-finance-investment-researcher", templates)
            self.assertEqual(
                templates["finance-finance-investment-researcher"].category,
                "finance",
            )
            self.assertIn("investment", market.describe_category("finance").lower())

    def test_scan_local_talent_ignores_hidden_and_readme_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            talent_dir = root / "prompts" / "talent"
            talent_dir.mkdir(parents=True, exist_ok=True)
            (talent_dir / ".github-pull_request_template.md").write_text(
                "## What does this PR do?\n",
                encoding="utf-8",
            )
            (talent_dir / "integrations-readme.md").write_text(
                "# Integrations\n\nSupport docs.\n",
                encoding="utf-8",
            )
            (talent_dir / "product-product-manager.md").write_text(
                "# Product Manager\n\nGeneral product leadership.\n",
                encoding="utf-8",
            )

            market = TalentMarket(root, OPCConfig())
            template_ids = {template.id for template in market.scan_local_talent()}

            self.assertNotIn("github-pull_request_template", template_ids)
            self.assertNotIn("integrations-readme", template_ids)
            self.assertIn("product-product-manager", template_ids)

    def test_ensure_hire_template_replaces_fallback_employee(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                TalentTemplateConfig(
                    id="engineering-game-developer",
                    name="Game Developer",
                    description="Builds 3D games.",
                    category="engineering",
                    prompt_ref="Game developer prompt.",
                )
            ]
            self._write_template(root, config.org.talent_templates[0])
            config.org.employees = [
                EmployeeConfig(
                    employee_id="cto-fallback-empty-employee",
                    template_id="fallback-empty-employee",
                    name="CTO Fallback Empty Employee",
                    role_id="cto",
                    metadata={
                        "is_fallback_employee": True,
                        "auto_created_for_role": "cto",
                        "employee_origin": "recruitment_fallback",
                    },
                )
            ]
            market = TalentMarket(root, config)

            employee = market.ensure_hire_template("engineering-game-developer", "cto")

            cto_employees = [item for item in config.org.employees if item.role_id == "cto"]
            self.assertEqual(len(cto_employees), 1)
            self.assertEqual(employee.template_id, "engineering-game-developer")
            self.assertEqual(cto_employees[0].employee_id, employee.employee_id)
            self.assertFalse(cto_employees[0].metadata.get("is_fallback_employee"))
            self.assertEqual(cto_employees[0].prompt_refs, ["prompts/talent/engineering-game-developer.md"])

    def test_ensure_hire_template_replaces_stale_role_employee(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = OPCConfig()
            config.org.talent_templates = [
                TalentTemplateConfig(
                    id="engineering-frontend-developer",
                    name="Frontend Developer",
                    description="Builds browser game interfaces.",
                    category="engineering",
                    prompt_ref="Frontend developer prompt.",
                )
            ]
            self._write_template(root, config.org.talent_templates[0])
            config.org.employees = [
                EmployeeConfig(
                    employee_id="marketing-bilibili-strategist",
                    template_id="marketing-bilibili-strategist",
                    name="Bilibili Content Strategist",
                    role_id="senior_engineer",
                    category="marketing",
                ),
                EmployeeConfig(
                    employee_id="senior_engineer-fallback-empty-employee",
                    template_id="fallback-empty-employee",
                    name="Senior Engineer Fallback Empty Employee",
                    role_id="senior_engineer",
                    metadata={
                        "is_fallback_employee": True,
                        "auto_created_for_role": "senior_engineer",
                    },
                ),
            ]
            market = TalentMarket(root, config)

            employee = market.ensure_hire_template(
                "engineering-frontend-developer",
                "senior_engineer",
            )

            senior_engineers = [item for item in config.org.employees if item.role_id == "senior_engineer"]
            self.assertEqual(len(senior_engineers), 2)
            self.assertEqual(employee.employee_id, "engineering-frontend-developer")
            self.assertIn("marketing-bilibili-strategist", {item.employee_id for item in senior_engineers})
            self.assertIn("engineering-frontend-developer", {item.employee_id for item in senior_engineers})
            self.assertNotIn("senior_engineer-fallback-empty-employee", {item.employee_id for item in senior_engineers})
            self.assertEqual(employee.template_id, "engineering-frontend-developer")


if __name__ == "__main__":
    unittest.main()
