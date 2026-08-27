"""Lightweight task context assembly for all execution modes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opc.core.company_tools import (
    MULTI_TEAM_COORDINATION_TURN_MODES,
    company_collaboration_enabled,
    company_collaboration_enabled_for_task,
    resolve_company_turn_mode,
)
from opc.core.models import Task, TaskStatus
from opc.layer2_organization import comms as _comms
from opc.layer2_organization.collaboration_policy import render_ownership_contract
from opc.layer2_organization.prompt_contract import (
    is_report_prompt_turn,
    normalize_prompt_contract,
    normalize_prompt_text_list,
    render_assignment_context_from_contract,
)
from opc.layer2_organization.turn_mode import (
    TurnMode,
    authoritative_gate_harness_rework_metadata,
    has_gate_harness_rework,
    infer_turn_mode,
    safe_rework_count,
)
from opc.layer2_organization.work_item_context_view import WorkItemContextView
from opc.layer2_organization.work_item_identity import turn_type_for_task
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task
from opc.layer2_organization.output_contract import (
    output_contract_metadata,
    render_output_contract_context,
)
from opc.layer4_tools.output_budget import clip_text


@dataclass
class ExternalContextLayers:
    """Structured context buckets for external agent prompt envelopes."""

    primary_task_brief: str = ""
    openopc_context: str = ""
    attachments_state_context: str = ""
    company_runtime_context: str = ""
    prepared_mailbox_context: str = ""
    recovery_context: str = ""

    def as_legacy_text(self) -> str:
        return "\n\n".join(
            part
            for part in (
                self.openopc_context,
                self.company_runtime_context,
                self.recovery_context,
                self.prepared_mailbox_context,
                self.attachments_state_context,
            )
            if str(part or "").strip()
        )


@dataclass
class PromptAssignmentView:
    """Prompt-facing assignment shape with one owner per information layer."""

    primary_task_brief: str = ""
    upstream_intent_summary: str = ""
    manager_planning_handoff: str = ""
    manager_outcome_dispatch: bool = False
    owned_outcome_kind: str = "execute"
    scope_key: str = ""
    deliverables: list[str] | None = None
    acceptance_criteria: list[str] | None = None
    dependency_specs: list[str] | None = None
    coordination_notes: str = ""
    delegation_rationale: str = ""
    non_overlap_guard: str = ""


def _join_context_parts(parts: list[str]) -> str:
    return "\n\n".join(str(part or "").strip() for part in parts if str(part or "").strip())


def _demote_embedded_headings(text: str, *, min_level: int = 4) -> str:
    """Keep embedded Markdown fragments from escaping their parent section."""
    min_level = max(int(min_level or 4), 1)
    normalized_lines: list[str] = []
    for raw_line in str(text or "").strip().splitlines():
        stripped = raw_line.lstrip()
        indent = raw_line[: len(raw_line) - len(stripped)]
        hash_count = len(stripped) - len(stripped.lstrip("#"))
        if (
            hash_count > 0
            and hash_count < min_level
            and len(stripped) > hash_count
            and stripped[hash_count] == " "
        ):
            normalized_lines.append(f"{indent}{'#' * min_level}{stripped[hash_count:]}")
        else:
            normalized_lines.append(raw_line)
    return "\n".join(normalized_lines).strip()


def _split_core_context(core_context: str) -> tuple[str, str]:
    core_context = str(core_context or "").strip()
    if not core_context:
        return "", ""
    marker = "\n\n## Runtime State\n"
    if marker in core_context:
        openopc_context, runtime_state = core_context.split(marker, 1)
        return openopc_context.strip(), ("## Runtime State\n" + runtime_state).strip()
    if core_context.startswith("## Runtime State\n"):
        return "", core_context
    return core_context, ""


def _latest_user_directive_from_task(task: Task) -> str:
    snapshot = dict(getattr(task, "context_snapshot", {}) or {})
    metadata = dict(getattr(task, "metadata", {}) or {})
    return str(
        snapshot.get("user_supplied_input")
        or metadata.get("latest_user_directive")
        or metadata.get("manager_mutation_user_input")
        or metadata.get("user_supplied_input")
        or ""
    ).strip()


class ContextAssembler:
    """Build minimal, mode-aware context without forcing a heavy schema."""

    def __init__(
        self,
        memory: Any,
        store: Any | None = None,
        communication: Any | None = None,
    ) -> None:
        self.memory = memory
        self.store = store
        self.communication = communication

    async def build_sections(self, task: Task, role_id: str = "") -> dict[str, str]:
        mode = str(task.metadata.get("execution_mode", "") or "")
        sections = {
            "turn_mode": await self.build_turn_mode_context(task),
            "assignment": await self.build_work_item_assignment_context(task),
            "rework_feedback": await self.build_rework_feedback_context(task),
            "core": await self.build_core_context(task),
            "member": self.build_member_session_context(task),
            "team_memory": self.build_team_memory_context(task),
            "attachments": self.build_attachment_context(task),
            "dependency": "",
            "reference": "",
            "collaboration": "",
            "recovery": self.build_recovery_context(task),
            "team_deliverables": "",
        }
        if task.dependencies or mode in {"multi_agent", "company_mode"}:
            sections["dependency"] = await self.build_dependency_context(task)
        if mode in {"company_mode", "multi_agent"}:
            sections["reference"] = await self.build_role_reference_context(task, role_id=role_id)
        if company_collaboration_enabled(mode):
            sections["collaboration"] = await self.build_collaboration_context(task, role_id=role_id)
        sections["team_deliverables"] = await self.build_team_deliverables_context(task)
        return sections

    async def _load_task_work_item(self, task: Task) -> Any:
        """Fetch the DelegationWorkItem linked to ``task`` (if any).

        Small helper to avoid scattered try/except blocks in mode-aware
        section builders. Returns None on any miss (no store, missing
        id, store error) so callers can short-circuit cleanly.
        """
        store = self.store
        if store is None:
            return None
        work_item_id = linked_work_item_id_for_task(task)
        if not work_item_id:
            return None
        get_work_item = getattr(store, "get_delegation_work_item", None)
        if get_work_item is None:
            return None
        try:
            return await get_work_item(work_item_id)
        except Exception:
            return None

    async def build_work_item_assignment_context(self, task: Task) -> str:
        """Render assignment metadata whose owner is not ``## Task Brief``.

        Company-mode prompts use source-level ownership instead of
        final-string de-duplication:

        * the current work-item brief is rendered only by ``## Task Brief``;
        * this section renders durable assignment metadata such as upstream
          intent, deliverables, acceptance criteria, dependencies, and
          boundaries.
        """
        if str((task.metadata or {}).get("runtime_model", "") or "").strip() != "multi_team_org":
            return ""
        if not linked_work_item_id_for_task(task):
            return ""
        contract = await self._prompt_contract_for_task(task)
        if not contract:
            return ""
        include_dispatch_fields = await self._is_dispatch_prompt_turn(task)
        return render_assignment_context_from_contract(
            contract,
            include_dispatch_fields=include_dispatch_fields,
        )

    async def build_prompt_assignment_view(self, task: Task) -> PromptAssignmentView:
        """Build the company prompt assignment view from source metadata.

        This is the source-of-truth shape for company prompt composition. It
        intentionally decides which source owns each information layer rather
        than rendering a broad packet and removing duplicates afterwards.
        """
        contract = await self._prompt_contract_for_task(task)
        assignment = dict(contract.get("assignment_context", {}) or {}) if contract else {}
        return PromptAssignmentView(
            primary_task_brief=str(contract.get("task_brief", "") or "").strip() if contract else "",
            upstream_intent_summary=str(assignment.get("upstream_intent_summary", "") or "").strip(),
            manager_planning_handoff=str(assignment.get("manager_planning_handoff", "") or "").strip(),
            manager_outcome_dispatch=bool(assignment.get("manager_outcome_dispatch", False)),
            owned_outcome_kind=str(assignment.get("owned_outcome_kind", "execute") or "execute").strip(),
            scope_key=str(assignment.get("scope_key", "") or "").strip(),
            deliverables=normalize_prompt_text_list(assignment.get("deliverables", [])),
            acceptance_criteria=normalize_prompt_text_list(assignment.get("acceptance_criteria", [])),
            dependency_specs=normalize_prompt_text_list(assignment.get("dependency_specs", [])),
            coordination_notes=str(assignment.get("coordination_notes", "") or "").strip(),
            delegation_rationale=str(assignment.get("delegation_rationale", "") or "").strip(),
            non_overlap_guard=str(assignment.get("non_overlap_guard", "") or "").strip(),
        )

    async def _prompt_contract_for_task(self, task: Task) -> dict[str, Any]:
        work_item = await self._load_task_work_item(task)
        work_meta = dict(getattr(work_item, "metadata", {}) or {}) if work_item is not None else {}
        contract = work_meta.get("prompt_contract")
        if not contract:
            contract = dict(task.metadata or {}).get("prompt_contract")
        normalized = normalize_prompt_contract(contract)
        if not str(normalized.get("task_brief", "") or "").strip() and not any(
            dict(normalized.get("assignment_context", {}) or {}).values()
        ):
            return {}
        return normalized

    async def _is_dispatch_prompt_turn(self, task: Task) -> bool:
        current = str(resolve_company_turn_mode(task) or "").strip()
        if current:
            return current in {"manager_decide", "dispatch_required", "delegate"}
        return await self._infer_turn_mode(task) == TurnMode.DELEGATE

    def _is_manager_capable_task(self, task: Task, *, work_meta: dict[str, Any] | None = None) -> bool:
        metadata = dict(task.metadata or {})
        work_meta = dict(work_meta or {})
        if bool(work_meta.get("manager_outcome_dispatch", False)):
            return True
        if any(
            list(metadata.get(key, []) or []) or list(work_meta.get(key, []) or [])
            for key in ("direct_report_seat_ids", "allowed_delegate_role_ids", "direct_report_role_ids")
        ):
            return True
        if str(metadata.get("managed_team_id", "") or work_meta.get("managed_team_id", "") or "").strip():
            return True
        topology = dict(metadata.get("runtime_topology", {}) or {})
        seats = [
            dict(item)
            for item in list(topology.get("seats", []) or [])
            if isinstance(item, dict)
        ]
        current_seat_id = str(metadata.get("delegation_seat_id", "") or metadata.get("seat_id", "") or "").strip()
        current_role_id = str(task.assigned_to or metadata.get("work_item_role_id", "") or "").strip()
        current_seat: dict[str, Any] = {}
        for seat in seats:
            if current_seat_id and str(seat.get("seat_id", "") or "").strip() == current_seat_id:
                current_seat = seat
                break
        if not current_seat and current_role_id:
            for seat in seats:
                if str(seat.get("role_id", "") or "").strip() == current_role_id:
                    current_seat = seat
                    break
        if current_seat:
            if any(list(current_seat.get(key, []) or []) for key in ("direct_report_seat_ids", "allowed_delegate_role_ids", "direct_report_role_ids")):
                return True
            if str(current_seat.get("managed_team_id", "") or "").strip():
                return True
        return False

    @staticmethod
    def _normalize_assignment_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, (list, tuple, set)):
            rendered_items: list[str] = []
            for item in value:
                if isinstance(item, dict):
                    rendered = str(
                        item.get("value", "")
                        or item.get("input", "")
                        or item.get("raw", "")
                        or item.get("work_item_id", "")
                        or ""
                    ).strip()
                else:
                    rendered = str(item).strip()
                if rendered:
                    rendered_items.append(rendered)
            return rendered_items
        rendered = str(value).strip()
        return [rendered] if rendered else []

    async def _infer_turn_mode(self, task: Task) -> TurnMode:
        """Classify the dispatch context for ``task``.

        Reads the work_item from the store and delegates to the
        stateless ``infer_turn_mode``. Returns ``TurnMode.EXECUTE``
        as a safe default when the work item can't be loaded.
        """
        task_metadata = dict(task.metadata or {})
        task_turn_type = turn_type_for_task(task, fallback="")
        current_turn_mode = resolve_company_turn_mode(task)
        is_report_entry = bool(
            task_metadata.get("report_execution_work_item", False)
            or task_turn_type == "report"
            or current_turn_mode == "report_required"
        )
        is_review_entry = bool(
            task_metadata.get("review_execution_work_item", False)
            or task_metadata.get("review_task", False)
            or task_turn_type == "review"
            or current_turn_mode == "review_execute"
        )
        # Task projection identity is the current execution envelope.  Report
        # stays above review, matching ``infer_turn_mode`` priority.
        if is_report_entry:
            return TurnMode.REPORT
        work_item = await self._load_task_work_item(task)
        if work_item is None:
            if is_review_entry:
                return TurnMode.REVIEW
            return (
                TurnMode.REWORK
                if has_gate_harness_rework(task_metadata)
                else TurnMode.EXECUTE
            )
        return infer_turn_mode(
            work_item,
            is_review_entry=is_review_entry,
            supplemental_metadata=task.metadata,
        )

    async def build_turn_mode_context(self, task: Task) -> str:
        """Render a compact ``## Turn Mode`` header so the agent knows
        both the scheduler's raw turn state and the action expected from it.

        Only emitted for multi_team_org tasks; plain tasks have no
        turn-mode concept.
        """
        if str((task.metadata or {}).get("runtime_model", "") or "").strip() != "multi_team_org":
            return ""
        turn_type = turn_type_for_task(task, fallback="")
        if turn_type == "self_evolution":
            retry_feedback = str(
                (task.context_snapshot or {}).get("self_evolution_patch_retry_feedback", "")
                or (task.metadata or {}).get("self_evolution_patch_retry_feedback", "")
                or ""
            ).strip()
            lines = [
                "## Turn Mode",
                "- Runtime state: `self_evolution`",
                "- Required action: update employee experience only. Do not continue the user delivery, edit files, or produce a user-facing report.",
                "- If direct reports should learn from this review, create child `self_evolution` WorkItems with `delegate_work`.",
                "- Final response must be strict JSON only: `{ \"patches\": [...] }`.",
            ]
            if retry_feedback:
                lines.append(f"- Retry feedback: {retry_feedback}")
            return "\n".join(lines)
        opaque_external_team = (
            str((task.metadata or {}).get("execution_unit_kind", "") or "").strip()
            == "opaque_external_team"
        )
        if opaque_external_team:
            return ""
        raw_turn_mode = str(resolve_company_turn_mode(task) or "").strip()
        mode = await self._infer_turn_mode(task)
        guidance = {
            TurnMode.EXECUTE: "Complete the current WorkItem according to its assigned execution scope.",
            TurnMode.DELEGATE: (
                "Assess the WorkItem and choose whether to delegate outcome-based child WorkItems "
                "or complete it within this leader-owned WorkItem. Delegate only when the split "
                "improves capability fit, parallelism, or independent review."
            ),
            TurnMode.REVIEW: (
                "A subordinate has handed back a deliverable. Inspect it, "
                "decide approve / rework, and emit a structured verdict. "
                "Do not restart the subtask yourself."
            ),
            TurnMode.INTEGRATE: (
                "Your team's subtasks are all approved. Integrate their "
                "outputs into this work item's final deliverable. Do NOT "
                "delegate again unless there is a remedial gap."
            ),
            TurnMode.REWORK: (
                "Your previous output was rejected by review or failed a "
                "deterministic gate. Address every item in the first-class "
                "rework feedback section and "
                "resubmit. Do not restart from scratch unless the "
                "feedback explicitly says so."
            ),
            TurnMode.REPORT: (
                "Your execution is complete. Produce a structured "
                "handoff report on this turn — do NOT do new work."
            ),
        }
        runtime_guidance = {
            "worker_execute": (
                "execute",
                guidance[TurnMode.EXECUTE],
            ),
            "manager_decide": (
                "decide",
                "Assess the WorkItem and choose whether to delegate outcome-based child WorkItems "
                "or complete it within this leader-owned WorkItem. Delegate only when the split "
                "improves capability fit, parallelism, or independent review.",
            ),
            # Persisted sessions may still carry the former state name. Its
            # semantics are upgraded in place; it no longer mandates dispatch.
            "dispatch_required": (
                "decide",
                "Assess the WorkItem and choose whether to delegate outcome-based child WorkItems "
                "or complete it within this leader-owned WorkItem. Delegate only when the split "
                "improves capability fit, parallelism, or independent review.",
            ),
            "monitor_children": (
                "monitor",
                "Inspect child WorkItem state and address blockers or pending coordination; "
                "do not redo work assigned to a child.",
            ),
            "review_pending": (
                "review",
                "Review pending child submissions and record approve or rework verdicts before continuing.",
            ),
            "review_execute": (
                "review",
                guidance[TurnMode.REVIEW],
            ),
            "synthesize_required": (
                "integrate",
                guidance[TurnMode.INTEGRATE],
            ),
            "deliver_required": (
                "deliver",
                "Produce the owner-facing delivery from approved and integrated results; "
                "reopen delegation only for a concrete remedial gap.",
            ),
            "report_required": (
                "report",
                guidance[TurnMode.REPORT],
            ),
        }
        # ``current_turn_mode`` is the Company runtime's authoritative
        # scheduler decision.  WorkItem inference remains the compatibility
        # fallback and the source for first-class rework, whose correction
        # requirement must not be hidden by a stale execute/manager state.
        if mode == TurnMode.REWORK:
            action_name = mode.value
            action_guidance = guidance[mode]
        elif raw_turn_mode in runtime_guidance:
            action_name, action_guidance = runtime_guidance[raw_turn_mode]
        elif mode == TurnMode.DELEGATE:
            # Unstamped/legacy manager tasks use the inferred enum. The enum's
            # historical name describes available delegation, not a mandate.
            action_name = "decide"
            action_guidance = guidance[mode]
        else:
            action_name = mode.value
            action_guidance = guidance.get(mode, guidance[TurnMode.EXECUTE])
        action = f"{action_name} — {action_guidance}"
        lines = []
        if raw_turn_mode:
            lines.append(f"- Runtime state: `{raw_turn_mode}`")
        lines.append(f"- Required action: {action}")
        return "## Turn Mode\n" + "\n".join(lines)

    async def build_rework_feedback_context(
        self,
        task: Task,
        *,
        include_previous_submission: bool = True,
    ) -> str:
        """Inject review or deterministic-gate failures on a rework turn.

        Without this block the agent's next attempt has no prominent,
        actionable explanation of why the previous output was rejected and
        can repeat the same mistake or merely re-verify an unchanged artifact.

        Gating is by the review or gate-harness metadata trail, NOT by phase:
        the dispatcher transitions the work item to RUNNING before the prompt
        is assembled, so a phase==READY_FOR_REWORK gate would silently swallow
        the feedback.

        Reads the work item fresh from the store for manager-review feedback.
        Controller-owned deterministic validation fields live on the Task, so
        their presence selects the Task group authoritatively; the WorkItem is
        only a compatibility fallback when the Task has no gate fields.
        """
        if bool((task.metadata or {}).get("suppress_company_rework_feedback_context", False)):
            return ""
        # Review/report auxiliary cards can inherit stale gate fields from the
        # business WorkItem they inspect.  Their canonical mode wins: gate
        # correction instructions belong only to the execute/rework owner and
        # must not be exposed as the auxiliary card's assignment.
        actual_mode = await self._infer_turn_mode(task)
        if actual_mode in {TurnMode.REVIEW, TurnMode.REPORT}:
            return ""
        work_metadata: dict[str, Any] = {}
        work_item = await self._load_task_work_item(task)
        if work_item is not None:
            work_metadata = dict(getattr(work_item, "metadata", {}) or {})
        if not work_metadata:
            work_metadata = dict(task.metadata or {})
        task_metadata = dict(task.metadata or {})
        feedback = str(work_metadata.get("rework_feedback", "") or "").strip()
        if not feedback:
            feedback = str(task_metadata.get("rework_feedback", "") or "").strip()

        # Task-owned controller state is authoritative as one group, even when
        # it explicitly contains empty/zero values.  Only a Task with no gate
        # keys at all may fall back to a WorkItem projection.
        gate_metadata = authoritative_gate_harness_rework_metadata(
            task_metadata=task_metadata,
            work_item_metadata=work_metadata,
        )
        raw_gate_request = gate_metadata.get("gate_harness_rework_request", {})
        gate_request = (
            dict(raw_gate_request)
            if isinstance(raw_gate_request, dict)
            else {}
        )

        gate_feedback = str(gate_request.get("feedback", "") or "").strip()
        if not gate_feedback:
            gate_feedback = str(
                gate_metadata.get("gate_harness_rework_feedback", "")
                or ""
            ).strip()
        gate_rework_count = max(
            safe_rework_count(
                gate_metadata.get("gate_harness_rework_count", 0)
            ),
            safe_rework_count(gate_request.get("rework_round", 0)),
        )
        gate_signal = has_gate_harness_rework(gate_metadata)
        if not feedback and not gate_signal:
            return ""

        # A manager-review rejection is the immediate cause when both trails
        # exist; gate fields intentionally remain durable across attempts.
        # Otherwise promote deterministic validation to its own unambiguous
        # rework block instead of burying it in the generic working summary.
        if not feedback and gate_signal:
            blockers = self._safe_rework_text_items(
                gate_request.get("blockers", [])
            )
            constraints = self._safe_rework_text_items(
                gate_request.get("constraints", [])
            )
            lines = [
                "## Deterministic Gate Feedback (Rework Required)",
                "",
                "Your previous submission FAILED deterministic validation. "
                "This is a correction turn, not a request to re-verify or "
                "resubmit unchanged output.",
                "",
            ]
            if gate_rework_count > 0:
                lines.append(f"Rework attempt: #{gate_rework_count}")
            requested_by = str(
                gate_request.get("source_work_item_title", "") or ""
            ).strip()
            if requested_by:
                lines.append(f"Requested by: {requested_by}")
            if gate_rework_count > 0 or requested_by:
                lines.append("")
            if gate_feedback:
                lines.append("### Deterministic Validation Failure")
                lines.append(gate_feedback)
            if blockers:
                lines.append("")
                lines.append("### Blocking Issues (must fix before resubmission)")
                lines.extend(f"- {item}" for item in blockers[:12])
            if constraints:
                lines.append("")
                lines.append("### Constraints To Preserve")
                lines.extend(f"- {item}" for item in constraints[:12])
            lines.extend(
                [
                    "",
                    "### Required Corrective Action",
                    "- Modify the owned deliverable(s) so every blocking issue above is actually fixed.",
                    "- Do not claim success based only on syntax checks, file existence, or an unchanged artifact.",
                    "- Re-run every task-specific evidence and verification step required for the corrected content.",
                    "- Read back the corrected output and confirm each blocker is absent before resubmitting.",
                ]
            )
            previous = (
                self._previous_submission_excerpt(task)
                if include_previous_submission
                else ""
            )
            if previous:
                lines.extend(
                    [
                        "",
                        "### Your Previous Submission (excerpt)",
                        "The deterministic failure above refers to this prior output. Do not repeat it unchanged.",
                        "",
                        previous,
                    ]
                )
            return "\n".join(lines).rstrip() + "\n"

        verdict = dict(work_metadata.get("structured_review_verdict", {}) or {})
        if not verdict:
            verdict = dict(task_metadata.get("structured_review_verdict", {}) or {})
        rework_count = safe_rework_count(
            work_metadata.get("review_rework_count", 0)
            or task_metadata.get("review_rework_count", 0)
        )
        reviewer_role = str(work_metadata.get("review_owner_role_id", "") or "").strip()
        lines: list[str] = [
            "## Reviewer Feedback (Rework Required)",
            "",
            "Your previous turn was REJECTED. Address each point below and",
            "resubmit. Do not restart from scratch unless the feedback",
            "explicitly says so.",
            "",
        ]
        if reviewer_role:
            lines.append(f"Reviewer: {reviewer_role}")
        if rework_count > 0:
            lines.append(f"Rework attempt: #{rework_count}")
        if reviewer_role or rework_count > 0:
            lines.append("")
        lines.append("### Reviewer's Reject Reason")
        lines.append(feedback)
        blocking = [
            str(item).strip()
            for item in list(verdict.get("blocking_issues", []) or [])
            if str(item).strip()
        ]
        followups = [
            str(item).strip()
            for item in list(verdict.get("followups", []) or [])
            if str(item).strip()
        ]
        if blocking:
            lines.append("")
            lines.append("### Blocking Issues (must fix before approval)")
            for item in blocking[:12]:
                lines.append(f"- {item}")
        if followups:
            lines.append("")
            lines.append("### Follow-ups (nice-to-have, non-blocking)")
            for item in followups[:12]:
                lines.append(f"- {item}")
        previous = (
            self._previous_submission_excerpt(task)
            if include_previous_submission
            else ""
        )
        if previous:
            lines.append("")
            lines.append("### Your Previous Submission (excerpt)")
            lines.append(
                "This is what you delivered last turn. The reviewer's reject "
                "reason above refers to this output — do NOT repeat it; address "
                "the gaps the reviewer named."
            )
            lines.append("")
            lines.append(previous)
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _safe_rework_text_items(value: Any) -> list[str]:
        """Normalize trusted text collections without iterating scalars."""

        if isinstance(value, str):
            rendered = value.strip()
            return [rendered] if rendered else []
        if not isinstance(value, (list, tuple, set)):
            return []
        rendered_items: list[str] = []
        for item in value:
            if isinstance(item, (dict, list, tuple, set)):
                continue
            rendered = str(item or "").strip()
            if rendered:
                rendered_items.append(rendered)
        return rendered_items

    @staticmethod
    def _previous_submission_excerpt(task: Task, *, budget: int = 1200) -> str:
        """Best-effort excerpt of the agent's prior turn output.

        Used only by ``build_rework_feedback_context`` to remind the
        agent what it just produced, so feedback like "missing file X"
        lands against a concrete prior deliverable rather than thin
        air. Truncated to ``budget`` chars.
        """
        result = getattr(task, "result", None)
        content = ""
        if isinstance(result, dict):
            content = str(result.get("content", "") or "").strip()
        elif result is not None:
            content = str(getattr(result, "content", "") or "").strip()
        if not content:
            return ""
        if len(content) <= budget:
            return content
        return content[:budget].rstrip() + "\n… (truncated)"

    async def build_system_context(self, task: Task, role_id: str = "") -> str:
        sections = await self.build_sections(task, role_id=role_id)
        return "\n\n".join(part for part in sections.values() if part)

    async def build_external_context(self, task: Task, role_id: str = "") -> str:
        return (await self.build_external_context_layers(task, role_id=role_id)).as_legacy_text()

    async def build_external_context_layers(self, task: Task, role_id: str = "") -> ExternalContextLayers:
        sections = await self.build_sections(task, role_id=role_id)
        prompt_assignment = await self.build_prompt_assignment_view(task)
        if role_id:
            sections["reference"] = sections["reference"] or await self.build_role_reference_context(task, role_id=role_id)
            if company_collaboration_enabled_for_task(task):
                sections["collaboration"] = sections["collaboration"] or await self.build_collaboration_context(task, role_id=role_id)
        core_openopc, runtime_state = _split_core_context(sections.get("core", ""))
        mode = str(task.metadata.get("execution_mode", "") or "")
        recovery_context = sections.get("recovery", "")
        attachments_state_context = _join_context_parts([
            runtime_state,
            sections.get("attachments", ""),
        ])
        prepared_mailbox_context = self.build_external_runtime_mailbox_context(task, role_id=role_id)
        if mode in {"company_mode", "multi_agent"}:
            if is_report_prompt_turn(task.metadata):
                return ExternalContextLayers(
                    primary_task_brief=prompt_assignment.primary_task_brief,
                    attachments_state_context=attachments_state_context,
                    company_runtime_context=_join_context_parts([
                        sections.get("turn_mode", ""),
                    ]),
                    prepared_mailbox_context="",
                    recovery_context=recovery_context,
                )
            return ExternalContextLayers(
                primary_task_brief=prompt_assignment.primary_task_brief,
                attachments_state_context=attachments_state_context,
                company_runtime_context=_join_context_parts([
                    sections.get("turn_mode", ""),
                    sections.get("reference", ""),
                    sections.get("assignment", ""),
                    sections.get("rework_feedback", ""),
                    core_openopc,
                    sections.get("member", ""),
                    sections.get("team_memory", ""),
                    sections.get("dependency", ""),
                    sections.get("collaboration", ""),
                    sections.get("team_deliverables", ""),
                ]),
                prepared_mailbox_context=prepared_mailbox_context,
                recovery_context=recovery_context,
            )
        return ExternalContextLayers(
            primary_task_brief=prompt_assignment.primary_task_brief,
            openopc_context=_join_context_parts([
                core_openopc,
                sections.get("turn_mode", ""),
                sections.get("assignment", ""),
                sections.get("rework_feedback", ""),
                sections.get("team_memory", ""),
                sections.get("dependency", ""),
                sections.get("reference", ""),
                sections.get("collaboration", ""),
                sections.get("team_deliverables", ""),
            ]),
            attachments_state_context=_join_context_parts([
                attachments_state_context,
                recovery_context,
            ]),
            prepared_mailbox_context=prepared_mailbox_context,
        )

    def build_task_brief(self, task: Task) -> str:
        original = task.metadata.get("original_message", "")
        is_subtask = bool(original and task.description != original)
        multi_team_org = str(task.metadata.get("runtime_model", "") or "").strip() == "multi_team_org"
        turn_mode = resolve_company_turn_mode(task)
        managerish = turn_mode in MULTI_TEAM_COORDINATION_TURN_MODES
        latest_user_directive = _latest_user_directive_from_task(task)
        rendered_latest_directive = False
        parts: list[str] = []
        work_item_assignment = dict(task.metadata.get("work_item_assignment", {}) or {})
        if work_item_assignment and not multi_team_org:
            parts.append(f"## Global Intent Summary\n{work_item_assignment.get('global_intent_summary', '')}")
            parts.append(f"## Your Responsibility\n{work_item_assignment.get('your_responsibility', '')}")
            for section, key in (
                ("Out of Scope", "out_of_scope"),
                ("Inputs", "inputs"),
                ("Deliverables", "deliverables"),
                ("Acceptance Criteria", "acceptance_criteria"),
            ):
                items = [str(item).strip() for item in work_item_assignment.get(key, []) if str(item).strip()]
                if items:
                    parts.append(f"## {section}\n" + "\n".join(f"- {item}" for item in items))
        elif is_subtask:
            if latest_user_directive:
                latest_preview = clip_text(
                    latest_user_directive,
                    limit=1600,
                    marker="latest user directive truncated",
                ).text
                parts.append(
                    "## Latest User Directive (AUTHORITATIVE)\n"
                    "Use this as the current source of truth. It supersedes conflicting details from the original request.\n\n"
                    f"{latest_preview}"
                )
                rendered_latest_directive = True
                parts.append(f"## Original User Request (background; superseded if conflicting)\n{original}")
                parts.append(f"## Your Current Work Item: {task.title}\n{task.description}")
            else:
                parts.append(f"## Original User Request\n{original}")
                parts.append(f"## Your Sub-task: {task.title}\n{task.description}")
        else:
            parts.append(f"## Task\n{task.description or task.title}")
        # work_item_projection_title redundant label dropped; task.title is shown above.
        if multi_team_org:
            lines = ["## Organization Runtime Turn", f"Current work item: {str(task.title or task.id).strip()}"]
            current_team = str(task.metadata.get("delegation_team_id", "") or "").strip()
            if current_team:
                lines.append(f"Current team: {current_team}")
            current_seat = str(task.metadata.get("delegation_seat_id", "") or "").strip()
            if current_seat:
                lines.append(f"Current seat: {current_seat}")
            parts.append("\n".join(lines))
        # Member session state is rendered exclusively via the
        # ``build_sections`` path (Runtime State bucket). We do NOT
        # call ``build_member_session_context`` here — doing so
        # would duplicate the member session block in the native
        # agent's prompt (once in the user message via
        # ``build_task_brief`` and again in the system context via
        # ``build_sections``).
        resident_assignment = dict(task.metadata.get("resident_assignment", {}) or task.context_snapshot.get("resident_assignment", {}) or {})
        if resident_assignment:
            assignment_lines = []
            manager_role = str(resident_assignment.get("manager_role_id", "") or "").strip()
            if manager_role:
                assignment_lines.append(f"- Manager Mailroom: {manager_role}")
            assignment_id = str(resident_assignment.get("assignment_id", "") or "").strip()
            if assignment_id:
                assignment_lines.append(f"- Assignment Id: {assignment_id}")
            if resident_assignment.get("dependency_snapshot"):
                assignment_lines.append(
                    "- Dependencies: "
                    + ", ".join(
                        str(item).strip()
                        for item in list(resident_assignment.get("dependency_snapshot", []) or [])
                        if str(item).strip()
                    )
                )
            if assignment_lines:
                parts.append(
                    ("## Current Assignment" if multi_team_org else "## Resident Assignment")
                    + "\n"
                    + "\n".join(assignment_lines)
                )
        work_item_turn_type = turn_type_for_task(task, fallback="")
        if work_item_turn_type and not multi_team_org:
            parts.append(f"## Work Item Turn Type\n{work_item_turn_type}")
        work_item_assignment_status = str(task.metadata.get("work_item_assignment_status", "")).strip()
        if work_item_assignment_status and not multi_team_org:
            parts.append(f"## Work Item Assignment Status\n{work_item_assignment_status}")
        work_item_assignment_source_projection_id = str(task.metadata.get("work_item_assignment_source_projection_id", "")).strip()
        if work_item_assignment_source_projection_id and not multi_team_org:
            parts.append(f"## Work Item Assignment Source\n{work_item_assignment_source_projection_id}")
        work_item_runtime_plan = self._render_work_item_runtime_plan(task.metadata.get("work_item_runtime_plan"))
        if work_item_runtime_plan and (not multi_team_org or not managerish):
            parts.append(f"## Work Item Runtime Plan\n{work_item_runtime_plan}")
        work_item_artifact_index = self._render_work_item_artifact_index(task.metadata.get("work_item_artifact_index"))
        if work_item_artifact_index and (not multi_team_org or not managerish):
            parts.append(f"## Work Item Artifact Index\n{work_item_artifact_index}")
        # Ownership contract is rendered exclusively by
        # ``build_collaboration_context``; we do NOT re-render it
        # here because doing so would double-emit the topology.
        if task.assigned_to:
            parts.append(f"## Current Role\n{task.assigned_to}")
        # Runtime role map is intentionally not rendered here — it
        # is superseded by the unified ``## Topology`` section,
        # which is produced by ``build_collaboration_context`` and
        # included in the system-context path. Rendering it in the
        # user-message path (build_task_brief) as well would
        # duplicate topology across the native agent prompt.
        meeting_turn_context = dict(task.context_snapshot.get("meeting_turn_context", {}) or {})
        if meeting_turn_context:
            round_no = meeting_turn_context.get("current_round")
            lines = [
                "## Meeting Turn",
                f"Room: {meeting_turn_context.get('room_id', '')}",
                f"Topic: {meeting_turn_context.get('topic', '')}",
            ]
            if round_no not in (None, ""):
                lines.append(f"Round: {round_no}")
            agenda = [str(item).strip() for item in list(meeting_turn_context.get("agenda", []) or []) if str(item).strip()]
            if agenda:
                lines.append("Agenda:")
                lines.extend(f"- {item}" for item in agenda)
            unresolved = [str(item).strip() for item in list((meeting_turn_context.get("consensus", {}) or {}).get("blocking_conflicts", []) or []) if str(item).strip()]
            if unresolved:
                lines.append("Current unresolved conflicts:")
                lines.extend(f"- {item}" for item in unresolved[:6])
            transcript = [item for item in list(meeting_turn_context.get("recent_transcript", []) or []) if isinstance(item, dict)]
            if transcript:
                lines.append("Recent meeting transcript:")
                for item in transcript[-6:]:
                    lines.append(
                        f"- {str(item.get('agent', '')).strip()}: {str(item.get('content', '')).strip()}"
                    )
            parts.append("\n".join(lines))

        user_reply = str(task.context_snapshot.get("user_supplied_input", "")).strip()
        if user_reply and not rendered_latest_directive:
            parts.append(
                "## User's Latest Reply (AUTHORITATIVE)\n"
                "Use this reply as the latest source of truth. "
                "If it resolves the blocker, continue. "
                "If not, ask only for the exact missing detail. "
                "Do not repeat the same broad question.\n\n"
                f"{user_reply}"
            )

        return "\n\n".join(parts)

    async def build_core_context(self, task: Task) -> str:
        project_id = task.project_id or "default"
        session_id = task.session_id
        parts: list[str] = []
        memory_policy = self._memory_policy(task)
        include_project_knowledge = bool(
            task.metadata.get("include_project_knowledge", False)
            or memory_policy.get("include_project_memory", False)
        )
        focus_query = self._memory_focus_query(task)
        use_focused_memory = bool(task.metadata.get("use_focused_memory", True))
        base = ""
        if use_focused_memory and hasattr(self.memory, "build_focused_memory_context"):
            base = await self.memory.build_focused_memory_context(
                query=focus_query,
                project_id=project_id,
                session_id=session_id,
                include_project_knowledge=include_project_knowledge,
            )
        if not base:
            base = await self.memory.build_memory_context(
                project_id=project_id,
                session_id=session_id,
                include_project_knowledge=include_project_knowledge,
            )
        if base:
            parts.append(base)
        # Work Item Runtime Plan is rendered only from ``build_task_brief``
        # (the user-message path). We intentionally do NOT re-render
        # it here in the system-context path — rendering it twice
        # within the same prompt would duplicate work-item identity.
        #
        # Runtime State bucket: the progress / handoff / gate-rework /
        # verification signals (``_build_working_summary``) and the
        # workspace / environment / data-readiness signals
        # (``_build_readiness_summary``) are folded into a single
        # ``## Runtime State`` section with ``### Working`` and
        # ``### Workspace`` sub-blocks. The header is only emitted
        # when at least one sub-block has content.
        # Runtime context prefetch: load the linked work_item once per core-context
        # build so the sync summary methods can read progress_log via the
        # view. task-mode (no work_item) degrades to a task-only view.
        work_item = await self._load_task_work_item(task)
        view = WorkItemContextView(work_item, task)
        working = self._build_working_summary(task, view)
        readiness_body = self._build_readiness_summary_body(task)
        runtime_sub_blocks: list[str] = []
        if working:
            runtime_sub_blocks.append("### Working\n" + working)
        if readiness_body:
            runtime_sub_blocks.append("### Workspace\n" + readiness_body)
        if runtime_sub_blocks:
            parts.append("## Runtime State\n" + "\n\n".join(runtime_sub_blocks))
        return "\n\n".join(parts)

    def build_member_session_context(self, task: Task) -> str:
        if not company_collaboration_enabled_for_task(task):
            return ""
        state = dict(task.metadata.get("member_session_state", {}) or task.context_snapshot.get("member_session", {}) or {})
        if not state:
            return ""
        # Identity (role / employee / member session id / inbox cursor)
        # is NOT rendered here — it lives in the unified ``## Self``
        # section (for role/employee identity) and is OPC-internal
        # plumbing that an agent cannot act on. Only the actionable
        # runtime state below is rendered.
        lines: list[str] = []
        multi_team_org = str(task.metadata.get("runtime_model", "") or "").strip() == "multi_team_org"
        if multi_team_org:
            lines.append("Mailbox is runtime-owned: this turn is driven by team board state plus the prepared mailbox backlog.")
        else:
            lines.append("Mailbox is runtime-owned: OpenOPC has already prepared the actionable backlog for this turn.")
        turn_mode = resolve_company_turn_mode(task, runtime_state=state)
        if turn_mode and not multi_team_org:
            lines.append(f"Current turn mode: {turn_mode}")
        actionable_inbox = [item for item in list(state.get("actionable_chat", []) or state.get("pending_inbox", []) or []) if isinstance(item, dict)]
        if actionable_inbox:
            lines.append("Actionable company chat backlog:")
            for item in actionable_inbox:
                from_agent = str(item.get("from_agent", "")).strip()
                subject = str(item.get("subject", "")).strip() or "(no subject)"
                body = str(item.get("body", "")).strip()
                lines.append(f"- From {from_agent}: {subject}")
                if body:
                    lines.append(f"  {body}")
        protocol_backlog = [item for item in list(state.get("protocol_backlog", []) or []) if isinstance(item, dict)]
        if protocol_backlog:
            lines.append("Pending worker protocol items:")
            for item in protocol_backlog:
                from_agent = str(item.get("from_agent", "")).strip()
                subject = str(item.get("subject", "")).strip() or "(no subject)"
                protocol_type = str(item.get("protocol_type", "") or dict(item.get("metadata", {}) or {}).get("protocol_type", "")).strip() or "protocol"
                body = str(item.get("body", "")).strip()
                lines.append(f"- {protocol_type} from {from_agent}: {subject}")
                if body:
                    lines.append(f"  {body}")
        latest_notification = dict(state.get("latest_notification", {}) or {})
        if latest_notification:
            notification_kind = str(
                latest_notification.get("notification_kind", "")
                or dict(latest_notification.get("metadata", {}) or {}).get("notification_kind", "")
                or "notification"
            ).strip()
            summary = str(
                latest_notification.get("summary", "")
                or latest_notification.get("subject", "")
                or latest_notification.get("body", "")
            ).strip()
            if summary:
                lines.append(f"Latest worker notification ({notification_kind}): {summary}")
        manager_board_summary = dict(
            state.get("manager_board_summary", {})
            or task.context_snapshot.get("manager_board_summary", {})
            or {}
        )
        if manager_board_summary:
            lines.append("Manager board summary:")
            lines.append(f"- Child items: {int(manager_board_summary.get('total_children', 0) or 0)}")
            derived_parent_status = str(manager_board_summary.get("derived_parent_status", "") or "").strip()
            if derived_parent_status:
                lines.append(f"- Derived parent status: {derived_parent_status}")
            releasable = [
                str(item).strip()
                for item in list(manager_board_summary.get("releasable_work_item_ids", []) or [])
                if str(item).strip()
            ]
            if releasable:
                lines.append(f"- Releasable child items: {', '.join(releasable[:6])}")
            blocked = [
                str(item).strip()
                for item in list(manager_board_summary.get("blocked_reasons", []) or [])
                if str(item).strip()
            ]
            if blocked:
                lines.append(f"- Board blocker: {blocked[0]}")
        if not multi_team_org:
            working_memory = [str(item).strip() for item in list(state.get("working_memory", []) or []) if str(item).strip()]
            if working_memory:
                lines.append("Recent member working memory:")
                lines.extend(f"- {item}" for item in working_memory[-6:])
            notification_backlog_count = state.get("notification_backlog_count")
            if isinstance(notification_backlog_count, int) and notification_backlog_count > 1:
                lines.append(f"Additional worker notifications queued: {notification_backlog_count - 1}")
            queued_inbox = [item for item in list(state.get("queued_inbox", []) or []) if isinstance(item, dict)]
            if queued_inbox:
                lines.append(f"Queued inbox for future activation: {len(queued_inbox)} message(s)")
            broker_pending_inbox = [item for item in list(task.context_snapshot.get("broker_pending_inbox", []) or []) if isinstance(item, dict)]
            if broker_pending_inbox:
                lines.append("Broker-queued inbox updates received mid-run:")
                for item in broker_pending_inbox:
                    from_agent = str(item.get("from_agent", "") or item.get("from", "")).strip()
                    subject = str(item.get("subject", "")).strip() or "(no subject)"
                    body = str(item.get("body", "")).strip()
                    lines.append(f"- From {from_agent}: {subject}")
                    if body:
                        lines.append(f"  {body}")
            inbox_gate = dict(task.context_snapshot.get("inbox_completion_gate", {}) or {})
            gate_messages = [item for item in list(inbox_gate.get("messages", []) or []) if isinstance(item, dict)]
            if gate_messages:
                lines.append("Inbox completion gate is pending: reply to these messages or acknowledge handled ones with `inbox(action=\"ack\")`.")
                for item in gate_messages:
                    from_agent = str(item.get("from_agent", "") or "").strip()
                    subject = str(item.get("subject", "") or "").strip() or "(no subject)"
                    msg_id = str(item.get("msg_id", "") or "").strip()
                    id_part = f" message_id={msg_id}" if msg_id else ""
                    lines.append(f"- From {from_agent}:{id_part} {subject}")
            resume_state = dict(state.get("resume_state", {}) or {})
            if resume_state:
                resume_lines: list[str] = []
                role_session_id = str(state.get("role_session_id", "")).strip()
                if role_session_id:
                    resume_lines.append(f"- Role session: {role_session_id}")
                focused_work_item_id = str(state.get("focused_work_item_id", "")).strip()
                if focused_work_item_id:
                    resume_lines.append(f"- Focused work item: {focused_work_item_id}")
                runtime_session_id = str(resume_state.get("runtime_session_id", "")).strip()
                if runtime_session_id:
                    resume_lines.append(f"- Runtime session: {runtime_session_id}")
                if resume_state.get("resume_cursor") not in (None, ""):
                    resume_lines.append(f"- Resume cursor: {resume_state.get('resume_cursor')}")
                verification_verdict = str(resume_state.get("verification_verdict", "")).strip()
                if verification_verdict:
                    resume_lines.append(f"- Last verifier verdict: {verification_verdict}")
                if resume_lines:
                    lines.append("Member resume state:")
                    lines.extend(resume_lines)
        if not lines:
            return ""
        return "## Member Session State\n" + "\n".join(lines)

    def _memory_focus_query(self, task: Task) -> str:
        pieces = [
            str(task.title or "").strip(),
            str(task.description or "").strip(),
            str(task.metadata.get("original_message", "") or "").strip(),
            str(task.context_snapshot.get("user_supplied_input", "") or "").strip(),
            str(task.metadata.get("latest_user_directive", "") or "").strip(),
            str(task.metadata.get("manager_mutation_user_input", "") or "").strip(),
        ]
        return "\n".join(part for part in pieces if part)

    async def build_dependency_context(self, task: Task) -> str:
        if not self.store or not task.dependencies:
            return ""
        lines: list[str] = []
        # Producer-side backstop: no upstream layer caps
        # ``task.dependencies``, so this is the only guard against
        # a pathological runtime with hundreds of dependencies. 16
        # is large enough for an aggregate / deliver work item that
        # fans in from every sibling in a corporate-sized runtime
        # (max observed: 12), while still bounded so a malformed
        # producer cannot flood the prompt. Each dependency that
        # makes the cut is rendered in full by
        # ``_summarize_dependency`` — no content-level chopping.
        for dep_id in task.dependencies[:16]:
            dep_task = await self.store.get_task(dep_id)
            if not dep_task or dep_task.status != TaskStatus.DONE:
                continue
            lines.extend(self._summarize_dependency(dep_task))
        if not lines:
            return ""
        return "## Direct Dependency Context\n" + "\n".join(lines)

    def build_attachment_context(self, task: Task) -> str:
        attachment_context = str(task.metadata.get("attachment_context", "")).strip()
        if attachment_context:
            return attachment_context

        refs = list(task.metadata.get("attachment_refs", []) or [])
        if not refs:
            return ""

        lines = ["## Attachments"]
        for item in refs[:6]:
            if not isinstance(item, dict):
                continue
            filename = str(item.get("filename", "attachment")).strip() or "attachment"
            mime_type = str(item.get("mime_type", "application/octet-stream")).strip() or "application/octet-stream"
            disk_path = str(item.get("disk_path", "")).strip()
            size_bytes = item.get("size_bytes")
            summary = f"- {filename} ({mime_type}"
            if isinstance(size_bytes, int):
                summary += f", {size_bytes} bytes"
            summary += ")"
            lines.append(summary)
            if disk_path:
                lines.append(f"  Path: {disk_path}")
        return "\n".join(lines)

    async def build_collaboration_context(self, task: Task, role_id: str = "") -> str:
        if not company_collaboration_enabled_for_task(task):
            return ""
        if not self.communication:
            return ""
        active_role = role_id or task.assigned_to or str(task.metadata.get("work_item_role_id", "")).strip()
        if not active_role:
            return ""
        ctx = await self.communication.build_agent_context(active_role, task=task)
        parts: list[str] = []
        # Topology bucket: the single source of truth for "who I can
        # talk to and what my boundaries are". Collapses what used to
        # be three separate sections (Ownership Contract, Contact
        # Directory, Runtime Role Map) into one ``## Topology``
        # block. See ``_build_topology_section`` for details.
        topology_section = await self._build_topology_section(task, ctx=ctx)
        if topology_section:
            parts.append(topology_section)
        inbox = list(ctx.get("inbox", []))
        if inbox:
            parts.append(
                "## Inbox\n"
                + "\n".join(
                    f"- From {item.get('from_agent', '')}: {item.get('subject', '')} :: {item.get('body', '')}"
                    for item in inbox
                )
            )
        annotations = list(ctx.get("annotations", []))
        if annotations:
            parts.append(
                "## Task Annotations\n"
                + "\n".join(
                    f"- {item.get('from', '')}: {str(item.get('body', ''))}"
                    for item in annotations
                )
            )
        handoff_context = str(task.metadata.get("handoff_context", "")).strip()
        if handoff_context and str(task.metadata.get("runtime_model", "") or "").strip() != "multi_team_org":
            parts.append(f"## Active Handoff\n{handoff_context}")
        pending_handoffs = list(ctx.get("pending_handoffs", []))
        if pending_handoffs and str(task.metadata.get("runtime_model", "") or "").strip() != "multi_team_org":
            parts.append(
                "## Pending Handoff Acknowledgements\n"
                + "\n".join(
                    (
                        f"- {item.get('handoff_id', '')}: from={item.get('from_role', '')} "
                        f"status={item.get('status', '')} requires_ack={'yes' if item.get('requires_ack') else 'no'} "
                        f"summary={item.get('summary', '')}"
                    ).strip()
                    for item in pending_handoffs[:8]
                )
            )
        multi_team_org = str(task.metadata.get("runtime_model", "") or "").strip() == "multi_team_org"
        migration_handoff = task.context_snapshot.get("migration_handoff")
        if migration_handoff and not multi_team_org:
            parts.append(f"## Migration Handoff\n{migration_handoff}")
        migration_reason = str(task.context_snapshot.get("migration_reason", "")).strip()
        if migration_reason and not multi_team_org:
            parts.append(f"## Migration Reason\n{migration_reason}")
        # Org version and reorg provenance are only worth surfacing
        # when this task was actually superseded by a reorg event.
        # A bare ``org_version`` integer with no reorg history
        # carries zero signal for the agent and is intentionally
        # omitted. The currently-active org version is already
        # encoded in the topology section when relevant.
        previous_reorg = str(task.metadata.get("superseded_by_reorg", "")).strip()
        if previous_reorg:
            current_org_version = task.metadata.get("org_version")
            org_version_line = f" (current org version: {current_org_version})" if current_org_version else ""
            parts.append(f"## Superseded By Reorg\n{previous_reorg}{org_version_line}")
        meeting_outcome = task.context_snapshot.get("meeting_outcome")
        if meeting_outcome:
            if isinstance(meeting_outcome, dict):
                decision = meeting_outcome.get("decision", "")
                action_items = meeting_outcome.get("action_items", [])
                reasoning = meeting_outcome.get("reasoning", "")
                lines = [f"## Meeting Decision\n{decision}"]
                decision_method = str(meeting_outcome.get("decision_method", "") or task.context_snapshot.get("meeting_decision_method", "")).strip()
                if decision_method:
                    lines.append(f"Decision method: {decision_method}")
                if reasoning:
                    lines.append(f"Reasoning: {reasoning}")
                if action_items:
                    lines.append(
                        "Action items you MUST follow:\n"
                        + "\n".join(f"- {item}" for item in action_items)
                    )
                lines.append(
                    "IMPORTANT: Incorporate this decision into your current work. "
                    "Do not revisit or contradict it."
                )
                parts.append("\n".join(lines))
            else:
                parts.append(f"## Meeting Decision\n{meeting_outcome}")
        latest_peer_reply = task.context_snapshot.get("latest_peer_reply")
        if latest_peer_reply:
            parts.append(f"## Latest Peer Reply\n{latest_peer_reply}")
        meeting_turn_context = task.context_snapshot.get("meeting_turn_context")
        if isinstance(meeting_turn_context, dict) and meeting_turn_context:
            parts.append(
                "## Active Meeting Turn\n"
                f"Room: {meeting_turn_context.get('room_id', '')}\n"
                f"Topic: {meeting_turn_context.get('topic', '')}\n"
                f"Round: {meeting_turn_context.get('current_round', '')}"
            )
        # --- Upstream collaboration awareness warnings ---
        upstream_warnings = list(task.context_snapshot.get("upstream_collaboration_warnings", []) or [])
        if not upstream_warnings:
            upstream_warnings = list(task.metadata.get("_upstream_collaboration_warnings", []) or [])
        if upstream_warnings and not multi_team_org:
            parts.append(
                "## Upstream Collaboration Warnings\n"
                "The following upstream execution work items completed with ZERO inter-agent collaboration.\n"
                "This means parallel peers did not coordinate with each other during execution.\n"
                "You SHOULD verify that their outputs are consistent and no integration gaps exist.\n"
                + "\n".join(f"- {w}" for w in upstream_warnings[:8])
            )
        # --- Parallel peers context (for work items currently running in parallel) ---
        parallel_ctx = self._build_parallel_peers_context(task) if not multi_team_org else ""
        if parallel_ctx:
            parts.append(parallel_ctx)
        return "\n\n".join(parts)

    def build_team_memory_context(self, task: Task) -> str:
        if not company_collaboration_enabled_for_task(task):
            return ""
        layout = self._comms_layout_for(task)
        if layout is None:
            return ""
        try:
            digest = _comms.read_team_memory_digest(layout, max_chars=1600)
        except Exception:
            return ""
        if not digest:
            return ""
        return "## Team Memory\n" + digest

    def _build_parallel_peers_context(self, task: Task) -> str:
        """Build a summary of parallel peer work items running alongside this task."""
        parallel_group = str(task.metadata.get("work_item_parallel_group", "") or "").strip()
        if not parallel_group:
            return ""
        execution_task_ids = list(task.metadata.get("execution_task_ids", []) or [])
        if not execution_task_ids:
            return ""
        peer_work_items: list[str] = []
        plan_projections = list(task.metadata.get("_work_item_plan_projections", []) or [])
        for projection_info in plan_projections:
            if not isinstance(projection_info, dict):
                continue
            if str(projection_info.get("parallel_group", "")).strip() == parallel_group:
                projection_role = str(projection_info.get("role_id", "")).strip()
                projection_title = str(projection_info.get("title", "")).strip()
                if projection_role and projection_role != str(task.assigned_to or "").strip():
                    peer_work_items.append(f"- {projection_role}: {projection_title}")
        if not peer_work_items:
            return ""
        return (
            "## Parallel Peer Work Items\n"
            "The following roles are executing work items IN PARALLEL with yours right now:\n"
            + "\n".join(peer_work_items)
        )

    def _org_engine(self) -> Any | None:
        return getattr(self.communication, "org_engine", None) if self.communication else None

    def _seat_runtime_topology(self, task: Task) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        topology = dict(task.metadata.get("runtime_topology", {}) or {})
        seats = {
            str(item.get("seat_id", "")).strip(): dict(item)
            for item in list(topology.get("seats", []) or [])
            if str(item.get("seat_id", "")).strip()
        }
        teams = {
            str(item.get("team_id", "")).strip(): dict(item)
            for item in list(topology.get("teams", []) or [])
            if str(item.get("team_id", "")).strip()
        }
        return topology, seats, teams

    def _current_seat_and_role(self, task: Task) -> tuple[str, dict[str, Any], str]:
        _, seats, _ = self._seat_runtime_topology(task)
        metadata = dict(task.metadata or {})
        employee_assignment = dict(metadata.get("employee_assignment", {}) or {})
        current_seat_id = str(
            metadata.get("delegation_seat_id", "")
            or metadata.get("seat_id", "")
            or ""
        ).strip()
        current_seat = dict(seats.get(current_seat_id, {}) or {})
        role_id = str(
            current_seat.get("role_id", "")
            or metadata.get("work_item_role_id", "")
            or task.assigned_to
            or employee_assignment.get("role_id", "")
            or ""
        ).strip()
        if not current_seat and role_id:
            for seat_id, seat in seats.items():
                if str(seat.get("role_id", "") or "").strip() == role_id:
                    current_seat_id = seat_id
                    current_seat = dict(seat)
                    break
        if not role_id:
            role_id = str(employee_assignment.get("role_id", "") or "").strip()
        return current_seat_id, current_seat, role_id

    async def _seat_status_by_id(self, task: Task) -> dict[str, str]:
        if not self.store:
            return {}
        run_id = str(task.metadata.get("delegation_run_id", "") or "").strip()
        if not run_id:
            return {}
        list_seat_states = getattr(self.store, "list_seat_states", None) or getattr(self.store, "list_delegation_seat_states", None)
        if not callable(list_seat_states):
            return {}
        try:
            persisted = await list_seat_states(run_id=run_id)
        except TypeError:
            persisted = await list_seat_states(run_id)
        except Exception:
            return {}
        status_by_id: dict[str, str] = {}
        for seat in list(persisted or []):
            seat_id = str(getattr(seat, "seat_id", "") or "").strip()
            if not seat_id:
                continue
            status = str(getattr(seat, "resident_status", "") or getattr(seat, "status", "") or "").strip()
            if status:
                status_by_id[seat_id] = status
        return status_by_id

    def _role_name(self, role_id: str, seat: dict[str, Any] | None = None) -> str:
        seat = dict(seat or {})
        name = str((seat.get("metadata", {}) or {}).get("role_name", "") or "").strip()
        if name:
            return name
        org_engine = self._org_engine()
        if org_engine and hasattr(org_engine, "get_agent"):
            agent = org_engine.get_agent(role_id)
            if agent is not None:
                resolved = str(getattr(agent, "name", "") or "").strip()
                if resolved:
                    return resolved
        return role_id

    def _role_responsibility(self, role_id: str, ctx: dict[str, Any], seat: dict[str, Any] | None = None) -> str:
        seat = dict(seat or {})
        contact_map = {
            str(item.get("role_id", "")).strip(): str(item.get("responsibility", "")).strip()
            for item in list(ctx.get("allowed_contacts", []) or [])
            if str(item.get("role_id", "")).strip()
        }
        if role_id in contact_map and contact_map[role_id]:
            return contact_map[role_id]
        metadata = dict(seat.get("metadata", {}) or {})
        seat_responsibility = str(
            metadata.get("responsibility", "")
            or metadata.get("role_responsibility", "")
            or seat.get("responsibility", "")
            or ""
        ).strip()
        if seat_responsibility:
            return seat_responsibility
        org_engine = self._org_engine()
        if org_engine and hasattr(org_engine, "get_agent"):
            agent = org_engine.get_agent(role_id)
            if agent is not None:
                return str(getattr(agent, "responsibility", "") or "").strip()
        return ""

    @staticmethod
    def _seat_employee_label(seat: dict[str, Any]) -> str:
        assignment = dict(seat.get("employee_assignment", {}) or {})
        return str(assignment.get("name", "") or seat.get("employee_id", "") or "").strip()

    @staticmethod
    def _seat_agent_label(seat: dict[str, Any]) -> str:
        return str(seat.get("selected_execution_agent", "") or seat.get("preferred_external_agent", "") or "native").strip() or "native"

    async def _build_topology_section(self, task: Task, *, ctx: dict[str, Any]) -> str:
        """Render the unified ``## Topology`` section.

        This is the single canonical source for "who I can talk to
        and what my boundaries are". It replaces three previously
        independent sections that overlapped each other:

        - ``## Ownership Contract`` (write scope, allowed targets,
          downstream consumers — boundary info)
        - ``## Contact Directory`` (allowed_contacts derived list)
        - ``## Runtime Role Map`` (whole org chart with pending
          work items — mostly noise for any single work item)

        The new section merges the slimmed ownership contract with
        the allowed_contacts short list. The full runtime role
        map is no longer rendered at all — it repeated ``Contact
        Directory`` plus a large amount of provisional placeholder
        text that carried no actionable content.
        Returns "" when there is no topology information worth
        surfacing (e.g. the task has no ownership contract and no
        allowed contacts).
        """
        sub_blocks: list[str] = []

        contract_block = render_ownership_contract(task)
        if contract_block:
            # strip the "## Ownership Contract" header — the
            # topology section is the single H2 header, and
            # the sub-sections use H3.
            header, _, body = contract_block.partition("\n")
            body = body.strip() if body else contract_block
            sub_blocks.append("### Boundary\n" + body)

        multi_team_org = str(task.metadata.get("runtime_model", "") or "").strip() == "multi_team_org"
        _, seats, _ = self._seat_runtime_topology(task)
        current_seat_id, current_seat, current_role_id = self._current_seat_and_role(task)
        direct_manager_role_id = ""
        direct_report_role_ids: set[str] = set()
        if multi_team_org and current_seat:
            manager_seat_id = str(current_seat.get("manager_seat_id", "") or task.metadata.get("manager_seat_id", "") or "").strip()
            manager_seat = dict(seats.get(manager_seat_id, {}) or {})
            if manager_seat:
                direct_manager_role_id = str(manager_seat.get("role_id", "") or "").strip()
                manager_label = self._role_name(direct_manager_role_id, manager_seat)
                manager_responsibility = self._role_responsibility(direct_manager_role_id, ctx, seat=manager_seat)
                line = f"- {direct_manager_role_id} ({manager_label})"
                if manager_responsibility:
                    line += f": {manager_responsibility}"
                sub_blocks.append("### Direct Manager\n" + line)

            managed_team_id = str(current_seat.get("managed_team_id", "") or task.metadata.get("managed_team_id", "") or "").strip()
            status_by_seat_id = await self._seat_status_by_id(task)
            direct_report_lines: list[str] = []
            if managed_team_id and current_role_id:
                for candidate in seats.values():
                    if str(candidate.get("team_id", "") or "").strip() != managed_team_id:
                        continue
                    if str(candidate.get("manager_role_id", "") or "").strip() != current_role_id:
                        continue
                    role_id_val = str(candidate.get("role_id", "") or "").strip()
                    if not role_id_val or role_id_val == current_role_id:
                        continue
                    direct_report_role_ids.add(role_id_val)
                    name_val = self._role_name(role_id_val, candidate)
                    responsibility_val = self._role_responsibility(role_id_val, ctx, seat=candidate)
                    employee_val = self._seat_employee_label(candidate)
                    agent_val = self._seat_agent_label(candidate)
                    status_val = str(status_by_seat_id.get(str(candidate.get("seat_id", "") or "").strip(), "idle") or "idle").strip()
                    line = f"- {role_id_val} ({name_val})"
                    if responsibility_val:
                        line += f": {responsibility_val}"
                    details = []
                    if employee_val:
                        details.append(f"employee={employee_val}")
                    if agent_val:
                        details.append(f"agent={agent_val}")
                    if status_val:
                        details.append(f"status={status_val}")
                    if details:
                        line += f" [{', '.join(details)}]"
                    direct_report_lines.append(line)
            if direct_report_lines:
                sub_blocks.append("### Direct Reports\n" + "\n".join(direct_report_lines[:12]))

        allowed_contacts = list(ctx.get("allowed_contacts", []) or [])
        if allowed_contacts:
            lines = [
                "### Direct Contacts",
                "You may directly contact only these roles in the current org structure:",
            ]
            for item in allowed_contacts[:12]:
                role_id_val = str(item.get("role_id", "")).strip()
                if multi_team_org and role_id_val and (role_id_val == direct_manager_role_id or role_id_val in direct_report_role_ids):
                    continue
                name_val = str(item.get("name", "")).strip()
                relation_val = str(item.get("relation", "peer")).strip() or "peer"
                responsibility_val = str(item.get("responsibility", "")).strip()
                line = f"- {role_id_val} ({name_val}) [{relation_val}]"
                if responsibility_val:
                    line += f": {responsibility_val}"
                lines.append(line)
            if len(lines) > 2:
                sub_blocks.append("\n".join(lines))

        if not sub_blocks:
            return ""
        return "## Topology\n" + "\n\n".join(sub_blocks)

    def build_external_runtime_mailbox_context(self, task: Task, role_id: str = "") -> str:
        """Build the comms section for company-mode external agent prompts.

        Collaboration tools are exposed via the ``opc-collab`` CLI, while
        mailbox discovery is runtime-owned: OpenOPC reads `.opc-comms`,
        classifies inbox state, and injects the actionable mailbox snapshot
        before each turn. Agents use `inbox` to peek or acknowledge messages
        explicitly when needed; prompt injection alone is not a read receipt.

        Messages still land in the per-session filesystem layout
        (``.opc-comms/<project>/<session>/inbox/<role>/...``) so the
        audit trail and blocking park-and-resume mechanics are unchanged.

        Blocking semantics (the rare 10% — meetings, urgent decisions
        the agent literally cannot work around) are handled by the
        broker's park/resume path: when the agent calls
        ``ask_peer_and_wait(...)``, OpenOPC parks the sender's work item in
        ``AWAITING_PEER``, runs the receiver, then resumes the sender
        once a reply has been written.
        """
        if not company_collaboration_enabled_for_task(task):
            return ""

        active_role = role_id or task.assigned_to or str(task.metadata.get("work_item_role_id", "")).strip()
        if not active_role:
            return ""

        layout = self._comms_layout_for(task)
        if layout is None:
            return ""

        # Make sure the session layout exists by the time the agent
        # reads the prompt — including all known peer roles so the
        # agent can address them. We do this lazily here so even
        # callers that bypass company_mode bootstrap still get a
        # working layout.
        peer_roles = self._all_known_role_ids(task)
        roles_to_seed = list(dict.fromkeys([active_role, *peer_roles]))
        try:
            _comms.ensure_layout(layout, roles_to_seed)
        except OSError:
            # Filesystem failure → fall back to no comms section
            # rather than crashing the prompt build.
            return ""

        # --- Inbox section: identity + runtime-owned mailbox snapshot.
        inbox_section = _comms.render_inbox_section(layout, active_role)

        # --- Meetings section: any open multi-party rooms this role is in.
        #     Returns empty when no meetings are open.
        try:
            meetings_section = _comms.render_meetings_section(layout, active_role)
        except Exception:
            meetings_section = ""
        if meetings_section:
            inbox_section = (inbox_section + "\n\n" + meetings_section) if inbox_section else meetings_section

        # --- Resumed-from-blocking note: replies are already prepared in
        #     the runtime-owned mailbox snapshot, so the agent can act
        #     directly on the injected context.
        resumed_replies = task.metadata.get("comms_resolved_blocking_replies") or {}
        if isinstance(resumed_replies, dict) and resumed_replies:
            lines = ["### Blocking Replies Received"]
            lines.append(
                "Your previous turn parked the work item waiting on the "
                "following blocking messages. Replies are already prepared in "
                "your runtime mailbox context:"
            )
            for sent_id, _reply_path in resumed_replies.items():
                lines.append(f"- Reply to `{sent_id}` is ready in the injected mailbox snapshot")
            lines.append("Continue from the prepared reply context. Do not re-send the original blocking message.")
            inbox_section = (inbox_section + "\n\n" + "\n".join(lines)) if inbox_section else "\n".join(lines)

        # --- Parallel peers (who else is working in parallel right now)
        multi_team_org = str(task.metadata.get("runtime_model", "") or "").strip() == "multi_team_org"
        parallel_ctx = self._build_parallel_peers_context(task) if not multi_team_org else ""

        parts = [p for p in (inbox_section, parallel_ctx) if p]
        return "\n\n".join(parts)

    def _comms_layout_for(self, task: Task):
        """Resolve the per-session comms layout for `task`. None if no workspace.

        Comms is OPC-internal collaboration state, NOT a deliverable —
        it must NOT live inside the project's `target_output_dir`. We
        prefer `comms_workspace_root` (engine-resolved, points at the
        user's general workspace root). Only as a last resort do we
        fall back to the deliverable folder so something still works
        for legacy tasks that pre-date this metadata key.
        """
        workspace_root = (
            str(task.metadata.get("comms_workspace_root", "") or "").strip()
            or str(task.metadata.get("workspace_root", "") or "").strip()
            or str(task.metadata.get("target_output_dir", "") or "").strip()
            or str(task.metadata.get("setup_workspace_prepared", "") or "").strip()
        )
        if not workspace_root:
            return None
        project_id = str(task.project_id or "default").strip() or "default"
        session_id = (
            str(task.parent_session_id or "").strip()
            or str(task.session_id or "").strip()
            or "default"
        )
        try:
            return _comms.resolve_layout(workspace_root, project_id, session_id)
        except Exception:
            return None

    def _all_known_role_ids(self, task: Task) -> list[str]:
        """Best-effort enumeration of every role visible in this task's
        work-item role map metadata. Used to pre-seed inbox dirs so an
        agent can address peers whose work items haven't started yet."""
        roles: list[str] = []
        role_map = task.metadata.get("work_item_role_task_map") or task.metadata.get("work_item_role_summary")
        if isinstance(role_map, dict):
            for k in role_map.keys():
                if k:
                    roles.append(str(k))
        elif isinstance(role_map, list):
            for item in role_map:
                if isinstance(item, dict):
                    rid = str(item.get("role_id") or item.get("id") or "").strip()
                    if rid:
                        roles.append(rid)
        peers = task.metadata.get("work_item_parallel_peers") or []
        if isinstance(peers, list):
            for p in peers:
                if isinstance(p, dict):
                    rid = str(p.get("role_id") or p.get("id") or "").strip()
                    if rid:
                        roles.append(rid)
        return roles

    async def build_role_reference_context(self, task: Task, role_id: str = "") -> str:
        if not self.store or not role_id:
            return ""
        parts: list[str] = []
        # Put the active role and hired-employee persona before memory
        # fragments so the agent reads "who am I?" before historical context.
        _self_work_item = await self._load_task_work_item(task)
        _self_view = WorkItemContextView(_self_work_item, task)
        self_section = self._build_self_section(task, _self_view)
        if self_section:
            parts.append(self_section)
        memory_policy = self._memory_policy(task)
        include_role_memory = bool(memory_policy.get("include_role_memory", False))
        include_decision_log = bool(memory_policy.get("include_decision_log", False))
        include_artifact_index = bool(memory_policy.get("include_artifact_index", False))
        role_memory = (
            await self.store.get_role_memory(project_id=task.project_id, role_id=role_id, limit=6)
            if include_role_memory
            else []
        )
        if role_memory:
            parts.append("## Role Local Memory\n" + "\n".join(f"- {item.summary}" for item in role_memory[:6]))
        decisions = (
            await self.store.get_work_item_decisions(project_id=task.project_id, projection_id=None, limit=6)
            if include_decision_log
            else []
        )
        if decisions:
            parts.append(
                "## Decision Log\n"
                + "\n".join(f"- [{item.projection_id or 'general'}] {item.summary}" for item in decisions[:6])
            )
        artifacts = (
            await self.store.get_artifacts(project_id=task.project_id, projection_id=None, limit=6)
            if include_artifact_index
            else []
        )
        if artifacts:
            parts.append(
                "## Artifact Index\n"
                + "\n".join(f"- {item.name} -> {item.location}" for item in artifacts[:6])
            )
        if hasattr(self.memory, "build_agent_memory_context"):
            employee_memory_context = await self.memory.build_agent_memory_context(task, role_id=role_id)
        elif hasattr(self.memory, "build_memory_context"):
            employee_memory_context = await self.memory.build_memory_context(
                project_id=task.project_id or "default",
                session_id=task.session_id,
            )
        else:
            employee_memory_context = ""
        if employee_memory_context:
            parts.append(employee_memory_context)
        return "\n\n".join(parts)

    def _build_self_section(
        self,
        task: Task,
        view: WorkItemContextView | None = None,
    ) -> str:
        """Render the unified 'who I am' block for the current role.

        This is the single canonical source for role identity, employee
        assignment, persona, and learned delta profile. It replaces the three
        previously separate sections (``## Assigned Employee`` /
        ``## Employee Persona`` / ``## Employee Delta Profile``).
        Returns "" when no substantive content is available.

        Runtime context prefetch: ``employee_prompt_context`` and
        ``employee_delta_context`` are read via ``WorkItemContextView``
        so they resolve from ``work_item.metadata`` when Step 9 has
        mirrored them. When no view is passed (task-mode callers), a
        task-only view degrades cleanly. ``employee_assignment`` stays
        task-side (Step 9 deferral — engine.py task-mode coupling).
        """
        if view is None:
            view = WorkItemContextView(None, task)
        sub_blocks: list[str] = []
        employee_assignment = dict(task.metadata.get("employee_assignment", {}) or {})
        current_seat_id, current_seat, role_id = self._current_seat_and_role(task)
        if role_id:
            role_lines: list[str] = []
            role_name = self._role_name(role_id, current_seat)
            role_label = f"{role_id} ({role_name})" if role_name and role_name != role_id else role_id
            role_lines.append(f"- Role: {role_label}")
            responsibility = self._role_responsibility(role_id, {}, seat=current_seat)
            if responsibility:
                role_lines.append(f"- Responsibility: {responsibility}")
            if current_seat_id:
                role_lines.append(f"- Seat: {current_seat_id}")
            sub_blocks.append("### Role\n" + "\n".join(role_lines))
        if employee_assignment:
            employee_lines: list[str] = []
            employee_name = str(employee_assignment.get("name", "") or "").strip()
            employee_id = str(employee_assignment.get("employee_id", "") or "").strip()
            category = str(employee_assignment.get("category", "") or "").strip()
            domains = [
                str(item).strip()
                for item in list(employee_assignment.get("domains", []) or [])
                if str(item).strip()
            ]
            try:
                experience_score = float(employee_assignment.get("experience_score", 0) or 0)
            except (TypeError, ValueError):
                experience_score = 0.0
            fallback_profile = category.lower() == "fallback" or "fallback" in employee_id.lower()
            if employee_name:
                employee_lines.append(f"- Employee: {employee_name}")
            if fallback_profile:
                employee_lines.append("- Assignment: fallback employee profile")
            else:
                if employee_id:
                    employee_lines.append(f"- Employee ID: {employee_id}")
                if category:
                    employee_lines.append(f"- Category: {category}")
                if domains:
                    employee_lines.append(f"- Domains: {', '.join(domains)}")
                if experience_score:
                    rendered_score = int(experience_score) if experience_score.is_integer() else experience_score
                    employee_lines.append(f"- Experience score: {rendered_score}")
            if employee_lines:
                sub_blocks.append("### Employee\n" + "\n".join(employee_lines))
        employee_prompt_context = str(view.get("employee_prompt_context", "") or "").strip()
        if employee_prompt_context:
            sub_blocks.append("### Employee Persona\n" + _demote_embedded_headings(employee_prompt_context))
        employee_delta_context = str(view.get("employee_delta_context", "") or "").strip()
        if employee_delta_context:
            sub_blocks.append("### Learned Working Profile\n" + _demote_embedded_headings(employee_delta_context))
        if not sub_blocks:
            return ""
        return "## Self\n" + "\n\n".join(sub_blocks)

    async def build_team_deliverables_context(self, task: Task) -> str:
        """Inject children's approved outputs when the parent card wakes.

        The leader's execute seat (e.g. seat::team::ceo::cmo) has its own
        codex session that saw only the delegation turn, not the review
        turns (those ran under seat::team::cmo::cmo). When children are
        all approved and this parent wakes to do integration, it has no
        in-context memory of what the team actually produced. We fetch
        each child's deliverable_summary + review_completion_report +
        artifact_manifest from the store and inline a compact handoff
        block, plus explicit guidance that the team has finished and the
        agent should not spawn new children.

        Fires only when the task is a parent resuming from a park:
            - metadata has dependency_work_item_ids
            - the work item exists and is currently RUNNING or READY_FOR_REWORK
            - children are all APPROVED (we render whatever is available
              otherwise; partial visibility is still better than silence)

        Returns empty string on any miss — no section is injected then.
        """
        store = self.store
        if store is None:
            return ""
        metadata = task.metadata or {}
        dep_ids_raw = metadata.get("dependency_work_item_ids", []) or []
        dep_ids = [str(x).strip() for x in list(dep_ids_raw) if str(x).strip()]
        if not dep_ids:
            return ""
        # Only inject on an actual resume: the parent task must be currently
        # runnable (PENDING/RUNNING). If still BLOCKED or DONE we're not in
        # a wake turn and the block would just be noise.
        if task.status not in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            return ""
        parent_work_item_id = linked_work_item_id_for_task(task)
        get_work_item = getattr(store, "get_delegation_work_item", None)
        if get_work_item is None:
            return ""
        self_evolution_turn = turn_type_for_task(task, fallback="") == "self_evolution"
        if self_evolution_turn:
            lines: list[str] = [
                "## Child Self-Evolution Results",
                "",
                "Your delegated self-evolution WorkItems are complete. Use their",
                "results only to decide your own employee experience patch. Do not",
                "produce a user-facing deliverable.",
                "",
            ]
        else:
            lines = [
                "## Team Deliverables to Integrate",
                "",
                "Your team's sub-tasks are all completed and reviewed. Below is a",
                "compact handoff of each child's approved output. You are in an",
                "integration turn now — produce this work item's final deliverable",
                "based on these. Do NOT call `delegate_work` again unless you",
                "discover a remedial gap; the team's work is done.",
                "",
            ]
        rendered_any = False
        for dep_id in dep_ids:
            try:
                child = await get_work_item(dep_id)
            except Exception:
                child = None
            if child is None:
                continue
            child_meta = dict(getattr(child, "metadata", {}) or {})
            role = str(getattr(child, "role_id", "") or "?")
            title = str(getattr(child, "title", "") or "").strip()
            phase = str(getattr(child, "phase", "") or "")
            phase_label = str(phase).split(".")[-1] if phase else "?"
            deliverable_summary = str(
                getattr(child, "deliverable_summary", "")
                or child_meta.get("work_item_summary_for_downstream", "")
                or child_meta.get("work_item_summary", "")
                or ""
            ).strip()
            # review_completion_report lives on the companion review work
            # item; the child's own metadata holds the last reviewer outcome.
            # The child's own deliverable_summary is what the agent produced;
            # prefer it as the authoritative digest.
            review_evidence = child_meta.get("review_evidence", {}) or {}
            if isinstance(review_evidence, dict):
                artifact_items = list(review_evidence.get("artifact_manifest", []) or [])
                verification_results = dict(review_evidence.get("verification_results", {}) or {})
                verification_status = dict(verification_results.get("status", {}) or {})
                output_paths = [
                    str(item).strip()
                    for item in list(review_evidence.get("output_paths", []) or [])
                    if str(item).strip()
                ]
            else:
                artifact_items = []
                verification_status = {}
                output_paths = []
            review_verdict = child_meta.get("structured_review_verdict", {}) or {}
            review_label = ""
            review_summary = ""
            if isinstance(review_verdict, dict):
                review_label = str(review_verdict.get("label", "") or "").strip()
                review_summary = str(review_verdict.get("summary", "") or "").strip()
            lines.append(f"### {role} — [{phase_label}] id={dep_id}")
            if title:
                lines.append(f"Title: {title}")
            if deliverable_summary:
                capped = clip_text(
                    deliverable_summary,
                    limit=1200,
                    marker="dependency deliverable preview truncated",
                ).text
                lines.append("Deliverable:")
                lines.append(capped)
            if review_label or review_summary:
                rendered_review = review_label or "review"
                if review_summary:
                    rendered_review = f"{rendered_review}: {review_summary}"
                lines.append(f"Review: {rendered_review}")
            if verification_status:
                label = str(verification_status.get("label", "") or "").strip()
                summary = str(verification_status.get("summary", "") or "").strip()
                if label or summary:
                    rendered_verification = label or "verification"
                    if summary:
                        rendered_verification = f"{rendered_verification}: {summary}"
                    lines.append(f"Verification: {rendered_verification}")
            if artifact_items:
                lines.append("Artifacts:")
                for art in artifact_items[:10]:
                    if not isinstance(art, dict):
                        continue
                    kind = str(art.get("kind", "") or "?")
                    label = str(art.get("label", "") or "")
                    value = str(art.get("value", "") or "")
                    lines.append(f"  - [{kind}] {label}: {value}")
            if output_paths:
                lines.append("Output paths:")
                lines.extend(f"  - {item}" for item in output_paths[:8])
            lines.append("")
            rendered_any = True
        if not rendered_any:
            return ""
        # Reference the parent id so the agent can disambiguate if needed.
        if parent_work_item_id:
            lines.append(f"(Parent work item: {parent_work_item_id})")
        return "\n".join(lines).rstrip() + "\n"

    def build_recovery_context(self, task: Task) -> str:
        parts: list[str] = []
        collaboration_enabled = company_collaboration_enabled_for_task(task)
        supplied = str(task.context_snapshot.get("user_supplied_input", "")).strip()
        self_evolution_retry = str(
            task.context_snapshot.get("self_evolution_patch_retry_feedback", "")
            or task.metadata.get("self_evolution_patch_retry_feedback", "")
            or ""
        ).strip()
        if supplied:
            parts.append(
                "## Recovery Input (LATEST USER ANSWER)\n"
                "Treat this answer as authoritative for the facts it contains. "
                "If it is enough, continue. "
                "If not, ask a narrower follow-up for the remaining gap only.\n\n"
                f"User's answer: {supplied}"
            )
        requested = task.context_snapshot.get("requested_user_input")
        if requested:
            parts.append(f"## Previous User-Input Request\n{requested}")
        if self_evolution_retry:
            parts.append(
                "## Self-Evolution JSON Retry\n"
                f"{self_evolution_retry}\n\n"
                "Return strict JSON only using the `{ \"patches\": [...] }` contract."
            )
        if collaboration_enabled:
            resume_hint = str(task.context_snapshot.get("peer_resume_hint", "")).strip()
            if resume_hint:
                parts.append(
                    "## Peer Coordination Update\n"
                    "The following guidance was provided while you were waiting for peer input:\n\n"
                    f"{resume_hint}\n\n"
                    "Use this information to adjust your approach and continue the task."
                )
            timeout_action = str(task.context_snapshot.get("peer_timeout_action", "")).strip()
            if timeout_action:
                parts.append(f"## Peer Timeout Action\n{timeout_action}")
            comms_failure = dict(task.context_snapshot.get("latest_comms_failure", {}) or {})
            if comms_failure:
                lines = ["## Communication Failure To Resolve"]
                lines.append(
                    "Your previous communication attempt failed. Use the structured error below to decide whether to retry, rewrite, switch recipient, open a meeting, or continue with parallel work first."
                )
                lines.append(f"- Operation: {str(comms_failure.get('operation', '') or '').strip()}")
                lines.append(f"- From: {str(comms_failure.get('from_role', '') or '').strip()}")
                lines.append(f"- To: {str(comms_failure.get('to_role', '') or '').strip()}")
                lines.append(f"- Reason: {str(comms_failure.get('reason', '') or '').strip()}")
                attempted_path = str(comms_failure.get("attempted_path", "") or "").strip()
                if attempted_path:
                    lines.append(f"- Path: {attempted_path}")
                attempted_command = str(comms_failure.get("attempted_command", "") or "").strip()
                if attempted_command:
                    lines.append(f"- Command: {attempted_command}")
                parts.append("\n".join(lines))
        if not parts:
            return ""
        return "\n\n".join(parts)

    def _build_working_summary(
        self,
        task: Task,
        view: WorkItemContextView | None = None,
    ) -> str:
        if view is None:
            view = WorkItemContextView(None, task)
        lines: list[str] = []
        # Runtime context prefetch: progress_log from view (work_item-side preferred
        # when Step 9 mirror present; task-side fallback otherwise).
        progress_log = view.get_list("progress_log")
        if progress_log:
            lines.append("Recent progress:")
            lines.extend(f"- {item}" for item in progress_log[-4:])
        latest_comms_failure = dict(task.context_snapshot.get("latest_comms_failure", {}) or {})
        if latest_comms_failure:
            lines.append(
                "Latest comms failure: "
                f"{str(latest_comms_failure.get('operation', '') or '').strip()} "
                f"{str(latest_comms_failure.get('from_role', '') or '').strip()} -> "
                f"{str(latest_comms_failure.get('to_role', '') or '').strip()} "
                f"({str(latest_comms_failure.get('reason', '') or '').strip()})"
            )
        working_memory = list(task.metadata.get("working_memory", []))
        if working_memory:
            lines.append("Working notes:")
            lines.extend(f"- {item}" for item in working_memory[-4:])
        work_item_summary_for_downstream = str(view.get("work_item_summary_for_downstream", "") or "").strip()
        if work_item_summary_for_downstream:
            lines.append(f"Latest handoff summary: {work_item_summary_for_downstream}")
        work_item_summary = str(view.get("work_item_summary", "") or "").strip()
        if work_item_summary:
            lines.append(f"Latest work-item summary: {work_item_summary}")
        verification_status = view.get_dict("verification_status")
        if verification_status:
            label = str(verification_status.get("label", "") or "").strip()
            summary = str(verification_status.get("summary", "") or "").strip()
            if label or summary:
                rendered = label or "verification"
                if summary:
                    rendered = f"{rendered}: {summary}"
                lines.append(f"Verification status: {rendered}")
        work_item_artifact_index = self._render_work_item_artifact_index(task.metadata.get("work_item_artifact_index"))
        if work_item_artifact_index:
            lines.append("Artifact index:")
            lines.extend(f"- {line}" for line in work_item_artifact_index.splitlines())
        gate_rework = task.metadata.get("gate_rework_request", {})
        gate_feedback = str(task.metadata.get("gate_review_feedback", "")).strip()
        if isinstance(gate_rework, dict) and gate_rework:
            gate_feedback = str(gate_rework.get("feedback", "") or gate_feedback).strip()
        if gate_feedback:
            rework_round = (
                gate_rework.get("rework_round", task.metadata.get("gate_rework_round", 1))
                if isinstance(gate_rework, dict)
                else task.metadata.get("gate_rework_round", 1)
            )
            reviewer_role = (
                str(gate_rework.get("reviewer_role", "")).strip()
                if isinstance(gate_rework, dict)
                else ""
            )
            review_projection = (
                str(gate_rework.get("review_projection_title", "")).strip()
                if isinstance(gate_rework, dict)
                else ""
            )
            gate_instructions = str(
                gate_rework.get("gate_instructions", task.metadata.get("gate_instructions", ""))
                if isinstance(gate_rework, dict)
                else task.metadata.get("gate_instructions", "")
            ).strip()
            lines.append(f"## Gate Rework (Round {rework_round})")
            if reviewer_role or review_projection:
                requested_by = " / ".join(part for part in (review_projection, reviewer_role) if part)
                lines.append(f"Requested by: {requested_by}")
            lines.append(f"Reviewer feedback:\n{gate_feedback}")
            if gate_instructions:
                lines.append(f"Gate criteria: {gate_instructions}")
            lines.append("Address ALL issues above before resubmitting.")
        review_rework_feedback = str(task.metadata.get("rework_feedback", "")).strip()
        if review_rework_feedback:
            review_verdict = task.metadata.get("structured_review_verdict", {})
            if not isinstance(review_verdict, dict):
                review_verdict = {}
            reviewer_role = str(review_verdict.get("reviewer_role", "") or "").strip()
            review_round = safe_rework_count(
                task.metadata.get("review_rework_count", 0)
            ) + 1
            blocking = [
                str(item).strip()
                for item in list(review_verdict.get("blocking_issues", []) or [])
                if str(item).strip()
            ]
            followups = [
                str(item).strip()
                for item in list(review_verdict.get("followups", []) or [])
                if str(item).strip()
            ]
            lines.append(f"## Review Rework (Round {review_round})")
            if reviewer_role:
                lines.append(f"Rejected by reviewer: {reviewer_role}")
            lines.append(f"Reviewer feedback:\n{review_rework_feedback}")
            if blocking:
                lines.append("Blocking issues to resolve:")
                lines.extend(f"- {item}" for item in blocking)
            if followups:
                lines.append("Follow-ups requested:")
                lines.extend(f"- {item}" for item in followups)
            lines.append(
                "Address ALL blocking issues above and resubmit — do not repeat the previous submission."
            )
        gate_harness_feedback = str(task.metadata.get("gate_harness_rework_feedback", "")).strip()
        if gate_harness_feedback and not self._suppress_gate_harness_working_summary(task):
            lines.append("## Gate Harness Rework")
            lines.append(f"LLM judge feedback:\n{gate_harness_feedback}")
            lines.append("Fix the issues above before resubmitting this work item.")
        contract_rework = task.metadata.get("contract_rework_request", {})
        contract_feedback = str(task.metadata.get("contract_rework_feedback", "")).strip()
        if isinstance(contract_rework, dict) and contract_rework:
            issues = [
                str(item).strip()
                for item in list(contract_rework.get("issues", []) or [])
                if str(item).strip()
            ]
            if issues:
                contract_feedback = "\n".join(f"- {item}" for item in issues)
        if contract_feedback:
            rework_round = (
                contract_rework.get("rework_round", task.metadata.get("contract_rework_count", 1))
                if isinstance(contract_rework, dict)
                else task.metadata.get("contract_rework_count", 1)
            )
            max_retries = (
                contract_rework.get("max_retries", task.metadata.get("contract_rework_max_retries", 2))
                if isinstance(contract_rework, dict)
                else task.metadata.get("contract_rework_max_retries", 2)
            )
            lines.append(f"## Contract Rework (Round {rework_round}/{max_retries})")
            lines.append(f"Missing required outputs:\n{contract_feedback}")
            lines.append("Fix every missing item above before claiming the work item is complete.")
        return "\n".join(lines).strip()

    @staticmethod
    def _suppress_gate_harness_working_summary(task: Task) -> bool:
        """Keep stale business-turn gate state out of auxiliary prompts."""

        metadata = dict(task.metadata or {})
        turn_type = turn_type_for_task(task, fallback="")
        current_turn_mode = resolve_company_turn_mode(task)
        return bool(
            metadata.get("suppress_company_rework_feedback_context", False)
            or turn_type in {"review", "report", "self_evolution"}
            or current_turn_mode in {"review_execute", "report_required"}
            or metadata.get("review_execution_work_item", False)
            or metadata.get("report_execution_work_item", False)
            or metadata.get("self_evolution_work_item", False)
            or metadata.get("attention_work_item", False)
            or metadata.get("runtime_auxiliary_task", False)
            or metadata.get("company_runtime_auxiliary_task", False)
        )

    def _build_readiness_summary(self, task: Task) -> str:
        lines: list[str] = []
        output_contract = render_output_contract_context(
            output_contract_metadata(task.metadata or {}),
            heading="Runtime output contract:",
            include_workspace_root=False,
        )
        if output_contract:
            lines.append(output_contract)
        workspace_manifest = dict(task.metadata.get("workspace_manifest", {}) or {})
        if workspace_manifest:
            root_path = str(workspace_manifest.get("root_path", "")).strip()
            if root_path:
                lines.append(f"Workspace root: {root_path}")
            reserved_paths = dict(workspace_manifest.get("reserved_paths", {}) or {})
            if reserved_paths:
                lines.append("Reserved workspace paths:")
                for key, value in list(reserved_paths.items())[:6]:
                    key_text = str(key).strip()
                    value_text = str(value).strip()
                    if key_text and value_text:
                        lines.append(f"- {key_text}: {value_text}")
        else:
            workspace_root = (
                str(task.metadata.get("workspace_root", "") or "").strip()
                or str(task.metadata.get("comms_workspace_root", "") or "").strip()
            )
            output_root = (
                str(task.metadata.get("output_root", "") or "").strip()
                or str(task.metadata.get("target_output_dir", "") or "").strip()
            )
            if workspace_root:
                lines.append(f"Workspace root: {workspace_root}")
                if output_root:
                    lines.append(f"Project output root: {output_root}")
                else:
                    lines.append("Project output root: not locked yet; choose one under the workspace if the task needs a dedicated folder.")
        environment_manifest = dict(task.metadata.get("environment_manifest", {}) or {})
        inherited_environment = dict(task.metadata.get("inherited_environment", {}) or {})
        env_tools = list(environment_manifest.get("tools_installed", []) or inherited_environment.get("tools_available", []) or [])
        if environment_manifest or inherited_environment:
            lines.append("Environment readiness is available from upstream manifests.")
            preview: list[str] = []
            for item in env_tools[:6]:
                if isinstance(item, dict):
                    label = str(item.get("name", "") or item.get("tool", "") or item.get("path", "")).strip()
                    if label:
                        preview.append(label)
                elif str(item).strip():
                    preview.append(str(item).strip())
            if preview:
                lines.append(f"Available tools: {', '.join(preview)}")
        data_report = dict(task.metadata.get("data_acquisition_report", {}) or {})
        if data_report:
            status = str(data_report.get("status", "")).strip()
            designated_input_dir = str(data_report.get("designated_input_dir", "")).strip()
            if status:
                lines.append(f"Data readiness status: {status}")
            if designated_input_dir:
                lines.append(f"Designated input dir: {designated_input_dir}")
            required_inputs = [str(item).strip() for item in list(data_report.get("required_inputs", []) or []) if str(item).strip()]
            missing_inputs = [str(item).strip() for item in list(data_report.get("missing_inputs", []) or []) if str(item).strip()]
            if required_inputs:
                lines.append("Required inputs:")
                lines.extend(f"- {item}" for item in required_inputs[:6])
            if missing_inputs:
                lines.append("Missing inputs:")
                lines.extend(f"- {item}" for item in missing_inputs[:6])
            attempted_sources = [str(item).strip() for item in list(data_report.get("attempted_sources", []) or []) if str(item).strip()]
            if attempted_sources:
                lines.append("Attempted sources:")
                lines.extend(f"- {item}" for item in attempted_sources[:6])
            prepared_assets = [str(item).strip() for item in list(data_report.get("prepared_assets", []) or []) if str(item).strip()]
            if prepared_assets:
                lines.append("Prepared assets:")
                lines.extend(f"- {item}" for item in prepared_assets[:6])
            blocked_reasons = [str(item).strip() for item in list(data_report.get("blocked_reasons", []) or []) if str(item).strip()]
            if blocked_reasons:
                lines.append("Acquisition blockers:")
                lines.extend(f"- {item}" for item in blocked_reasons[:6])
            provenance_summary = str(data_report.get("provenance_summary", "")).strip()
            if provenance_summary:
                lines.append(f"Provenance summary: {provenance_summary}")
        if not lines:
            return ""
        return "## Readiness Artifacts\n" + "\n".join(lines)

    def _build_readiness_summary_body(self, task: Task) -> str:
        """Return the readiness summary body text WITHOUT a header.

        Used by ``build_core_context`` to fold workspace / environment /
        data-readiness signals into the unified ``## Runtime State``
        section instead of emitting a separate ``## Readiness Artifacts``
        header.
        """
        rendered = self._build_readiness_summary(task)
        if not rendered:
            return ""
        _, _, body = rendered.partition("\n")
        return body.strip()

    def _render_work_item_runtime_plan(self, value: Any) -> str:
        """Render the work-item-specific runtime plan fields.

        Inputs / Deliverables / Acceptance Criteria / Out of Scope
        are intentionally NOT rendered here — they are the canonical
        property of the work-item identity block (``_build_work_item_description``
        in company_mode.py and the work_item_assignment section in
        ``build_task_brief``), and re-rendering them here would be a
        duplication. This function is only responsible for the
        work-item execution fields that the identity block does
        not carry (collaboration expectations, execution sequence,
        media mode rules, download priority, verification status).
        Returns "" if none of those fields are populated.
        """
        if not isinstance(value, dict):
            return ""
        lines: list[str] = []
        summary = str(value.get("summary", "") or "").strip()
        turn_type = str(value.get("turn_type", "") or "").strip()
        if turn_type:
            lines.append(f"Work item turn type: {turn_type}")
        if summary:
            lines.append(summary)
        for heading, key in (
            ("Collaboration Expectations", "collaboration_expectations"),
            ("Execution Sequence", "execution_sequence"),
            ("Media Mode Triggers", "media_mode_triggers"),
            ("Media Mode Rules", "media_mode_rules"),
            ("Download Priority", "download_priority"),
        ):
            items = [str(item).strip() for item in value.get(key, []) if str(item).strip()]
            if items:
                lines.append(f"{heading}:")
                lines.extend(f"- {item}" for item in items[:6])
        verification_required = value.get("verification_required")
        if isinstance(verification_required, bool):
            lines.append(
                "Verification required before completion."
                if verification_required
                else "No automatic verification pass is required for this work item."
            )
        return "\n".join(lines).strip()

    def _render_work_item_artifact_index(self, value: Any) -> str:
        if not isinstance(value, list):
            return ""
        lines: list[str] = []
        for item in value[:8]:
            if isinstance(item, dict):
                label = str(item.get("label", "") or item.get("name", "") or "artifact").strip() or "artifact"
                rendered = str(item.get("value", "") or item.get("location", "") or item.get("path", "") or "").strip()
                if rendered:
                    lines.append(f"{label}: {rendered}")
            elif isinstance(item, str) and item.strip():
                lines.append(item.strip())
        return "\n".join(lines)

    def _summarize_dependency(self, dep_task: Task) -> list[str]:
        lines = [f"- {dep_task.title}: completed"]
        result_content = ""
        if dep_task.result and dep_task.result.get("content"):
            result_content = str(dep_task.result["content"]).strip()
        work_item_summary_for_downstream = str(dep_task.metadata.get("work_item_summary_for_downstream", "")).strip()
        summary = work_item_summary_for_downstream or result_content
        if summary:
            lines.append(f"  Summary: {summary}")
        # Producer-side backstops: ``dep_task.metadata.decisions /
        # risks / artifacts`` are not capped upstream, so these
        # are the only guards against a pathological upstream
        # work item producing unbounded lists. Each individual entry
        # is rendered in full (no character chopping) — only the
        # item count is bounded.
        decisions = list(dep_task.metadata.get("decisions", []))
        if decisions:
            lines.append("  Decisions:")
            lines.extend(f"  - {item}" for item in decisions[:6])
        risks = list(dep_task.metadata.get("risks", []))
        if risks:
            lines.append("  Risks:")
            lines.extend(f"  - {item}" for item in risks[:6])
        artifacts = list(dep_task.metadata.get("artifacts", []))
        if artifacts:
            lines.append("  Artifacts:")
            lines.extend(f"  - {item}" for item in artifacts[:8])
        return lines

    def _memory_policy(self, task: Task) -> dict[str, Any]:
        runtime_policy = task.metadata.get("policy") or task.metadata.get("runtime_policy", {})
        if not isinstance(runtime_policy, dict):
            return {}
        memory_policy = runtime_policy.get("memory", {})
        return memory_policy if isinstance(memory_policy, dict) else {}
