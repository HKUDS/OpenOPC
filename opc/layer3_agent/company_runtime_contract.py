"""Shared company/runtime contract builders for native and external agents."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from opc.core.company_tools import resolve_company_turn_mode
from opc.core.models import Task
from opc.layer2_organization.prompt_contract import render_target_prompt_contract
from opc.layer2_organization.work_item_identity import turn_type_for_task
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task
from opc.layer4_tools.output_budget import clip_text

ContractAudience = Literal["native", "external"]


def _runtime_clock_block(now: datetime | None = None) -> str:
    """Anchor relative-time work to the runtime clock for every executor."""

    current = (now or datetime.now().astimezone()).astimezone()
    timezone_name = str(current.tzname() or current.strftime("%z") or "local")
    return "\n".join(
        [
            "## Runtime Clock",
            f"- Current local date: `{current.date().isoformat()}`",
            f"- Current local time zone: `{timezone_name}` (`{current.strftime('%z')}`)",
            "- Resolve relative periods such as today, this week, and the last three months against this clock. State the resulting absolute date range before delegating or researching.",
        ]
    )

_COMPANY_WORK_ITEM_GUIDELINES = """
## Company Work Item Contract
- You are executing one projected work item inside company mode, not the whole project alone.
- Stay inside the current work-item boundary and use upstream handoffs, annotations, and inbox context before re-solving prior work.
- Prefer asynchronous collaboration tools for cross-role clarification. Use meetings only for genuine cross-role decisions or conflicts.
- Keep downstream handoffs crisp: preserve decisions, risks, open questions, artifacts, and verification status when they matter.
- Team collaboration MUST flow through the collaboration tools and inbox context. Plain assistant prose does not count as inter-member coordination.
- External executors such as Codex / OpenCode / Claude Code are temporary workers you may call; they are not organization members and do not replace your role ownership.
- Respect the work-item ownership and artifact contracts in task metadata. If you cannot satisfy them, surface that explicitly instead of silently widening scope.

## Proactive Collaboration Requirements
- BEFORE completing your work item, review the injected inbox context for messages from peers, managers, or downstream consumers; reply to pending questions or acknowledge handled messages with `inbox(action="ack")`.
- If you handle an approval/review request through the manager board or task verdict, acknowledge the matching inbox message unless `reply_message` already acknowledged it.
- If your work depends on or overlaps with a parallel peer's output, use `send_dm` or `ask_peer_and_wait` to confirm alignment BEFORE finalizing.
- If you discover a gap, conflict, or dependency that affects another role, use `send_dm` or `broadcast_issue` immediately - do NOT silently deliver incomplete work.
- If you cannot complete your deliverables because a peer hasn't finished, send them a message and continue with what you can. Use `ask_peer_and_wait` with `on_timeout="continue"` so you resume automatically if no reply arrives within the timeout window.
- Delivering incomplete work without attempting coordination is a policy violation.
- Waiting forever without a timeout fallback is also a policy violation - always set a reasonable `on_timeout` (prefer `continue` or `manager`).
"""

_MULTI_TEAM_ORG_GUIDELINES = """
## Organization Runtime Contract
- You are one seat inside company mode; work inside your assigned WorkItem.
- The runtime has prepared your assignment, mailbox snapshot, board context, and workspace roots for this turn.
- The WorkItem is the collaboration source of truth. Use WorkItem IDs for collaboration; never use runtime Task IDs as WorkItem IDs.
- `workspace_root` and `comms_workspace_root` are guaranteed. `output_root` may be blank; if needed, choose a suitable subfolder under `workspace_root` and communicate it in handoffs or delegation briefs.
- Kanban state is advanced by the runtime from completion reports and review verdicts. Do not manually flip board states.
- There is no need to poll work items for progress: the runtime watches state changes and re-activates whichever roles need to act once delegated or dependent work completes. So after you have confirmed that your delegation succeeded or that your review verdict advanced the work, end your current run if nothing else requires your own work — the system monitors on your behalf.
- Use mailbox tools only for coordination, questions, blockers, or handoffs.
- Cross-team collaboration is request-based; only direct managers delegate executable work.
- If you are the root final decider, only your finished turn is the authoritative owner-facing result.
"""


_MANAGER_RUNTIME_CONTRACT = """
## Manager Runtime Contract
- You have direct reports or an allowed delegation surface. Decide from the current WorkItem whether child WorkItems improve capability fit, parallelism, or independent review; having reports alone is not a reason to delegate.
- Use `delegate_work` only to CREATE child WorkItems for direct reports.
- Use `modify_work_item` to revise an existing child WorkItem when the owner is still right but the brief, deliverables, acceptance criteria, or dependencies changed.
- Use `delete_work_item` to cancel/hide an obsolete or wrong child WorkItem so it no longer blocks the parent board.
- Use `manager_board_read` only to READ child-board state. For your current board, omit `parent_work_item_id` or use the current WorkItem ID; never use the runtime Task ID.
- Do not send executable orders outside your direct reports; coordinate with peers or other teams by request.
"""

_COMPANY_PLAN_WORK_ITEM_GUIDELINES = """
## Company Work Item Turn: Plan / Intake / Dispatch
- Investigate first, then convert findings into a concrete work-item plan with sequencing, assumptions, and validation targets.
- Prefer read-only `agent_spawn(profile='explore')` exploration when the codebase slice is broad.
- Avoid implementation-level edits unless the task explicitly assigns execution to this work item.
- During intake or initial dispatch, do NOT use `ask_peer_and_wait` on a role that does not already have an active work package. Create or delegate the work first, then use `send_dm` for non-blocking coordination.
- If you need another role's view before delegation exists, send a non-blocking message or include the question in the delegated brief; do not park the whole project startup.
"""

_COMPANY_EXECUTE_WORK_ITEM_GUIDELINES = """
## Company Work Item Turn: Execute
- Follow the current WorkItem's assigned execution scope once the approach is clear.
- Use `agent_spawn(profile='explore')` for read-only exploration when it reduces context noise.
- If work-item swarm tools are available, use the shared microtask board to break the assigned slice into tactical work items before spawning burst workers.
- Before handing off, leave evidence that a reviewer can verify quickly: changed areas, artifact pointers, and any remaining risks.
- Treat write scope as constrained by the ownership contract. Do not edit outside that scope unless the task metadata explicitly expands it.
- Your completion bar is higher than "it works on my turn": leave a handoff that satisfies summary, artifact index, decisions, risks, open questions, and verification status.
"""

_COMPANY_REVIEW_WORK_ITEM_GUIDELINES = """
## Company Work Item Turn: Review

