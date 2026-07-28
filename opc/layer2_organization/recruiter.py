"""LLM-assisted recruiter for pre-execution staffing decisions."""

from __future__ import annotations

import json
import re
from typing import Any

from loguru import logger

from opc.core.config import TalentTemplateConfig
from opc.core.models import (
    RecruitmentCandidateRecommendation,
    RecruitmentEmployeeRecommendation,
    RecruitmentNeed,
    RecruitmentPlan,
    RecruitmentProposal,
)

RECRUITER_PROMPT = """\
You are the staffing recruiter for a company before it starts execution.

Your job is to choose the best staffing option for a single company role. You must compare:
- existing employees already hired for the role
- new imported talent templates that could be hired now

Return strict JSON:
{
  "status": "existing_staff" | "proposed_hire" | "fallback_role_only",
  "employee_id": "existing employee id or empty string",
  "template_id": "candidate template id or empty string",
  "proposed_employee_name": "short employee display name or empty string",
  "rationale": "brief reason",
}

Rules:
- If a strong existing employee already fits, prefer `existing_staff`.
- If a new candidate is clearly better than existing staff or no existing staff exists, choose `proposed_hire`.
- If none of the provided options are credible, return status `fallback_role_only`.
- Respect user recruiter feedback if present.
- Pick a single durable hire per role.
- Judge new candidates mainly from their category context, name, and description.
- Return JSON only.
"""

GLOBAL_RECRUITER_PROMPT = """\
You are the staffing recruiter for a company before it starts execution.

Your job is to produce one global staffing plan for all roles at once. Compare:
- each role's responsibility and the user's request
- the shared top-level employee_pool of existing company employees with experience
- the shared top-level candidate_pool of imported talent templates
- org_graph, the reporting/delegation structure between roles
- recruiter feedback from earlier revisions

Return strict JSON:
{
  "proposals": [
    {
      "role_id": "role id from the payload",
      "status": "existing_staff" | "proposed_hire" | "fallback_role_only" | "direct_role_execution",
      "employee_id": "existing employee id or empty string",
      "template_id": "candidate template id or empty string",
      "proposed_employee_name": "short employee display name or empty string",
      "rationale": "brief reason"
    }
  ]
}

Rules:
- Return exactly one proposal for every role in the payload and no extra roles.
- Consider both the user's request and every role's role_responsibility.
- Use org_graph as the reporting/delegation structure. Managers may coordinate or cover adjacent work; leaf roles usually represent dedicated execution/review specialties. Reuse the same employee/template across roles only when this structure and role responsibilities make shared coverage sensible; explain why.
- If you repeat the same employee or template across roles, explain why it still fits each role in that role's rationale.
- selected_categories are role-level hints from triage; employee_pool and candidate_pool are shared across all roles.
- Only choose employee_id values from the top-level employee_pool and template_id values from the top-level candidate_pool.
- Choosing employee_id means use the existing experienced employee. Choosing template_id means use the template-only version for this run.
- Use `direct_role_execution` when ordinary role execution is enough and no staffing decision is useful.
- Use `fallback_role_only` when the visible candidates and staff are not credible for the role.
- Respect recruiter feedback if present.
- Return JSON only.
"""

STAFFING_TRIAGE_PROMPT = """\
You are the staffing recruiter for a company before it starts execution.

First decide which roles need deliberate staffing and which talent categories should be considered.

Return strict JSON:
{
  "roles": [
    {
      "role_id": "role id from the payload",
      "action": "direct_role_execution" | "category_screening",
      "categories": ["category-1", "category-2"],
      "rationale": "brief reason"
    }
  ]
}

Rules:
- Return exactly one entry for every role in the payload and no extra roles.
- Use the user's request as the primary signal, then map useful categories to roles with role responsibilities and org_graph.
- Generic or short role responsibilities do not by themselves mean staffing is unnecessary.
- Choose `direct_role_execution` only when no staffing comparison is useful for that role.
- Choose `category_screening` when the role could benefit from a specialized employee/template for this request.
- If action is `direct_role_execution`, return an empty categories list.
- If action is `category_screening`, select 1 to 3 categories only.
- Only choose categories from the provided category catalog.
- Use org_graph as the reporting/delegation structure.
- Respect recruiter feedback if present.
- Return JSON only.
"""

RECRUITMENT_AGENT_CHOICES = frozenset({
    "native",
    "codex",
    "claude_code",
    "cursor",
    "opencode",
})
DEFAULT_RECRUITMENT_EXECUTION_AGENT = "codex"
VALID_RECRUITMENT_STATUSES = frozenset({
    "existing_staff",
    "proposed_hire",
    "fallback_role_only",
    "direct_role_execution",
})


def normalize_recruitment_agent_choice(value: Any, default: str | None = None) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized in RECRUITMENT_AGENT_CHOICES:
        return normalized
    fallback = str(default or "").strip().lower().replace("-", "_")
    if fallback in RECRUITMENT_AGENT_CHOICES:
        return fallback
    return None


def resolve_effective_execution_agent(
    selected_agent: Any,
    preferred_external_agent: Any = None,
    *,
    force_native_execution: bool = False,
) -> tuple[str, str | None, bool]:
    """Resolve UI/recruitment selection into display, assigned external, native flag."""
    preferred = normalize_recruitment_agent_choice(preferred_external_agent)
    if preferred == "native":
        preferred = None
    selected = normalize_recruitment_agent_choice(
        selected_agent,
        default=("native" if not preferred else preferred),
    )
    resolved_force_native = bool(force_native_execution or selected == "native")
    if resolved_force_native:
        return "native", None, True
    assigned_external = selected if selected and selected != "native" else preferred
    return selected or assigned_external or "native", assigned_external, False


def ensure_recruitment_plan_default_agents(
    plan: RecruitmentPlan,
    *,
    default_agent: str = DEFAULT_RECRUITMENT_EXECUTION_AGENT,
) -> RecruitmentPlan:
    normalized_default = (
        normalize_recruitment_agent_choice(
            default_agent,
            default=DEFAULT_RECRUITMENT_EXECUTION_AGENT,
        )
        or DEFAULT_RECRUITMENT_EXECUTION_AGENT
    )
    for proposal in plan.proposals:
        metadata = dict(proposal.metadata or {})
        metadata["selected_execution_agent"] = (
            normalize_recruitment_agent_choice(metadata.get("selected_execution_agent"))
            or normalized_default
        )
        proposal.metadata = metadata
    return plan


def apply_recruitment_role_agent_overrides(
    plan: RecruitmentPlan,
    role_agent_overrides: dict[str, Any] | None,
) -> None:
    if not role_agent_overrides:
        return
    normalized_overrides: dict[str, str] = {}
    for raw_role_id, raw_agent in dict(role_agent_overrides).items():
        role_id = str(raw_role_id or "").strip()
        agent = normalize_recruitment_agent_choice(raw_agent)
        if role_id and agent:
            normalized_overrides[role_id] = agent
    if not normalized_overrides:
        return
    for proposal in plan.proposals:
        role_id = str(proposal.role_id or "").strip()
        selected_agent = normalized_overrides.get(role_id)
        if not selected_agent:
            continue
        proposal.metadata = dict(proposal.metadata)
        proposal.metadata["selected_execution_agent"] = selected_agent


