"""Employee experience tracking, project reflections, and learned-skill distillation."""

from __future__ import annotations

import re
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opc.core.config import validate_organization_id
from opc.core.models import TaskStatus
from opc.layer2_organization.work_item_identity import projection_id_for_task
from opc.layer5_memory.preference import PreferenceManager
from opc.layer5_memory.skill_library import Skill, SkillLibrary


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class EmployeeEvolutionManager:
    """Tracks employee outcomes, project reflections, and learned skills."""

    LEARNED_SKILL_THRESHOLD = 2
    EMPLOYEE_EXPERIENCE_SCHEMA_VERSION = 1

    def __init__(self, opc_home) -> None:
        self.opc_home = Path(opc_home)
        self.preferences = PreferenceManager(self.opc_home)
        self.skills = SkillLibrary(self.opc_home)
        self.skills.load_all()

    def evolution_profile_path(self, project_id: str | None = None) -> Path:
        """Return the structured company-evolution state path.

        Employee evolution is runtime/company state, not user/project durable
        memory. Keeping it outside ``memory/*.md`` lets company mode retain
        experience scoring and learned playbooks without reintroducing hidden
        user preference writes.
        """
        project = str(project_id or "").strip()
        if project:
            return self.opc_home / "projects" / project / "employee_evolution.json"
        return self.opc_home / "evolution" / "employees.json"

    def load_evolution_profile(self, project_id: str | None = None) -> dict[str, Any]:
        path = self.evolution_profile_path(project_id)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_evolution_profile(self, profile: dict[str, Any], project_id: str | None = None) -> None:
        path = self.evolution_profile_path(project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(dict(profile or {}), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def employee_experience_dir(self, organization_id: str) -> Path:
        org_id = validate_organization_id(organization_id)
        return self.opc_home / "company_state" / org_id / "employee_experience"

    def employee_experience_path(self, organization_id: str, employee_id: str) -> Path:
        safe_id = self._safe_employee_filename(employee_id)
        return self.employee_experience_dir(organization_id) / f"{safe_id}.json"

    def load_employee_experience(self, organization_id: str, employee_id: str) -> dict[str, Any]:
        path = self.employee_experience_path(organization_id, employee_id)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save_employee_experience(self, organization_id: str, employee_id: str, profile: dict[str, Any]) -> None:
        path = self.employee_experience_path(organization_id, employee_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(dict(profile or {}), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def get_employee_profile(self, employee_id: str, project_id: str | None = None) -> dict[str, Any]:
        global_profile = self.load_evolution_profile()
        project_profile = self.load_evolution_profile(project_id) if project_id else {}
        global_data = dict(global_profile.get("employees", {}).get(employee_id, {}))
        project_data = dict(project_profile.get("employees", {}).get(employee_id, {}))
        return self._deep_merge(global_data, project_data)

    def get_learned_skill_refs(self, employee_id: str, project_id: str | None = None) -> list[str]:
        profile = self.get_employee_profile(employee_id, project_id)
        refs = list(profile.get("learned_skill_refs", []))
        unique: list[str] = []
        for ref in refs:
            if ref and ref not in unique:
                unique.append(ref)
        return unique

    def build_employee_delta_context(
        self,
        employee_id: str,
        project_id: str | None = None,
        organization_id: str | None = None,
    ) -> str:
        profile = self.get_employee_profile(employee_id, project_id)
        delta = dict(profile.get("delta_profile", {}))
        experience_profile: dict[str, Any] = {}
        experience_delta: dict[str, Any] = {}
        if organization_id:
            experience_profile = self.load_employee_experience(organization_id, employee_id)
            experience_delta = dict(experience_profile.get("delta_profile", {}) or {})
        if not delta and not experience_delta:
            return ""

        parts: list[str] = []
        evolution_count = int(experience_profile.get("evolution_count", 0) or len(experience_profile.get("events", []) or []))
        if evolution_count:
            parts.append(f"Self-evolution reviews: {evolution_count}")
        projects_reflected = int(profile.get("projects_reflected", 0))
        if projects_reflected:
            parts.append(f"Reflected projects: {projects_reflected}")

        for title, key in (
            ("Self-Evolved Strengths", "strengths"),
            ("Self-Evolved Adjustments", "adjustments"),
            ("Self-Evolved Watchouts", "avoid_next_time"),
            ("Self-Evolved Routing Notes", "routing_notes"),
        ):
            values = [str(item).strip() for item in list(experience_delta.get(key, []) or []) if str(item).strip()]
            if values:
                parts.append(f"## {title}\n" + "\n".join(f"- {item}" for item in values[:6]))

        for title, key in (
            ("Working Patterns", "working_patterns"),
            ("Default Checklists", "default_checklists"),
            ("Reviewer Preferences", "reviewer_preferences"),
            ("Risk Watchouts", "risk_watchouts"),
            ("Tool Preferences", "tool_preferences"),
            ("Fit Domains", "fit_domains"),
            ("Avoid Domains", "avoid_domains"),
        ):
            values = [str(item).strip() for item in delta.get(key, []) if str(item).strip()]
            if values:
                parts.append(f"## {title}\n" + "\n".join(f"- {item}" for item in values[:6]))

        return "\n\n".join(parts)

    def get_experience_score(
        self,
        employee_id: str,
        role_id: str,
        domains: list[str] | None = None,
        project_id: str | None = None,
        organization_id: str | None = None,
    ) -> float:
        profile = self.get_employee_profile(employee_id, project_id)
        total_successes = int(profile.get("successes", 0))
        total_partials = int(profile.get("partial_successes", 0))
        total_failures = int(profile.get("failures", 0))
        role_successes = int(profile.get("roles", {}).get(role_id, {}).get("successes", 0))
        role_partials = int(profile.get("roles", {}).get(role_id, {}).get("partial_successes", 0))
        role_failures = int(profile.get("roles", {}).get(role_id, {}).get("failures", 0))
        learned_bonus = 2 * len(profile.get("learned_skill_refs", []))
        reflection_bonus = min(4, int(profile.get("projects_reflected", 0)))
        domain_bonus = 0
        for domain in domains or []:
            domain_record = profile.get("domains", {}).get(domain, {})
            domain_bonus += int(domain_record.get("successes", 0))
            domain_bonus += 0.5 * int(domain_record.get("partial_successes", 0))
            domain_bonus -= 0.25 * int(domain_record.get("failures", 0))
        score = (
            total_successes
            + (0.5 * total_partials)
            - (0.25 * total_failures)
            + (2 * role_successes)
            + role_partials
            - (0.5 * role_failures)
            + domain_bonus
            + learned_bonus
            + reflection_bonus
        )
        if organization_id:
            experience_profile = self.load_employee_experience(organization_id, employee_id)
            events = [item for item in list(experience_profile.get("events", []) or []) if isinstance(item, dict)]
            score += min(8.0, float(len(events)))
            for event in events[-8:]:
                if str(event.get("role_id", "") or "").strip() == role_id:
                    score += 0.5
        return float(max(0.0, score))

    def apply_employee_evolution_patch(
        self,
        *,
        organization_id: str,
        patch: dict[str, Any],
        source: dict[str, Any] | None = None,
        allowed_employee_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        org_id = validate_organization_id(organization_id)
        source_payload = dict(source or {})
        raw_events = list(patch.get("patches", []) or [])
        allowed = {str(item).strip() for item in (allowed_employee_ids or set()) if str(item).strip()}
        recorded: list[dict[str, Any]] = []

        for raw_event in raw_events:
            if not isinstance(raw_event, dict):
                continue
            employee_id = str(raw_event.get("employee_id", "") or "").strip()
            if not employee_id or (allowed and employee_id not in allowed):
                continue
            event = self._normalize_self_evolution_event(raw_event, source_payload)
            if not event:
                continue

            profile = self.load_employee_experience(org_id, employee_id)
            if not profile:
                profile = {
                    "schema_version": self.EMPLOYEE_EXPERIENCE_SCHEMA_VERSION,
                    "kind": "company_employee_experience",
                    "organization_id": org_id,
                    "employee_id": employee_id,
                    "events": [],
                    "delta_profile": {},
                    "evolution_count": 0,
                }
            profile["schema_version"] = self.EMPLOYEE_EXPERIENCE_SCHEMA_VERSION
            profile["kind"] = "company_employee_experience"
            profile["organization_id"] = org_id
            profile["employee_id"] = employee_id
            events = [item for item in list(profile.get("events", []) or []) if isinstance(item, dict)]
            event_id = str(event.get("event_id", "") or "").strip()
            if event_id and any(str(item.get("event_id", "") or "").strip() == event_id for item in events):
                continue
            events.append(event)
            profile["events"] = events[-100:]
            profile["evolution_count"] = int(profile.get("evolution_count", 0) or 0) + 1
            profile["updated_at"] = _utc_now()
            profile["delta_profile"] = self._build_self_evolution_delta(profile["events"])
            self.save_employee_experience(org_id, employee_id, profile)
            recorded.append({
                "employee_id": employee_id,
                "event_id": event_id,
                "status": "recorded",
                "path": str(self.employee_experience_path(org_id, employee_id)),
            })

        return recorded

    def ingest_human_contractor_trajectory(
        self,
        organization_id: str,
        employee_id: str,
        role_id: str,
        task_id: str,
        deliverable_content: str,
        artifacts: list[dict] | None = None,
    ) -> dict[str, Any]:
        """Distill human contractor deliverable into private role experience and shared playbook."""
        org_id = validate_organization_id(organization_id)
        event_id = f"human_traj_{task_id}_{int(datetime.now().timestamp())}"
        
        event = {
            "event_id": event_id,
            "employee_id": employee_id,
            "role_id": role_id,
            "task_id": task_id,
            "event_kind": "human_deliverable_completed",
            "content_summary": deliverable_content[:512],
            "artifact_count": len(artifacts or []),
            "timestamp": _utc_now(),
        }

        patch = {"patches": [event]}
        recorded = self.apply_employee_evolution_patch(
            organization_id=org_id,
            patch=patch,
            allowed_employee_ids={employee_id},
        )

        # Promote recurring human patterns to Shared Playbook in SkillLibrary
        if deliverable_content:
            skill_name = f"human-playbook-{role_id}"
            self.skills.add_skill(
                name=skill_name,
                description=f"Shared playbook distilled from human contractor deliverables for role {role_id}.",
                content=f"# Shared Playbook ({role_id})\n\n{deliverable_content}",
                category="human_contractor_playbook",
            )

        return {"recorded": recorded, "event_id": event_id}

    def record_work_item_completion(
        self,
        task: Any,
        result_content: str,
        *,
        outcome: str = "success",
        feedback_summary: str = "",
        strengths: list[str] | None = None,
        weaknesses: list[str] | None = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        assignment = dict(getattr(task, "metadata", {}).get("employee_assignment", {}) or {})
        employee_id = str(assignment.get("employee_id", "")).strip()
        if not employee_id:
            return {}

        role_id = str(
            assignment.get("role_id")
            or getattr(task, "assigned_to", "")
            or getattr(task, "metadata", {}).get("work_item_role_id", "")
        ).strip()
        domains = list(assignment.get("domains") or getattr(task, "tags", []) or [])
        project_id = getattr(task, "project_id", None) or None
        summary = str(getattr(task, "metadata", {}).get("work_item_summary_for_downstream", "") or result_content).strip()
        pattern_key = self._pattern_key(role_id, domains)
        normalized_outcome = self._normalize_outcome(outcome)
        base_payload = {
            "employee_id": employee_id,
            "employee_name": assignment.get("name", ""),
            "role_id": role_id,
            "template_id": assignment.get("template_id", ""),
            "category": assignment.get("category", ""),
        }

        global_profile = self.load_evolution_profile()
        self._record_in_profile(
            global_profile,
            base_payload=base_payload,
                role_id=role_id,
                domains=domains,
                pattern_key=pattern_key,
                summary=summary,
                outcome=normalized_outcome,
                feedback_summary=feedback_summary,
                strengths=list(strengths or []),
                weaknesses=list(weaknesses or []),
                rationale=rationale,
            )
        self.save_evolution_profile(global_profile)

        if project_id:
            project_profile = self.load_evolution_profile(project_id)
            self._record_in_profile(
                project_profile,
                base_payload=base_payload,
                role_id=role_id,
                domains=domains,
                pattern_key=pattern_key,
                summary=summary,
                outcome=normalized_outcome,
                feedback_summary=feedback_summary,
                strengths=list(strengths or []),
                weaknesses=list(weaknesses or []),
                rationale=rationale,
            )
            self.save_evolution_profile(project_profile, project_id)

        return {
            "employee_id": employee_id,
            "project_id": project_id or "",
            "pattern_key": pattern_key,
            "outcome": normalized_outcome,
        }

    def record_project_reflections(
        self,
        delivery_task: Any,
        work_item_tasks: list[Any],
        partial: bool = False,
        *,
        feedback: dict[str, Any] | None = None,
        evaluation: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        project_id = str(getattr(delivery_task, "project_id", "") or "").strip()
        delivery_task_id = str(getattr(delivery_task, "id", "") or "").strip()
        if not project_id or not delivery_task_id:
            return []

        tasks = self._normalize_work_item_tasks(work_item_tasks, delivery_task)
        terminal = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
        employee_groups: dict[str, list[Any]] = {}
        for task in tasks:
            if getattr(task, "status", None) not in terminal:
                continue
            assignment = dict(getattr(task, "metadata", {}).get("employee_assignment", {}) or {})
            employee_id = str(assignment.get("employee_id", "")).strip()
            if not employee_id:
                continue
            employee_groups.setdefault(employee_id, []).append(task)

        results: list[dict[str, Any]] = []
        for employee_id, employee_tasks in employee_groups.items():
            reflection = self._build_project_reflection(
                delivery_task=delivery_task,
                employee_tasks=employee_tasks,
                project_id=project_id,
                partial=partial,
                feedback=feedback,
                evaluation=evaluation,
            )
            reflection_path = self.evolution_profile_path(project_id)

            global_profile = self.load_evolution_profile()
            self._record_project_reflection_in_profile(
                global_profile,
                reflection=reflection,
                reflection_path=reflection_path,
            )
            learned_skill_ref = self._maybe_promote_reflection_skill(global_profile, reflection, project_id=project_id)
            self.save_evolution_profile(global_profile)

            project_profile = self.load_evolution_profile(project_id)
            self._record_project_reflection_in_profile(
                project_profile,
                reflection=reflection,
                reflection_path=reflection_path,
            )
            if learned_skill_ref:
                self._attach_learned_skill(
                    project_profile,
                    reflection["employee_id"],
                    reflection["pattern_key"],
                    learned_skill_ref,
                )
            else:
                learned_skill_ref = self._maybe_promote_reflection_skill(project_profile, reflection, project_id=project_id)
            self.save_evolution_profile(project_profile, project_id)

            results.append({
                "employee_id": employee_id,
                "reflection_path": str(reflection_path),
                "learned_skill_ref": learned_skill_ref,
                "status": "recorded",
                "reflection": reflection,
            })

        return results

    def _record_in_profile(
        self,
        profile: dict[str, Any],
        *,
        base_payload: dict[str, Any],
        role_id: str,
        domains: list[str],
        pattern_key: str,
        summary: str,
        outcome: str,
        feedback_summary: str,
        strengths: list[str],
        weaknesses: list[str],
        rationale: str,
    ) -> None:
        employees = profile.setdefault("employees", {})
        record = employees.setdefault(base_payload["employee_id"], {})
        record.update({
            "employee_name": base_payload.get("employee_name", ""),
            "role_id": role_id,
            "template_id": base_payload.get("template_id", ""),
            "category": base_payload.get("category", ""),
            "updated_at": _utc_now(),
        })
        self._increment_outcome_counts(record, outcome)
        record["last_outcome"] = outcome
        if feedback_summary:
            record["last_feedback_summary"] = feedback_summary
        if rationale:
            record["last_feedback_rationale"] = rationale
        roles = record.setdefault("roles", {})
        role_record = roles.setdefault(role_id, {"successes": 0, "partial_successes": 0, "failures": 0})
        self._increment_outcome_counts(role_record, outcome)
        role_record["last_outcome"] = outcome
        domain_records = record.setdefault("domains", {})
        for domain in domains:
            domain_record = domain_records.setdefault(domain, {"successes": 0, "partial_successes": 0, "failures": 0})
            self._increment_outcome_counts(domain_record, outcome)
            domain_record["last_outcome"] = outcome
        patterns = record.setdefault("patterns", {})
        pattern = patterns.setdefault(pattern_key, self._base_pattern_record(role_id, domains))
        self._increment_outcome_counts(pattern, outcome)
        pattern["last_summary"] = summary
        pattern["last_outcome"] = outcome
        pattern["last_feedback_summary"] = feedback_summary
        pattern["last_feedback_rationale"] = rationale
        if outcome == "success":
            pattern["last_success_at"] = _utc_now()
        elif outcome == "partial_success":
            pattern["last_partial_success_at"] = _utc_now()
        else:
            pattern["last_failure_at"] = _utc_now()
        self._increment_counts(record, "working_pattern_counts", strengths)
        self._increment_counts(record, "risk_watchout_counts", weaknesses)
        self._increment_counts(pattern, "working_pattern_counts", strengths)
        self._increment_counts(pattern, "risk_watchout_counts", weaknesses)
        record["delta_profile"] = self._build_delta_profile(record)
        record.setdefault("learned_skill_refs", [])

    def _record_project_reflection_in_profile(
        self,
        profile: dict[str, Any],
        *,
        reflection: dict[str, Any],
        reflection_path: Path,
    ) -> None:
        employees = profile.setdefault("employees", {})
        employee_id = str(reflection.get("employee_id", "")).strip()
        if not employee_id:
            return
        record = employees.setdefault(employee_id, {})
        record.update({
            "employee_name": reflection.get("employee_name", ""),
            "role_id": reflection.get("role_id", ""),
            "template_id": reflection.get("template_id", ""),
            "category": reflection.get("category", ""),
            "updated_at": _utc_now(),
            "last_reflection_at": _utc_now(),
        })
        reflection_ids = record.setdefault("project_reflection_ids", [])
        if reflection["delivery_task_id"] in reflection_ids:
            return
        reflection_ids.append(reflection["delivery_task_id"])
        reflection_paths = record.setdefault("reflection_paths", [])
        reflection_paths.append(str(reflection_path))
        project_ids = record.setdefault("project_ids", [])
        if reflection["project_id"] not in project_ids:
            project_ids.append(reflection["project_id"])
        record["projects_reflected"] = len(project_ids)

        patterns = record.setdefault("patterns", {})
        pattern_key = str(reflection.get("pattern_key", ""))
        pattern = patterns.setdefault(
            pattern_key,
            self._base_pattern_record(
                str(reflection.get("role_id", "")).strip(),
                list(reflection.get("domains", [])),
            ),
        )
        pattern["role_id"] = reflection.get("role_id", "")
        pattern["domains"] = list(reflection.get("domains", []))
        pattern["reflection_count"] = int(pattern.get("reflection_count", 0)) + 1
        pattern["latest_project_summary"] = str(reflection.get("project_summary", ""))
        pattern["last_reflection_at"] = _utc_now()
        project_reflection_paths = pattern.setdefault("reflection_paths", [])
        project_reflection_paths.append(str(reflection_path))

        self._increment_counts(pattern, "working_pattern_counts", reflection.get("what_worked", []))
        self._increment_counts(pattern, "checklist_counts", reflection.get("reusable_checklist", []))
        self._increment_counts(pattern, "reviewer_preference_counts", reflection.get("reviewer_preferences", []))
        self._increment_counts(pattern, "tool_preference_counts", reflection.get("tool_preferences", []))
        self._increment_counts(pattern, "risk_watchout_counts", reflection.get("mistakes_to_avoid", []))
        self._increment_counts(pattern, "fit_domain_counts", reflection.get("suitable_for", []))
        self._increment_counts(pattern, "avoid_domain_counts", reflection.get("avoid_for", []))

        self._increment_counts(record, "working_pattern_counts", reflection.get("what_worked", []))
        self._increment_counts(record, "checklist_counts", reflection.get("reusable_checklist", []))
        self._increment_counts(record, "reviewer_preference_counts", reflection.get("reviewer_preferences", []))
        self._increment_counts(record, "tool_preference_counts", reflection.get("tool_preferences", []))
        self._increment_counts(record, "risk_watchout_counts", reflection.get("mistakes_to_avoid", []))
        self._increment_counts(record, "fit_domain_counts", reflection.get("suitable_for", []))
        self._increment_counts(record, "avoid_domain_counts", reflection.get("avoid_for", []))
        record["delta_profile"] = self._build_delta_profile(record)
        record.setdefault("learned_skill_refs", [])

    def _build_project_reflection(
        self,
        *,
        delivery_task: Any,
        employee_tasks: list[Any],
        project_id: str,
        partial: bool = False,
        feedback: dict[str, Any] | None = None,
        evaluation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        first_assignment = dict(employee_tasks[0].metadata.get("employee_assignment", {}) or {})
        employee_id = str(first_assignment.get("employee_id", "")).strip()
        role_id = str(first_assignment.get("role_id") or getattr(employee_tasks[0], "assigned_to", "") or "").strip()
        domains = self._collect_domains(employee_tasks, fallback=list(first_assignment.get("domains", [])))
        pattern_key = self._pattern_key(role_id, domains)
        task_summaries = self._collect_task_summaries(employee_tasks)
        artifacts = self._collect_items(employee_tasks, "artifacts")
        decisions = self._collect_items(employee_tasks, "decisions")
        risks = self._collect_items(employee_tasks, "risks")
        open_questions = self._collect_items(employee_tasks, "open_questions")
        preferred_agents = self._collect_preferred_agents(employee_tasks, first_assignment)
        employee_feedback = self._find_employee_feedback(employee_id, evaluation)
        historical_context = self.build_employee_delta_context(employee_id, project_id=project_id)
        feedback_summary = str((evaluation or {}).get("summary", "") or (feedback or {}).get("raw_feedback", "")).strip()

        what_worked = [
            "Break work into explicit deliverables and leave concise handoff summaries.",
            "Preserve reviewer-friendly artifacts so downstream validation is faster.",
        ]
        if decisions:
            what_worked.append("Capture explicit implementation decisions that downstream reviewers can verify.")
        if artifacts:
            what_worked.append("Reference exact artifact paths or outputs in every handoff.")

        reusable_checklist = [
            "State the objective and completion summary explicitly in the handoff.",
            "Leave reviewer-friendly artifacts for the next work item.",
        ]
        if decisions:
            reusable_checklist.append("Record key decisions that affect downstream execution.")
        if artifacts:
            reusable_checklist.append("List concrete artifact references for changed outputs.")
        if any(domain in {"coding", "api", "backend", "frontend", "devops"} for domain in domains):
            reusable_checklist.append("Include validation or test evidence before requesting review.")

        reviewer_preferences = [
            "Make decisions, risks, and artifact references explicit for review.",
        ]
        if risks or open_questions:
            reviewer_preferences.append("Flag unresolved risks and open questions before requesting approval.")
        if artifacts:
            reviewer_preferences.append("Point reviewers to exact changed files or deliverables.")

        tool_preferences = [
            f"Prefer external agent `{agent}` for similar `{role_id}` work."
            for agent in preferred_agents
        ]

        mistakes_to_avoid = list(risks[:4])
        if open_questions:
            mistakes_to_avoid.extend(item for item in open_questions[:2] if item not in mistakes_to_avoid)
        if not mistakes_to_avoid:
            mistakes_to_avoid.append("Avoid handoffs that omit concrete artifacts, risks, or validation notes.")
        what_worked.extend(str(item).strip() for item in list(employee_feedback.get("strengths", [])) if str(item).strip())
        mistakes_to_avoid.extend(str(item).strip() for item in list(employee_feedback.get("weaknesses", [])) if str(item).strip())
        if feedback_summary:
            reviewer_preferences.append(f"Carry forward user feedback themes: {feedback_summary}")

        suitable_for = list(domains or [role_id])
        avoid_for = self._infer_avoid_domains(mistakes_to_avoid)
        confidence = round(min(0.95, 0.55 + (0.08 * len(employee_tasks))), 2)

        failed_tasks = [
            t for t in employee_tasks
            if getattr(t, "status", None) != TaskStatus.DONE
        ]
        failures: list[dict[str, str]] = []
        if failed_tasks:
            for t in failed_tasks:
                reason = str(getattr(t, "metadata", {}).get("failure_reason", ""))
                failures.append({
                    "task": getattr(t, "title", ""),
                    "status": str(getattr(t, "status", "")),
                    "reason": reason,
                })
                if reason:
                    mistakes_to_avoid.append(reason)

        if partial:
            confidence = round(min(confidence, 0.4), 2)

        result: dict[str, Any] = {
            "employee_id": employee_id,
            "employee_name": first_assignment.get("name", ""),
            "template_id": first_assignment.get("template_id", ""),
            "category": first_assignment.get("category", ""),
            "project_id": project_id,
            "delivery_task_id": str(getattr(delivery_task, "id", "") or ""),
            "delivery_projection_id": projection_id_for_task(delivery_task),
            "role_id": role_id,
            "domains": domains,
            "pattern_key": pattern_key,
            "project_summary": " ".join(task_summaries[:3]),
            "what_worked": self._dedupe_preserve_order(what_worked),
            "mistakes_to_avoid": self._dedupe_preserve_order(mistakes_to_avoid),
            "reusable_checklist": self._dedupe_preserve_order(reusable_checklist),
            "reviewer_preferences": self._dedupe_preserve_order(reviewer_preferences),
            "tool_preferences": self._dedupe_preserve_order(tool_preferences),
            "suitable_for": self._dedupe_preserve_order(suitable_for),
            "avoid_for": self._dedupe_preserve_order(avoid_for),
            "confidence": confidence,
            "source_task_ids": [str(getattr(task, "id", "")) for task in employee_tasks],
            "created_at": _utc_now(),
            "employee_outcome": self._normalize_outcome(str(employee_feedback.get("outcome", "partial_success") or "partial_success")),
            "feedback_summary": feedback_summary,
            "feedback_rationale": str(employee_feedback.get("reason", "")).strip(),
        }
        if historical_context:
            result["historical_context"] = historical_context
        if feedback:
            result["user_feedback"] = {
                "label": str(feedback.get("label", "")).strip(),
                "raw_feedback": str(feedback.get("raw_feedback", "")).strip(),
                "scope": str(feedback.get("scope", "")).strip(),
            }
        if evaluation:
            result["runtime_feedback_evaluation"] = {
                "overall_outcome": str(evaluation.get("overall_outcome", "")).strip(),
                "summary": str(evaluation.get("summary", "")).strip(),
                "strengths": [str(item).strip() for item in list(evaluation.get("strengths", [])) if str(item).strip()][:6],
                "weaknesses": [str(item).strip() for item in list(evaluation.get("weaknesses", [])) if str(item).strip()][:6],
            }
        if failures:
            result["failures"] = failures
            result["partial"] = True
        return result

    def _maybe_promote_reflection_skill(self, profile: dict[str, Any], reflection: dict[str, Any], project_id: str | None = None) -> str:
        employees = profile.setdefault("employees", {})
        employee_id = str(reflection.get("employee_id", "")).strip()
        record = employees.get(employee_id)
        if not record:
            return ""
        pattern_key = str(reflection.get("pattern_key", "")).strip()
        pattern = dict(record.get("patterns", {}).get(pattern_key, {}))
        if not pattern or pattern.get("learned_skill_ref"):
            return str(pattern.get("learned_skill_ref", ""))
        if int(pattern.get("reflection_count", 0)) < self.LEARNED_SKILL_THRESHOLD:
            return ""

        repeated_working = self._repeated_items(pattern.get("working_pattern_counts", {}))
        repeated_checklists = self._repeated_items(pattern.get("checklist_counts", {}))
        repeated_reviewer = self._repeated_items(pattern.get("reviewer_preference_counts", {}))
        repeated_tools = self._repeated_items(pattern.get("tool_preference_counts", {}))
        repeated_risks = self._repeated_items(pattern.get("risk_watchout_counts", {}))
        if not any((repeated_working, repeated_checklists, repeated_reviewer, repeated_tools, repeated_risks)):
            return ""

        employee_name = str(reflection.get("employee_name", "Employee")).strip() or "Employee"
        role_id = str(reflection.get("role_id", "")).strip() or record.get("role_id", "general")
        domains = list(reflection.get("domains", [])) or list(pattern.get("domains", [])) or [role_id]
        primary_domain = domains[0] if domains else role_id
        display_name = f"{employee_name} {role_id} {primary_domain} playbook"
        skill_name = self._normalize_skill_name(display_name)
        reflection_count = int(pattern.get("reflection_count", 0))
        sections = [
            f"# {display_name}",
            "",
            f"Learned playbook for **{employee_name}** in role `{role_id}` across {', '.join(domains)}.",
            f"Distilled from {reflection_count} project reflections.",
        ]
        if repeated_working:
            sections.extend(["", "## Successful Behaviors", *[f"- {item}" for item in repeated_working]])
        if repeated_checklists:
            sections.extend(["", "## Default Checklist", *[f"- {item}" for item in repeated_checklists]])
        if repeated_reviewer:
            sections.extend(["", "## Reviewer Preferences", *[f"- {item}" for item in repeated_reviewer]])
        if repeated_tools:
            sections.extend(["", "## Tool Preferences", *[f"- {item}" for item in repeated_tools]])
        if repeated_risks:
            sections.extend(["", "## Risk Watchouts", *[f"- {item}" for item in repeated_risks]])
        avoid_for = self._rank_items(record.get("avoid_domain_counts", {}), limit=4)
        if avoid_for:
            sections.extend(["", "## Avoid For", *[f"- {item}" for item in avoid_for]])

        skill = Skill(
            name=skill_name,
            description=(
                f"Learned playbook for `{role_id}` work ({', '.join(domains)}) by {employee_name}. "
                f"Use when assigning similar {role_id} tasks in these domains."
            ),
            metadata={
                "employee_id": employee_id,
                "employee_name": employee_name,
                "role_id": role_id,
                "template_id": reflection.get("template_id", ""),
                "pattern_key": pattern_key,
                "built_from": "project_reflections",
            },
            content="\n".join(sections).strip() + "\n",
        )
        self.skills.save_skill(skill, project_id=project_id)
        self._attach_learned_skill(profile, employee_id, pattern_key, skill.name)
        return skill.name

    @staticmethod
    def _normalize_skill_name(raw: str) -> str:
        """Normalize to lowercase hyphen-case, matching skill-creator conventions."""
        normalized = raw.strip().lower()
        normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
        normalized = normalized.strip("-")
        normalized = re.sub(r"-{2,}", "-", normalized)
        return normalized[:64]

    def _attach_learned_skill(
        self,
        profile: dict[str, Any],
        employee_id: str,
        pattern_key: str,
        skill_name: str,
    ) -> None:
        employees = profile.setdefault("employees", {})
        record = employees.setdefault(employee_id, {})
        refs = record.setdefault("learned_skill_refs", [])
        if skill_name not in refs:
            refs.append(skill_name)
        patterns = record.setdefault("patterns", {})
        pattern = patterns.setdefault(pattern_key, {})
        pattern["learned_skill_ref"] = skill_name
        record["updated_at"] = _utc_now()
        record.setdefault("delta_profile", {})

    def _reflection_path(self, project_id: str, employee_id: str, delivery_task_id: str) -> Path:
        return self.opc_home / "projects" / project_id / "employees" / employee_id / "reflections" / f"{delivery_task_id}.yaml"

    def _base_pattern_record(self, role_id: str, domains: list[str]) -> dict[str, Any]:
        return {
            "role_id": role_id,
            "domains": list(domains),
            "successes": 0,
            "partial_successes": 0,
            "failures": 0,
            "learned_skill_ref": "",
            "reflection_count": 0,
        }

    def _normalize_work_item_tasks(self, work_item_tasks: list[Any], delivery_task: Any) -> list[Any]:
        tasks_by_id: dict[str, Any] = {}
        for task in work_item_tasks:
            task_id = str(getattr(task, "id", "") or "").strip()
            if task_id:
                tasks_by_id[task_id] = task
        delivery_task_id = str(getattr(delivery_task, "id", "") or "").strip()
        if delivery_task_id:
            tasks_by_id[delivery_task_id] = delivery_task
        return list(tasks_by_id.values())

    def _collect_domains(self, tasks: list[Any], fallback: list[str]) -> list[str]:
        collected: list[str] = []
        for task in tasks:
            assignment = dict(getattr(task, "metadata", {}).get("employee_assignment", {}) or {})
            for value in list(assignment.get("domains", [])) + list(getattr(task, "tags", []) or []):
                item = str(value).strip()
                if item and item not in collected:
                    collected.append(item)
        for value in fallback:
            item = str(value).strip()
            if item and item not in collected:
                collected.append(item)
        return collected

    def _collect_task_summaries(self, tasks: list[Any]) -> list[str]:
        summaries: list[str] = []
        for task in tasks:
            for candidate in (
                str(getattr(task, "metadata", {}).get("work_item_summary_for_downstream", "") or "").strip(),
                str(getattr(task, "result", {}).get("content", "") or "").strip(),
                str(getattr(task, "title", "") or "").strip(),
            ):
                if candidate and candidate not in summaries:
                    summaries.append(candidate)
                    break
        return summaries

    def _collect_items(self, tasks: list[Any], key: str) -> list[str]:
        items: list[str] = []
        for task in tasks:
            for value in list(getattr(task, "metadata", {}).get(key, []) or []):
                text = str(value).strip()
                if text and text not in items:
                    items.append(text)
        return items

    def _collect_preferred_agents(self, tasks: list[Any], assignment: dict[str, Any]) -> list[str]:
        agents: list[str] = []
        assignment_agent = str(assignment.get("preferred_external_agent", "") or "").strip()
        if assignment_agent:
            agents.append(assignment_agent)
        for task in tasks:
            value = str(getattr(task, "assigned_external_agent", "") or "").strip()
            if value and value not in agents:
                agents.append(value)
        return agents

    def _infer_avoid_domains(self, risks: list[str]) -> list[str]:
        avoid: list[str] = []
        joined = " ".join(risks).lower()
        for token in ("security", "compliance", "billing", "finance", "production", "deployment"):
            if token in joined and token not in avoid:
                avoid.append(token)
        return avoid

    def _find_employee_feedback(self, employee_id: str, evaluation: dict[str, Any] | None) -> dict[str, Any]:
        if not evaluation:
            return {}
        for item in list(evaluation.get("employees", [])):
            if str(item.get("employee_id", "")).strip() == employee_id:
                return dict(item)
        return {}

    def _increment_outcome_counts(self, container: dict[str, Any], outcome: str) -> None:
        normalized = self._normalize_outcome(outcome)
        if normalized == "success":
            container["successes"] = int(container.get("successes", 0)) + 1
        elif normalized == "failure":
            container["failures"] = int(container.get("failures", 0)) + 1
        else:
            container["partial_successes"] = int(container.get("partial_successes", 0)) + 1

    def _normalize_outcome(self, outcome: str) -> str:
        normalized = str(outcome or "").strip().lower()
        if normalized in {"success", "approved", "fully_approved", "complete_success"}:
            return "success"
        if normalized in {"failure", "failed", "rejected", "fully_rejected"}:
            return "failure"
        return "partial_success"

    def _increment_counts(self, container: dict[str, Any], key: str, items: list[str]) -> None:
        counts = container.setdefault(key, {})
        for item in self._dedupe_preserve_order(items):
            counts[item] = int(counts.get(item, 0)) + 1

    def _build_delta_profile(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "working_patterns": self._rank_items(record.get("working_pattern_counts", {}), limit=5),
            "default_checklists": self._rank_items(record.get("checklist_counts", {}), limit=6),
            "reviewer_preferences": self._rank_items(record.get("reviewer_preference_counts", {}), limit=5),
            "risk_watchouts": self._rank_items(record.get("risk_watchout_counts", {}), limit=5),
            "tool_preferences": self._rank_items(record.get("tool_preference_counts", {}), limit=4),
            "fit_domains": self._rank_items(record.get("fit_domain_counts", {}), limit=4),
            "avoid_domains": self._rank_items(record.get("avoid_domain_counts", {}), limit=4),
        }

    def _normalize_self_evolution_event(
        self,
        event: dict[str, Any],
        source: dict[str, Any],
    ) -> dict[str, Any]:
        employee_id = str(event.get("employee_id", "") or "").strip()
        if not employee_id:
            return {}
        event_id = str(event.get("event_id", "") or "").strip()
        if not event_id:
            checkpoint_id = str(source.get("checkpoint_id", "") or "").strip()
            role_id = str(event.get("role_id", "") or "").strip()
            nonce = json.dumps(event, ensure_ascii=False, sort_keys=True)
            digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()[:12]
            event_id = self._safe_employee_filename(f"{checkpoint_id or _utc_now()}-{employee_id}-{role_id}-{digest}")
        return {
            "event_id": event_id,
            "employee_id": employee_id,
            "role_id": str(event.get("role_id", "") or "").strip(),
            "summary": str(event.get("summary", "") or "").strip(),
            "strengths": self._string_list(event.get("strengths", []), limit=8),
            "adjustments": self._string_list(event.get("adjustments", event.get("adjust", [])), limit=8),
            "avoid_next_time": self._string_list(event.get("avoid_next_time", event.get("avoid", [])), limit=8),
            "routing_notes": self._string_list(event.get("routing_notes", []), limit=8),
            "evidence_task_ids": self._string_list(event.get("evidence_task_ids", []), limit=12),
            "confidence": self._bounded_float(event.get("confidence", 0.7), default=0.7),
            "source": dict(source or {}),
            "created_at": _utc_now(),
        }

    def _build_self_evolution_delta(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        counts: dict[str, dict[str, int]] = {
            "strengths": {},
            "adjustments": {},
            "avoid_next_time": {},
            "routing_notes": {},
        }
        for event in events:
            if not isinstance(event, dict):
                continue
            for key in counts:
                for item in self._string_list(event.get(key, []), limit=20):
                    counts[key][item] = int(counts[key].get(item, 0)) + 1
        return {
            "strengths": self._rank_items(counts["strengths"], limit=8),
            "adjustments": self._rank_items(counts["adjustments"], limit=8),
            "avoid_next_time": self._rank_items(counts["avoid_next_time"], limit=8),
            "routing_notes": self._rank_items(counts["routing_notes"], limit=8),
        }

    @staticmethod
    def _safe_employee_filename(value: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip()).strip("-")
        return safe or "employee"

    @staticmethod
    def _bounded_float(value: Any, *, default: float = 0.0) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = default
        return max(0.0, min(1.0, number))

    @staticmethod
    def _string_list(value: Any, *, limit: int = 8) -> list[str]:
        items = value if isinstance(value, list) else [value]
        result: list[str] = []
        for item in items:
            text = str(item or "").strip()
            if text and text not in result:
                result.append(text)
            if len(result) >= limit:
                break
        return result

    def _rank_items(self, counts: dict[str, Any], *, limit: int = 6) -> list[str]:
        ranked = sorted(
            ((str(item).strip(), int(count)) for item, count in dict(counts).items() if str(item).strip()),
            key=lambda pair: (-pair[1], pair[0].lower()),
        )
        return [item for item, _count in ranked[:limit]]

    def _repeated_items(self, counts: dict[str, Any], *, minimum: int = 2) -> list[str]:
        ranked = sorted(
            ((str(item).strip(), int(count)) for item, count in dict(counts).items() if int(count) >= minimum and str(item).strip()),
            key=lambda pair: (-pair[1], pair[0].lower()),
        )
        return [item for item, _count in ranked[:6]]

    def _pattern_key(self, role_id: str, domains: list[str]) -> str:
        domain_key = ",".join(sorted({domain.strip().lower() for domain in domains if domain.strip()}))
        return f"{role_id}|{domain_key or 'general'}"

    def _dedupe_preserve_order(self, items: list[str]) -> list[str]:
        unique: list[str] = []
        for item in items:
            text = str(item).strip()
            if text and text not in unique:
                unique.append(text)
        return unique

    def _deep_merge(self, base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        merged = dict(base)
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = self._deep_merge(merged[key], value)
            elif isinstance(value, list) and isinstance(merged.get(key), list):
                existing = list(merged[key])
                for item in value:
                    if item not in existing:
                        existing.append(item)
                merged[key] = existing
            else:
                merged[key] = value
        return merged