You are reviewing a subordinate's deliverable. The runtime applies your verdict mechanically — approve sends the work to done, reject sends it back to the worker with your summary + blocking_issues as rework feedback. The runtime does NOT second-guess the shape or content of your verdict; you are responsible for the call.

### How to judge
- You have read access to the workspace. Use your tools (file_read, bash, git_*, web_search, etc.) to verify the worker's claims against the actual current state. Don't trust the handoff blindly and don't reject blindly either.
- Current workspace evidence is the truth. Previous review notes and old memory are leads to re-check, not facts.
- Reject only for gaps that still exist now. If an earlier finding was fixed, don't repeat it.
- Compare the deliverable against the original brief and acceptance criteria in plain English: did the assignee produce the requested output, or did they only provide analysis/planning? If the brief asked for an artifact and the worker shipped a plan, reject.

### Verdict (suggested JSON shape)
End your turn with one JSON object on its own line:

  Approve: `{"review_verdict":"approve","summary":"<concrete reason it meets the bar>"}`
  Reject:  `{"review_verdict":"reject","summary":"<overall reason>","blocking_issues":["<specific change needed>"],"followups":["<non-blocking improvement>"]}`

If you cannot be parsed into approve or reject, the runtime will spawn one more review attempt and ask you again; after that it will escalate to a human. So please emit a clear label.

You are trusted to choose the level of detail. A short summary on a clear approve is fine. For rejections, name specific files / tests / artifacts in `blocking_issues` so the worker can act.
"""

_COMPANY_AGGREGATE_WORK_ITEM_GUIDELINES = """
## Company Work Item Turn: Aggregate / Deliver
- Synthesize upstream outputs into a decision-ready summary instead of repeating every intermediate detail.
- Preserve artifact pointers, unresolved risks, and owner-facing next actions.
- Keep the final surface area small enough that the next work item can act without replaying the whole run.
- Aggregation does not erase accountability: preserve which role produced which artifact or review conclusion when it matters for follow-up.
- When possible, end with a compact JSON object like `{"delivery_package":{"executive_summary":"...","delivered_items":[],"artifact_manifest":[],"risks":[],"open_issues":[],"next_steps":[]}}` so the final delivery stays structured.
"""


_COMPANY_REPORT_GENERATION_HEADER = """
## Work Item Turn: Report Generation

You have just finished executing the assigned work. This turn is dedicated to writing a self-contained handoff report for your reviewer. Do NOT do any new execution work in this turn — your execution is already done. Do NOT delegate, do not message peers, do not run the original task again.

Use your own session context plus the runtime-injected execute-turn summary/evidence below. If your session memory is unavailable but the injected execute-turn summary, output, artifacts, or verification evidence are present, use those injected facts as the handoff source. Do not claim you lack context merely because this is a report-only turn.

### Suggested report shape (JSON, not strictly required)
End your turn with EXACTLY one JSON object on its own line:
  ```
  {
    "summary": "<2-3 sentence overall outcome>",
    "deliverables": [
      {"name": "<deliverable name>", "path": "<path or pointer>", "status": "complete" | "partial" | "blocked"}
    ],
    "acceptance_status": [
      {"criterion": "<original acceptance criterion>", "met": true | false, "evidence": "<file path / command / proof>"}
    ],
    "risks": ["<known risk or caveat>"],
    "next_actions": ["<what reviewer or downstream should do next>"]
  }
  ```

If a structured shape doesn't fit your situation, write a clear narrative report instead — the runtime will pass your prose to the reviewer as-is. Do NOT make up content to fill the schema; leave fields out or fall back to narrative.