def extract_recruitment_role_agent_overrides(plan: RecruitmentPlan) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for proposal in plan.proposals:
        role_id = str(proposal.role_id or "").strip()
        if not role_id:
            continue
        selected_agent = normalize_recruitment_agent_choice(
            dict(proposal.metadata or {}).get("selected_execution_agent")
        )
        if selected_agent:
            overrides[role_id] = selected_agent
    return overrides


def build_staffing_overrides(plan: RecruitmentPlan) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for proposal in plan.proposals:
        if proposal.status == "existing_staff" and proposal.existing_employee:
            overrides[proposal.role_id] = proposal.existing_employee.employee_id
        elif proposal.status == "proposed_hire" and proposal.candidate and proposal.candidate.proposed_employee_id:
            overrides[proposal.role_id] = proposal.candidate.proposed_employee_id
    return overrides


def build_staffing_experience_modes(plan: RecruitmentPlan) -> dict[str, str]:
    modes: dict[str, str] = {}
    for proposal in plan.proposals:
        role_id = str(proposal.role_id or "").strip()
        if not role_id:
            continue
        if proposal.status == "existing_staff" and proposal.existing_employee:
            modes[role_id] = "with_experience"
        elif proposal.status == "proposed_hire" and proposal.candidate:
            modes[role_id] = "template_only"
    return modes


def build_fallback_role_ids(plan: RecruitmentPlan) -> set[str]:
    return {
        str(proposal.role_id).strip()
        for proposal in plan.proposals
        if proposal.status == "fallback_role_only" and str(proposal.role_id).strip()
    }


def recruitment_plan_requires_confirmation(plan: RecruitmentPlan) -> bool:
    """Prompt for staffing confirmation only when the plan actually changes staffing.

    A team that is already fully staffed (every role has an assigned employee
    that the plan simply reuses) needs no confirmation. Confirmation is required
    only when there is a genuine staffing decision:
    - a new hire is proposed (``proposed_hire``), or
    - an existing pool employee is assigned to a role that currently has no
      employee of its own (``existing_staff`` with no same-role employees), i.e.
      a not-fully-staffed or new team.
    """
    for proposal in plan.proposals:
        if proposal.status == "proposed_hire":
            return True
        if proposal.status == "existing_staff" and not list(proposal.existing_employee_ids or []):
            return True
    return False


def serialize_recruitment_plan(plan: RecruitmentPlan) -> dict[str, Any]:
    return {
        "company_profile": plan.company_profile,
        "proposals": [
            {
                "role_id": proposal.role_id,
                "status": proposal.status,
                "rationale": proposal.rationale,
                "role_labels": list(proposal.role_labels),
                "candidate": (
                    {
                        "template_id": proposal.candidate.template_id,
                        "template_name": proposal.candidate.template_name,
                        "category": proposal.candidate.category,
                        "domains": list(proposal.candidate.domains),
                        "prompt_ref": proposal.candidate.prompt_ref,
                        "preferred_external_agent": proposal.candidate.preferred_external_agent,
                        "source_path": proposal.candidate.source_path,
                        "rationale": proposal.candidate.rationale,
                        "proposed_employee_name": proposal.candidate.proposed_employee_name,
                        "proposed_employee_id": proposal.candidate.proposed_employee_id,
                        "metadata": dict(proposal.candidate.metadata),
                    }
                    if proposal.candidate
                    else None
                ),
                "existing_employee": (
                    {
                        "employee_id": proposal.existing_employee.employee_id,
                        "employee_name": proposal.existing_employee.employee_name,
                        "role_id": proposal.existing_employee.role_id,
                        "category": proposal.existing_employee.category,
                        "domains": list(proposal.existing_employee.domains),
                        "learned_skill_refs": list(proposal.existing_employee.learned_skill_refs),
                        "experience_score": proposal.existing_employee.experience_score,
                        "rationale": proposal.existing_employee.rationale,
                        "metadata": dict(proposal.existing_employee.metadata),
                    }
                    if proposal.existing_employee
                    else None
                ),
                "existing_employee_ids": list(proposal.existing_employee_ids),
                "metadata": dict(proposal.metadata),
            }
            for proposal in plan.proposals
        ],
        "recruiter_feedback": list(plan.recruiter_feedback),
        "summary": plan.summary,
        "metadata": dict(plan.metadata),
    }


def deserialize_recruitment_plan(data: dict[str, Any]) -> RecruitmentPlan:
    proposals: list[RecruitmentProposal] = []
    for item in data.get("proposals", []):
        candidate_data = item.get("candidate")
        existing_employee_data = item.get("existing_employee")
        candidate = None
        existing_employee = None
        if candidate_data:
            candidate = RecruitmentCandidateRecommendation(
                template_id=str(candidate_data.get("template_id", "")),
                template_name=str(candidate_data.get("template_name", "")),
                category=str(candidate_data.get("category", "")),
                domains=list(candidate_data.get("domains", [])),
                prompt_ref=str(candidate_data.get("prompt_ref", "")),
                preferred_external_agent=candidate_data.get("preferred_external_agent"),
                source_path=str(candidate_data.get("source_path", "")),
                rationale=str(candidate_data.get("rationale", "")),
                proposed_employee_name=str(candidate_data.get("proposed_employee_name", "")),
                proposed_employee_id=str(candidate_data.get("proposed_employee_id", "")),
                metadata=dict(candidate_data.get("metadata", {})),
            )
        if existing_employee_data:
            existing_employee = RecruitmentEmployeeRecommendation(
                employee_id=str(existing_employee_data.get("employee_id", "")),
                employee_name=str(existing_employee_data.get("employee_name", "")),
                role_id=str(existing_employee_data.get("role_id", "")),
                category=str(existing_employee_data.get("category", "")),
                domains=list(existing_employee_data.get("domains", [])),
                learned_skill_refs=list(existing_employee_data.get("learned_skill_refs", [])),
                experience_score=float(existing_employee_data.get("experience_score", 0.0) or 0.0),
                rationale=str(existing_employee_data.get("rationale", "")),
                metadata=dict(existing_employee_data.get("metadata", {})),
            )
        proposals.append(
            RecruitmentProposal(
                role_id=str(item.get("role_id", "")),
                status=str(item.get("status", "fallback_role_only")),
                rationale=str(item.get("rationale", "")),
                role_labels=list(item.get("role_labels", [])),
                candidate=candidate,
                existing_employee=existing_employee,
                existing_employee_ids=list(item.get("existing_employee_ids", [])),
                metadata=dict(item.get("metadata", {})),
            )
        )
    return ensure_recruitment_plan_default_agents(RecruitmentPlan(
        company_profile=str(data.get("company_profile", "corporate")),
        proposals=proposals,
        recruiter_feedback=list(data.get("recruiter_feedback", [])),
        summary=str(data.get("summary", "")),
        metadata=dict(data.get("metadata", {})),
    ))


