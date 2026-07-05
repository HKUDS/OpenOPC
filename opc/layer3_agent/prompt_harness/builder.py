"""Prompt harness builder for NativeAgent."""

from __future__ import annotations

from typing import Any

from opc.layer2_organization.prompt_contract import is_report_prompt_turn
from opc.layer2_organization.session_scoping import is_top_level_company_session

from .artifacts import build_runtime_artifact_manifest, render_runtime_artifact_messages
from .tool_strategy import NativeToolStrategyBuilder
from .types import PromptHarnessOutput, RuntimeArtifact


def _final_decider_role_id(task: Any) -> str:
    metadata = dict(getattr(task, "metadata", {}) or {})
    final_role = str(metadata.get("final_decider_role_id", "") or "").strip()
    if final_role:
        return final_role
    top_level = [str(item).strip() for item in list(metadata.get("top_level_role_ids", []) or []) if str(item).strip()]
    if len(top_level) == 1:
        return top_level[0]
    return ""


def _memory_skill_user_facing(task: Any, role_id: str) -> bool:
    metadata = dict(getattr(task, "metadata", {}) or {})
    execution_mode = str(metadata.get("execution_mode", "") or "").strip()
    if execution_mode != "company_mode":
        return True
    if is_report_prompt_turn(metadata):
        return False
    current_role = str(role_id or getattr(task, "assigned_to", "") or metadata.get("work_item_role_id", "") or "").strip()
    if not current_role or current_role != _final_decider_role_id(task):
        return False
    return bool(metadata.get("user_visible", False) or is_top_level_company_session(task))


def _execution_mode(task: Any) -> str | None:
    return str(getattr(task, "metadata", {}).get("execution_mode", "") or "").strip() or None


def _resume_content(runtime_resume: dict[str, Any]) -> str:
    lines = [
        f"- Runtime session: {str(runtime_resume.get('runtime_session_id', '') or '').strip()}",
        f"- Resume cursor: {runtime_resume.get('resume_cursor', '')}",
        f"- Active subagents: {len(runtime_resume.get('active_subagents', []) or [])}",
        f"- Permission requests: {len(runtime_resume.get('permission_requests', []) or [])}",
        f"- Task ledger items: {len(runtime_resume.get('task_ledger', []) or [])}",
    ]
    worktree_path = str(runtime_resume.get("worktree_path", "") or "").strip()
    if worktree_path:
        lines.append(f"- Worktree path: {worktree_path}")
    verification_verdict = str(runtime_resume.get("verification_verdict", "") or "").strip()
    if verification_verdict:
        lines.append(f"- Last verification: {verification_verdict}")
    return "Resume envelope:\n" + "\n".join(lines)


def _runtime_resume_payload(task: Any) -> dict[str, Any]:
    context_snapshot = getattr(task, "context_snapshot", {}) or {}
    if not isinstance(context_snapshot, dict):
        return {}
    raw_resume = context_snapshot.get("runtime_resume", {})
    return dict(raw_resume) if isinstance(raw_resume, dict) else {}


class PromptHarnessBuilder:
    """Build dynamic sections and boot artifact messages for NativeAgent."""

    def __init__(
        self,
        *,
        task: Any,
        role_id: str,
        config: Any,
        context_assembler: Any,
        preferences: Any,
        skills: Any,
        skill_refs: list[str] | None = None,
    ) -> None:
        self.task = task
        self.role_id = role_id
        self.config = config
        self.context_assembler = context_assembler
        self.preferences = preferences
        self.skills = skills
        self.skill_refs = list(skill_refs or [])

    async def build(
        self,
        *,
        system_prompt: str,
        allowed_tools: list[str] | None = None,
        runtime_policy_messages: list[dict[str, Any]] | None = None,
    ) -> PromptHarnessOutput:
        runtime_policy_messages = list(runtime_policy_messages or [])
        cfg = self.config.system.native_runtime.prompt_harness
        if not cfg.enabled:
            return PromptHarnessOutput(
                system_prompt=system_prompt,
                runtime_policy_messages=runtime_policy_messages,
            )

        workspace_context_messages: list[dict[str, str]] = []
        dynamic_section_ids: list[str] = []
        if cfg.split_static_dynamic:
            assembled_ctx = await self.context_assembler.build_system_context(self.task, role_id=self.role_id)
            if assembled_ctx:
                workspace_context_messages.append({"role": "system", "content": assembled_ctx})
                dynamic_section_ids.append("assembled_context")
            # employee_delta_context is already rendered inside the
            # unified Self section produced by
            # ``ContextAssembler._build_self_section`` (which is
            # included in ``assembled_ctx`` above). Emitting it a
            # second time here would duplicate the delta profile in
            # the prompt, so we rely solely on the assembled context.

        artifacts: list[RuntimeArtifact] = []
        if cfg.artifact_messages_enabled:
            execution_mode = _execution_mode(self.task)
            tool_surface = NativeToolStrategyBuilder(
                list(allowed_tools or []),
                company_mode=execution_mode == "company_mode",
            ).render()
            artifacts.append(RuntimeArtifact(
                artifact_type="tool_surface_delta",
                title="Tool Strategy",
                content=tool_surface,
                metadata={"allowed_tools": sorted(list(allowed_tools or []))},
            ))
            skills_summary = str(
                self.skills.build_skills_summary(
                    self.task.project_id,
                    execution_mode=execution_mode,
                    role_id=self.role_id,
                    user_facing=_memory_skill_user_facing(self.task, self.role_id),
                    final_decider_role_id=_final_decider_role_id(self.task),
                    skill_refs=self.skill_refs,
                )
                or ""
            ).strip()
            if skills_summary:
                artifacts.append(RuntimeArtifact(
                    artifact_type="skills_delta",
                    title="Skills",
                    content=skills_summary,
                    metadata={"project_id": self.task.project_id or "default"},
                ))
            resident_assignment = dict(self.task.context_snapshot.get("resident_assignment", {}) or self.task.metadata.get("resident_assignment", {}) or {})
            team_memory_digest = str(resident_assignment.get("team_memory_digest", "") or "").strip()
            if team_memory_digest:
                artifacts.append(RuntimeArtifact(
                    artifact_type="team_memory_delta",
                    title="Team Memory",
                    content=team_memory_digest,
                    metadata={"assignment_id": resident_assignment.get("assignment_id", "")},
                ))
            runtime_resume = _runtime_resume_payload(self.task)
            if runtime_resume:
                artifacts.append(RuntimeArtifact(
                    artifact_type="resume_state",
                    title="Resume State",
                    content=_resume_content(runtime_resume),
                    metadata={"runtime_session_id": runtime_resume.get("runtime_session_id", "")},
                ))

        previous_hashes = dict((self.task.metadata.get("prompt_harness", {}) or {}).get("artifact_hashes", {}) or {})
        artifact_messages = render_runtime_artifact_messages(
            artifacts,
            previous_hashes=previous_hashes,
            emit_delta_messages=cfg.emit_delta_messages,
        )
        artifact_manifest, artifact_hashes = build_runtime_artifact_manifest(artifacts)
        return PromptHarnessOutput(
            system_prompt=system_prompt,
            runtime_policy_messages=runtime_policy_messages,
            workspace_context_messages=workspace_context_messages,
            dynamic_messages=workspace_context_messages,
            artifact_messages=artifact_messages,
            static_section_ids=["system_prompt"],
            dynamic_section_ids=dynamic_section_ids,
            artifact_manifest=artifact_manifest,
            artifact_hashes=artifact_hashes,
        )