### Why this matters
The reviewer will receive this report PLUS the original brief and will independently verify your claims with their own tools (file_read, bash, etc.). Be honest about partial work and open issues — silent gaps will be caught by the reviewer and counted against this delivery.
""".strip()


_REVIEW_PENDING_HEADER = """
## Review Requirement
- One or more of your direct reports has submitted a completed work item for your review. You MUST clear the review queue before dispatching new children, monitoring, or executing local work.
- For each pending item: compare the deliverable against the original acceptance criteria and non_overlap_guard. Inspect artifacts, completion reports, and any cross-team coordination notes.
- Also compare the result against the original work item brief in plain English: did the assignee produce the requested output, or did they only provide analysis/planning/search notes? If the brief asked for actual production, reject planning-only submissions and request concrete rework.
- Treat current files and command results as the truth. Previous review findings are only leads; verify them against the latest workspace before repeating them.

### Verdict (suggested JSON shape, one per item)
  Approve: `{"review_verdict":"approve","summary":"<concrete reason>"}`
  Reject:  `{"review_verdict":"reject","summary":"<reason>","blocking_issues":["<specific fix>"],"followups":["<nice-to-have>"]}`

The runtime applies the verdict mechanically. For rejections, name specific files / tests / artifacts in `blocking_issues` so the worker can act on your feedback.

- If a review depends on information you lack (e.g., evidence from another team), send a targeted `send_dm` or `ask_peer_and_wait` message rather than approving blindly.
- Do NOT approve simply to unblock the pipeline; reject with specific, actionable feedback if acceptance criteria are not met.
""".strip()


_REVIEW_EXECUTE_HEADER = """
## Kanban Review Turn

This turn is a dedicated review of one child work item. Do not dispatch new work, do not rewrite scope, and do not message peers unless your review depends on information you cannot get yourself.

### Inputs you have
- The original brief (target description) below.
- The worker's handoff report below.
- Your own session memory of any prior review on this item.
- Read access to the workspace via your tools (file_read, bash, git_*, web_search, etc.).

### How to judge
- Use your tools to verify the worker's claims directly against the workspace. Don't trust the handoff blindly and don't reject blindly. Current workspace evidence is the truth.
- Treat the original brief as the contract. Approve only if the submitted result actually satisfies the requested production output; if the worker shipped only a plan or concept memo when the brief required an artifact or implementation, reject and request rework.
- Before rejecting, re-check the cited paths from the latest report; do not reuse stale line numbers or already-fixed findings.

### Verdict (suggested JSON shape)
End your turn with one JSON object on its own line:

  Approve: `{"review_verdict":"approve","summary":"<concrete reason it meets the bar>"}`
  Reject:  `{"review_verdict":"reject","summary":"<overall reason>","blocking_issues":["<specific change needed>"],"followups":["<non-blocking improvement>"]}`

The runtime applies your verdict mechanically — approve moves the child to done, reject sends it back to the worker with your summary + blocking_issues as rework feedback. The runtime does NOT second-guess the shape or content of your verdict. You are responsible for the call.