class CompanyRecruiter:
    """Generate runtime recruitment proposals before company execution."""

    def __init__(self, llm: Any, org_engine: Any, talent_market: Any) -> None:
        self.llm = llm
        self.org_engine = org_engine
        self.talent_market = talent_market

    def _build_organization_payload(self) -> dict[str, Any]:
        agents = list(self.org_engine.list_agents()) if self.org_engine else []
        role_ids = {
            str(getattr(agent, "role_id", "") or "").strip()
            for agent in agents
            if str(getattr(agent, "role_id", "") or "").strip()
        }
        direct_reports: dict[str, list[str]] = {}
        for agent in agents:
            role_id = str(getattr(agent, "role_id", "") or "").strip()
            manager_id = str(getattr(agent, "reports_to", "") or "").strip()
            if not role_id or manager_id == "owner" or manager_id not in role_ids:
                continue
            direct_reports.setdefault(manager_id, [])
            if role_id not in direct_reports[manager_id]:
                direct_reports[manager_id].append(role_id)

        org_graph: dict[str, list[str]] = {}
        for manager in agents:
            manager_id = str(getattr(manager, "role_id", "") or "").strip()
            children = list(direct_reports.get(manager_id) or [])
            if not manager_id or not children:
                continue
            spawn_order = [
                str(role_id or "").strip()
                for role_id in list(getattr(manager, "can_spawn", []) or [])
                if str(role_id or "").strip() in children
            ]
            standing_reports = [role_id for role_id in children if role_id not in spawn_order]
            org_graph[manager_id] = [*standing_reports, *spawn_order]

        final_decider_role_id = ""
        getter = getattr(self.org_engine, "get_final_decider_role_id", None)
        if callable(getter):
            try:
                final_decider_role_id = str(getter() or "").strip()
            except Exception:
                logger.opt(exception=True).debug("failed to resolve final decider role for recruiter payload")

        return {
            "final_decider_role_id": final_decider_role_id,
            "org_graph": org_graph,
        }

    async def build_recruitment_plan(
        self,
        runtime_spec: Any,
        *,
        domains: list[str],
        project_id: str,
        recruiter_feedback: list[str] | None = None,
        recruitment_llm: Any | None = None,
        recruitment_agent: str | None = None,
    ) -> RecruitmentPlan:
        domains = list(domains or [])
        feedback = list(recruiter_feedback or [])
        active_llm = recruitment_llm if recruitment_llm is not None else self.llm
        selected_recruitment_agent = normalize_recruitment_agent_choice(
            recruitment_agent,
            default="native",
        ) or "native"
        needs = self._collect_needs(runtime_spec)
        # Fast path for a fully-staffed team: every role already has an active,
        # non-placeholder same-role employee, so there is no staffing decision
        # to make. Skip the (LLM) triage + global recruit entirely and reuse
        # existing staff. This avoids slow recruiter calls and hiring prompts on
        # every run when nothing about staffing needs to change.
        fully_staffed_proposals = self._fully_staffed_proposals(needs, project_id=project_id)
        if fully_staffed_proposals is not None:
            plan_metadata = {
                "project_id": project_id,
                "execution_mode": str(getattr(runtime_spec, "metadata", {}).get("execution_mode", "company_mode") or "company_mode"),
                "request_label": str(getattr(runtime_spec, "metadata", {}).get("request_label", "runtime") or "runtime"),
                "recruitment_agent": selected_recruitment_agent,
                "fully_staffed_fast_path": True,
            }
            plan = ensure_recruitment_plan_default_agents(
                RecruitmentPlan(
                    company_profile=str(getattr(runtime_spec, "profile", "corporate") or "corporate"),
                    proposals=fully_staffed_proposals,
                    recruiter_feedback=feedback,
                    metadata=plan_metadata,
                )
            )
            plan.summary = self.render_recruitment_summary(plan)
            return plan
        triage_by_role = await self._triage_staffing_for_needs(
            needs,
            recruiter_feedback=feedback,
            llm=active_llm,
        )
        prepared_needs: list[dict[str, Any]] = []
        selected_category_union: list[str] = []
        for need in needs:
            existing = self.org_engine.list_employees(role_id=need.role_id)
            triage_action, selected_categories, category_rationale = triage_by_role.get(
                need.role_id,
                ("direct_role_execution", [], "No staffing triage was produced for this role."),
            )
            for category in selected_categories:
                if category not in selected_category_union:
                    selected_category_union.append(category)
            prepared_needs.append(
                {
                    "need": need,
                    "existing_employees": list(existing),
                    "candidates": [],
                    "triage_action": triage_action,
                    "selected_categories": list(selected_categories),
                    "category_rationale": category_rationale,
                }
            )
        candidate_pool = self._recall_candidates_for_need(
            selected_categories=selected_category_union,
        )
        employee_pool = self._recall_existing_employee_pool(
            prepared_needs,
            candidate_pool=candidate_pool,
            selected_categories=selected_category_union,
            project_id=project_id,
        )
        for item in prepared_needs:
            item["candidates"] = candidate_pool
            item["employee_pool"] = employee_pool
        if active_llm:
            proposals = await self._recruit_globally(
                prepared_needs,
                recruiter_feedback=feedback,
                project_id=project_id,
                llm=active_llm,
                candidate_pool=candidate_pool,
                employee_pool=employee_pool,
            )
        else:
            proposals = [
                self._heuristic_proposal_for_prepared_need(item, project_id=project_id)
                for item in prepared_needs
            ]
        plan_metadata = {
            "project_id": project_id,
            "execution_mode": str(getattr(runtime_spec, "metadata", {}).get("execution_mode", "company_mode") or "company_mode"),
            "request_label": str(getattr(runtime_spec, "metadata", {}).get("request_label", "runtime") or "runtime"),
            "recruitment_agent": selected_recruitment_agent,
        }
        plan = ensure_recruitment_plan_default_agents(
            RecruitmentPlan(
                company_profile=str(getattr(runtime_spec, "profile", "corporate") or "corporate"),
                proposals=proposals,
                recruiter_feedback=feedback,
                metadata=plan_metadata,
            )
        )
        summary = self.render_recruitment_summary(plan)
        return ensure_recruitment_plan_default_agents(RecruitmentPlan(
            company_profile=str(getattr(runtime_spec, "profile", "corporate") or "corporate"),
            proposals=proposals,
            recruiter_feedback=feedback,
            summary=summary,
            metadata=plan_metadata,
        ))

    @staticmethod
    def _is_placeholder_employee(employee: Any) -> bool:
        metadata = dict(getattr(employee, "metadata", {}) or {})
        return bool(metadata.get("is_default_employee") or metadata.get("is_fallback_employee"))

    def _fully_staffed_proposals(
        self,
        needs: list[RecruitmentNeed],
        *,
        project_id: str,
    ) -> list[RecruitmentProposal] | None:
        """Return reuse-existing-staff proposals when every role is already staffed.

        Returns ``None`` (no fast path) if any role lacks an active,
        non-placeholder same-role employee, so not-fully-staffed and new teams
        still go through normal recruitment.
        """
        if not needs:
            return None
        proposals: list[RecruitmentProposal] = []
        for need in needs:
            same_role = [
                employee
                for employee in self.org_engine.list_employees(role_id=need.role_id)
                if not self._is_placeholder_employee(employee)
            ]
            if not same_role:
                return None
            summaries = [
                (
                    employee,
                    self._build_existing_employee_summary(
                        employee,
                        role_id=need.role_id,
                        domains=[],
                        project_id=project_id,
                    ),
                )
                for employee in same_role
            ]
            employee, _ = max(
                summaries,
                key=lambda pair: float(pair[1].get("experience_score", 0.0) or 0.0),
            )
            recommendation = self._make_existing_employee_recommendation(
                employee,
                role_id=need.role_id,
                domains=[],
                project_id=project_id,
                rationale="Reusing the employee already staffed for this role; no staffing change is needed.",
            )
            proposals.append(
                RecruitmentProposal(
                    role_id=need.role_id,
                    status="existing_staff",
                    rationale=recommendation.rationale,
                    role_labels=[need.role_name] if need.role_name else [],
                    existing_employee=recommendation,
                    existing_employee_ids=[item.employee_id for item in same_role],
                    metadata={"selection_source": "fully_staffed_fast_path"},
                )
            )
        return proposals

    def render_recruitment_summary(self, plan: RecruitmentPlan) -> str:
        execution_mode = str(plan.metadata.get("execution_mode", "company_mode") or "company_mode")
        if execution_mode == "task_mode":
            intro = "Task mode has a pending staffing decision before execution."
            cancel_line = "Reply `deny` / `stop` to cancel task-mode execution."
        else:
            intro = "Company mode has a pending staffing decision before execution."
            cancel_line = "Reply `deny` / `stop` to cancel company-mode execution."
        lines = [
            intro,
            "",
            f"Company profile: `{plan.company_profile}`",
        ]
        if plan.recruiter_feedback:
            lines.extend(
                [
                    "",
                    "Recruiter feedback so far:",
                    *[f"- {item}" for item in plan.recruiter_feedback[-5:]],
                ]
            )
        if not plan.proposals:
            lines.extend(
                [
                    "",
                    "No staffing action is needed.",
                    "Reply `approve` to continue execution.",
                ]
            )
            return "\n".join(lines)

        lines.append("")
        lines.append("Recruitment decisions:")
        for proposal in plan.proposals:
            role_label = proposal.role_labels[0] if proposal.role_labels else proposal.role_id
            if proposal.status == "direct_role_execution":
                lines.append(f"- Role `{proposal.role_id}` ({role_label}): use ordinary role execution without staffing.")
                lines.append(f"  Reason: {proposal.rationale}")
            elif proposal.status == "existing_staff":
                existing = ", ".join(proposal.existing_employee_ids) or "(existing staff)"
                selected = proposal.existing_employee
                if selected:
                    lines.append(
                        f"- Role `{proposal.role_id}` ({role_label}): keep existing employee "
                        f"`{selected.employee_name}` ({selected.employee_id})."
                    )
                    lines.append(
                        f"  Experience score: {selected.experience_score}; learned skills: {len(selected.learned_skill_refs)}; "
                        f"existing pool: {existing}"
                    )
                else:
                    lines.append(f"- Role `{proposal.role_id}` ({role_label}): reuse existing staff {existing}.")
                lines.append(f"  Reason: {proposal.rationale}")
            elif proposal.status == "proposed_hire" and proposal.candidate:
                lines.append(
                    f"- Role `{proposal.role_id}` ({role_label}): hire `{proposal.candidate.template_name}` "
                    f"as `{proposal.candidate.proposed_employee_name or proposal.candidate.proposed_employee_id}`."
                )
                if proposal.existing_employee_ids:
                    lines.append(
                        f"  Compared against existing staff: {', '.join(proposal.existing_employee_ids)}"
                    )
                lines.append(f"  Reason: {proposal.rationale or proposal.candidate.rationale}")
            else:
                lines.append(f"- Role `{proposal.role_id}` ({role_label}): fallback to role-only execution.")
                lines.append(f"  Reason: {proposal.rationale}")
        lines.extend(
            [
                "",
                "Reply `1` or `approve` / `continue` to accept these hires and start execution.",
                "Reply `2` or `deny` / `stop` / `cancel` to reject this staffing proposal and stop execution.",
                "Any other input will be treated as feedback or a suggestion for revising the staffing proposal.",
                cancel_line,
            ]
        )
        return "\n".join(lines)

    def _collect_needs(self, runtime_spec: Any) -> list[RecruitmentNeed]:
        """One staffing need per role in the live org topology.

        Recruitment runs once before execution enters the org, so it does not
        consume runtime work-item projections. ``runtime_spec.metadata['original_request']``
        is forwarded to the triage LLM as request-specific context.
        """
        metadata = dict(getattr(runtime_spec, "metadata", {}) or {})
        request_text = str(
            getattr(runtime_spec, "original_request", "")
            or metadata.get("original_request", "")
            or ""
        ).strip()
        grouped: dict[str, RecruitmentNeed] = {}
        # Roles in the active topology.
        for agent in self.org_engine.list_agents():
            role_id = str(getattr(agent, "role_id", "") or "").strip()
            if not role_id or role_id == "task_generalist":
                continue
            grouped[role_id] = RecruitmentNeed(
                role_id=role_id,
                role_name=str(getattr(agent, "name", "") or role_id),
                role_responsibility=str(getattr(agent, "responsibility", "") or "").strip(),
                request_text=request_text,
            )
        # Roles outside the topology that already have configured employees
        # (e.g. custom roles registered via OrgConfig but not yet promoted to
        # an active agent). Recruitment should still consider them.
        try:
            extra_employees = self.org_engine.list_employees()
        except TypeError:
            extra_employees = []
        for employee in extra_employees:
            role_id = str(getattr(employee, "role_id", "") or "").strip()
            if not role_id or role_id == "task_generalist" or role_id in grouped:
                continue
            grouped[role_id] = RecruitmentNeed(
                role_id=role_id,
                role_name=role_id,
                role_responsibility="",
                request_text=request_text,
            )
        return list(grouped.values())

    def _recall_candidates_for_need(
        self,
        *,
        selected_categories: list[str],
    ) -> list[TalentTemplateConfig]:
        merged: dict[str, TalentTemplateConfig] = {}
        for candidate in self.talent_market.list_templates_by_categories(categories=selected_categories):
            candidate_id = str(getattr(candidate, "id", "") or "").strip()
            if candidate_id and candidate_id not in merged:
                merged[candidate_id] = candidate
        return list(merged.values())

    def _recall_existing_employee_pool(
        self,
        prepared_needs: list[dict[str, Any]],
        *,
        candidate_pool: list[TalentTemplateConfig],
        selected_categories: list[str],
        project_id: str,
    ) -> list[Any]:
        _ = project_id
        categories = {
            str(category or "").strip().lower()
            for category in list(selected_categories or [])
            if str(category or "").strip()
        }
        candidate_template_ids = {
            str(getattr(candidate, "id", "") or "").strip()
            for candidate in list(candidate_pool or [])
            if str(getattr(candidate, "id", "") or "").strip()
        }
        same_role_ids = {
            str(getattr(employee, "employee_id", "") or "").strip()
            for item in prepared_needs
            for employee in list(item.get("existing_employees") or [])
            if str(getattr(employee, "employee_id", "") or "").strip()
        }
        try:
            employees = self.org_engine.list_employees()
        except Exception:
            employees = []
        selected: dict[str, Any] = {}
        for employee in employees:
            employee_id = str(getattr(employee, "employee_id", "") or "").strip()
            if not employee_id:
                continue
            metadata = dict(getattr(employee, "metadata", {}) or {})
            if metadata.get("is_default_employee") or metadata.get("is_fallback_employee"):
                continue
            category = str(getattr(employee, "category", "") or "").strip().lower()
            template_id = str(getattr(employee, "template_id", "") or "").strip()
            employee_domains = {
                str(domain or "").strip().lower()
                for domain in list(getattr(employee, "domains", []) or [])
                if str(domain or "").strip()
            }
            if (
                employee_id in same_role_ids
                or category in categories
                or template_id in candidate_template_ids
                or bool(employee_domains & categories)
            ):
                selected[employee_id] = employee
        return list(selected.values())

    def _heuristic_proposal_for_prepared_need(
        self,
        item: dict[str, Any],
        *,
        project_id: str,
    ) -> RecruitmentProposal:
        need: RecruitmentNeed = item["need"]
        existing_employees = list(item.get("existing_employees") or [])
        employee_pool = list(item.get("employee_pool") or existing_employees)
        candidates = list(item.get("candidates") or [])
        triage_action = str(item.get("triage_action", "") or "")
        selected_categories = list(item.get("selected_categories") or [])
        category_rationale = str(item.get("category_rationale", "") or "")
        metadata = {
            "triage_action": triage_action,
            "selected_categories": selected_categories,
            "category_rationale": category_rationale,
        }
        role_labels = [need.role_name] if need.role_name else []
        if triage_action == "direct_role_execution":
            return RecruitmentProposal(
                role_id=need.role_id,
                status="direct_role_execution",
                rationale=category_rationale or "Ordinary role execution is sufficient; no staffing comparison is needed.",
                role_labels=role_labels,
                existing_employee_ids=[item.employee_id for item in existing_employees],
                metadata={**metadata, "selection_source": "heuristic_direct"},
            )
        if employee_pool:
            existing_payload = [
                self._build_existing_employee_summary(
                    employee,
                    role_id=need.role_id,
                    domains=[],
                    project_id=project_id,
                )
                for employee in employee_pool
            ]
            selected = max(existing_payload, key=lambda item: float(item.get("experience_score", 0.0)))
            employee = next(item for item in employee_pool if item.employee_id == selected["employee_id"])
            recommendation = self._make_existing_employee_recommendation(
                employee,
                role_id=need.role_id,
                domains=[],
                project_id=project_id,
                rationale="Selected heuristically because an existing experienced employee was available.",
            )
            return RecruitmentProposal(
                role_id=need.role_id,
                status="existing_staff",
                rationale=recommendation.rationale,
                role_labels=role_labels,
                existing_employee=recommendation,
                existing_employee_ids=[item.employee_id for item in existing_employees],
                metadata={**metadata, "selection_source": "heuristic_existing"},
            )
        if candidates:
            candidate = candidates[0]
            recommendation = self._make_candidate_recommendation(
                candidate,
                rationale="Selected heuristically because no recruiter LLM was available.",
                role_id=need.role_id,
            )
            return RecruitmentProposal(
                role_id=need.role_id,
                status="proposed_hire",
                rationale=recommendation.rationale,
                role_labels=role_labels,
                candidate=recommendation,
                metadata={**metadata, "selection_source": "heuristic_hire"},
            )
        return RecruitmentProposal(
            role_id=need.role_id,
            status="fallback_role_only",
            rationale="No credible imported talent template matched this role need. Execution should fall back to the role-only path.",
            role_labels=role_labels,
            metadata={**metadata, "selection_source": "heuristic_fallback", "fallback_reason": "no_candidate_templates"},
        )

    async def _recruit_globally(
        self,
        prepared_needs: list[dict[str, Any]],
        *,
        recruiter_feedback: list[str],
        project_id: str,
        llm: Any,
        candidate_pool: list[TalentTemplateConfig],
        employee_pool: list[Any],
    ) -> list[RecruitmentProposal]:
        if not prepared_needs:
            return []
        role_payloads: list[dict[str, Any]] = []
        prepared_by_role: dict[str, dict[str, Any]] = {}
        role_order: list[str] = []
        candidate_payload = [
            self.talent_market.build_candidate_summary(candidate)
            for candidate in candidate_pool
        ]
        employee_pool_payload = [
            self._build_existing_employee_summary(
                employee,
                role_id=str(getattr(employee, "role_id", "") or ""),
                domains=[],
                project_id=project_id,
            )
            for employee in employee_pool
        ]
        for item in prepared_needs:
            need: RecruitmentNeed = item["need"]
            existing_employees = list(item.get("existing_employees") or [])
            existing_payload = [
                self._build_existing_employee_summary(
                    employee,
                    role_id=need.role_id,
                    domains=[],
                    project_id=project_id,
                )
                for employee in existing_employees
            ]
            prepared_item = {
                **item,
                "existing_payload": existing_payload,
            }
            prepared_by_role[need.role_id] = prepared_item
            role_order.append(need.role_id)
            role_payloads.append(
                {
                    "role_id": need.role_id,
                    "role_name": need.role_name,
                    "role_responsibility": need.role_responsibility,
                    "user_request": need.request_text,
                    "triage_action": str(item.get("triage_action", "") or ""),
                    "selected_categories": list(item.get("selected_categories") or []),
                    "category_rationale": str(item.get("category_rationale", "") or ""),
                    "existing_employees": existing_payload,
                    "current_role_employee_ids": [
                        str(employee.get("employee_id", "") or "").strip()
                        for employee in existing_payload
                        if str(employee.get("employee_id", "") or "").strip()
                    ],
                }
            )
        prompt_payload = {
            "project_id": project_id,
            "recruiter_feedback": list(recruiter_feedback),
            **self._build_organization_payload(),
            "employee_pool": employee_pool_payload,
            "candidate_pool": candidate_payload,
            "roles": role_payloads,
        }
        retry_feedback: list[str] = []
        for attempt in range(1, 4):
            payload = dict(prompt_payload)
            if retry_feedback:
                payload["retry_feedback"] = list(retry_feedback)
            try:
                raw = await llm.simple_chat(
                    prompt=json.dumps(payload, ensure_ascii=False),
                    system=GLOBAL_RECRUITER_PROMPT,
                    task_type="quick_tasks",
                )
            except Exception as exc:
                retry_feedback.append(f"Recruiter LLM call failed: {type(exc).__name__}: {exc}")
                continue
            try:
                data = self._parse_llm_json(raw)
            except json.JSONDecodeError as exc:
                retry_feedback.append(f"Response was not valid JSON: {exc.msg} at char {exc.pos}.")
                continue
            proposals_data = data.get("proposals")
            if not isinstance(proposals_data, list):
                retry_feedback.append("Response must contain a `proposals` array.")
                continue
            try:
                return self._build_global_proposals_from_response(
                    proposals_data,
                    prepared_by_role=prepared_by_role,
                    role_order=role_order,
                    project_id=project_id,
                    attempt=attempt,
                    candidate_pool=candidate_pool,
                    employee_pool=employee_pool,
                )
            except ValueError as exc:
                retry_feedback.append(str(exc))
        logger.warning("Global recruiter exhausted retries; falling back to heuristic staffing decisions")
        return [
            self._heuristic_proposal_for_prepared_need(item, project_id=project_id)
            for item in prepared_needs
        ]

    @staticmethod
    def _parse_llm_json(raw: str) -> dict[str, Any]:
        text = str(raw or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
        data = json.loads(text)
        if not isinstance(data, dict):
            raise json.JSONDecodeError("response must be a JSON object", text, 0)
        return data

    def _build_global_proposals_from_response(
        self,
        proposals_data: list[Any],
        *,
        prepared_by_role: dict[str, dict[str, Any]],
        role_order: list[str],
        project_id: str,
        attempt: int,
        candidate_pool: list[TalentTemplateConfig],
        employee_pool: list[Any],
    ) -> list[RecruitmentProposal]:
        expected_roles = set(role_order)
        proposal_by_role: dict[str, RecruitmentProposal] = {}
        candidate_by_id = {
            str(getattr(candidate, "id", "") or "").strip(): candidate
            for candidate in candidate_pool
            if str(getattr(candidate, "id", "") or "").strip()
        }
        global_employee_by_id = {
            str(getattr(employee, "employee_id", "") or "").strip(): employee
            for employee in employee_pool
            if str(getattr(employee, "employee_id", "") or "").strip()
        }
        for index, raw_item in enumerate(proposals_data):
            if not isinstance(raw_item, dict):
                raise ValueError(f"Proposal at index {index} must be a JSON object.")
            role_id = str(raw_item.get("role_id", "") or "").strip()
            if role_id not in expected_roles:
                raise ValueError(f"Invalid role_id `{role_id}`. Choose one of: {', '.join(sorted(expected_roles))}.")
            if role_id in proposal_by_role:
                raise ValueError(f"Duplicate proposal for role_id `{role_id}`.")
            status = str(raw_item.get("status", "fallback_role_only") or "").strip().lower()
            if status not in VALID_RECRUITMENT_STATUSES:
                raise ValueError(
                    f"Invalid status `{status}` for role `{role_id}`. "
                    f"Choose one of: {', '.join(sorted(VALID_RECRUITMENT_STATUSES))}."
                )
            item = prepared_by_role[role_id]
            need: RecruitmentNeed = item["need"]
            existing_employees = list(item.get("existing_employees") or [])
            employee_by_id = {
                str(getattr(employee, "employee_id", "") or "").strip(): employee
                for employee in existing_employees
                if str(getattr(employee, "employee_id", "") or "").strip()
            }
            metadata = {
                "selection_source": "llm_global",
                "attempts": attempt,
                "triage_action": str(item.get("triage_action", "") or ""),
                "selected_categories": list(item.get("selected_categories") or []),
                "category_rationale": str(item.get("category_rationale", "") or ""),
            }
            rationale = str(raw_item.get("rationale", "") or "").strip()
            role_labels = [need.role_name] if need.role_name else []
            existing_employee_ids = list(employee_by_id)
            if status == "direct_role_execution":
                proposal_by_role[role_id] = RecruitmentProposal(
                    role_id=role_id,
                    status="direct_role_execution",
                    rationale=rationale or "Recruiter determined that ordinary role execution is sufficient.",
                    role_labels=role_labels,
                    existing_employee_ids=existing_employee_ids,
                    metadata=metadata,
                )
                continue
            if status == "fallback_role_only":
                proposal_by_role[role_id] = RecruitmentProposal(
                    role_id=role_id,
                    status="fallback_role_only",
                    rationale=rationale or "Recruiter determined that no provided option was a credible fit.",
                    role_labels=role_labels,
                    existing_employee_ids=existing_employee_ids,
                    metadata=metadata,
                )
                continue
            if status == "existing_staff":
                employee_id = str(raw_item.get("employee_id", "") or "").strip()
                if employee_id not in global_employee_by_id:
                    raise ValueError(
                        f"Invalid employee_id `{employee_id}` for role `{role_id}`. "
                        f"Choose one of: {', '.join(sorted(global_employee_by_id))}."
                    )
                recommendation = self._make_existing_employee_recommendation(
                    global_employee_by_id[employee_id],
                    role_id=role_id,
                    domains=[],
                    project_id=project_id,
                    rationale=rationale or "Recruiter selected the existing employee based on role fit and prior experience.",
                )
                proposal_by_role[role_id] = RecruitmentProposal(
                    role_id=role_id,
                    status="existing_staff",
                    rationale=recommendation.rationale,
                    role_labels=role_labels,
                    existing_employee=recommendation,
                    existing_employee_ids=existing_employee_ids,
                    metadata=metadata,
                )
                continue
            template_id = str(raw_item.get("template_id", "") or "").strip()
            if template_id not in candidate_by_id:
                raise ValueError(
                    f"Invalid template_id `{template_id}` for role `{role_id}`. "
                    f"Choose one of: {', '.join(sorted(candidate_by_id))}."
                )
            recommendation = self._make_candidate_recommendation(
                candidate_by_id[template_id],
                rationale=rationale or "Recruiter selected this candidate based on role and domain fit.",
                role_id=role_id,
                proposed_employee_name=str(raw_item.get("proposed_employee_name", "") or "").strip(),
            )
            proposal_by_role[role_id] = RecruitmentProposal(
                role_id=role_id,
                status="proposed_hire",
                rationale=recommendation.rationale,
                role_labels=role_labels,
                candidate=recommendation,
                existing_employee_ids=existing_employee_ids,
                metadata=metadata,
            )
        missing = [role_id for role_id in role_order if role_id not in proposal_by_role]
        if missing:
            raise ValueError(f"Missing proposals for role_id(s): {', '.join(missing)}.")
        return [proposal_by_role[role_id] for role_id in role_order]

    async def _recruit_for_need(
        self,
        need: RecruitmentNeed,
        *,
        existing_employees: list[Any],
        candidates: list[TalentTemplateConfig],
        recruiter_feedback: list[str],
        project_id: str,
        selected_categories: list[str],
        category_rationale: str,
    ) -> RecruitmentProposal:
        existing_payload = [
            self._build_existing_employee_summary(
                employee,
                role_id=need.role_id,
                domains=[],
                project_id=project_id,
            )
            for employee in existing_employees
        ]
        if not self.llm:
            if existing_payload:
                selected = max(existing_payload, key=lambda item: float(item.get("experience_score", 0.0)))
                employee = next(item for item in existing_employees if item.employee_id == selected["employee_id"])
                recommendation = self._make_existing_employee_recommendation(
                    employee,
                    role_id=need.role_id,
                    domains=[],
                    project_id=project_id,
                    rationale="Selected heuristically because an existing experienced employee was available.",
                )
                return RecruitmentProposal(
                    role_id=need.role_id,
                    status="existing_staff",
                    rationale=recommendation.rationale,
                    role_labels=[need.role_name] if need.role_name else [],
                    existing_employee=recommendation,
                    existing_employee_ids=[item.employee_id for item in existing_employees],
                    metadata={
                        "selection_source": "heuristic_existing",
                        "selected_categories": list(selected_categories),
                        "category_rationale": category_rationale,
                    },
                )
            candidate = candidates[0]
            recommendation = self._make_candidate_recommendation(
                candidate,
                rationale="Selected heuristically because no recruiter LLM was available.",
                role_id=need.role_id,
            )
            return RecruitmentProposal(
                role_id=need.role_id,
                status="proposed_hire",
                rationale=recommendation.rationale,
                role_labels=[need.role_name] if need.role_name else [],
                candidate=recommendation,
                metadata={
                    "selection_source": "heuristic_hire",
                    "selected_categories": list(selected_categories),
                    "category_rationale": category_rationale,
                },
            )

        candidate_payload = [
            self.talent_market.build_candidate_summary(candidate)
            for candidate in candidates
        ]
        prompt_payload = {
            "project_id": project_id,
            "need": {
                "role_id": need.role_id,
                "role_name": need.role_name,
                "role_responsibility": need.role_responsibility,
                "user_request": need.request_text,
            },
            "recruiter_feedback": recruiter_feedback,
            "selected_categories": list(selected_categories),
            "category_rationale": category_rationale,
            "existing_employees": existing_payload,
            "new_candidates": candidate_payload,
        }
        retry_feedback: list[str] = []
        max_attempts = 3
        valid_template_ids = {candidate.id for candidate in candidates}
        valid_employee_ids = {employee.employee_id for employee in existing_employees}
        for attempt in range(1, max_attempts + 1):
            payload = dict(prompt_payload)
            if retry_feedback:
                payload["retry_feedback"] = list(retry_feedback)
            raw = await self.llm.simple_chat(
                prompt=json.dumps(payload, ensure_ascii=False),
                system=RECRUITER_PROMPT,
                task_type="quick_tasks",
            )
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                retry_feedback.append(f"Response was not valid JSON: {e.msg} at char {e.pos}.")
                continue
            status = str(data.get("status", "fallback_role_only")).strip()
            employee_id = str(data.get("employee_id", "")).strip()
            template_id = str(data.get("template_id", "")).strip()
            rationale = str(data.get("rationale", "")).strip()
            proposed_name = str(data.get("proposed_employee_name", "")).strip()
            if status == "fallback_role_only":
                return RecruitmentProposal(
                    role_id=need.role_id,
                    status="fallback_role_only",
                    rationale=rationale or "Recruiter determined that no provided template was a credible fit.",
                    role_labels=[need.role_name] if need.role_name else [],
                    metadata={
                        "selection_source": "llm",
                        "selected_categories": list(selected_categories),
                        "category_rationale": category_rationale,
                    },
                )
            if status == "existing_staff":
                if employee_id not in valid_employee_ids:
                    retry_feedback.append(
                        f"Invalid employee_id `{employee_id}`. Choose one of: {', '.join(sorted(valid_employee_ids))}."
                    )
                    continue
                chosen_employee = next(employee for employee in existing_employees if employee.employee_id == employee_id)
                recommendation = self._make_existing_employee_recommendation(
                    chosen_employee,
                    role_id=need.role_id,
                    domains=[],
                    project_id=project_id,
                    rationale=rationale or "Recruiter selected the existing employee based on role fit and prior experience.",
                )
                return RecruitmentProposal(
                    role_id=need.role_id,
                    status="existing_staff",
                    rationale=recommendation.rationale,
                    role_labels=[need.role_name] if need.role_name else [],
                    existing_employee=recommendation,
                    existing_employee_ids=[employee.employee_id for employee in existing_employees],
                    metadata={
                        "selection_source": "llm",
                        "attempts": attempt,
                        "selected_categories": list(selected_categories),
                        "category_rationale": category_rationale,
                    },
                )
            if template_id not in valid_template_ids:
                retry_feedback.append(
                    f"Invalid template_id `{template_id}`. Choose one of: {', '.join(sorted(valid_template_ids))}."
                )
                continue
            chosen = next(candidate for candidate in candidates if candidate.id == template_id)
            recommendation = self._make_candidate_recommendation(
                chosen,
                rationale=rationale or "Recruiter selected this candidate based on role and domain fit.",
                role_id=need.role_id,
                proposed_employee_name=proposed_name,
            )
            return RecruitmentProposal(
                role_id=need.role_id,
                status="proposed_hire",
                rationale=recommendation.rationale,
                role_labels=[need.role_name] if need.role_name else [],
                candidate=recommendation,
                existing_employee_ids=[employee.employee_id for employee in existing_employees],
                metadata={
                    "selection_source": "llm",
                    "attempts": attempt,
                    "selected_categories": list(selected_categories),
                    "category_rationale": category_rationale,
                },
            )
        logger.warning(f"Recruiter exhausted retries for role `{need.role_id}`; falling back to role-only execution")
        return RecruitmentProposal(
            role_id=need.role_id,
            status="fallback_role_only",
            rationale="Recruiter could not produce a valid staffing decision after retries.",
            role_labels=[need.role_name] if need.role_name else [],
            metadata={
                "selection_source": "llm_fallback",
                "selected_categories": list(selected_categories),
                "category_rationale": category_rationale,
            },
        )

    async def _triage_staffing_for_needs(
        self,
        needs: list[RecruitmentNeed],
        *,
        recruiter_feedback: list[str],
        llm: Any | None = None,
    ) -> dict[str, tuple[str, list[str], str]]:
        if not needs:
            return {}
        category_catalog = self.talent_market.list_category_catalog()
        valid_categories = {item["category"] for item in category_catalog}
        active_llm = llm if llm is not None else self.llm
        if not category_catalog:
            return {
                need.role_id: self._heuristic_staffing_triage(need, category_catalog)
                for need in needs
            }
        if not active_llm:
            return {
                need.role_id: self._heuristic_staffing_triage(need, category_catalog)
                for need in needs
            }

        payload = {
            "user_request": next((need.request_text for need in needs if need.request_text), ""),
            "recruiter_feedback": recruiter_feedback,
            **self._build_organization_payload(),
            "roles": [
                {
                    "role_id": need.role_id,
                    "role_name": need.role_name,
                    "role_responsibility": need.role_responsibility,
                }
                for need in needs
            ],
            "category_catalog": category_catalog,
        }
        retry_feedback: list[str] = []
        expected_roles = {need.role_id for need in needs}
        for _ in range(3):
            attempt_payload = dict(payload)
            if retry_feedback:
                attempt_payload["retry_feedback"] = list(retry_feedback)
            try:
                raw = await active_llm.simple_chat(
                    prompt=json.dumps(attempt_payload, ensure_ascii=False),
                    system=STAFFING_TRIAGE_PROMPT,
                    task_type="quick_tasks",
                )
            except Exception as exc:
                retry_feedback.append(f"Staffing triage LLM call failed: {type(exc).__name__}: {exc}")
                continue
            text = raw.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                retry_feedback.append(f"Response was not valid JSON: {exc.msg} at char {exc.pos}.")
                continue
            roles_data = data.get("roles")
            if not isinstance(roles_data, list):
                retry_feedback.append("Response must contain a `roles` array.")
                continue
            try:
                return self._build_triage_plan_from_response(
                    roles_data,
                    expected_roles=expected_roles,
                    valid_categories=valid_categories,
                )
            except ValueError as exc:
                retry_feedback.append(str(exc))
        return {
            need.role_id: self._heuristic_staffing_triage(need, category_catalog)
            for need in needs
        }

    @staticmethod
    def _build_triage_plan_from_response(
        roles_data: list[Any],
        *,
        expected_roles: set[str],
        valid_categories: set[str],
    ) -> dict[str, tuple[str, list[str], str]]:
        triage_by_role: dict[str, tuple[str, list[str], str]] = {}
        for index, raw_item in enumerate(roles_data):
            if not isinstance(raw_item, dict):
                raise ValueError(f"Triage entry at index {index} must be a JSON object.")
            role_id = str(raw_item.get("role_id", "") or "").strip()
            if role_id not in expected_roles:
                raise ValueError(f"Invalid role_id `{role_id}`. Choose one of: {', '.join(sorted(expected_roles))}.")
            if role_id in triage_by_role:
                raise ValueError(f"Duplicate triage entry for role_id `{role_id}`.")
            action = str(raw_item.get("action", "category_screening") or "").strip().lower()
            rationale = str(raw_item.get("rationale", "") or "").strip()
            if action == "direct_role_execution":
                triage_by_role[role_id] = (action, [], rationale)
                continue
            if action != "category_screening":
                raise ValueError(
                    f"Invalid action `{action}` for role `{role_id}`. "
                    "Choose `direct_role_execution` or `category_screening`."
                )
            chosen = [
                str(category).strip().lower()
                for category in raw_item.get("categories", [])
                if str(category).strip()
            ]
            chosen = [category for category in chosen if category in valid_categories]
            chosen = list(dict.fromkeys(chosen))[:3]
            if not chosen:
                raise ValueError(
                    f"Role `{role_id}` chose category_screening without valid categories. "
                    f"Choose 1 to 3 valid categories from: {', '.join(sorted(valid_categories))}."
                )
            triage_by_role[role_id] = ("category_screening", chosen, rationale)
        missing = [role_id for role_id in sorted(expected_roles) if role_id not in triage_by_role]
        if missing:
            raise ValueError(f"Missing triage entries for role_id(s): {', '.join(missing)}.")
        return triage_by_role

    @staticmethod
    def _need_text(need: RecruitmentNeed) -> str:
        return " ".join(
            text for text in (need.role_name, need.role_responsibility, need.request_text) if text
        ).strip()

    def _heuristic_staffing_triage(
        self,
        need: RecruitmentNeed,
        category_catalog: list[dict[str, Any]],
    ) -> tuple[str, list[str], str]:
        combined_text = self._need_text(need).lower()
        if len(combined_text) <= 24 or combined_text in {"hi", "hello", "你好", "您好", "thanks", "谢谢"}:
            return "direct_role_execution", [], "The request is simple enough to handle without staffing."
        if not category_catalog:
            return "direct_role_execution", [], "No staffing catalog is available; use ordinary role execution."
        selected = self._heuristic_category_selection(need, category_catalog)
        if not selected:
            return "direct_role_execution", [], "No strong staffing categories stood out."
        return "category_screening", selected, "Selected heuristically from category descriptions."

    def _heuristic_category_selection(
        self,
        need: RecruitmentNeed,
        category_catalog: list[dict[str, Any]],
    ) -> list[str]:
        need_tokens = set(
            re.findall(
                r"[a-z0-9][a-z0-9+-]{2,}",
                self._need_text(need).lower(),
            )
        )
        scored: list[tuple[float, str]] = []
        for item in category_catalog:
            category = str(item.get("category", "")).strip().lower()
            description = str(item.get("description", "")).strip().lower()
            template_count = int(item.get("template_count", 0) or 0)
            category_tokens = set(re.findall(r"[a-z0-9][a-z0-9+-]{2,}", f"{category} {description}"))
            score = float(len(need_tokens & category_tokens)) + min(template_count, 3) * 0.1
            scored.append((score, category))
        scored.sort(key=lambda item: (-item[0], item[1]))
        selected = [category for score, category in scored if score > 0][:3]
        if selected:
            return selected
        return [item["category"] for item in category_catalog[:2]]

    def _build_existing_employee_summary(
        self,
        employee: Any,
        *,
        role_id: str,
        domains: list[str],
        project_id: str,
    ) -> dict[str, Any]:
        experience_score = 0.0
        learned_skill_refs: list[str] = []
        if self.org_engine.employee_evolution:
            organization_id = str(getattr(self.org_engine.config.org, "organization_id", "") or "").strip()
            experience_score = self.org_engine.employee_evolution.get_experience_score(
                employee.employee_id,
                role_id=role_id,
                domains=domains,
                project_id=project_id,
                organization_id=organization_id or None,
            )
            learned_skill_refs = self.org_engine.employee_evolution.get_learned_skill_refs(
                employee.employee_id,
                project_id=project_id,
            )
        return {
            "employee_id": employee.employee_id,
            "employee_name": employee.name,
            "template_id": employee.template_id,
            "home_role_id": employee.role_id,
            "role_ids": (
                list(self.org_engine.employee_role_ids(employee))
                if hasattr(self.org_engine, "employee_role_ids")
                else [employee.role_id]
            ),
            "category": employee.category,
            "experience_score": experience_score,
            "learned_skill_refs": list(learned_skill_refs),
            "description": employee.description,
        }

    def _make_existing_employee_recommendation(
        self,
        employee: Any,
        *,
        role_id: str,
        domains: list[str],
        project_id: str,
        rationale: str,
    ) -> RecruitmentEmployeeRecommendation:
        summary = self._build_existing_employee_summary(
            employee,
            role_id=role_id,
            domains=domains,
            project_id=project_id,
        )
        return RecruitmentEmployeeRecommendation(
            employee_id=employee.employee_id,
            employee_name=employee.name,
            role_id=employee.role_id,
            category=employee.category,
            domains=list(employee.domains),
            learned_skill_refs=list(summary.get("learned_skill_refs", [])),
            experience_score=float(summary.get("experience_score", 0.0) or 0.0),
            rationale=rationale,
            metadata={"template_id": employee.template_id, "experience_mode": "with_experience"},
        )

    def _make_candidate_recommendation(
        self,
        candidate: TalentTemplateConfig,
        *,
        rationale: str,
        role_id: str,
        proposed_employee_name: str = "",
    ) -> RecruitmentCandidateRecommendation:
        employee_name = proposed_employee_name.strip() or candidate.name.strip() or candidate.id
        employee_id = self.talent_market.build_employee_id(role_id=role_id, template_id=candidate.id)
        return RecruitmentCandidateRecommendation(
            template_id=candidate.id,
            template_name=candidate.name,
            category=candidate.category,
            domains=list(candidate.domains),
            prompt_ref=candidate.prompt_ref,
            preferred_external_agent=candidate.preferred_external_agent,
            source_path=candidate.source_path,
            rationale=rationale,
            proposed_employee_name=employee_name,
            proposed_employee_id=employee_id,
            metadata={"source_repo": candidate.source_repo, "experience_mode": "template_only"},
        )


def build_recruitment_plan_from_payload(data: dict[str, Any]) -> RecruitmentPlan:
    return deserialize_recruitment_plan(data)


def build_recruitment_feedback(reply: str) -> str:
    cleaned = re.sub(r"\s+", " ", reply.strip())
    return cleaned