If your output cannot be parsed into approve or reject, the runtime will give you one more review attempt with a parse-failure hint, and after that it will escalate to a human reviewer. So please emit a clear label.
""".strip()


def _format_pending_review_items(items: list[Any]) -> str:
    lines: list[str] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        work_item_id = str(entry.get("work_item_id") or "").strip()
        if not work_item_id:
            continue
        title = str(entry.get("title") or "").strip() or "(untitled)"
        role_id = str(entry.get("role_id") or "").strip() or "unknown"
        deliverable_summary = str(entry.get("deliverable_summary") or "").strip()
        phase = str(entry.get("phase") or "").strip() or "awaiting_manager_review"
        line = f"- work_item_id=`{work_item_id}` role=`{role_id}` title=`{title}` phase=`{phase}`"
        if deliverable_summary:
            preview = clip_text(
                deliverable_summary.replace("\n", " "),
                limit=200,
                marker="deliverable summary preview truncated",
                prefer_newline=False,
            ).text
            line += f" deliverable_preview=`{preview}`"
        review_evidence = dict(entry.get("review_evidence", {}) or {})
        output_paths = [
            str(item).strip()
            for item in list(review_evidence.get("output_paths", []) or [])
            if str(item).strip()
        ]
        verification_status = dict(review_evidence.get("verification_results", {}) or {}).get("status", {})
        verification_label = str(verification_status.get("label", "") or "").strip()
        if verification_label:
            line += f" verification=`{verification_label}`"
        if output_paths:
            line += f" outputs=`{', '.join(output_paths[:3])}`"
        lines.append(line)
    return "\n".join(lines)


def _review_pending_block(task: Task) -> str:
    runtime_model = str(task.metadata.get("runtime_model", "") or "").strip()
    if runtime_model != "multi_team_org":
        return ""
    if resolve_company_turn_mode(task) != "review_pending":
        return ""
    pending_items = list(task.metadata.get("pending_review_items", []) or [])
    if not pending_items:
        pending_items = list(task.context_snapshot.get("pending_review_items", []) or [])
    rendered = _format_pending_review_items(pending_items)
    if not rendered:
        return _REVIEW_PENDING_HEADER
    return f"{_REVIEW_PENDING_HEADER}\n\n### Pending Review Queue\n{rendered}"


def _review_execute_block(task: Task) -> str:
    runtime_model = str(task.metadata.get("runtime_model", "") or "").strip()
    if runtime_model != "multi_team_org":
        return ""
    turn_mode = resolve_company_turn_mode(task)
    work_item_turn_type = turn_type_for_task(task, fallback="")
    explicit_review_turn = bool(
        task.metadata.get("review_execution_work_item", False)
        or task.metadata.get("review_task", False)
        or str(task.metadata.get("review_target_work_item_id", "") or "").strip()
    )
    if not explicit_review_turn:
        return ""
    if turn_mode != "review_execute" and work_item_turn_type != "review":
        return ""
    target_work_item_id = str(
        task.metadata.get("review_target_work_item_id")
        or linked_work_item_id_for_task(task)
        or ""
    ).strip()
    if not target_work_item_id:
        return _REVIEW_EXECUTE_HEADER
    title = str(task.metadata.get("review_target_title", "") or "").strip()
    worker_role_id = str(task.metadata.get("review_target_worker_role_id", "") or "").strip()
    completion_report = str(task.metadata.get("review_completion_report", "") or "").strip()
    review_evidence = dict(task.metadata.get("review_evidence", {}) or {})
    prompt_contract = dict(task.metadata.get("prompt_contract", {}) or {})
    target_contract = dict(task.metadata.get("review_target_prompt_contract", {}) or prompt_contract.get("target_contract", {}) or {})
    lines = [
        _REVIEW_EXECUTE_HEADER,
        "",
        "### Target Child Work Item",
        f"- work_item_id=`{target_work_item_id}`",
    ]
    if title:
        lines.append(f"- title=`{title}`")
    if worker_role_id:
        lines.append(f"- worker_role=`{worker_role_id}`")
    rendered_target_contract = render_target_prompt_contract(target_contract)
    if rendered_target_contract:
        lines.append("")
        lines.append(rendered_target_contract)
    if completion_report:
        lines.append("")
        lines.append("### Completion Report")
        lines.append(clip_text(
            completion_report,
            limit=2000,
            marker="completion report preview truncated",
        ).text)
    artifact_manifest = [
        dict(item)
        for item in list(review_evidence.get("artifact_manifest", []) or [])
        if isinstance(item, dict)
    ]
    changed_areas = [
        str(item).strip()
        for item in list(review_evidence.get("changed_areas", []) or [])
        if str(item).strip()
    ]
    verification_results = dict(review_evidence.get("verification_results", {}) or {})
    verification_status = dict(verification_results.get("status", {}) or {})
    verification_checks = [
        dict(item)
        for item in list(verification_results.get("checks", []) or [])
        if isinstance(item, dict)
    ]
    key_commands = [
        str(item).strip()
        for item in list(review_evidence.get("key_commands", []) or [])
        if str(item).strip()
    ]
    output_paths = [
        str(item).strip()
        for item in list(review_evidence.get("output_paths", []) or [])
        if str(item).strip()
    ]
    open_risks = [
        str(item).strip()
        for item in list(review_evidence.get("open_risks", []) or [])
        if str(item).strip()
    ]
    if artifact_manifest:
        lines.append("")
        lines.append("### Artifact Manifest")
        for item in artifact_manifest[:10]:
            label = str(item.get("label", "") or item.get("kind", "") or "artifact").strip()
            value = str(item.get("value", "") or "").strip()
            lines.append(f"- {label}: `{value}`" if value else f"- {label}")
    if changed_areas:
        lines.append("")
        lines.append("### Changed Areas")
        lines.extend(f"- `{item}`" for item in changed_areas[:10])
    if verification_status or verification_checks:
        lines.append("")
        lines.append("### Verification")
        if verification_status:
            label = str(verification_status.get("label", "") or "").strip()
            summary = str(verification_status.get("summary", "") or "").strip()
            if label:
                lines.append(f"- status=`{label}`")
            if summary:
                lines.append(f"- summary={clip_text(summary, limit=300, marker='verification summary preview truncated').text}")
        for item in verification_checks[:6]:
            command = str(item.get("command", "") or "").strip()
            status = str(item.get("status", "") or "").strip()
            summary = str(item.get("summary", "") or "").strip()
            rendered = f"- `{command}` -> `{status}`" if command else f"- `{status}`"
            if summary:
                rendered += f" :: {summary[:220]}"
            lines.append(rendered)
    if key_commands:
        lines.append("")
        lines.append("### Key Commands")
        lines.extend(f"- `{item}`" for item in key_commands[:8])
    if output_paths:
        lines.append("")
        lines.append("### Output Paths")
        lines.extend(f"- `{item}`" for item in output_paths[:10])
    if open_risks:
        lines.append("")
        lines.append("### Open Risks")
        lines.extend(f"- {item}" for item in open_risks[:8])
    return "\n".join(lines)


def _render_report_source_evidence(task: Task) -> str:
    evidence = dict(task.metadata.get("report_source_evidence", {}) or {})
    summary = str(task.metadata.get("report_source_summary", "") or "").strip()
    result_content = str(task.metadata.get("report_source_result_content", "") or "").strip()
    lines: list[str] = []

    if summary:
        lines.append("### Last Execute-Turn Summary")
        lines.append(clip_text(summary, limit=2000, marker="execute summary preview truncated").text)
    if result_content and result_content != summary:
        lines.append("")
        lines.append("### Last Execute-Turn Output")
        lines.append(clip_text(result_content, limit=2000, marker="execute output preview truncated").text)

    artifact_manifest = [
        dict(item)
        for item in list(evidence.get("artifact_manifest", []) or [])
        if isinstance(item, dict)
    ]
    changed_areas = [
        str(item).strip()
        for item in list(evidence.get("changed_areas", []) or [])
        if str(item).strip()
    ]
    verification_results = dict(evidence.get("verification_results", {}) or {})
    verification_status = dict(verification_results.get("status", {}) or {})
    verification_checks = [
        dict(item)
        for item in list(verification_results.get("checks", []) or [])
        if isinstance(item, dict)
    ]
    key_commands = [
        str(item).strip()
        for item in list(evidence.get("key_commands", []) or [])
        if str(item).strip()
    ]
    output_paths = [
        str(item).strip()
        for item in list(evidence.get("output_paths", []) or [])
        if str(item).strip()
    ]
    open_risks = [
        str(item).strip()
        for item in list(evidence.get("open_risks", []) or [])
        if str(item).strip()
    ]

    if artifact_manifest:
        lines.append("")
        lines.append("### Known Artifacts From Execute Turn")
        for item in artifact_manifest[:10]:
            label = str(item.get("label", "") or item.get("kind", "") or "artifact").strip()
            value = str(item.get("value", "") or "").strip()
            lines.append(f"- {label}: `{value}`" if value else f"- {label}")
    if changed_areas:
        lines.append("")
        lines.append("### Changed Areas")
        lines.extend(f"- `{item}`" for item in changed_areas[:10])
    if verification_status or verification_checks:
        lines.append("")
        lines.append("### Verification Evidence From Execute Turn")
        if verification_status:
            label = str(verification_status.get("label", "") or "").strip()
            summary_text = str(verification_status.get("summary", "") or "").strip()
            if label:
                lines.append(f"- status=`{label}`")
            if summary_text:
                lines.append(f"- summary={clip_text(summary_text, limit=300, marker='verification summary preview truncated').text}")
        for item in verification_checks[:6]:
            command = str(item.get("command", "") or "").strip()
            status = str(item.get("status", "") or "").strip()
            summary_text = str(item.get("summary", "") or "").strip()
            rendered = f"- `{command}` -> `{status}`" if command else f"- `{status}`"
            if summary_text:
                rendered += f" :: {summary_text[:220]}"
            lines.append(rendered)
    if key_commands:
        lines.append("")
        lines.append("### Key Commands")
        lines.extend(f"- `{item}`" for item in key_commands[:8])
    if output_paths:
        lines.append("")
        lines.append("### Output Paths")
        lines.extend(f"- `{item}`" for item in output_paths[:10])
    if open_risks:
        lines.append("")
        lines.append("### Open Risks")
        lines.extend(f"- {item}" for item in open_risks[:8])

    if not lines:
        return ""
    return "\n".join(lines).strip()


def _report_execute_block(task: Task) -> str:
    """Return the report-generation prompt block when this turn is the
    hidden auxiliary report card spawned after a worker DONE.

    Two-turn worker→review handoff: instead of the worker's last execute
    turn prose being treated as the report, the runtime spawns a separate
    report card that resumes the same worker session and asks for an
    explicit structured handoff. This block is the prompt for that turn.
    """
    runtime_model = str(task.metadata.get("runtime_model", "") or "").strip()
    if runtime_model != "multi_team_org":
        return ""
    turn_mode = resolve_company_turn_mode(task)
    work_item_turn_type = turn_type_for_task(task, fallback="")
    is_report_turn = (
        turn_mode == "report_required"
        or work_item_turn_type == "report"
        or bool(task.metadata.get("report_execution_work_item", False))
    )
    if not is_report_turn:
        return ""
    prompt_contract = dict(task.metadata.get("prompt_contract", {}) or {})
    target_contract = dict(task.metadata.get("report_target_prompt_contract", {}) or prompt_contract.get("target_contract", {}) or {})
    rendered_target_contract = render_target_prompt_contract(
        target_contract,
        heading="### Work Item Contract To Report Against",
    )
    source_evidence = _render_report_source_evidence(task)
    parts = [_COMPANY_REPORT_GENERATION_HEADER]
    if rendered_target_contract:
        parts.append(rendered_target_contract)
    if source_evidence:
        parts.append(source_evidence)
    return "\n\n".join(part for part in parts if part)


def _multi_team_manager_capable(task: Task) -> bool:
    """Return whether this seat has a management/delegation surface.

    This deliberately keys off role capability, not the current turn mode:
    a middle manager can be in an execute/integrate/review turn and still
    need manager guidance, while a leaf worker should not receive delegation
    planning rules.
    """
    metadata = dict(task.metadata or {})
    for key in ("direct_report_seat_ids", "allowed_delegate_role_ids", "direct_report_role_ids"):
        if [str(item).strip() for item in list(metadata.get(key, []) or []) if str(item).strip()]:
            return True
    if str(metadata.get("managed_team_id", "") or "").strip():
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
    if not current_seat:
        return False
    for key in ("direct_report_seat_ids", "allowed_delegate_role_ids", "direct_report_role_ids"):
        if [str(item).strip() for item in list(current_seat.get(key, []) or []) if str(item).strip()]:
            return True
    return bool(str(current_seat.get("managed_team_id", "") or "").strip())


def _manager_execution_decision_block(task: Task) -> str:
    runtime_model = str(task.metadata.get("runtime_model", "") or "").strip()
    if runtime_model != "multi_team_org":
        return ""
    if resolve_company_turn_mode(task) not in {"manager_decide", "dispatch_required"}:
        return ""
    if not _multi_team_manager_capable(task):
        return ""
    lines = [
        """
## Manager Execution Decision
- Assess the assigned outcome before choosing an execution shape. Preserve its requested deliverables, constraints, dependencies, and acceptance criteria.
- Delegate only when distinct downstream outcomes benefit from a direct report's capabilities, parallel execution, or independent review.
- Complete the WorkItem directly when it is small, primarily requires this leader's judgment, or decomposition would add coordination without improving the result.
- If delegating, create outcome-based child WorkItems with clear scope, dependencies, deliverables, and acceptance criteria. Do not create make-work merely because direct reports exist.
- If completing directly, produce the requested result in this turn; a plan or delegation discussion is not a substitute for the deliverable.
- For a follow-up over an existing board, inspect it with `manager_board_read` and use `modify_work_item` / `delete_work_item` when the existing allocation is no longer correct.
""".strip()
    ]
    metadata = dict(task.metadata or {})
    snapshot = dict(task.context_snapshot or {})
    followup_text = str(snapshot.get("user_supplied_input", "") or "").strip()
    is_final_decider_followup = bool(metadata.get("followup_routed_to_final_decider", False)) or bool(followup_text)
    if is_final_decider_followup:
        if followup_text:
            followup_preview = clip_text(followup_text, limit=800, marker="follow-up truncated").text
            lines.append(
                "\n".join(
                    [
                        "## User Follow-up Board Reconciliation",
                        f"User follow-up: {followup_preview}",
                    ]
                )
            )
        else:
            lines.append("## User Follow-up Board Reconciliation")
        lines.append(
            "\n".join(
                [
                    "- You are resuming this same role session with a fresh owner directive; rely on the session history already available to you.",
                    "- Decide the next step from the current state and available collaboration tools: answer directly, close review when appropriate, inspect or revise the board, delegate more work, or take another supported runtime action.",
                    "- When changing an existing board, inspect it with `manager_board_read` and prefer `modify_work_item` / `delete_work_item` for stale or wrong child work before adding more work.",
                    "- If you create replacement child work, also resolve obsolete siblings so old work does not keep running or blocking completion.",
                    "- Keep your final response focused on what you decided or changed.",
                ]
            )
        )
    mutation_action = str(metadata.get("manager_mutation_action", "") or "").strip()
    mutation_reason = str(metadata.get("manager_mutation_reason", "") or "").strip()
    mutation_user_input = str(
        metadata.get("latest_user_directive", "")
        or metadata.get("manager_mutation_user_input", "")
        or ""
    ).strip()
    if mutation_action == "modify" or mutation_user_input:
        mutation_lines = ["## Upstream Work Item Mutation Reconciliation"]
        if mutation_user_input:
            mutation_lines.append(
                f"Latest upstream user instruction: {clip_text(mutation_user_input, limit=800, marker='upstream user input truncated').text}"
            )
        if mutation_reason:
            mutation_lines.append(
                f"Manager mutation reason: {clip_text(mutation_reason, limit=500, marker='mutation reason truncated').text}"
            )
        mutation_lines.extend(
            [
                "- Your current WorkItem must follow this latest upstream instruction. Before creating replacement child work, inspect your existing child board with `manager_board_read`.",
                "- For each existing child, decide whether it still supports the revised parent brief. Use `modify_work_item` for a child that should continue under the new scope.",
                "- Use `delete_work_item` for stale children that still describe the old direction, especially suspended/running children left over from before Stop.",
                "- If you delegate a replacement child, also resolve the obsolete child so old work does not keep running or remain visible on the board.",
            ]
        )
        lines.append("\n".join(mutation_lines))
    return "\n\n".join(lines)


_EXTERNAL_TOOL_WORDING_REPLACEMENTS = {
    "Prefer read-only `agent_spawn(profile='explore')` exploration when the codebase slice is broad.": (
        "Prefer read-only exploration using your external agent's own search, "
        "inspection, or context-isolation capabilities when the workspace slice is broad."
    ),
    "Use `agent_spawn(profile='explore')` for read-only exploration when it reduces context noise.": (
        "Use your external agent's own search, inspection, or context-isolation "
        "capabilities for read-only exploration when it reduces context noise."
    ),
    "You have read access to the workspace. Use your tools (file_read, bash, git_*, web_search, etc.) to verify the worker's claims against the actual current state. Don't trust the handoff blindly and don't reject blindly either.": (
        "You have read access to the workspace. Use your external agent's "
        "available inspection, search, shell, version-control, browser, and "
        "verification capabilities to verify the worker's claims against the "
        "actual current state. Don't trust the handoff blindly and don't reject blindly either."
    ),
    "The reviewer will receive this report PLUS the original brief and will independently verify your claims with their own tools (file_read, bash, etc.). Be honest about partial work and open issues — silent gaps will be caught by the reviewer and counted against this delivery.": (
        "The reviewer will receive this report PLUS the original brief and will "
        "independently verify your claims with their own workspace inspection "
        "and verification capabilities. Be honest about partial work and open "
        "issues — silent gaps will be caught by the reviewer and counted against this delivery."
    ),
    "- Read access to the workspace via your tools (file_read, bash, git_*, web_search, etc.).": (
        "- Read access to the workspace via your external agent's available "
        "inspection, search, shell, version-control, browser, and verification capabilities."
    ),
}


def _normalize_contract_audience(audience: str | None) -> ContractAudience:
    return "external" if str(audience or "").strip().lower() == "external" else "native"


def _contract_text_for_audience(text: str, audience: ContractAudience) -> str:
    if audience != "external":
        return text
    rendered = text
    for old, new in _EXTERNAL_TOOL_WORDING_REPLACEMENTS.items():
        rendered = rendered.replace(old, new)
    return rendered


def _string_list(value: Any) -> list[str]:
    return [
        str(item).strip()
        for item in list(value or [])
        if str(item).strip()
    ]


def _team_capability_manifest_block(task: Task) -> str:
    metadata = dict(task.metadata or {})
    binding = dict(metadata.get("external_team_binding", {}) or {})
    manifest = dict(
        metadata.get("capability_manifest")
        or binding.get("capability_manifest")
        or {}
    )
    if not manifest:
        return ""
    lines = [
        "## Team Capability Manifest",
        f"- organizational_target_role_id: `{manifest.get('organizational_identity', '')}`",
        f"- display_name: `{manifest.get('display_name', '')}`",
        f"- manifest_hash: `{manifest.get('manifest_hash', '')}`",
        "- This manifest is compiled from OpenOPC role configuration; it describes the assigned organizational boundary, not the external team's internal structure.",
    ]
    covered_roles = [
        dict(item)
        for item in list(manifest.get("covered_roles", []) or [])
        if isinstance(item, dict)
    ]
    if covered_roles:
        lines.extend(["", "### Covered Role Responsibilities"])
        for role in covered_roles:
            role_id = str(role.get("role_id", "") or "").strip()
            name = str(role.get("name", "") or role_id).strip()
            responsibility = str(role.get("responsibility", "") or "").strip()
            rendered = f"- `{role_id}` ({name})"
            if responsibility:
                rendered += ": " + clip_text(
                    responsibility,
                    limit=600,
                    marker="responsibility truncated",
                ).text
            lines.append(rendered)
    for heading, key in (
        ("Declared Capabilities", "capabilities"),
        ("Expected Deliverables", "deliverables"),
        ("Artifact Contracts", "artifact_contract_refs"),
        ("Out Of Scope", "out_of_scope"),
    ):
        values = _string_list(manifest.get(key, []))
        if values:
            lines.extend(["", f"### {heading}"])
            lines.extend(f"- {item}" for item in values)
    return "\n".join(lines)


def _delegation_capability_catalog_block(task: Task) -> str:
    metadata = dict(task.metadata or {})
    catalog = [
        dict(item)
        for item in list(metadata.get("delegation_capability_catalog", []) or [])
        if isinstance(item, dict)
    ]
    topology = dict(metadata.get("runtime_topology", {}) or {})
    current_seat_id = str(
        metadata.get("delegation_seat_id") or metadata.get("seat_id") or ""
    ).strip()
    current_role_id = str(
        task.assigned_to or metadata.get("work_item_role_id") or ""
    ).strip()
    current_seat: dict[str, Any] = {}
    seats = [
        dict(item)
        for item in list(topology.get("seats", []) or [])
        if isinstance(item, dict)
    ]
    if not catalog:
        current_seat = next(
            (
                seat
                for seat in seats
                if current_seat_id
                and str(seat.get("seat_id", "") or "").strip() == current_seat_id
            ),
            {},
        )
        if not current_seat and current_role_id:
            current_seat = next(
                (
                    seat
                    for seat in seats
                    if str(seat.get("role_id", "") or "").strip()
                    == current_role_id
                    and bool(seat.get("is_team_lead", False))
                ),
                {},
            )
        catalog = [
            dict(item)
            for item in list(
                current_seat.get("delegation_capability_catalog", []) or []
            )
            if isinstance(item, dict)
        ]
    if not catalog:
        allowed = set(_string_list(metadata.get("allowed_delegate_role_ids", [])))
        catalog = [
            dict(item)
            for item in list(topology.get("delegation_capability_catalog", []) or [])
            if isinstance(item, dict)
            and str(item.get("target_role_id", "") or "").strip() in allowed
        ]
    if not catalog:
        return ""

    lines = [
        "## Delegation Capability Catalog",
        "Delegate to the organizational `target_role_id`; never delegate to the provider name. Runtime binding resolves that role to the external team.",
    ]
    for item in catalog:
        target_role_id = str(item.get("target_role_id", "") or "").strip()
        display_name = str(item.get("display_name", "") or target_role_id).strip()
        covered = _string_list(item.get("covered_role_ids", []))
        lines.append(
            f"- target_role_id=`{target_role_id}` — {display_name} "
            f"(opaque team; covers: {', '.join(covered)})"
        )
        responsibilities = [
            str(role.get("responsibility", "") or "").strip()
            for role in list(item.get("covered_roles", []) or [])
            if isinstance(role, dict) and str(role.get("responsibility", "") or "").strip()
        ]
        if responsibilities:
            lines.append(
                "  - responsibilities: "
                + clip_text(
                    " | ".join(responsibilities),
                    limit=1200,
                    marker="team responsibilities truncated",
                ).text
            )
        capabilities = _string_list(item.get("capabilities", []))
        if capabilities:
            lines.append("  - declared capabilities: " + ", ".join(capabilities))
        deliverables = _string_list(item.get("deliverables", []))
        if deliverables:
            lines.append("  - deliverables: " + ", ".join(deliverables))
        artifact_contracts = _string_list(item.get("artifact_contract_refs", []))
        if artifact_contracts:
            lines.append("  - artifact contracts: " + ", ".join(artifact_contracts))
        out_of_scope = _string_list(item.get("out_of_scope", []))
        if out_of_scope:
            lines.append("  - out of scope: " + ", ".join(out_of_scope))
    return "\n".join(lines)


def build_company_work_item_contract(
    task: Task,
    *,
    audience: str | None = "native",
) -> str:
    """Return the shared company/runtime contract for a task."""
    resolved_audience = _normalize_contract_audience(audience)
    runtime_clock = _runtime_clock_block()
    if str(task.metadata.get("runtime_model", "") or "").strip() == "multi_team_org":
        if str(task.metadata.get("execution_unit_kind", "") or "").strip() == "opaque_external_team":
            work_item_id = str(
                linked_work_item_id_for_task(task)
                or task.metadata.get("work_item_id")
                or ""
            ).strip()
            attempt_id = str(
                task.metadata.get("external_invocation_id")
                or task.metadata.get("attempt_id")
                or task.metadata.get("claimed_work_item_attempt_seq")
                or "current"
            ).strip()
            covered_roles = [
                str(item).strip()
                for item in list(task.metadata.get("covered_role_ids", []) or [])
                if str(item).strip()
            ]
            identity = "\n".join(
                [
                    "## OpenOPC WorkItem",
                    f"- work_item_id: `{work_item_id}`",
                    f"- attempt_id: `{attempt_id}`",
                    f"- covered_role_ids: `{', '.join(covered_roles)}`",
                    "- Final response: one JSON object containing `work_item_id`, "
                    "`attempt_id`, `status`, `summary`, `deliverables`, "
                    "`verification`, `risks`, `open_questions`, and `handoff`. "
                    "Use the exact ids above.",
                ]
            )
            manifest_block = _team_capability_manifest_block(task)
            return _contract_text_for_audience(
                "\n\n".join(
                    part
                    for part in (
                        runtime_clock,
                        identity,
                        manifest_block,
                    )
                    if part
                ),
                resolved_audience,
            )
        # Hidden auxiliary cards are single-purpose and take precedence over
        # the generic multi-team guidelines: the seat is here for exactly
        # one job (write the report, or apply the verdict) and nothing else.
        report_execute_block = _report_execute_block(task)
        if report_execute_block:
            return _contract_text_for_audience(report_execute_block, resolved_audience)
        review_execute_block = _review_execute_block(task)
        if review_execute_block:
            return _contract_text_for_audience(review_execute_block, resolved_audience)
        parts = [runtime_clock, _MULTI_TEAM_ORG_GUIDELINES.strip()]
        if _multi_team_manager_capable(task):
            parts.append(_MANAGER_RUNTIME_CONTRACT.strip())
            capability_catalog = _delegation_capability_catalog_block(task)
            if capability_catalog:
                parts.append(capability_catalog)
        review_block = _review_pending_block(task)
        if review_block:
            parts.append(review_block)
        decision_block = _manager_execution_decision_block(task)
        if decision_block:
            parts.append(decision_block)
        return _contract_text_for_audience("\n\n".join(parts), resolved_audience)

    turn_type = turn_type_for_task(task, fallback="execute")
    work_item_name = str(task.title or "").strip()
    orchestration = str(task.metadata.get("work_item_orchestration_profile", "") or "").strip()
    verification_required = bool(task.metadata.get("work_item_verification_required", False))
    header = [
        runtime_clock,
        _COMPANY_WORK_ITEM_GUIDELINES.strip(),
        f"Current work item: `{work_item_name or 'projected work item'}`",
        f"Work item turn type: `{turn_type}`",
    ]
    if orchestration:
        header.append(f"Orchestration profile: `{orchestration}`")
    header.append(
        "Work item verification requirement: "
        + ("required before completion." if verification_required else "not automatically required for this work item.")
    )
    if turn_type in {"intake", "plan", "dispatch"}:
        header.append(_COMPANY_PLAN_WORK_ITEM_GUIDELINES.strip())
    elif turn_type == "review":
        header.append(_COMPANY_REVIEW_WORK_ITEM_GUIDELINES.strip())
    elif turn_type in {"aggregate", "deliver"}:
        header.append(_COMPANY_AGGREGATE_WORK_ITEM_GUIDELINES.strip())
    else:
        header.append(_COMPANY_EXECUTE_WORK_ITEM_GUIDELINES.strip())
    return _contract_text_for_audience("\n\n".join(part for part in header if part), resolved_audience)


def build_external_company_work_item_contract(task: Task) -> str:
    """Return the company/runtime contract with external-agent tool wording."""
    return build_company_work_item_contract(task, audience="external")
