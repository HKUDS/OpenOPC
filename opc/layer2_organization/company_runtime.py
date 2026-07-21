"""Persistent company-member runtime for company runtime execution."""

from __future__ import annotations

from contextvars import ContextVar, Token
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable

from loguru import logger

from opc.core.models import (
    CompanyMemberSession,
    CommsSemanticType,
    DelegationRoleSession,
    Phase,
    ResidentAssignmentEnvelope,
    Task,
    TaskResult,
    TaskStatus,
    normalize_role_runtime_status,
)
from opc.core.worker_envelope import classify_worker_message
from opc.layer2_organization import comms as _comms
from opc.layer2_organization.collaboration_policy import render_ownership_contract
from opc.layer2_organization.phase import (
    IN_REVIEW_PHASES,
    is_dispatchable,
    is_report_execution_work_item_metadata,
    is_review_execution_work_item_metadata,
)
from opc.layer2_organization.metadata_ownership import sync_work_item_current_turn_mode
from opc.layer2_organization.session_scoping import (
    external_resume_allowed_for_scope,
    role_home_team_instance_id,
    scoped_member_session_id,
    scoped_queue_key,
    task_session_scope_id,
)
from opc.layer2_organization.work_item_runtime import (
    is_work_item_runtime_metadata,
    mark_work_item_runtime,
)
from opc.layer2_organization.work_item_identity import (
    projection_id_for_task,
    turn_type_for_task,
    work_item_identity_payload,
    work_item_identity_payload_for_task,
    work_item_projection_id_from_metadata,
    work_item_turn_type_from_metadata,
)
from opc.layer2_organization.work_item_links import (
    linked_work_item_id_for_task,
    set_linked_work_item_id,
    task_by_linked_work_item_id,
)


SaveRuntimeSessionFn = Callable[..., Awaitable[None]]
EmitRuntimeEventFn = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class CompanyRuntimeState:
    """Mutable scheduler state for one top-level company run."""

    member_sessions: dict[str, CompanyMemberSession] = field(default_factory=dict)
    role_sessions: dict[str, DelegationRoleSession] = field(default_factory=dict)
    role_queues: dict[str, deque[str]] = field(default_factory=lambda: defaultdict(deque))
    queued_task_ids: set[str] = field(default_factory=set)
    claimed_task_ids: set[str] = field(default_factory=set)
    queued_work_item_ids: set[str] = field(default_factory=set)
    claimed_work_item_ids: set[str] = field(default_factory=set)
    home_team_instance_by_role: dict[str, str] = field(default_factory=dict)


# ── Canonical role_runtime_session_id generator ──────────────────────────
#
# Fix 5 PR1 — dropped ``team_instance_id`` from the session-identity key.
# The user's design is "同一角色 → 同一 session"：a role has exactly one
# runtime session per run, shared across every team context it appears
# in. Work items still carry their team_instance_id (org placement is a
# per-work-item property), but the session that *executes* the work is
# role-scoped. This supports the serial-queue semantics landing in PR3
# (new work for a busy role goes to ``pending_work_item_ids`` instead
# of spawning a second claim on the same role).
#
# History: Fix 2 used the format ``role-runtime::{run}::{team|_no_team}::
# {role}`` to collapse the three divergent generators that produced 2–3
# rows per role in new16/app12. That fixed the duplicates within a team,
# but parallel branches of the same role across teams still split. PR1
# completes the collapse by keying on role alone.
#
# ``_NO_TEAM_SENTINEL`` and the 4-segment form are retained as strings
# so the DB migration in ``_migrate_role_sessions_merge_by_role`` can
# recognize legacy rows and fold them into the canonical 3-segment ID.

_NO_TEAM_SENTINEL = "_no_team"
_ROLE_RUNTIME_PREFIX = "role-runtime::"


def canonical_role_session_id(
    *,
    run_id: str,
    role_id: str,
    team_instance_id: str = "",
) -> str:
    """Single source of truth for role_runtime_session_id.

    Format: ``role-runtime::{run_id}::{role_id}``

    ``team_instance_id`` is accepted for API backwards-compat with the
    Fix-2-era callers but is *ignored*. Team context lives on the work
    item, not on the session (see module docstring). Raises ``ValueError``
    on missing ``run_id`` or ``role_id``.
    """
    rid = str(run_id or "").strip()
    if not rid:
        raise ValueError("canonical_role_session_id: run_id is required")
    role = str(role_id or "").strip()
    if not role:
        raise ValueError("canonical_role_session_id: role_id is required")
    _ = team_instance_id  # intentionally ignored; see module docstring
    return f"{_ROLE_RUNTIME_PREFIX}{rid}::{role}"


def parse_role_session_id(role_session_id: str) -> tuple[str, str] | None:
    """Parse a canonical role_session_id back into ``(run_id, role_id)``.

    Returns ``None`` for legacy/non-canonical forms (4-segment Fix-2
    format, seat-embedded, ephemeral) so callers can detect them and
    route through the migration path instead of trusting parsed bits.
    Canonical form always has exactly 2 segments after the
    ``role-runtime::`` prefix.
    """
    if not role_session_id:
        return None
    text = str(role_session_id).strip()
    if not text.startswith(_ROLE_RUNTIME_PREFIX):
        return None
    tail = text[len(_ROLE_RUNTIME_PREFIX):]
    parts = tail.split("::")
    if len(parts) != 2:
        return None
    run_id, role_id = parts
    if not run_id or not role_id:
        return None
    return run_id, role_id


class CompanyRuntime:
    """Keeps long-lived company-member session state across work-item turns."""

    @staticmethod
    def _set_member_session_status(
        session: CompanyMemberSession,
        status: str,
        *,
        focused_work_item_id: str | None = None,
    ) -> str:
        if focused_work_item_id is not None:
            session.focused_work_item_id = str(focused_work_item_id or "").strip()
        normalized = normalize_role_runtime_status(
            status,
            session.focused_work_item_id,
        )
        if normalized == "idle":
            session.focused_work_item_id = ""
        session.status = normalized
        session.resident_status = normalized
        return normalized

    @staticmethod
    def _normalize_member_session_status(session: CompanyMemberSession) -> str:
        return CompanyRuntime._set_member_session_status(
            session,
            session.status,
        )

    def __init__(
        self,
        *,
        org_engine: Any | None,
        communication: Any | None,
        store: Any | None = None,
        save_runtime_session: SaveRuntimeSessionFn | None = None,
        emit_runtime_event: EmitRuntimeEventFn | None = None,
    ) -> None:
        self.org_engine = org_engine
        self.communication = communication
        self.store = store
        self.save_runtime_session = save_runtime_session
        self.emit_runtime_event = emit_runtime_event
        self._default_state = CompanyRuntimeState()
        self._state_var: ContextVar[CompanyRuntimeState | None] = ContextVar(
            f"company-runtime-state:{id(self)}",
            default=None,
        )
        self.member_sessions = {}
        self.role_sessions = {}
        self.role_queues = defaultdict(deque)
        self._queued_task_ids = set()
        self._claimed_task_ids = set()
        self._queued_work_item_ids = set()
        self._claimed_work_item_ids = set()
        # Phase A role-instance cache: role_id → home team_instance_id.
        # The home team is where this role is the leader (seat.team_id
        # matches f"team::{role_id}"); for leaf roles it's the only
        # team they sit in. Included in the session / queue key so
        # two parallel team_instances with the same role name
        # (multi-branch) don't collide. Populated in bootstrap.
        self._home_team_instance_by_role = {}

    def create_state(self) -> CompanyRuntimeState:
        return CompanyRuntimeState()

    def use_state(self, state: CompanyRuntimeState) -> Token[CompanyRuntimeState | None]:
        return self._state_var.set(state)

    def reset_state(self, token: Token[CompanyRuntimeState | None]) -> None:
        self._state_var.reset(token)

    def _state(self) -> CompanyRuntimeState:
        return self._state_var.get() or self._default_state

    @property
    def member_sessions(self) -> dict[str, CompanyMemberSession]:
        return self._state().member_sessions

    @member_sessions.setter
    def member_sessions(self, value: dict[str, CompanyMemberSession]) -> None:
        self._state().member_sessions = value

    @property
    def role_sessions(self) -> dict[str, DelegationRoleSession]:
        return self._state().role_sessions

    @role_sessions.setter
    def role_sessions(self, value: dict[str, DelegationRoleSession]) -> None:
        self._state().role_sessions = value

    @property
    def role_queues(self) -> dict[str, deque[str]]:
        return self._state().role_queues

    @role_queues.setter
    def role_queues(self, value: dict[str, deque[str]]) -> None:
        if isinstance(value, defaultdict):
            self._state().role_queues = value
        else:
            self._state().role_queues = defaultdict(deque, value)

    @property
    def _queued_task_ids(self) -> set[str]:
        return self._state().queued_task_ids

    @_queued_task_ids.setter
    def _queued_task_ids(self, value: set[str]) -> None:
        self._state().queued_task_ids = value

    @property
    def _claimed_task_ids(self) -> set[str]:
        return self._state().claimed_task_ids

    @_claimed_task_ids.setter
    def _claimed_task_ids(self, value: set[str]) -> None:
        self._state().claimed_task_ids = value

    @property
    def _queued_work_item_ids(self) -> set[str]:
        return self._state().queued_work_item_ids

    @_queued_work_item_ids.setter
    def _queued_work_item_ids(self, value: set[str]) -> None:
        self._state().queued_work_item_ids = value

    @property
    def _claimed_work_item_ids(self) -> set[str]:
        return self._state().claimed_work_item_ids

    @_claimed_work_item_ids.setter
    def _claimed_work_item_ids(self, value: set[str]) -> None:
        self._state().claimed_work_item_ids = value

    @property
    def _home_team_instance_by_role(self) -> dict[str, str]:
        return self._state().home_team_instance_by_role

    @_home_team_instance_by_role.setter
    def _home_team_instance_by_role(self, value: dict[str, str]) -> None:
        self._state().home_team_instance_by_role = value

    @staticmethod
    def _direct_report_metadata_for_seat(
        seat: dict[str, Any],
        seats: list[dict[str, Any]],
    ) -> tuple[list[str], list[str]]:
        role_id = str(seat.get("role_id", "") or "").strip()
        seat_metadata = dict(seat.get("metadata", {}) or {})
        managed_team_id = str(
            seat.get("managed_team_id", "")
            or seat_metadata.get("managed_team_id", "")
            or ""
        ).strip()
        if not role_id or not managed_team_id:
            return [], []
        report_roles: list[str] = []
        report_seats: list[str] = []
        for candidate in seats:
            candidate_metadata = dict(candidate.get("metadata", {}) or {})
            candidate_team_id = str(
                candidate.get("team_id", "")
                or candidate_metadata.get("team_id", "")
                or ""
            ).strip()
            if candidate_team_id != managed_team_id:
                continue
            candidate_manager_role_id = str(
                candidate.get("manager_role_id", "")
                or candidate_metadata.get("manager_role_id", "")
                or ""
            ).strip()
            if candidate_manager_role_id != role_id:
                continue
            candidate_role_id = str(candidate.get("role_id", "") or "").strip()
            candidate_seat_id = str(candidate.get("seat_id", "") or "").strip()
            if not candidate_role_id or not candidate_seat_id or candidate_role_id == role_id:
                continue
            report_roles.append(candidate_role_id)
            report_seats.append(candidate_seat_id)
        return sorted(dict.fromkeys(report_roles)), sorted(dict.fromkeys(report_seats))

    @staticmethod
    def _pending_reviews_from_board_summary(
        session: CompanyMemberSession,
    ) -> list[dict[str, Any]]:
        """Return the list of child work items currently awaiting this manager's review.

        The caller is expected to have already populated ``manager_board_summary``
        (via ``_refresh_manager_board_state``). We filter the summary's
        ``upstream_summary`` for items in ``in_review`` whose ``review_owner_*``
        matches this manager session.
        """
        board_summary = dict((session.metadata or {}).get("manager_board_summary", {}) or {})
        upstream = list(board_summary.get("upstream_summary", []) or [])
        if not upstream:
            return []
        manager_role_id = str(session.role_id or "").strip().lower()
        manager_seat_id = str(session.seat_id or (session.metadata or {}).get("seat_id", "") or "").strip().lower()
        pending: list[dict[str, Any]] = []
        for item in upstream:
            if not isinstance(item, dict):
                continue
            phase = str(item.get("phase") or "").strip().lower()
            kanban_column_value = str(item.get("kanban_column") or "").strip().lower()
            if phase in {"approved", "failed", "cancelled"}:
                continue
            if (
                kanban_column_value != "in_review"
                and phase not in {"awaiting_manager_review", "awaiting_human"}
            ):
                continue
            owner_role = str(item.get("review_owner_role_id") or "").strip().lower()
            owner_seat = str(item.get("review_owner_seat_id") or "").strip().lower()
            if owner_role and manager_role_id and owner_role != manager_role_id:
                # Reviewer is explicitly someone else (e.g. human gate).
                continue
            if owner_seat and manager_seat_id and owner_seat != manager_seat_id:
                continue
            pending.append(dict(item))
        return pending

    def _resolve_current_turn_mode(
        self,
        session: CompanyMemberSession,
        task: Task | None = None,
    ) -> str:
        task_metadata = dict(getattr(task, "metadata", {}) or {}) if task is not None else {}
        task_context = dict(getattr(task, "context_snapshot", {}) or {}) if task is not None else {}
        runtime_model = str(
            task_metadata.get("runtime_model", "") if task is not None else ""
            or (session.metadata or {}).get("runtime_model", "")
            or ""
        ).strip()
        if runtime_model != "multi_team_org":
            return ""
        explicit_task_turn_mode = str(
            task_metadata.get("current_turn_mode")
            or task_context.get("current_turn_mode")
            or ""
        ).strip()
        if (
            bool(task_metadata.get("followup_routed_to_final_decider", False))
            and explicit_task_turn_mode == "dispatch_required"
        ):
            return "dispatch_required"
        work_item_turn_type = (
            turn_type_for_task(task, fallback="")
            if task is not None
            else (
                work_item_turn_type_from_metadata(dict(session.current_assignment or {}), fallback="")
                or work_item_turn_type_from_metadata(dict(session.current_work_item or {}), fallback="")
                or work_item_turn_type_from_metadata(session.metadata or {}, fallback="")
            )
        )
        direct_report_seat_ids = [
            str(item).strip()
            for item in list((session.metadata or {}).get("direct_report_seat_ids", []) or [])
            if str(item).strip()
        ]
        allowed_delegate_role_ids = [
            str(item).strip()
            for item in list((session.metadata or {}).get("allowed_delegate_role_ids", []) or [])
            if str(item).strip()
        ]
        managed_team_id = str((session.metadata or {}).get("managed_team_id", "") or "").strip()
        manager_board_summary = dict((session.metadata or {}).get("manager_board_summary", {}) or {})
        total_children = int(manager_board_summary.get("total_children", 0) or 0)
        is_attention_work_item = bool(task_metadata.get("attention_work_item", False))
        has_review_target = bool(str(task_metadata.get("review_target_work_item_id", "") or "").strip())
        review_execution_turn = bool(
            (task_metadata.get("review_execution_work_item", False) if task is not None else False)
            or ((task_metadata.get("review_task", False) if task is not None else False) and has_review_target)
            or has_review_target
            or (work_item_turn_type == "review" and not is_attention_work_item and has_review_target)
        )
        report_execution_turn = bool(
            (task_metadata.get("report_execution_work_item", False) if task is not None else False)
            or work_item_turn_type == "report"
        )
        if work_item_turn_type == "deliver":
            return "deliver_required"
        if work_item_turn_type == "aggregate":
            return "synthesize_required"
        if report_execution_turn:
            return "report_required"
        if review_execution_turn:
            return "review_execute"
        if direct_report_seat_ids or allowed_delegate_role_ids or managed_team_id:
            # Pending manager review takes priority over any other managerial
            # turn mode: a manager must clear their review queue before
            # dispatching or monitoring further children.
            if self._pending_reviews_from_board_summary(session):
                return "review_pending"
            return "monitor_children" if total_children > 0 else "dispatch_required"
        return "worker_execute"

    def _update_current_turn_mode(
        self,
        session: CompanyMemberSession,
        task: Task | None = None,
    ) -> str:
        current_turn_mode = self._resolve_current_turn_mode(session, task)
        session.current_turn_mode = current_turn_mode
        session.metadata = dict(session.metadata or {})
        if current_turn_mode:
            session.metadata["current_turn_mode"] = current_turn_mode
        else:
            session.metadata.pop("current_turn_mode", None)
        if session.current_assignment:
            session.current_assignment = dict(session.current_assignment)
            assignment_metadata = dict(session.current_assignment.get("metadata", {}) or {})
            if current_turn_mode:
                assignment_metadata["current_turn_mode"] = current_turn_mode
            else:
                assignment_metadata.pop("current_turn_mode", None)
            session.current_assignment["metadata"] = assignment_metadata
        if session.current_work_item:
            session.current_work_item = {
                **dict(session.current_work_item or {}),
                **({"current_turn_mode": current_turn_mode} if current_turn_mode else {}),
            }
        return current_turn_mode

    async def _sync_current_turn_mode_to_work_item(
        self,
        task: Task | None,
        current_turn_mode: str,
    ) -> None:
        if task is None or not self.store:
            return
        wid = linked_work_item_id_for_task(task)
        if not wid:
            return
        try:
            await sync_work_item_current_turn_mode(self.store, wid, current_turn_mode)
        except Exception:
            logger.opt(exception=True).debug(
                "WorkItem current_turn_mode sync failed task=%s work_item=%s",
                getattr(task, "id", ""),
                wid,
            )

    async def _refresh_manager_board_state(
        self,
        session: CompanyMemberSession,
        task: Task | None = None,
    ) -> dict[str, Any]:
        if not self.store:
            return {}
        summarize = getattr(self.store, "summarize_parent_status", None)
        if not callable(summarize):
            return {}
        run_id = str(
            (task.metadata or {}).get("delegation_run_id", "") if task is not None else ""
            or (session.metadata or {}).get("delegation_run_id", "")
            or ""
        ).strip()
        manager_seat_id = str(session.seat_id or (session.metadata or {}).get("seat_id", "") or "").strip()
        parent_work_item_id = str(
            session.focused_work_item_id
            or (linked_work_item_id_for_task(task) if task is not None else "")
            or ""
        ).strip()
        if not run_id or not manager_seat_id or not parent_work_item_id:
            session.metadata = dict(session.metadata or {})
            session.metadata.pop("manager_board_summary", None)
            session.metadata.pop("parent_board_scope", None)
            return {}
        summary = await summarize(
            run_id,
            manager_seat_id=manager_seat_id,
            parent_work_item_id=parent_work_item_id,
        )
        session.metadata = dict(session.metadata or {})
        session.metadata["delegation_run_id"] = run_id
        if summary.get("total_children", 0):
            session.metadata["manager_board_summary"] = dict(summary)
            session.metadata["parent_board_scope"] = f"{manager_seat_id}:{parent_work_item_id}"
        else:
            session.metadata.pop("manager_board_summary", None)
            session.metadata.pop("parent_board_scope", None)
        pending_reviews = self._pending_reviews_from_board_summary(session)
        if pending_reviews:
            session.metadata["pending_review_items"] = pending_reviews
        else:
            session.metadata.pop("pending_review_items", None)
        return dict(summary)

    async def bootstrap(self, tasks: list[Task]) -> None:
        if any(is_work_item_runtime_metadata(getattr(task, "metadata", {}) or {}) for task in tasks):
            await self._bootstrap_work_item_runtime_sessions(tasks)
            await self.refresh_inbox_state(tasks)
            return
        await self._bootstrap_role_sessions(tasks)
        for task in sorted(tasks, key=lambda item: item.created_at):
            created = False
            member_session_id = self._member_session_id(
                task,
                role_id=self._role_id(task),
                employee_id=self._employee_id(task),
            )
            if member_session_id not in self.member_sessions:
                created = True
            session = self._ensure_member_session(task)
            task.metadata = dict(task.metadata)
            task.metadata["member_session_id"] = session.member_session_id
            task.metadata["member_session_state"] = self._serialize_session(session)
            if created:
                await self._persist_session(session, task=task)
                await self._emit(
                    "member_session_started",
                    {
                        "member_session_id": session.member_session_id,
                        "role_id": session.role_id,
                        "employee_id": session.employee_id,
                        "task_id": task.id,
                    },
                )
        await self.refresh_inbox_state(tasks)

    async def _bootstrap_work_item_runtime_sessions(self, tasks: list[Task]) -> None:
        runtime_tasks = [
            task
            for task in tasks
            if is_work_item_runtime_metadata(getattr(task, "metadata", {}) or {})
        ]
        if not runtime_tasks:
            return
        root_task = sorted(runtime_tasks, key=lambda item: item.created_at)[0]
        run_id = str((root_task.metadata or {}).get("delegation_run_id", "") or "").strip()
        runtime_topology = dict((root_task.metadata or {}).get("runtime_topology", {}) or {})
        if self.store is not None and bool(getattr(self.store, "is_ready", False)) and run_id and hasattr(self.store, "get_delegation_run"):
            run = await self.store.get_delegation_run(run_id)
            if run is not None:
                runtime_topology = dict((getattr(run, "metadata", {}) or {}).get("runtime_topology", {}) or runtime_topology)
        await self._bootstrap_role_sessions(runtime_tasks)
        seats: list[dict[str, Any]] = []
        list_seat_states = getattr(self.store, "list_delegation_seat_states", None)
        if callable(list_seat_states) and run_id:
            try:
                persisted = await list_seat_states(run_id)
            except Exception:
                persisted = []
            for item in persisted:
                seat_payload = getattr(item, "__dict__", None)
                if isinstance(seat_payload, dict):
                    seat_data = dict(seat_payload)
                    seat_data["metadata"] = dict(seat_data.get("metadata", {}) or {})
                    seats.append(seat_data)
        if not seats:
            seats = [dict(item) for item in list(runtime_topology.get("seats", []) or []) if isinstance(item, dict)]
        task_by_seat = {
            self._seat_id(task): task
            for task in runtime_tasks
            if self._seat_id(task)
        }
        # Role-instance model (Phase A): seats are organizational anchors,
        # not identity. Group seats by role so that e.g. CMO's "upward"
        # seat in CEO's team and "downward" seat in CMO's own team share
        # **one** CompanyMemberSession + DelegationRoleSession. The seat
        # that is the role's leader (team_id == f"team::{role}") is
        # preferred as the session's primary seat; other seats are
        # recorded in role_session.seat_ids for org lookups.
        seats_by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for seat in seats:
            role_id = str(seat.get("role_id", "") or "").strip()
            seat_id = str(seat.get("seat_id", "") or "").strip()
            if role_id and seat_id:
                seats_by_role[role_id].append(seat)

        for role_id, role_seats in seats_by_role.items():
            # Primary seat = leader seat if present, else the first seat.
            # Leader seat has team_id == f"team::{role_id}" by convention.
            primary_seat = next(
                (s for s in role_seats if str(s.get("team_id", "") or "").strip() == f"team::{role_id}"),
                role_seats[0],
            )
            primary_seat_id = str(primary_seat.get("seat_id", "") or "").strip()
            all_seat_ids = sorted({str(s.get("seat_id", "") or "").strip() for s in role_seats if s.get("seat_id")})
            project_id = str(root_task.project_id or "default").strip() or "default"
            seat_employee_id = (
                str(primary_seat.get("employee_id", "") or "").strip()
                or self._employee_id(root_task)
            )
            # Pick a representative task: prefer one whose seat is the
            # primary seat; fall back to any task with this role. Only use
            # a "this role's task" as the explicit_id source — falling back
            # to root_task would leak the root's member_session_id across
            # all roles and collapse them into one session.
            representative_task = task_by_seat.get(primary_seat_id)
            if representative_task is None:
                representative_task = next(
                    (task for task in runtime_tasks if self._role_id(task) == role_id),
                    None,
                )
            scope_source_task = representative_task or root_task
            session_scope_id = task_session_scope_id(scope_source_task)
            # Fix 5 PR4: compute home_team_instance_id for diagnostics only
            # (surfaced on session.team_instance_id + session.metadata for
            # the UI and logs). The session / queue keys no longer include
            # it — same role = one session across every team context.
            home_team_instance_id = role_home_team_instance_id(
                role_id, seats
            ) or str(primary_seat.get("team_instance_id", "") or "").strip()
            if home_team_instance_id:
                self._home_team_instance_by_role[role_id] = home_team_instance_id
            explicit_member_session_id = (
                str((representative_task.metadata or {}).get("member_session_id", "") or "").strip()
                if representative_task is not None
                else ""
            )
            member_session_id = scoped_member_session_id(
                project_id=project_id,
                session_scope_id=session_scope_id,
                role_id=role_id,
                employee_id=seat_employee_id,
                explicit_id=explicit_member_session_id,
            )
            if run_id:
                role_session_id = canonical_role_session_id(
                    run_id=run_id,
                    role_id=role_id,
                    team_instance_id=home_team_instance_id,
                )
            else:
                # Pre-run ephemeral path (tests / in-memory scratch runs
                # that never hit the DB). Distinct prefix so it never
                # collides with canonical rows.
                role_session_id = f"role-session::ephemeral::{member_session_id}"
            direct_report_role_ids, direct_report_seat_ids = self._direct_report_metadata_for_seat(primary_seat, seats)
            session = self.member_sessions.get(member_session_id)
            if session is None:
                session = CompanyMemberSession(
                    member_session_id=member_session_id,
                    role_id=role_id,
                    employee_id=seat_employee_id,
                )
                self.member_sessions[member_session_id] = session
            session.team_instance_id = home_team_instance_id
            session.team_id = str(primary_seat.get("team_id", "") or "").strip()
            session.seat_id = primary_seat_id
            session.seat_state_id = str(primary_seat.get("seat_state_id", "") or "").strip()
            # Collect merged metadata across all of this role's seats.
            all_contact_role_ids: list[str] = []
            all_allowed_delegate_role_ids: list[str] = []
            all_managed_team_ids: list[str] = []
            all_manager_seat_ids: list[str] = []
            for s in role_seats:
                all_contact_role_ids.extend(list(s.get("contact_role_ids", []) or []))
                all_allowed_delegate_role_ids.extend(list(s.get("allowed_delegate_role_ids", []) or []))
                mt = str(s.get("managed_team_id", "") or "").strip()
                if mt:
                    all_managed_team_ids.append(mt)
                ms = str(s.get("manager_seat_id", "") or "").strip()
                if ms:
                    all_manager_seat_ids.append(ms)
            session.metadata = mark_work_item_runtime({
                **dict(session.metadata or {}),
                "seat_id": primary_seat_id,
                "seat_ids": all_seat_ids,
                "team_id": session.team_id,
                "team_instance_id": home_team_instance_id,
                "manager_seat_id": str(primary_seat.get("manager_seat_id", "") or "").strip(),
                "managed_team_id": str(primary_seat.get("managed_team_id", "") or "").strip(),
                "managed_team_ids": sorted(set(all_managed_team_ids)),
                "contact_role_ids": sorted(set(all_contact_role_ids)),
                "allowed_delegate_role_ids": sorted(set(all_allowed_delegate_role_ids)),
                "direct_report_role_ids": direct_report_role_ids,
                "direct_report_seat_ids": direct_report_seat_ids,
                "session_scope_id": session_scope_id,
                **dict(primary_seat.get("metadata", {}) or {}),
            })
            session.manager_role_id = str(primary_seat.get("manager_role_id", "") or "").strip()
            session.manager_role_ids = sorted(
                {
                    *list(session.manager_role_ids or []),
                    *[
                        str(item).strip()
                        for item in [session.manager_role_id, *all_contact_role_ids]
                        if str(item).strip()
                    ],
                }
            )
            role_session = self.role_sessions.get(role_session_id)
            if role_session is None:
                role_session = DelegationRoleSession(
                    role_session_id=role_session_id,
                    run_id=run_id,
                    project_id=project_id,
                    team_instance_id=home_team_instance_id,
                    team_id=session.team_id,
                    role_id=role_id,
                    seat_id=primary_seat_id,
                    seat_state_id=str(primary_seat.get("seat_state_id", "") or "").strip(),
                    employee_id=session.employee_id,
                    manager_role_ids=list(session.manager_role_ids),
                    manager_seat_ids=sorted(set(all_manager_seat_ids)),
                    seat_ids=all_seat_ids,
                    status="idle",
                    metadata=mark_work_item_runtime({
                        "shared_role_executor": True,
                        "session_scope_id": session_scope_id,
                    }),
                )
                self.role_sessions[role_session_id] = role_session
                if self.store and hasattr(self.store, "save_delegation_role_session"):
                    await self.store.save_delegation_role_session(role_session)
            else:
                role_session.project_id = getattr(role_session, "project_id", "") or project_id
                role_session.team_instance_id = getattr(role_session, "team_instance_id", "") or home_team_instance_id
                role_session.team_id = getattr(role_session, "team_id", "") or session.team_id
                role_session.seat_id = getattr(role_session, "seat_id", "") or primary_seat_id
                role_session.seat_state_id = getattr(role_session, "seat_state_id", "") or str(primary_seat.get("seat_state_id", "") or "").strip()
                role_session.manager_seat_ids = sorted(
                    {
                        *list(getattr(role_session, "manager_seat_ids", []) or []),
                        *all_manager_seat_ids,
                    }
                )
                role_session.seat_ids = sorted(
                    {*list(getattr(role_session, "seat_ids", []) or []), *all_seat_ids}
                )
                role_session.metadata = {
                    **dict(getattr(role_session, "metadata", {}) or {}),
                    "session_scope_id": session_scope_id,
                }
            session.role_session_id = role_session.role_session_id
            self._sync_member_session_from_role_session(session, role_session)
            # Attach identity to every task for this role (regardless of which seat it sits in).
            for seat_entry in role_seats:
                entry_seat_id = str(seat_entry.get("seat_id", "") or "").strip()
                seat_task = task_by_seat.get(entry_seat_id)
                if seat_task is None:
                    continue
                seat_task.metadata = dict(seat_task.metadata)
                seat_task.metadata["member_session_id"] = session.member_session_id
                seat_task.metadata["member_session_state"] = self._serialize_session(session)
                seat_task.metadata["delegation_role_session_id"] = role_session.role_session_id

    async def _bootstrap_role_sessions(self, tasks: list[Task]) -> None:
        if self.store is not None and not bool(getattr(self.store, "is_ready", False)):
            return
        run_ids = {
            str((task.metadata or {}).get("delegation_run_id", "") or "").strip()
            for task in tasks
            if str((task.metadata or {}).get("delegation_run_id", "") or "").strip()
        }
        if not run_ids:
            return
        for run_id in sorted(run_ids):
            existing_sessions: list[DelegationRoleSession] = []
            if self.store and hasattr(self.store, "list_delegation_role_sessions"):
                existing_sessions = await self.store.list_delegation_role_sessions(run_id)
            task_by_role: dict[str, Task] = {}
            for task in tasks:
                if str((task.metadata or {}).get("delegation_run_id", "") or "").strip() != run_id:
                    continue
                role_id = self._role_id(task)
                if role_id and role_id not in task_by_role:
                    task_by_role[role_id] = task
            for session in existing_sessions:
                self.role_sessions[session.role_session_id] = session
                role_task = task_by_role.get(session.role_id)
                if role_task is not None:
                    self._attach_role_session_to_task(role_task, session)
            for role_id, task in task_by_role.items():
                role_session_id = self._role_session_id(task, role_id=role_id)
                if role_session_id in self.role_sessions:
                    continue
                manager_role_ids = self._manager_role_ids(task)
                employee_id = self._employee_id(task)
                role_session = DelegationRoleSession(
                    role_session_id=role_session_id,
                    run_id=run_id,
                    role_id=role_id,
                    employee_id=employee_id,
                    manager_role_ids=manager_role_ids,
                    status="idle",
                )
                self.role_sessions[role_session_id] = role_session
                if self.store and hasattr(self.store, "save_delegation_role_session"):
                    await self.store.save_delegation_role_session(role_session)
                self._attach_role_session_to_task(task, role_session)

    def _attach_role_session_to_task(self, task: Task, role_session: DelegationRoleSession) -> None:
        task.metadata = dict(task.metadata)
        task.metadata["delegation_role_session_id"] = role_session.role_session_id

    def _sync_member_session_from_role_session(
        self,
        session: CompanyMemberSession,
        role_session: DelegationRoleSession | None,
    ) -> None:
        if role_session is None:
            return
        session.role_session_id = role_session.role_session_id
        session.focused_work_item_id = str(getattr(role_session, "focused_work_item_id", "") or "").strip()
        role_status = normalize_role_runtime_status(
            getattr(role_session, "status", ""),
            session.focused_work_item_id,
        )
        if role_status == "idle":
            session.focused_work_item_id = ""
        session.status = role_status
        session.resident_status = role_status
        session.background_work_item_ids = list(getattr(role_session, "background_work_item_ids", []) or [])
        session.manager_role_ids = list(getattr(role_session, "manager_role_ids", []) or session.manager_role_ids or [])
        session.adapter_session_state = dict(getattr(role_session, "adapter_session_state", {}) or {})
        session.inbox_state = dict(getattr(role_session, "inbox_state", {}) or {})
        session.memory_slices_by_work_item = dict(getattr(role_session, "memory_slices_by_work_item", {}) or {})
        session.resume_state = {
            **dict(getattr(role_session, "resume_state", {}) or {}),
            **dict(session.resume_state or {}),
        }
        session.current_work_item = (
            {}
            if role_status == "idle"
            else dict(getattr(role_session, "current_work_item", {}) or {})
        )
        session.latest_notification = dict(getattr(role_session, "latest_notification", {}) or {})
        session.manager_digest = dict(getattr(role_session, "manager_digest", {}) or {})

    async def reset_for_company_runtime_resume(
        self,
        tasks: list[Task],
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Clear stale in-memory claims/sessions before replaying a suspended run.

        Stop kills the live coroutine/subprocess.  The DB claim is released by
        the engine, but the long-lived Office UI process may still have member
        sessions marked ``running`` from the cancelled turn.  If those remain,
        ``claim_runnable_tasks`` correctly refuses to dispatch new work.  Resume
        therefore has to converge memory back to DB truth before the executor's
        first dispatch tick.
        """
        payload = dict(payload or {})
        affected_task_ids = {
            str(getattr(task, "id", "") or "").strip()
            for task in tasks
            if str(getattr(task, "id", "") or "").strip()
        }
        affected_work_item_ids = {
            linked_work_item_id_for_task(task)
            for task in tasks
            if linked_work_item_id_for_task(task)
        }
        affected_role_session_ids = {
            str((getattr(task, "metadata", {}) or {}).get("delegation_role_session_id", "") or "").strip()
            for task in tasks
            if str((getattr(task, "metadata", {}) or {}).get("delegation_role_session_id", "") or "").strip()
        }
        affected_role_session_ids.update(
            str(item).strip()
            for item in list(payload.get("role_runtime_session_ids", []) or [])
            if str(item).strip()
        )
        if not affected_task_ids and not affected_work_item_ids and not affected_role_session_ids:
            return

        self._claimed_task_ids.difference_update(affected_task_ids)
        self._queued_task_ids.difference_update(affected_task_ids)
        self._claimed_work_item_ids.difference_update(affected_work_item_ids)
        self._queued_work_item_ids.difference_update(affected_work_item_ids)
        blocked_queue_entries = {
            *affected_task_ids,
            *(f"review-task::{task_id}" for task_id in affected_task_ids),
            *(f"work-item::{work_item_id}" for work_item_id in affected_work_item_ids),
            *(f"review-work-item::{work_item_id}" for work_item_id in affected_work_item_ids),
        }
        for queue_key, queue in list(self.role_queues.items()):
            if not queue:
                continue
            self.role_queues[queue_key] = deque(
                entry for entry in queue if str(entry or "").strip() not in blocked_queue_entries
            )

        now = datetime.now()
        update_role_session = getattr(self.store, "update_delegation_role_session", None) if self.store else None
        for session in list(self.member_sessions.values()):
            role_session_id = str(getattr(session, "role_session_id", "") or "").strip()
            focused_work_item_id = str(getattr(session, "focused_work_item_id", "") or "").strip()
            current_task_id = str(getattr(session, "current_task_id", "") or "").strip()
            affected = (
                role_session_id in affected_role_session_ids
                or focused_work_item_id in affected_work_item_ids
                or current_task_id in affected_task_ids
            )
            if not affected:
                continue
            session.status = "idle"
            session.resident_status = "idle"
            session.current_task_id = ""
            session.focused_work_item_id = ""
            session.current_work_item = {}
            session.current_assignment = {}
            session.updated_at = now
            role_session = self.role_sessions.get(role_session_id)
            if role_session is not None:
                role_session.status = "idle"
                role_session.focused_work_item_id = ""
                role_session.current_work_item = {}
                role_session.updated_at = now
            if callable(update_role_session) and role_session_id:
                try:
                    await update_role_session(
                        role_session_id,
                        focused_work_item_id="",
                        current_work_item={},
                        status="idle",
                        metadata_updates={
                            "last_resume_memory_reset_at": now.isoformat(),
                            "last_resume_checkpoint_id": str(payload.get("checkpoint_id", "") or ""),
                        },
                    )
                except Exception:
                    logger.opt(exception=True).debug("company runtime resume reset: role session persist failed")

    def _role_session_id(self, task: Task, *, role_id: str) -> str:
        explicit = str((task.metadata or {}).get("delegation_role_session_id", "") or "").strip()
        if explicit:
            return explicit
        run_id = str((task.metadata or {}).get("delegation_run_id", "") or "").strip()
        if run_id and role_id:
            # Fix 5 PR1: same role = one session per run; team_instance_id
            # is not part of the session key anymore. The multi-source
            # team resolution fallback that used to live here is obsolete.
            return canonical_role_session_id(run_id=run_id, role_id=role_id)
        return f"role-session::ephemeral::{self._member_session_id(task, role_id=role_id, employee_id=self._employee_id(task))}"

    def _manager_role_ids(self, task: Task) -> list[str]:
        explicit = [
            str(item).strip()
            for item in list((task.metadata or {}).get("manager_role_ids", []) or [])
            if str(item).strip()
        ]
        if explicit:
            return sorted(dict.fromkeys(explicit))
        direct_manager = str((task.metadata or {}).get("manager_role_id", "") or "").strip()
        if direct_manager:
            return [direct_manager]
        if self.org_engine is not None:
            agent = self.org_engine.get_agent(self._role_id(task))
            manager_role = str(getattr(agent, "reports_to", "") or "").strip()
            if manager_role and manager_role != "owner":
                return [manager_role]
        return []

    async def refresh_inbox_state(self, tasks: list[Task]) -> None:
        if not self.communication or not hasattr(self.communication, "read_inbox"):
            return
        task_scope_ids = self._task_scope_ids(tasks)
        representative_task_by_key: dict[str, Task] = {}
        for task in sorted(tasks, key=lambda item: item.created_at, reverse=True):
            key = self._queue_key_for_task(task)
            if key and key not in representative_task_by_key:
                representative_task_by_key[key] = task
        for session in self.member_sessions.values():
            self._normalize_member_session_status(session)
            representative_task = representative_task_by_key.get(self._queue_key_for_session(session))
            if representative_task is None:
                representative_task = representative_task_by_key.get(session.role_id)
            messages = await self.communication.read_inbox(
                agent_id=session.role_id,
                task=representative_task,
                task_ids=task_scope_ids,
                unread_only=True,
                limit=12,
                mark_read=False,
            )
            classified = [classify_worker_message(dict(item)) for item in messages if isinstance(item, dict)]
            sorted_messages = sorted(classified, key=lambda item: self._message_sort_key(session, item))
            actionable_chat = [dict(item) for item in sorted_messages if item.get("message_class") == "chat" and bool(item.get("actionable", True))]
            protocol_backlog = [dict(item) for item in sorted_messages if item.get("message_class") == "protocol"]
            notification_backlog = [dict(item) for item in sorted_messages if item.get("message_class") == "notification"]
            session.actionable_chat = actionable_chat[:8]
            session.protocol_backlog = protocol_backlog[:8]
            session.notification_backlog = notification_backlog[:8]
            session.actionable_inbox_count = len(actionable_chat)
            session.protocol_backlog_count = len(protocol_backlog)
            session.notification_backlog_count = len(notification_backlog)
            session.latest_notification = self._latest_notification(notification_backlog)
            inbox_initialized = bool((session.metadata or {}).get("inbox_initialized", False)) or session.inbox_cursor > 0
            if not inbox_initialized:
                session.pending_inbox = []
                session.queued_inbox = [dict(item) for item in actionable_chat[:12]]
                session.inbox_cursor = max(
                    session.inbox_cursor,
                    len(session.queued_inbox) + len(protocol_backlog) + len(notification_backlog),
                )
                session.metadata = dict(session.metadata or {})
                session.metadata["inbox_initialized"] = True
            else:
                seen_ids = {
                    str(item).strip()
                    for item in list(session.resume_state.get("seen_inbox_message_ids", []))
                    if str(item).strip()
                }
                for item in classified:
                    msg_id = str(item.get("msg_id", "")).strip()
                    if msg_id:
                        seen_ids.add(msg_id)
                session.pending_inbox = [dict(item) for item in actionable_chat[:8]]
                session.resume_state = dict(session.resume_state)
                session.resume_state["seen_inbox_message_ids"] = sorted(seen_ids)
                session.inbox_cursor = len(seen_ids)
            session.current_work_item = self._build_current_work_item(session, representative_task)
            await self._refresh_manager_board_state(session, representative_task)
            current_turn_mode = self._update_current_turn_mode(session, representative_task)
            await self._sync_current_turn_mode_to_work_item(representative_task, current_turn_mode)
            session.manager_digest = self._build_manager_digest(session, representative_task)
            session.inbox_state = {
                "actionable_chat": [dict(item) for item in session.actionable_chat],
                "protocol_backlog": [dict(item) for item in session.protocol_backlog],
                "notification_backlog": [dict(item) for item in session.notification_backlog],
                "pending_inbox": [dict(item) for item in session.pending_inbox],
                "queued_inbox": [dict(item) for item in session.queued_inbox],
                "latest_notification": dict(session.latest_notification or {}),
                "actionable_inbox_count": session.actionable_inbox_count,
                "protocol_backlog_count": session.protocol_backlog_count,
                "notification_backlog_count": session.notification_backlog_count,
                "current_work_item": dict(session.current_work_item or {}),
                "current_turn_mode": str(session.current_turn_mode or "").strip(),
                "manager_board_summary": dict((session.metadata or {}).get("manager_board_summary", {}) or {}),
                "manager_digest": dict(session.manager_digest or {}),
            }
            role_session = self._role_session_for_member_session(session)
            if role_session is not None:
                role_session.inbox_state = dict(session.inbox_state)
                role_session.current_work_item = dict(session.current_work_item or {})
                role_session.latest_notification = dict(session.latest_notification or {})
                role_session.manager_digest = dict(session.manager_digest or {})
                role_session.updated_at = datetime.now()
                if self.store and bool(getattr(self.store, "is_ready", False)) and hasattr(self.store, "save_delegation_role_session"):
                    await self.store.save_delegation_role_session(role_session)
            if representative_task is not None:
                representative_task.metadata = dict(representative_task.metadata)
                representative_task.metadata["current_turn_mode"] = str(session.current_turn_mode or "").strip()
                representative_task.metadata["member_session_state"] = self._serialize_session(session)
                representative_task.context_snapshot = dict(representative_task.context_snapshot)
                representative_task.context_snapshot["current_turn_mode"] = str(session.current_turn_mode or "").strip()
                representative_task.context_snapshot["member_session"] = self._serialize_session(session)
            session.updated_at = datetime.now()
            await self._persist_session(session, task=representative_task)
            await self._emit(
                "member_inbox_updated",
                {
                    "member_session_id": session.member_session_id,
                    "role_id": session.role_id,
                        "employee_id": session.employee_id,
                        "pending_count": len(session.pending_inbox),
                        "actionable_inbox_count": session.actionable_inbox_count,
                        "protocol_backlog_count": session.protocol_backlog_count,
                        "notification_backlog_count": session.notification_backlog_count,
                        "resident_status": session.resident_status,
                        "latest_notification": dict(session.latest_notification or {}),
                        "message_priority": self._highest_message_priority(session),
                        "inbox_cursor": session.inbox_cursor,
                    },
                )

    def get_message_triggered_sessions(self) -> list[tuple["CompanyMemberSession", dict]]:
        """Return (session, message) pairs for idle agents that have actionable
        messages needing a reply.  The caller (CompanyWorkItemExecutor) can
        create lightweight response tasks so the agent actually acts on the
        message instead of letting it sit in the inbox.
        """
        triggered: list[tuple["CompanyMemberSession", dict]] = []
        for session in self.member_sessions.values():
            if self._normalize_member_session_status(session) != "idle":
                continue
            for msg in list(session.inbox_state.get("actionable_chat", []) or []):
                if not isinstance(msg, dict):
                    continue
                if msg.get("reply_needed") or msg.get("urgency") in {"blocking", "high"}:
                    triggered.append((session, msg))
                    break  # one trigger per session per cycle
        return triggered

    def enqueue_runnable_tasks(self, tasks: list[Task]) -> None:
        """Append PENDING plain tasks to their role queue (dedup-safe).

        Phase B/#7 note: no longer uses ``_queued_task_ids`` as a
        gate. The queue itself is scanned for an existing entry —
        the DB (``task.status``) is the only source of truth for
        runnability; the deque provides dedup. Claim-time race
        safety still relies on ``_claimed_task_ids`` in
        ``claim_runnable_tasks`` (that set is actively maintained
        through complete_claim, so removing it would need a different
        race guard).
        """
        for task in tasks:
            if not self._is_runnable(task):
                continue
            queue_key = self._queue_key_for_task(task)
            if not queue_key:
                continue
            queue = self.role_queues[queue_key]
            # Dedup by scanning the deque instead of consulting an
            # easily-drifted shadow set.
            review_tag = f"review-task::{task.id}"
            if task.id in queue or review_tag in queue:
                continue
            # Kanban-push review tasks take priority over regular work so a
            # manager role always clears its review backlog before dispatching
            # or executing its own work.
            if bool((task.metadata or {}).get("review_task", False)):
                queue.appendleft(review_tag)
            else:
                queue.append(task.id)

    def enqueue_runnable_work_items(
        self,
        work_items: list[Any],
        *,
        task_by_work_item_id: dict[str, Task] | None = None,
    ) -> None:
        """Append dispatchable work items to their role queue.

        Phase B/#7 note: no longer uses ``_queued_work_item_ids`` as a
        gate. Gate is ``is_dispatchable`` (DB truth on phase + claim)
        plus a queue-scan for existing entries.
        """
        for work_item in work_items:
            work_item_id = str(getattr(work_item, "work_item_id", "") or "").strip()
            metadata = dict(getattr(work_item, "metadata", {}) or {})
            session_scope_id = str(metadata.get("session_scope_id", "") or "").strip()
            if not session_scope_id and task_by_work_item_id is not None:
                task = task_by_work_item_id.get(work_item_id)
                if task is not None:
                    session_scope_id = task_session_scope_id(task)
                    if session_scope_id:
                        work_item.metadata = {
                            **metadata,
                            "session_scope_id": session_scope_id,
                        }
                        metadata = dict(work_item.metadata or {})
            role_id = str(
                getattr(work_item, "role_id", "")
                or metadata.get("role_id", "")
                or ""
            ).strip()
            # Fix 5 PR4: queue key is role-scoped only. Same role across
            # every team context lands in one queue — aligned with the
            # role-scoped canonical_role_session_id from PR1.
            queue_key = scoped_queue_key(
                session_scope_id=session_scope_id,
                role_id=role_id,
            )
            if not work_item_id or not queue_key:
                continue
            if not is_dispatchable(work_item):
                continue
            queue = self.role_queues[queue_key]
            work_tag = f"work-item::{work_item_id}"
            review_tag = f"review-work-item::{work_item_id}"
            if work_tag in queue or review_tag in queue:
                continue
            if is_review_execution_work_item_metadata(metadata):
                queue.appendleft(review_tag)
            else:
                queue.append(work_tag)

    async def claim_runnable_tasks(
        self,
        tasks: list[Task],
        work_items: list[Any] | None = None,
    ) -> list[tuple[CompanyMemberSession, Task]]:
        hydrate_links = getattr(self.store, "hydrate_task_work_item_links", None) if self.store is not None else None
        if callable(hydrate_links):
            try:
                await hydrate_links(tasks)
            except Exception:
                logger.opt(exception=True).debug("claim_runnable_tasks: link hydration failed")
        task_map = {task.id: task for task in tasks}
        task_by_work_item_id = task_by_linked_work_item_id(tasks)
        work_item_map = {
            str(getattr(work_item, "work_item_id", "") or "").strip(): work_item
            for work_item in list(work_items or [])
            if str(getattr(work_item, "work_item_id", "") or "").strip()
        }
        claims: list[tuple[CompanyMemberSession, Task]] = []
        sessions = sorted(self.member_sessions.values(), key=self._session_sort_key)

        def _skip(reason: str, **ctx: Any) -> None:
            """Log a per-iteration skip with enough context to later
            answer "why wasn't this role dispatched on tick T?"."""
            detail = " ".join(f"{k}={v}" for k, v in ctx.items() if v is not None)
            logger.debug(f"claim skip: {reason}  {detail}")

        for session in sessions:
            session_status = self._normalize_member_session_status(session)
            session_label = f"role={session.role_id} sid={session.member_session_id}"
            queue = self.role_queues.get(self._queue_key_for_session(session))
            # Kanban-push soft-wake: a manager seat that is `blocked` on its
            # own AWAITING_PEER / AWAITING_* task must still be able to
            # process review tasks that arrive in its queue (otherwise the
            # review turn cannot run until something external unblocks the
            # manager, which in the kanban-push model is itself the review).
            can_soft_wake = (
                session_status == "blocked"
                and queue is not None
                and any(
                    entry.startswith("review-task::") or entry.startswith("review-work-item::")
                    for entry in queue
                )
            )
            if session_status == "running":
                logger.trace(
                    "claim skip: session.status blocks claim  session={} status={}",
                    session_label,
                    session_status,
                )
                continue
            if session_status == "blocked" and not can_soft_wake:
                _skip(
                    "session.status=blocked and no review-soft-wake entry in queue",
                    session=session_label,
                )
                continue
            role_session = self._role_session_for_member_session(session)
            role_session_status = ""
            if role_session is not None:
                role_session.status = normalize_role_runtime_status(
                    role_session.status,
                    role_session.focused_work_item_id,
                )
                if role_session.status == "idle":
                    role_session.focused_work_item_id = ""
                role_session_status = role_session.status
            if role_session_status == "running":
                logger.trace(
                    "claim skip: role_session already running  session={} role_session_id={}",
                    session_label,
                    role_session.role_session_id,
                )
                continue
            if not queue:
                # Empty queue is expected in steady state; log only at TRACE.
                logger.trace(f"claim skip: empty queue  session={session_label}")
                continue
            while queue:
                queued_item_id = self._pop_next_queue_entry(queue)
                work_item = None
                task = None
                if queued_item_id.startswith("review-task::"):
                    task_id = queued_item_id.split("::", 1)[1]
                    self._queued_task_ids.discard(task_id)
                    if task_id in self._claimed_task_ids:
                        _skip("review-task already claimed by this dispatcher",
                              session=session_label, task_id=task_id)
                        continue
                    task = task_map.get(task_id)
                    if task is None:
                        _skip("review-task not in tasks list this tick",
                              session=session_label, task_id=task_id)
                        continue
                    if not self._is_runnable(task):
                        _skip("review-task status not pending",
                              session=session_label, task_id=task_id,
                              status=getattr(task, "status", None))
                        continue
                    self._claimed_task_ids.add(task_id)
                elif queued_item_id.startswith("review-work-item::") or queued_item_id.startswith("work-item::"):
                    work_item_id = queued_item_id.split("::", 1)[1]
                    self._queued_work_item_ids.discard(work_item_id)
                    if work_item_id in self._claimed_work_item_ids:
                        _skip("work_item already claimed by this dispatcher",
                              session=session_label, work_item_id=work_item_id)
                        continue
                    work_item = work_item_map.get(work_item_id)
                    if work_item is None:
                        _skip("work_item not in work_items list this tick",
                              session=session_label, work_item_id=work_item_id)
                        continue
                    metadata = dict(getattr(work_item, "metadata", {}) or {})
                    if not is_dispatchable(work_item):
                        queued_behind = str(
                            metadata.get("queued_behind_session", "") or ""
                        ).strip()
                        reason = (
                            "valid queued behind role session"
                            if queued_behind
                            else "phase not runnable and not orphan"
                        )
                        _skip(f"is_dispatchable=False ({reason})",
                              session=session_label, work_item_id=work_item_id,
                              phase=getattr(getattr(work_item, "phase", None), "value", None),
                              claim=getattr(work_item, "claimed_by_role_runtime_session_id", ""),
                              queued_behind_session=queued_behind or None)
                        continue
                    if queued_item_id.startswith("review-work-item::"):
                        target_work_item_id = str(metadata.get("review_target_work_item_id", "") or "").strip()
                        target_work_item = work_item_map.get(target_work_item_id)
                        if target_work_item is None:
                            _skip("review-work-item's target not in work_items",
                                  session=session_label, review_wid=work_item_id,
                                  target_wid=target_work_item_id)
                            continue
                        if target_work_item.phase not in IN_REVIEW_PHASES:
                            _skip("review-work-item target no longer in review phase",
                                  session=session_label, review_wid=work_item_id,
                                  target_phase=getattr(target_work_item.phase, "value", ""))
                            continue
                    task = task_by_work_item_id.get(work_item_id)
                    if task is None:
                        get_runtime_task = getattr(self.store, "get_runtime_task_for_work_item", None) if self.store is not None else None
                        linked_task = None
                        if callable(get_runtime_task):
                            try:
                                linked_task = await get_runtime_task(work_item_id)
                            except Exception:
                                logger.opt(exception=True).debug("claim_runnable_tasks: linked runtime task lookup failed")
                        if linked_task is not None:
                            set_linked_work_item_id(linked_task, work_item_id)
                            task_map[linked_task.id] = linked_task
                            task_by_work_item_id[work_item_id] = linked_task
                            task = linked_task
                        else:
                            _skip("no task materialized for work_item this tick",
                                  session=session_label, work_item_id=work_item_id)
                            continue
                else:
                    task_id = queued_item_id
                    self._queued_task_ids.discard(task_id)
                    if task_id in self._claimed_task_ids:
                        _skip("plain task already claimed",
                              session=session_label, task_id=task_id)
                        continue
                    task = task_map.get(task_id)
                    if task is None:
                        _skip("plain task not in tasks list this tick",
                              session=session_label, task_id=task_id)
                        continue
                    if not self._is_runnable(task):
                        _skip("plain task status not pending",
                              session=session_label, task_id=task_id,
                              status=getattr(task, "status", None))
                        continue
                    self._claimed_task_ids.add(task_id)
                if work_item is not None:
                    claimed = await self._claim_role_session_work_item(
                        session,
                        work_item,
                        task,
                    )
                    if not claimed:
                        fresh_work_item = None
                        get_work_item = getattr(
                            self.store,
                            "get_delegation_work_item",
                            None,
                        )
                        if callable(get_work_item):
                            fresh_work_item = await get_work_item(work_item_id)
                        if fresh_work_item is not None:
                            work_item_map[work_item_id] = fresh_work_item
                        _skip(
                            "atomic WorkItem claim lost to a phase/hold/owner update",
                            session=session_label,
                            work_item_id=work_item_id,
                        )
                        continue
                    self._claimed_work_item_ids.add(work_item_id)
                if can_soft_wake and (
                    bool((task.metadata or {}).get("review_task", False))
                    or bool((task.metadata or {}).get("review_execution_work_item", False))
                ):
                    # Stash the prior focus so complete_claim can restore
                    # the `blocked` state when the review turn ends. The
                    # session only gets here if the original task was still
                    # awaiting something, so un-focusing it entirely would
                    # lose the peer_wait context.
                    session.metadata = dict(session.metadata or {})
                    session.metadata["_review_preempt_prev_task_id"] = str(session.current_task_id or "")
                    session.metadata["_review_preempt_prev_work_item_id"] = str(session.focused_work_item_id or "")
                    session.metadata["_review_preempt_active"] = True
                session.current_task_id = task.id
                session.focused_work_item_id = linked_work_item_id_for_task(task)
                self._set_member_session_status(session, "running")
                session.updated_at = datetime.now()
                self.prepare_task_for_session(session, task)
                await self._sync_current_turn_mode_to_work_item(task, session.current_turn_mode)
                if work_item is not None:
                    # #7: mirror the claim onto the in-memory work_item so
                    # subsequent iterations in the same claim pass see it
                    # and is_dispatchable returns False (race safety after
                    # removing the _queued_* memory-set gate).
                    try:
                        role_session = self._role_session_for_member_session(session)
                        if role_session is not None:
                            work_item.claimed_by_role_runtime_session_id = role_session.role_session_id
                    except Exception:
                        logger.opt(exception=True).debug("post-claim in-memory mirror failed")
                self._set_member_session_status(session, "running")
                await self._persist_session(session)
                await self._emit(
                    "member_claimed_work_item",
                    {
                        "member_session_id": session.member_session_id,
                        "role_id": session.role_id,
                        "employee_id": session.employee_id,
                        "seat_id": str((session.metadata or {}).get("seat_id", "") or "").strip(),
                        "team_id": str((session.metadata or {}).get("team_id", "") or "").strip(),
                        "task_id": task.id,
                        **work_item_identity_payload_for_task(task),
                        "message_priority": task.metadata.get("message_priority", "ready_queue"),
                        "work_item_id": linked_work_item_id_for_task(task),
                        "role_session_id": session.role_session_id,
                    },
                )
                claims.append((session, task))
                break
        return claims

    def prepare_task_for_session(self, session: CompanyMemberSession, task: Task) -> None:
        task.metadata = dict(task.metadata)
        task.context_snapshot = dict(task.context_snapshot)
        role_session = self._ensure_role_session(task)
        manager_role = session.manager_role_id or str(session.resume_state.get("manager_role_id", "") or "").strip()
        if not manager_role and self.org_engine is not None:
            agent = self.org_engine.get_agent(session.role_id)
            manager_role = str(getattr(agent, "reports_to", "") or "").strip()
        session.manager_role_id = manager_role
        session.manager_role_ids = sorted(dict.fromkeys([*session.manager_role_ids, *self._manager_role_ids(task)]))
        if role_session is not None:
            session.role_session_id = role_session.role_session_id
            session.focused_work_item_id = role_session.focused_work_item_id or linked_work_item_id_for_task(task)
            session.background_work_item_ids = list(role_session.background_work_item_ids or [])
            session.memory_slices_by_work_item = dict(role_session.memory_slices_by_work_item or {})
            session.adapter_session_state = dict(role_session.adapter_session_state or {})
            session.inbox_state = dict(role_session.inbox_state or session.inbox_state)
            session.manager_role_ids = list(role_session.manager_role_ids or session.manager_role_ids)
            session.resume_state = {**dict(role_session.resume_state or {}), **dict(session.resume_state or {})}
            session.current_work_item = dict(role_session.current_work_item or session.current_work_item)
            session.latest_notification = dict(role_session.latest_notification or session.latest_notification)
            session.manager_digest = dict(role_session.manager_digest or session.manager_digest)
            session.team_instance_id = str(getattr(role_session, "team_instance_id", "") or session.team_instance_id or "").strip()
            session.team_id = str(getattr(role_session, "team_id", "") or session.team_id or "").strip()
            session.seat_id = str(getattr(role_session, "seat_id", "") or session.seat_id or "").strip()
            session.seat_state_id = str(getattr(role_session, "seat_state_id", "") or session.seat_state_id or "").strip()
        session.metadata = {
            **dict(session.metadata or {}),
            "session_scope_id": task_session_scope_id(task),
        }
        assignment = self._build_assignment_envelope(session, task)
        session.current_assignment = dict(assignment)
        session.current_work_item = self._build_current_work_item(session, task)
        self._update_current_turn_mode(session, task)
        session.manager_digest = self._build_manager_digest(session, task)
        task.metadata["member_session_id"] = session.member_session_id
        task.metadata["current_turn_mode"] = str(session.current_turn_mode or "").strip()
        if session.role_session_id:
            task.metadata["delegation_role_session_id"] = session.role_session_id
        adapter_session_state = dict(session.adapter_session_state or {})
        resume_scope_id = str(
            adapter_session_state.get("external_resume_session_scope_id", "")
            or adapter_session_state.get("session_scope_id", "")
            or ""
        ).strip()
        assigned_agent = str(task.assigned_external_agent or "").strip()
        state_agent = str(
            adapter_session_state.get("external_resume_agent_type")
            or adapter_session_state.get("selected_execution_agent")
            or ""
        ).strip()
        if assigned_agent:
            agent_entry = adapter_session_state.get(assigned_agent)
            if isinstance(agent_entry, dict):
                adapter_session_state = {**adapter_session_state, **dict(agent_entry)}
                entry_token = str(
                    agent_entry.get("external_resume_session_id")
                    or agent_entry.get("resume_session_id")
                    or agent_entry.get("provider_session_id")
                    or ""
                ).strip()
                if entry_token:
                    adapter_session_state["external_resume_session_id"] = entry_token
                state_agent = assigned_agent
        if not external_resume_allowed_for_scope(task, resume_scope_id=resume_scope_id):
            adapter_session_state.pop("external_resume_session_id", None)
            adapter_session_state.pop("external_resume_session_scope_id", None)
            adapter_session_state.pop("external_resume_agent_type", None)
        external_resume_session_id = str(adapter_session_state.get("external_resume_session_id", "") or "").strip()
        if external_resume_session_id and assigned_agent and state_agent == assigned_agent:
            task.metadata["external_resume_session_id"] = external_resume_session_id
            task.metadata["external_resume_session_scope_id"] = (
                resume_scope_id or task_session_scope_id(task)
            )
            task.metadata["external_resume_agent_type"] = assigned_agent
        else:
            task.metadata.pop("external_resume_session_id", None)
            task.metadata.pop("external_resume_session_scope_id", None)
            task.metadata.pop("external_resume_agent_type", None)
        session_state_payload = self._serialize_session(session)
        session_state_payload["adapter_session_state"] = dict(adapter_session_state)
        task.metadata["member_session_state"] = session_state_payload
        if adapter_session_state:
            task.context_snapshot["seat_adapter_session_state"] = dict(adapter_session_state)
        seat_id = str((session.metadata or {}).get("seat_id", "") or "").strip()
        if seat_id:
            # ``delegation_*`` fields are the WorkItem owner envelope and must
            # not be overwritten by a shared resident role session. Keep the
            # runtime/session seat as explicit runtime audit instead.
            task.metadata["runtime_session_seat_id"] = seat_id
            task.metadata["runtime_session_team_id"] = str(
                (session.metadata or {}).get("team_id", "") or session.team_id or ""
            ).strip()
            task.metadata["runtime_session_team_instance_id"] = str(
                (session.metadata or {}).get("team_instance_id", "")
                or session.team_instance_id
                or ""
            ).strip()
            task.metadata.setdefault("seat_contact_role_ids", list((session.metadata or {}).get("contact_role_ids", []) or []))
            task.metadata.setdefault("allowed_delegate_role_ids", list((session.metadata or {}).get("allowed_delegate_role_ids", []) or []))
            task.metadata["direct_report_role_ids"] = list((session.metadata or {}).get("direct_report_role_ids", []) or [])
            task.metadata["direct_report_seat_ids"] = list((session.metadata or {}).get("direct_report_seat_ids", []) or [])
            task.metadata.setdefault("managed_team_id", str((session.metadata or {}).get("managed_team_id", "") or "").strip())
            task.metadata.setdefault("manager_seat_id", str((session.metadata or {}).get("manager_seat_id", "") or "").strip())
        task.metadata["message_priority"] = self._highest_message_priority(session)
        task.metadata["resident_assignment"] = dict(session.current_assignment or assignment)
        task.context_snapshot["member_session"] = dict(session_state_payload)
        task.context_snapshot["resident_assignment"] = dict(session.current_assignment or assignment)
        task.context_snapshot["current_turn_mode"] = str(session.current_turn_mode or "").strip()
        if role_session is not None:
            task.context_snapshot["delegation_role_session"] = self._serialize_role_session(role_session)
        if session.working_memory:
            task.context_snapshot["member_working_memory"] = list(session.working_memory[-8:])
        if session.resume_state:
            task.context_snapshot["member_resume_state"] = dict(session.resume_state)
            task.context_snapshot.setdefault("runtime_resume", dict(session.resume_state))
        pending_inbox = list(session.pending_inbox or [])
        if session.queued_inbox:
            pending_inbox = [*pending_inbox, *list(session.queued_inbox)]
            session.pending_inbox = pending_inbox[-8:]
            session.actionable_chat = list(session.pending_inbox)
            session.queued_inbox = []
            queued_session_state = self._serialize_session(session)
            queued_session_state["adapter_session_state"] = dict(adapter_session_state)
            task.metadata["member_session_state"] = queued_session_state
        if pending_inbox:
            task.context_snapshot["company_member_inbox"] = list(pending_inbox[-8:])
        if session.protocol_backlog:
            task.context_snapshot["company_member_protocol_backlog"] = list(session.protocol_backlog[:6])
        if session.notification_backlog:
            task.context_snapshot["company_member_notification_backlog"] = list(session.notification_backlog[:6])
        if session.latest_notification:
            task.context_snapshot["latest_company_notification"] = dict(session.latest_notification)
        if session.manager_digest:
            task.context_snapshot["manager_digest"] = dict(session.manager_digest)
        manager_board_summary = dict((session.metadata or {}).get("manager_board_summary", {}) or {})
        if manager_board_summary:
            task.context_snapshot["manager_board_summary"] = manager_board_summary
        pending_review_items = list(
            (session.metadata or {}).get("pending_review_items", []) or []
        )
        if pending_review_items:
            task.context_snapshot["pending_review_items"] = [
                dict(item) for item in pending_review_items if isinstance(item, dict)
            ]
            task.metadata["pending_review_items"] = [
                dict(item) for item in pending_review_items if isinstance(item, dict)
            ]
        else:
            task.context_snapshot.pop("pending_review_items", None)
            task.metadata.pop("pending_review_items", None)
        task.context_snapshot["resident_status"] = session.resident_status

    async def complete_claim(
        self,
        session: CompanyMemberSession,
        task: Task,
        result: TaskResult | None = None,
    ) -> None:
        multi_team_org = str((task.metadata or {}).get("runtime_model", "") or "").strip() == "multi_team_org"
        synthetic_inbox_turn = bool((task.metadata or {}).get("synthetic_inbox_turn", False))
        self._claimed_task_ids.discard(task.id)
        work_item_id = linked_work_item_id_for_task(task)
        if work_item_id:
            self._claimed_work_item_ids.discard(work_item_id)
        completed_focus_id = str(session.focused_work_item_id or work_item_id).strip()
        next_status = "idle"
        true_blocking_statuses = {
            TaskStatus.AWAITING_PEER,
            TaskStatus.AWAITING_HUMAN,
            TaskStatus.BLOCKED,
        }
        if not multi_team_org:
            true_blocking_statuses.update(
                {
                    TaskStatus.AWAITING_MANAGER_REVIEW,
                    TaskStatus.AWAITING_REVIEW,
                }
            )
        if task.status in true_blocking_statuses or dict(task.metadata.get("peer_wait", {}) or {}):
            next_status = "blocked"
        elif (not multi_team_org) and any(
            bool(item.get("requires_ack", False))
            for item in list(task.context_snapshot.get("pending_handoffs", []) or [])
            if isinstance(item, dict)
        ):
            next_status = "blocked"
        # Kanban-push soft-wake restore: if this turn was a review Task that
        # preempted a blocked manager session, and that session's prior task
        # is still awaiting something, restore the `blocked` state + the
        # original focus so the manager remains parked where it was.
        review_preempt_active = bool((session.metadata or {}).get("_review_preempt_active", False))
        restored_to_blocked = False
        if review_preempt_active and (
            bool((task.metadata or {}).get("review_task", False))
            or bool((task.metadata or {}).get("review_execution_work_item", False))
        ):
            prev_task_id = str((session.metadata or {}).get("_review_preempt_prev_task_id", "") or "")
            prev_focus = str((session.metadata or {}).get("_review_preempt_prev_work_item_id", "") or "")
            restore_blocked = bool(prev_focus and prev_task_id)
            if restore_blocked and self.store is not None and hasattr(self.store, "get_task"):
                try:
                    prev_task = await self.store.get_task(prev_task_id)
                except Exception:
                    prev_task = None
                if prev_task is not None:
                    prev_peer_wait = dict((prev_task.metadata or {}).get("peer_wait", {}) or {})
                    restore_blocked = bool(
                        prev_task.status in true_blocking_statuses
                        or prev_peer_wait
                    )
            next_status = "blocked" if restore_blocked else "idle"
            session.current_task_id = prev_task_id if restore_blocked else ""
            session.focused_work_item_id = prev_focus if restore_blocked else ""
            self._set_member_session_status(session, next_status)
            session.metadata = dict(session.metadata or {})
            session.metadata.pop("_review_preempt_active", None)
            session.metadata.pop("_review_preempt_prev_task_id", None)
            session.metadata.pop("_review_preempt_prev_work_item_id", None)
            restored_to_blocked = True
        if not restored_to_blocked:
            session.current_task_id = task.id if next_status == "blocked" else ""
            if next_status == "blocked":
                session.focused_work_item_id = completed_focus_id
            else:
                session.focused_work_item_id = ""
            self._set_member_session_status(session, next_status)
        session.updated_at = datetime.now()
        released_assignment = dict(session.current_assignment or {})
        summary = self._task_summary(task, result)
        if summary:
            session.working_memory = [*session.working_memory, summary][-12:]
            focused_work_item_id = completed_focus_id
            if focused_work_item_id:
                existing_slice = list(session.memory_slices_by_work_item.get(focused_work_item_id, []) or [])
                session.memory_slices_by_work_item[focused_work_item_id] = [*existing_slice, summary][-12:]
        runtime_state = dict(task.metadata.get("runtime_v2", {}) or {})
        if not runtime_state and result is not None:
            runtime_state = self._runtime_state_from_result(result)
        if runtime_state:
            session.resume_state = {
                **dict(session.resume_state),
                **runtime_state,
            }
        await self._refresh_adapter_session_state_from_store(session)
        adapter_state_updates = self._adapter_state_from_result(task, result)
        if adapter_state_updates:
            session.adapter_session_state = {
                **dict(session.adapter_session_state or {}),
                **adapter_state_updates,
            }
        if next_status != "running":
            session.current_assignment = {}
        session.current_work_item = self._build_current_work_item(session, task)
        await self._refresh_manager_board_state(session, task)
        current_turn_mode = self._update_current_turn_mode(session, task)
        await self._sync_current_turn_mode_to_work_item(task, current_turn_mode)
        session.manager_digest = self._build_manager_digest(session, task)
        session.inbox_state = {
            **dict(session.inbox_state or {}),
            "latest_notification": dict(session.latest_notification or {}),
            "current_work_item": dict(session.current_work_item or {}),
            "current_turn_mode": str(session.current_turn_mode or "").strip(),
            "manager_board_summary": dict((session.metadata or {}).get("manager_board_summary", {}) or {}),
            "manager_digest": dict(session.manager_digest or {}),
        }
        role_session = self._role_session_for_member_session(session)
        if role_session is not None:
            role_session.status = normalize_role_runtime_status(
                next_status,
                session.focused_work_item_id if next_status == "blocked" else "",
            )
            role_session.focused_work_item_id = session.focused_work_item_id if next_status == "blocked" else ""
            role_session.background_work_item_ids = [item for item in session.background_work_item_ids if item != role_session.focused_work_item_id]
            role_session.resume_state = dict(session.resume_state)
            role_session.inbox_state = dict(session.inbox_state)
            role_session.memory_slices_by_work_item = dict(session.memory_slices_by_work_item)
            role_session.adapter_session_state = dict(session.adapter_session_state)
            role_session.current_work_item = dict(session.current_work_item or {})
            role_session.latest_notification = dict(session.latest_notification or {})
            role_session.manager_digest = dict(session.manager_digest or {})
            role_session.updated_at = datetime.now()
            if self.store and bool(getattr(self.store, "is_ready", False)) and hasattr(self.store, "save_delegation_role_session"):
                await self.store.save_delegation_role_session(role_session)
        task.metadata = dict(task.metadata)
        task.metadata["member_session_state"] = self._serialize_session(session)
        task.metadata["current_turn_mode"] = str(session.current_turn_mode or "").strip()
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["member_session"] = self._serialize_session(session)
        task.context_snapshot["current_turn_mode"] = str(session.current_turn_mode or "").strip()
        await self._persist_session(session, task=task)
        projection_id = projection_id_for_task(task)
        status_body = (
            f"Resident teammate `{session.role_id}` is now `{next_status}` after "
            f"work item `{projection_id or task.id}`."
        )
        if self.communication and session.manager_role_id and hasattr(self.communication, "send_manager_notification") and not synthetic_inbox_turn:
            assignment_id = str(released_assignment.get("assignment_id", "") or "").strip()
            # A worker's successful completion of a delegated work item should
            # automatically request the manager's review. The work item will
            # already have transitioned to review, and here we ensure the
            # manager gets an actionable notification
            # with reply_needed=True so the runtime will wake the manager up
            # (via _queue_multi_team_response_tasks / claim_runnable_tasks).
            review_required = bool(
                multi_team_org
                and work_item_id
                and next_status == "idle"
                and task.status == TaskStatus.DONE
            )
            if summary or review_required:
                completion_semantic = (
                    CommsSemanticType.APPROVAL_REQUEST
                    if review_required
                    else CommsSemanticType.COMPLETION
                    if multi_team_org
                    else CommsSemanticType.WORK_ITEM_RESULT
                )
                if review_required:
                    completion_subject = f"Review needed: {task.title}"
                elif multi_team_org:
                    completion_subject = f"Completion: {task.title}"
                else:
                    completion_subject = f"Work item result: {projection_id or task.title}"
                completion_body = summary or (
                    f"`{session.role_id}` completed work item `{work_item_id}` "
                    f"(task `{task.title or task.id}`) without a structured report. "
                    f"Please verify the deliverable directly before approving."
                    if review_required
                    else ""
                )
                completion_metadata = {
                    **work_item_identity_payload(projection_id=projection_id, turn_type=turn_type_for_task(task, fallback="")),
                    "assignment_id": assignment_id,
                    "current_work_item": dict(session.current_work_item or {}),
                    "manager_board_summary": dict((session.metadata or {}).get("manager_board_summary", {}) or {}),
                }
                if review_required:
                    completion_metadata["review_required"] = True
                    completion_metadata["work_item_id"] = work_item_id
                    completion_metadata["completion_report"] = summary
                await self.communication.send_manager_notification(
                    from_agent=session.role_id,
                    task=task,
                    semantic_type=completion_semantic,
                    subject=completion_subject,
                    body=completion_body,
                    metadata=completion_metadata,
                    reply_needed=review_required,
                )
            status_semantic = CommsSemanticType.STATUS_DIGEST if multi_team_org else CommsSemanticType.IDLE_NOTIFICATION
            status_subject = f"Status digest: {task.title}" if multi_team_org else f"Resident teammate {next_status}"
            status_reply_needed = False
            if next_status == "blocked":
                status_semantic = CommsSemanticType.BLOCKER if multi_team_org else CommsSemanticType.BLOCKED_ON_DECISION
                status_subject = f"Blocked: {task.title}" if multi_team_org else f"Resident teammate blocked: {projection_id or task.title}"
                waiting_on = [
                    str(item).strip()
                    for item in list(
                        dict(task.metadata.get("peer_wait", {}) or {}).get("waiting_on_agents")
                        or dict(task.metadata.get("peer_wait", {}) or {}).get("awaiting_replies_from")
                        or []
                    )
                    if str(item).strip()
                ]
                waiting_suffix = f" Waiting on: {', '.join(waiting_on)}." if waiting_on else ""
                status_body = (
                    f"Resident teammate `{session.role_id}` is blocked after "
                    f"work item `{projection_id or task.id}`.{waiting_suffix}"
                )
                status_reply_needed = True
            await self.communication.send_manager_notification(
                from_agent=session.role_id,
                task=task,
                semantic_type=status_semantic,
                subject=status_subject,
                body=status_body,
                metadata={
                    **work_item_identity_payload(projection_id=projection_id, turn_type=turn_type_for_task(task, fallback="")),
                    "assignment_id": assignment_id,
                    "resident_status": next_status,
                    "current_work_item": dict(session.current_work_item or {}),
                    "manager_digest": dict(session.manager_digest or {}),
                    "manager_board_summary": dict((session.metadata or {}).get("manager_board_summary", {}) or {}),
                },
                reply_needed=status_reply_needed,
            )
        notification_payload = {
            "member_session_id": session.member_session_id,
            "worker_id": session.member_session_id,
            "worker_type": "company_member",
            "notification_kind": (
                "blocked"
                if next_status == "blocked"
                else "status_digest"
                if multi_team_org
                else "idle"
            ),
            "summary": summary or status_body,
            "resident_status": next_status,
            "role_id": session.role_id,
            "employee_id": session.employee_id,
            "task_id": task.id,
            "session_id": str(task.session_id or task.parent_session_id or "").strip(),
            **work_item_identity_payload_for_task(task),
        }
        await self._emit("member_idle", notification_payload)
        await self._emit("worker_notification", notification_payload)

    def session_for_task(self, task: Task) -> CompanyMemberSession:
        session_id = str(task.metadata.get("member_session_id", "")).strip()
        if session_id and session_id in self.member_sessions:
            return self.member_sessions[session_id]
        return self._ensure_member_session(task)

    def _role_session_for_member_session(self, session: CompanyMemberSession) -> DelegationRoleSession | None:
        role_session_id = str(session.role_session_id or "").strip()
        if role_session_id and role_session_id in self.role_sessions:
            return self.role_sessions[role_session_id]
        session_scope_id = str((session.metadata or {}).get("session_scope_id", "") or "").strip()
        for candidate in self.role_sessions.values():
            candidate_scope = str((getattr(candidate, "metadata", {}) or {}).get("session_scope_id", "") or "").strip()
            if session_scope_id and candidate_scope != session_scope_id:
                continue
            if candidate.seat_id and candidate.seat_id == session.seat_id:
                return candidate
        for candidate in self.role_sessions.values():
            candidate_scope = str((getattr(candidate, "metadata", {}) or {}).get("session_scope_id", "") or "").strip()
            if session_scope_id and candidate_scope != session_scope_id:
                continue
            if candidate.role_id == session.role_id and candidate.seat_id == session.seat_id:
                return candidate
        return None

    async def _refresh_adapter_session_state_from_store(
        self,
        session: CompanyMemberSession,
    ) -> None:
        role_session_id = str(session.role_session_id or "").strip()
        getter = getattr(self.store, "get_delegation_role_session", None)
        if not role_session_id or not callable(getter):
            return
        try:
            persisted = await getter(role_session_id)
        except Exception:
            logger.opt(exception=True).debug(
                "company runtime adapter-state refresh failed"
            )
            return
        if persisted is None:
            return
        adapter_state = dict(
            getattr(persisted, "adapter_session_state", {}) or {}
        )
        session.adapter_session_state = adapter_state
        role_session = self.role_sessions.get(role_session_id)
        if role_session is not None:
            role_session.adapter_session_state = dict(adapter_state)

    def _ensure_role_session(self, task: Task) -> DelegationRoleSession | None:
        role_id = self._role_id(task)
        if not role_id:
            return None
        role_session_id = self._role_session_id(task, role_id=role_id)
        existing = self.role_sessions.get(role_session_id)
        if existing is not None:
            existing.status = normalize_role_runtime_status(
                existing.status,
                existing.focused_work_item_id,
            )
            if existing.status == "idle":
                existing.focused_work_item_id = ""
            existing.metadata = {
                **dict(existing.metadata or {}),
                "session_scope_id": task_session_scope_id(task),
            }
            return existing
        run_id = str((task.metadata or {}).get("delegation_run_id", "") or "").strip()
        role_session = DelegationRoleSession(
            role_session_id=role_session_id,
            run_id=run_id,
            role_id=role_id,
            employee_id=self._employee_id(task),
            manager_role_ids=self._manager_role_ids(task),
            status="idle",
            metadata={"session_scope_id": task_session_scope_id(task)},
        )
        self.role_sessions[role_session_id] = role_session
        return role_session

    async def _claim_role_session_work_item(
        self,
        session: CompanyMemberSession,
        work_item: Any,
        task: Task,
    ) -> bool:
        """Atomically claim ``work_item`` for the role-instance behind
        ``session``.

        In the role-instance model the claim identity is
        ``role_runtime_session_id``. Seat / manager-seat columns are
        still written for org-chart lookups but they are NOT part of
        the claim key — only the role session is. Pure in-memory runtimes have
        no durable race to arbitrate and keep the same local claim semantics.
        """
        work_item_id = str(getattr(work_item, "work_item_id", "") or "").strip()
        if not work_item_id:
            return False
        role_session = self._ensure_role_session(task)
        if role_session is None:
            return False
        work_item_revision = 0
        try:
            work_item_revision = int((getattr(work_item, "metadata", {}) or {}).get("manager_mutation_revision") or 0)
        except (TypeError, ValueError):
            work_item_revision = 0
        store_ready = self.store is not None and bool(
            getattr(self.store, "is_ready", False)
        )
        claim = (
            getattr(self.store, "claim_delegation_work_item_if_dispatchable", None)
            if store_ready
            else None
        )
        if store_ready:
            # A durable runtime must win the store CAS before it mutates any
            # in-memory scheduling state.  This is the Stop/shutdown race
            # boundary: a missing CAS API is a failed claim, not permission to
            # fall back to the former best-effort update path.
            if not callable(claim):
                return False
            persisted = await claim(
                work_item_id,
                expected_phase=getattr(work_item, "phase", Phase.READY),
                role_runtime_session_id=role_session.role_session_id,
                seat_id=str(getattr(session, "seat_id", "") or "").strip(),
                task_id=task.id,
                work_item_revision=work_item_revision,
            )
            if persisted is None:
                return False

            work_item.phase = persisted.phase
            work_item.role_runtime_session_id = persisted.role_runtime_session_id
            work_item.claimed_by_role_runtime_session_id = (
                persisted.claimed_by_role_runtime_session_id
            )
            work_item.claimed_by_seat_id = persisted.claimed_by_seat_id
            work_item.metadata = dict(persisted.metadata or {})
        ready_background_ids = [
            item_id
            for item_id in list(role_session.background_work_item_ids or [])
            if item_id and item_id != work_item_id
        ]
        role_session.focused_work_item_id = work_item_id
        role_session.background_work_item_ids = ready_background_ids
        role_session.status = "running"
        role_session.updated_at = datetime.now()
        session.role_session_id = role_session.role_session_id
        session.focused_work_item_id = work_item_id
        session.background_work_item_ids = list(role_session.background_work_item_ids)
        task.metadata = dict(task.metadata)
        task.metadata["delegation_role_session_id"] = role_session.role_session_id
        task.metadata["started_work_item_revision"] = work_item_revision
        task.metadata["claimed_work_item_revision"] = work_item_revision
        if self.store and bool(getattr(self.store, "is_ready", False)) and hasattr(self.store, "save_delegation_role_session"):
            await self.store.save_delegation_role_session(role_session)
        return True

    def ensure_role_instance_session(
        self, task: Task
    ) -> tuple[CompanyMemberSession, DelegationRoleSession | None]:
        """Role-instance upsert.

        Phase A collapsed "per-seat session" into "per-role-instance
        session" — the ``CompanyMemberSession`` (runtime scheduling
        view) and the ``DelegationRoleSession`` (persistent seat
        memory view) are 1-to-1 coupled by ``role_session_id``. This
        helper returns both, upserting the pair atomically.

        Callers that only want one of the two use the backwards-compat
        ``_ensure_member_session`` / ``_ensure_role_session`` below.
        New call sites should prefer this helper so the 1:1 invariant
        is explicit.
        """
        member = self._ensure_member_session(task)
        role = self._role_session_for_member_session(member)
        return member, role

    def _ensure_member_session(self, task: Task) -> CompanyMemberSession:
        """Upsert the CompanyMemberSession for ``task``'s role.

        The linked DelegationRoleSession is created as a side-effect
        (see ``_ensure_role_session`` call below) to preserve the 1:1
        invariant — callers that want both entities should use
        ``ensure_role_instance_session`` for clarity.
        """
        role_id = self._role_id(task)
        employee_id = self._employee_id(task)
        member_session_id = self._member_session_id(task, role_id=role_id, employee_id=employee_id)
        existing = self.member_sessions.get(member_session_id)
        if existing is not None:
            self._merge_task_session_state(existing, task)
            self._sync_member_session_from_role_session(
                existing,
                self._ensure_role_session(task),
            )
            return existing

        session = CompanyMemberSession(
            member_session_id=member_session_id,
            role_id=role_id,
            employee_id=employee_id,
        )
        session.team_id = str((task.metadata or {}).get("delegation_team_id", "") or "").strip()
        session.seat_id = self._seat_id(task)
        session.metadata = {
            **dict(session.metadata or {}),
            "team_id": session.team_id,
            "seat_id": session.seat_id,
            "session_scope_id": task_session_scope_id(task),
            "manager_seat_id": str((task.metadata or {}).get("manager_seat_id", "") or "").strip(),
            "managed_team_id": str((task.metadata or {}).get("managed_team_id", "") or "").strip(),
            "contact_role_ids": list((task.metadata or {}).get("seat_contact_role_ids", []) or []),
            "allowed_delegate_role_ids": list((task.metadata or {}).get("allowed_delegate_role_ids", []) or []),
            "direct_report_role_ids": list((task.metadata or {}).get("direct_report_role_ids", []) or []),
            "direct_report_seat_ids": list((task.metadata or {}).get("direct_report_seat_ids", []) or []),
        }
        self._merge_task_session_state(session, task)
        session.metadata = {
            **dict(session.metadata or {}),
            "session_scope_id": task_session_scope_id(task),
        }
        role_session = self._ensure_role_session(task)
        if role_session is not None:
            self._sync_member_session_from_role_session(session, role_session)
        self.member_sessions[member_session_id] = session
        return session

    def _merge_task_session_state(self, session: CompanyMemberSession, task: Task) -> None:
        persisted = dict(task.metadata.get("member_session_state", {}) or {})
        persisted_metadata = dict(persisted.get("metadata", {}) or {})
        if persisted_metadata:
            session.metadata = {
                **dict(session.metadata or {}),
                **persisted_metadata,
            }
        persisted_status = str(persisted.get("status", "") or "").strip()
        persisted_resident_status = str(persisted.get("resident_status", "") or "").strip()
        persisted_role_session_id = str(persisted.get("role_session_id", "") or task.metadata.get("delegation_role_session_id", "") or "").strip()
        if persisted_role_session_id:
            session.role_session_id = persisted_role_session_id
        team_instance_id = str(persisted.get("team_instance_id", "") or "").strip()
        if team_instance_id:
            session.team_instance_id = team_instance_id
        team_id = str(persisted.get("team_id", "") or "").strip()
        if team_id:
            session.team_id = team_id
        seat_id = str(persisted.get("seat_id", "") or task.metadata.get("delegation_seat_id", "") or "").strip()
        if seat_id:
            session.seat_id = seat_id
        seat_state_id = str(persisted.get("seat_state_id", "") or "").strip()
        if seat_state_id:
            session.seat_state_id = seat_state_id
        persisted_current_task = str(persisted.get("current_task_id", "") or "").strip()
        if persisted_current_task:
            session.current_task_id = persisted_current_task
        focused_work_item_id = str(persisted.get("focused_work_item_id", "") or "").strip()
        if focused_work_item_id:
            session.focused_work_item_id = focused_work_item_id
        if persisted_status or persisted_resident_status:
            self._set_member_session_status(
                session,
                persisted_status or persisted_resident_status,
            )
        background_work_item_ids = persisted.get("background_work_item_ids")
        if isinstance(background_work_item_ids, list):
            session.background_work_item_ids = [str(item).strip() for item in background_work_item_ids if str(item).strip()]
        working_memory = list(persisted.get("working_memory", []) or [])
        if working_memory:
            merged_memory = [*session.working_memory, *[str(item).strip() for item in working_memory if str(item).strip()]]
            session.working_memory = list(dict.fromkeys(merged_memory))[-12:]
        memory_slices_by_work_item = persisted.get("memory_slices_by_work_item")
        if isinstance(memory_slices_by_work_item, dict):
            session.memory_slices_by_work_item = {
                str(key).strip(): [str(item).strip() for item in list(value or []) if str(item).strip()]
                for key, value in memory_slices_by_work_item.items()
                if str(key).strip()
            }
        inbox_cursor = persisted.get("inbox_cursor")
        if isinstance(inbox_cursor, int):
            session.inbox_cursor = max(session.inbox_cursor, inbox_cursor)
        pending_inbox = persisted.get("pending_inbox")
        if isinstance(pending_inbox, list) and pending_inbox:
            session.pending_inbox = [dict(item) for item in pending_inbox[:8] if isinstance(item, dict)]
        queued_inbox = persisted.get("queued_inbox")
        if isinstance(queued_inbox, list) and queued_inbox:
            session.queued_inbox = [dict(item) for item in queued_inbox[:12] if isinstance(item, dict)]
        actionable_chat = persisted.get("actionable_chat")
        if isinstance(actionable_chat, list) and actionable_chat:
            session.actionable_chat = [dict(item) for item in actionable_chat[:8] if isinstance(item, dict)]
        protocol_backlog = persisted.get("protocol_backlog")
        if isinstance(protocol_backlog, list) and protocol_backlog:
            session.protocol_backlog = [dict(item) for item in protocol_backlog[:8] if isinstance(item, dict)]
        notification_backlog = persisted.get("notification_backlog")
        if isinstance(notification_backlog, list) and notification_backlog:
            session.notification_backlog = [dict(item) for item in notification_backlog[:8] if isinstance(item, dict)]
        latest_notification = persisted.get("latest_notification")
        if isinstance(latest_notification, dict) and latest_notification:
            session.latest_notification = dict(latest_notification)
        current_work_item = persisted.get("current_work_item")
        if isinstance(current_work_item, dict) and current_work_item:
            session.current_work_item = dict(current_work_item)
        manager_digest = persisted.get("manager_digest")
        if isinstance(manager_digest, dict) and manager_digest:
            session.manager_digest = dict(manager_digest)
        inbox_state = persisted.get("inbox_state")
        if isinstance(inbox_state, dict) and inbox_state:
            session.inbox_state = dict(inbox_state)
        for field_name in (
            "actionable_inbox_count",
            "protocol_backlog_count",
            "notification_backlog_count",
        ):
            value = persisted.get(field_name)
            if isinstance(value, int):
                setattr(session, field_name, value)
        resume_state = persisted.get("resume_state")
        if isinstance(resume_state, dict):
            session.resume_state = {
                **dict(session.resume_state),
                **resume_state,
            }
        current_turn_mode = str(
            persisted.get("current_turn_mode", "")
            or persisted_metadata.get("current_turn_mode", "")
            or task.metadata.get("current_turn_mode", "")
            or dict(task.context_snapshot.get("member_session", {}) or {}).get("current_turn_mode", "")
            or task.context_snapshot.get("current_turn_mode", "")
            or ""
        ).strip()
        if current_turn_mode:
            session.current_turn_mode = current_turn_mode
        if session.seat_id:
            session.metadata = {
                **dict(session.metadata or {}),
                "seat_id": session.seat_id,
                "team_id": session.team_id,
            }
        for key in (
            "manager_seat_id",
            "managed_team_id",
        ):
            value = str((task.metadata or {}).get(key, "") or "").strip()
            if value:
                session.metadata = {
                    **dict(session.metadata or {}),
                    key: value,
                }
        for key in (
            "seat_contact_role_ids",
            "allowed_delegate_role_ids",
            "direct_report_role_ids",
            "direct_report_seat_ids",
        ):
            values = [str(item).strip() for item in list((task.metadata or {}).get(key, []) or []) if str(item).strip()]
            if values:
                session.metadata = {
                    **dict(session.metadata or {}),
                    (
                        "contact_role_ids" if key == "seat_contact_role_ids"
                        else "allowed_delegate_role_ids" if key == "allowed_delegate_role_ids"
                        else key
                    ): values,
                }
        if session.current_turn_mode:
            session.metadata = {
                **dict(session.metadata or {}),
                "current_turn_mode": session.current_turn_mode,
            }
        manager_role_id = str(persisted.get("manager_role_id", "") or "").strip()
        if manager_role_id:
            session.manager_role_id = manager_role_id
        manager_role_ids = persisted.get("manager_role_ids")
        if isinstance(manager_role_ids, list):
            session.manager_role_ids = [str(item).strip() for item in manager_role_ids if str(item).strip()]
        adapter_session_state = persisted.get("adapter_session_state")
        if isinstance(adapter_session_state, dict):
            session.adapter_session_state = dict(adapter_session_state)
        assignment = dict(persisted.get("current_assignment", {}) or {})
        if assignment:
            session.current_assignment = assignment
        if self.org_engine is not None:
            agent = self.org_engine.get_agent(session.role_id)
            manager_role = str(getattr(agent, "reports_to", "") or "").strip()
            if manager_role and manager_role != "owner":
                session.manager_role_id = manager_role
                session.manager_role_ids = sorted(dict.fromkeys([*session.manager_role_ids, manager_role]))
                session.resume_state = dict(session.resume_state)
                session.resume_state["manager_role_id"] = manager_role
        self._normalize_member_session_status(session)
        if task.result and isinstance(task.result, dict) and task.result.get("content"):
            summary = str(task.result.get("content", "")).strip()
            if summary:
                session.working_memory = [*session.working_memory, summary][-12:]

    async def _persist_session(self, session: CompanyMemberSession, task: Task | None = None) -> None:
        self._normalize_member_session_status(session)
        await self._persist_seat_state(session, task=task)
        if not self.save_runtime_session:
            if self.store and bool(getattr(self.store, "is_ready", False)) and hasattr(self.store, "save_delegation_role_session"):
                role_session = self._role_session_for_member_session(session)
                if role_session is not None:
                    await self.store.save_delegation_role_session(role_session)
            return
        role_session = self._role_session_for_member_session(session)
        project_id = str(getattr(task, "project_id", "") or getattr(role_session, "project_id", "") or "default")
        runtime_task_id = str(getattr(task, "id", "") or session.current_task_id or "").strip() or None
        runtime_session_id = str(getattr(task, "session_id", None) or "").strip() or None
        await self.save_runtime_session(
            runtime_session_id=session.member_session_id,
            project_id=project_id,
            session_id=runtime_session_id,
            task_id=runtime_task_id,
            status=session.status,
            metadata=self._serialize_session(session),
        )
        if self.store and bool(getattr(self.store, "is_ready", False)) and hasattr(self.store, "save_delegation_role_session"):
            if role_session is not None:
                await self.store.save_delegation_role_session(role_session)

    async def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.emit_runtime_event is None:
            return
        await self.emit_runtime_event(event_type, payload)

    def _highest_message_priority(self, session: CompanyMemberSession) -> str:
        inbox = self._session_priority_messages(session)
        if not inbox:
            return "ready_queue"
        top = inbox[0]
        return str(self._message_priority_label(session, top))

    def _message_sort_key(self, session: CompanyMemberSession, message: dict[str, Any]) -> tuple[int, str]:
        priority = self._message_priority_value(session, message)
        msg_id = str(message.get("msg_id", "")).strip()
        return (priority, msg_id)

    def _message_priority_value(self, session: CompanyMemberSession, message: dict[str, Any]) -> int:
        manager_role = str(session.manager_role_id or session.resume_state.get("manager_role_id", "")).strip()
        from_agent = str(message.get("from_agent", "") or "").strip()
        message_class = str(message.get("message_class", "") or dict(message.get("metadata", {}) or {}).get("message_class", "")).strip().lower()
        protocol_type = str(message.get("protocol_type", "") or dict(message.get("metadata", {}) or {}).get("protocol_type", "")).strip().lower()
        actionable = bool(message.get("actionable", dict(message.get("metadata", {}) or {}).get("actionable", True)))
        if protocol_type == "shutdown_request":
            return -1
        if manager_role and from_agent == manager_role:
            return 0
        if message_class == "protocol":
            return 1
        if bool(message.get("reply_needed")) or str(message.get("urgency", "")).strip().lower() == "blocking":
            return 2
        if actionable:
            return 3
        if message_class == "notification":
            return 4
        return 5

    def _message_priority_label(self, session: CompanyMemberSession, message: dict[str, Any]) -> str:
        priority = self._message_priority_value(session, message)
        if priority == 0:
            return "manager"
        if priority == 1:
            return "protocol"
        if priority == 2:
            return "blocking_reply"
        if priority == 3:
            return "peer_dm"
        return "notification"

    def _get_role_config(self, role_id: str) -> Any | None:
        if not self.org_engine:
            return None
        org_config = getattr(getattr(self.org_engine, "config", None), "org", None)
        if org_config is not None:
            for role in getattr(org_config, "roles", []) or []:
                if role.id == role_id:
                    return role
        effective_roles = getattr(self.org_engine, "_effective_roles", None)
        if callable(effective_roles):
            try:
                for role in list(effective_roles()) or []:
                    if getattr(role, "id", "") == role_id:
                        return role
            except Exception:
                pass
        return None

    def _session_sort_key(self, session: CompanyMemberSession) -> tuple[int, str]:
        priority = 3
        inbox = self._session_priority_messages(session)
        if inbox:
            priority = self._message_priority_value(session, inbox[0])
        # Coordinators with accumulated notifications get scheduling priority
        role_cfg = self._get_role_config(session.role_id)
        if role_cfg and (
            getattr(role_cfg, "role_type", "worker") == "coordinator"
            or bool(list(getattr(role_cfg, "can_spawn", []) or []))
        ):
            threshold = 3
            policy = getattr(role_cfg, "coordinator_policy", None)
            if policy:
                threshold = getattr(policy, "inbox_threshold", 3)
            pending_count = len(session.inbox_state.get("actionable_chat", []))
            if pending_count >= threshold:
                priority = max(priority - 10, -10)  # boost priority
        return (priority, session.member_session_id)

    @staticmethod
    def _is_runnable(task: Task) -> bool:
        return getattr(task, "status", None) and str(task.status.value if hasattr(task.status, "value") else task.status) == "pending"

    @staticmethod
    def _pop_next_queue_entry(queue: deque[str]) -> str:
        """Pop the next queue entry, prioritizing review tasks.

        Review tasks are always pulled before any other work in the seat's
        queue — this enforces "manager must clear pending reviews before
        dispatching or executing own work" at the scheduler level rather
        than at the prompt level.
        """
        for index, entry in enumerate(queue):
            if entry.startswith("review-task::") or entry.startswith("review-work-item::"):
                if index == 0:
                    return queue.popleft()
                del queue[index]
                return entry
        return queue.popleft()

    @staticmethod
    def _role_id(task: Task) -> str:
        return str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip()

    @staticmethod
    def _seat_id(task: Task) -> str:
        metadata = dict(getattr(task, "metadata", {}) or {})
        return str(
            metadata.get("delegation_seat_id", "")
            or metadata.get("seat_id", "")
            or ""
        ).strip()

    def _queue_key_for_task(self, task: Task) -> str:
        # Fix 5 PR4: role-scoped (no team_instance slot). Same role =
        # same queue regardless of which team's work arrived. The role's
        # single session drains this queue serially per PR3.
        return scoped_queue_key(
            session_scope_id=task_session_scope_id(task),
            role_id=self._role_id(task),
        )

    def _queue_key_for_session(self, session: CompanyMemberSession) -> str:
        # Fix 5 PR4: matches _queue_key_for_task — role-scoped only.
        return scoped_queue_key(
            session_scope_id=str((session.metadata or {}).get("session_scope_id", "") or "").strip(),
            role_id=str(session.role_id or "").strip(),
        )

    def _employee_id(self, task: Task) -> str:
        assignment = dict(task.metadata.get("employee_assignment", {}) or {})
        employee_id = str(assignment.get("employee_id", "") or "").strip()
        if employee_id:
            return employee_id
        role_id = self._role_id(task)
        return f"{role_id}-default-session"

    def _member_session_id(self, task: Task, *, role_id: str, employee_id: str) -> str:
        # Fix 5 PR4: role-scoped (no team_instance slot). One role = one
        # member_session across every team context, aligned with the
        # role-scoped canonical_role_session_id.
        return scoped_member_session_id(
            project_id=str(getattr(task, "project_id", "") or "default").strip() or "default",
            session_scope_id=task_session_scope_id(task),
            role_id=role_id,
            employee_id=employee_id,
            explicit_id=str((task.metadata or {}).get("member_session_id", "") or "").strip(),
        )

    def _build_assignment_envelope(self, session: CompanyMemberSession, task: Task) -> dict[str, Any]:
        workspace_root = (
            str(task.metadata.get("comms_workspace_root", "") or "").strip()
            or str(task.metadata.get("workspace_root", "") or "").strip()
            or str(task.metadata.get("target_output_dir", "") or "").strip()
        )
        team_memory_digest = ""
        team_memory_metadata: dict[str, Any] = {}
        if workspace_root:
            try:
                layout = _comms.resolve_layout(
                    workspace_root,
                    str(task.project_id or "default").strip() or "default",
                    str(task.parent_session_id or task.session_id or "default").strip() or "default",
                )
                _comms.ensure_layout(layout, [session.role_id, session.manager_role_id or session.role_id])
                team_memory_payload = _comms.read_team_memory_digest_payload(layout, max_chars=1200)
                team_memory_digest = str(team_memory_payload.get("digest", "") or "").strip()
                team_memory_metadata = {
                    key: value
                    for key, value in team_memory_payload.items()
                    if key != "digest"
                }
            except Exception:
                team_memory_digest = ""
                team_memory_metadata = {}
        # Fix 5 PR5: team_instance / team_id / seat_id belong to the
        # CURRENT task — the session now serves multiple team contexts
        # over its lifetime, so ``session.*`` reflects only the most
        # recently observed context and is wrong for any other task.
        # Prefer per-task metadata; fall back to session as a last resort.
        task_metadata = dict(task.metadata or {})
        assignment_team_instance = (
            str(task_metadata.get("delegation_team_instance_id", "") or "").strip()
            or str(task_metadata.get("team_instance_id", "") or "").strip()
            or str(session.team_instance_id or "").strip()
        )
        assignment_team_id = (
            str(task_metadata.get("delegation_team_id", "") or "").strip()
            or str(task_metadata.get("team_id", "") or "").strip()
            or str(session.team_id or (session.metadata or {}).get("team_id", "") or "").strip()
        )
        assignment_seat_id = (
            str(task_metadata.get("delegation_seat_id", "") or "").strip()
            or str(task_metadata.get("seat_id", "") or "").strip()
            or str(session.seat_id or (session.metadata or {}).get("seat_id", "") or "").strip()
        )
        runtime_status = self._normalize_member_session_status(session)
        assignment = ResidentAssignmentEnvelope(
            member_session_id=session.member_session_id,
            team_instance_id=assignment_team_instance,
            team_id=assignment_team_id,
            seat_id=assignment_seat_id,
            seat_state_id=str(session.seat_state_id or "").strip(),
            role_runtime_session_id=str(session.role_session_id or "").strip(),
            work_item_projection_id=projection_id_for_task(task),
            work_item_turn_type=turn_type_for_task(task, fallback=""),
            role_id=session.role_id,
            employee_id=session.employee_id,
            manager_role_id=session.manager_role_id,
            task_id=str(task.id or "").strip(),
            session_id=str(task.session_id or task.parent_session_id or "").strip(),
            write_scope=str(task.metadata.get("write_scope", "") or "").strip(),
            ownership_contract=render_ownership_contract(task),
            dependency_snapshot=[str(dep).strip() for dep in list(task.dependencies or []) if str(dep).strip()],
            pending_inbox=list([*list(session.pending_inbox or []), *list(session.queued_inbox or [])][-8:]),
            actionable_chat=list(session.actionable_chat[:8]),
            protocol_backlog=list(session.protocol_backlog[:6]),
            latest_notification=dict(session.latest_notification or {}),
            resident_status=runtime_status,
            team_memory_digest=team_memory_digest,
            artifact_refs=list(task.metadata.get("artifacts", []) or []),
            metadata={
                "message_priority": self._highest_message_priority(session),
                "actionable_inbox_count": session.actionable_inbox_count,
                "protocol_backlog_count": session.protocol_backlog_count,
                "notification_backlog_count": session.notification_backlog_count,
                "delegation_role_session_id": str(session.role_session_id or "").strip(),
                "focused_work_item_id": str(session.focused_work_item_id or "").strip(),
                "work_item_id": str(linked_work_item_id_for_task(task) or session.focused_work_item_id or "").strip(),
                # Fix 5 PR5: task-scoped (see envelope-level note above).
                "team_id": assignment_team_id,
                "seat_id": assignment_seat_id,
                "manager_seat_id": str((session.metadata or {}).get("manager_seat_id", "") or "").strip(),
                "manager_board_summary": dict((session.metadata or {}).get("manager_board_summary", {}) or {}),
                "parent_board_scope": str((session.metadata or {}).get("parent_board_scope", "") or "").strip(),
                "current_turn_mode": str(session.current_turn_mode or "").strip(),
                **team_memory_metadata,
            },
        )
        return asdict(assignment)

    @staticmethod
    def _session_priority_messages(session: CompanyMemberSession) -> list[dict[str, Any]]:
        return [
            *list(session.protocol_backlog or []),
            *list(session.pending_inbox or session.queued_inbox or []),
            *list(session.notification_backlog or []),
        ]

    @staticmethod
    def _latest_notification(messages: list[dict[str, Any]]) -> dict[str, Any]:
        if not messages:
            return {}
        def _timestamp(item: dict[str, Any]) -> str:
            return str(
                item.get("timestamp")
                or item.get("sent_at")
                or dict(item.get("metadata", {}) or {}).get("timestamp")
                or ""
            )
        latest = max(messages, key=_timestamp)
        return dict(latest)

    def _build_current_work_item(
        self,
        session: CompanyMemberSession,
        task: Task | None = None,
    ) -> dict[str, Any]:
        task_work_item_id = ""
        if task is not None:
            task_work_item_id = linked_work_item_id_for_task(task)
        work_item_id = str(
            session.focused_work_item_id
            or dict(session.current_assignment or {}).get("metadata", {}).get("work_item_id", "")
            or task_work_item_id
        ).strip()
        if not work_item_id and not task and not session.current_assignment:
            return {}
        title = ""
        if task is not None:
            title = str(task.title or "").strip()
        if not title:
            title = work_item_projection_id_from_metadata(dict(session.current_assignment or {}), fallback="")
        current_assignment = dict(session.current_assignment or {})
        projection_id = projection_id_for_task(task) if task is not None else work_item_projection_id_from_metadata(current_assignment, fallback="")
        turn_type = turn_type_for_task(task, fallback="") if task is not None else work_item_turn_type_from_metadata(current_assignment, fallback="")
        return {
            "work_item_id": work_item_id,
            "task_id": str(getattr(task, "id", "") or session.current_task_id or "").strip(),
            "title": title,
            "status": self._normalize_member_session_status(session),
            "role_id": str(session.role_id or "").strip(),
            "seat_id": str(session.seat_id or (session.metadata or {}).get("seat_id", "") or "").strip(),
            **work_item_identity_payload(projection_id=projection_id, turn_type=turn_type),
        }

    def _build_pending_decisions(self, session: CompanyMemberSession) -> list[dict[str, Any]]:
        decisions: list[dict[str, Any]] = []
        for item in list(session.protocol_backlog or [])[:8]:
            metadata = dict(item.get("metadata", {}) or {})
            protocol_type = str(item.get("protocol_type") or metadata.get("protocol_type") or "").strip()
            semantic_type = str(item.get("semantic_type") or metadata.get("semantic_type") or "").strip()
            message_class = str(item.get("message_class") or metadata.get("message_class") or "").strip().lower()
            if not protocol_type and (message_class == "protocol" or item.get("reply_needed")):
                protocol_type = "decision_request"
            if not (protocol_type or semantic_type or item.get("reply_needed")):
                continue
            decisions.append(
                {
                    "msg_id": str(item.get("msg_id", "") or "").strip(),
                    "from_agent": str(item.get("from_agent", "") or "").strip(),
                    "subject": str(item.get("subject", "") or "").strip(),
                    "protocol_type": protocol_type or semantic_type,
                    "reply_needed": bool(item.get("reply_needed")),
                }
            )
        return decisions[:4]

    def _blocked_reason(
        self,
        session: CompanyMemberSession,
        task: Task | None = None,
    ) -> str:
        if task is not None:
            peer_wait = dict((task.metadata or {}).get("peer_wait", {}) or {})
            waiting_on = [
                str(item).strip()
                for item in list(
                    peer_wait.get("waiting_on_agents")
                    or peer_wait.get("awaiting_replies_from")
                    or []
                )
                if str(item).strip()
            ]
            if waiting_on:
                return f"Waiting on {', '.join(waiting_on)}"
        for item in [*list(session.protocol_backlog or []), *list(session.notification_backlog or [])]:
            urgency = str(item.get("urgency", "") or "").strip().lower()
            if item.get("reply_needed") or urgency == "blocking":
                return str(item.get("subject", "") or item.get("body", "") or "").strip()
        board_summary = dict((session.metadata or {}).get("manager_board_summary", {}) or {})
        blocked_reasons = [
            str(item).strip()
            for item in list(board_summary.get("blocked_reasons", []) or [])
            if str(item).strip()
        ]
        if blocked_reasons:
            return blocked_reasons[0]
        return ""

    def _build_manager_digest(
        self,
        session: CompanyMemberSession,
        task: Task | None = None,
    ) -> dict[str, Any]:
        latest_notification = dict(session.latest_notification or {})
        last_deliverable_summary = ""
        notification_kind = str(
            latest_notification.get("notification_kind")
            or dict(latest_notification.get("metadata", {}) or {}).get("notification_kind")
            or ""
        ).strip()
        if notification_kind in {"handoff_ready", "delivery_candidate", "idle", "completion", "status_digest", "task_complete"}:
            last_deliverable_summary = str(
                latest_notification.get("summary")
                or latest_notification.get("body")
                or latest_notification.get("subject")
                or ""
            ).strip()
        if not last_deliverable_summary:
            for item in list(session.notification_backlog or []):
                item_kind = str(
                    item.get("notification_kind")
                    or dict(item.get("metadata", {}) or {}).get("notification_kind")
                    or ""
                ).strip()
                if item_kind in {"handoff_ready", "delivery_candidate", "idle", "completion", "status_digest", "task_complete"}:
                    last_deliverable_summary = str(
                        item.get("summary")
                        or item.get("body")
                        or item.get("subject")
                        or ""
                    ).strip()
                    if last_deliverable_summary:
                        break
        if not last_deliverable_summary and session.notification_backlog:
            first_notification = dict(session.notification_backlog[0] or {})
            last_deliverable_summary = str(
                first_notification.get("summary")
                or first_notification.get("body")
                or first_notification.get("subject")
                or ""
            ).strip()
        if not last_deliverable_summary and session.working_memory:
            last_deliverable_summary = str(session.working_memory[-1] or "").strip()
        return {
            "actionable_chat": [dict(item) for item in list(session.actionable_chat or [])[:6]],
            "protocol_backlog": [dict(item) for item in list(session.protocol_backlog or [])[:6]],
            "notification_backlog": [dict(item) for item in list(session.notification_backlog or [])[:6]],
            "latest_notification": latest_notification,
            "manager_board_summary": dict((session.metadata or {}).get("manager_board_summary", {}) or {}),
            "parent_board_scope": str((session.metadata or {}).get("parent_board_scope", "") or "").strip(),
            "current_turn_mode": str(session.current_turn_mode or "").strip(),
            "resident_status": self._normalize_member_session_status(session),
            "current_work_item": self._build_current_work_item(session, task),
            "last_deliverable_summary": last_deliverable_summary,
            "blocked_reason": self._blocked_reason(session, task),
            "pending_decisions": self._build_pending_decisions(session),
        }

    async def _persist_seat_state(
        self,
        session: CompanyMemberSession,
        *,
        task: Task | None = None,
    ) -> None:
        if not (self.store and bool(getattr(self.store, "is_ready", False))):
            return
        save_seat_state = getattr(self.store, "save_seat_state", None) or getattr(self.store, "save_delegation_seat_state", None)
        if not callable(save_seat_state):
            return
        role_session = self._role_session_for_member_session(session)
        seat_state = None
        get_seat_state = getattr(self.store, "get_seat_state", None) or getattr(self.store, "get_delegation_seat_state", None)
        if session.seat_state_id and callable(get_seat_state):
            seat_state = await get_seat_state(session.seat_state_id)
        if seat_state is None and session.seat_id:
            list_seat_states = getattr(self.store, "list_seat_states", None) or getattr(self.store, "list_delegation_seat_states", None)
            if callable(list_seat_states):
                run_id = str(
                    getattr(task, "metadata", {}).get("delegation_run_id", "") if task is not None else ""
                    or getattr(role_session, "run_id", "") or ""
                ).strip()
                if run_id:
                    candidates = await list_seat_states(run_id=run_id, seat_id=session.seat_id)
                    seat_state = candidates[0] if candidates else None
        if seat_state is None:
            return
        seat_state.member_session_id = session.member_session_id
        seat_state.role_runtime_session_id = str(session.role_session_id or seat_state.role_runtime_session_id or "").strip()
        session_status = self._normalize_member_session_status(session)
        active_work_item_id = str(session.focused_work_item_id or "").strip()
        seat_state.status = session_status
        seat_state.resident_status = session_status
        seat_state.current_task_id = (
            str(session.current_task_id or getattr(task, "id", "") or "").strip()
            if session_status != "idle"
            else ""
        )
        seat_state.current_work_item_id = (
            active_work_item_id
            if session_status != "idle"
            else ""
        )
        seat_state.inbox_state = dict(session.inbox_state or {})
        seat_state.resume_state = dict(session.resume_state or {})
        seat_state.current_work_item = dict(session.current_work_item or self._build_current_work_item(session, task))
        seat_state.latest_notification = dict(session.latest_notification or {})
        seat_state.manager_digest = dict(session.manager_digest or self._build_manager_digest(session, task))
        seat_state.metadata = {
            **dict(seat_state.metadata or {}),
            "manager_digest": dict(seat_state.manager_digest),
            "current_turn_mode": str(session.current_turn_mode or "").strip(),
        }
        seat_state.updated_at = datetime.now()
        await save_seat_state(seat_state)

    def _serialize_session(self, session: CompanyMemberSession) -> dict[str, Any]:
        self._normalize_member_session_status(session)
        payload = asdict(session)
        payload["created_at"] = session.created_at.isoformat()
        payload["updated_at"] = session.updated_at.isoformat()
        # Fix 5 PR7: expose pending queue depth + ids on the member
        # session payload so the kanban / UI can show "CTO has 2 queued
        # tasks" without a separate round-trip. Drawn from the linked
        # role_session (CompanyMemberSession itself doesn't hold the queue
        # — that state lives on RoleRuntimeSession per PR3).
        pending_ids: list[str] = []
        role_session_id = str(session.role_session_id or "").strip()
        if role_session_id:
            role_session = self.role_sessions.get(role_session_id)
            if role_session is not None:
                pending_ids = list(
                    getattr(role_session, "pending_work_item_ids", []) or []
                )
        payload["pending_work_item_ids"] = pending_ids
        payload["pending_queue_depth"] = len(pending_ids)
        return payload

    @staticmethod
    def _serialize_role_session(session: DelegationRoleSession) -> dict[str, Any]:
        session.status = normalize_role_runtime_status(
            session.status,
            session.focused_work_item_id,
        )
        if session.status == "idle":
            session.focused_work_item_id = ""
        payload = asdict(session)
        payload["created_at"] = session.created_at.isoformat()
        payload["updated_at"] = session.updated_at.isoformat()
        return payload

    @staticmethod
    def _task_scope_ids(tasks: list[Task]) -> list[str]:
        scope: list[str] = []
        seen: set[str] = set()
        for task in tasks:
            for candidate in [str(task.id).strip(), *[str(item).strip() for item in list(task.metadata.get("execution_task_ids", []) or [])]]:
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    scope.append(candidate)
        return scope

    @staticmethod
    def _task_summary(task: Task, result: TaskResult | None) -> str:
        summary = str(task.metadata.get("work_item_summary", "") or "").strip()
        if summary:
            return summary
        if result is not None and result.content:
            return str(result.content).strip()
        if isinstance(task.result, dict) and task.result.get("content"):
            return str(task.result.get("content", "")).strip()
        return ""

    @staticmethod
    def _runtime_state_from_result(result: TaskResult) -> dict[str, Any]:
        artifacts = dict(result.artifacts or {})
        runtime_session_id = str(artifacts.get("runtime_session_id", "") or "").strip()
        if not runtime_session_id:
            return {}
        return {
            "runtime_session_id": runtime_session_id,
            "resume_cursor": artifacts.get("resume_cursor"),
            "active_subagents": list(artifacts.get("active_subagents", []) or []),
            "permission_requests": list(artifacts.get("permission_requests", []) or []),
            "compaction_boundaries": list(artifacts.get("compaction_boundaries", []) or []),
            "compaction_records": list(artifacts.get("compaction_records", artifacts.get("compaction_boundaries", [])) or []),
            "task_ledger": list(artifacts.get("task_ledger", []) or []),
            "prefetch_hits": list(artifacts.get("prefetch_hits", []) or []),
            "verification": dict(artifacts.get("verification", {}) or {}),
            "verification_evidence": dict(artifacts.get("verification_evidence", {}) or {}),
            "verification_verdict": str(artifacts.get("verification_verdict", "") or "").strip(),
            "artifact_manifest": list(artifacts.get("artifact_manifest", []) or []),
            "resume_state": dict(artifacts.get("resume_state", {}) or {}),
            "worktree_path": str(artifacts.get("worktree_path", "") or "").strip(),
        }

    @staticmethod
    def _adapter_state_from_result(task: Task, result: TaskResult | None) -> dict[str, Any]:
        if result is None:
            return {}
        artifacts = dict(result.artifacts or {})
        state: dict[str, Any] = {}
        session_scope_id = task_session_scope_id(task)
        if session_scope_id:
            state["session_scope_id"] = session_scope_id
        selected_execution_agent = str(
            task.metadata.get("selected_execution_agent", "")
            or task.assigned_external_agent
            or ""
        ).strip()
        if selected_execution_agent:
            state["selected_execution_agent"] = selected_execution_agent
        resume_agent_type = str(
            artifacts.get("agent")
            or artifacts.get("resume_agent_type")
            or task.metadata.get("external_resume_agent_type", "")
            or ""
        ).strip()
        external_resume_session_id = str(
            task.metadata.get("external_resume_session_id", "")
            or artifacts.get("resume_session_id", "")
            or artifacts.get("provider_session_id", "")
            or artifacts.get("resume_session_token", "")
            or ""
        ).strip()
        if (
            external_resume_session_id
            and result.status == TaskStatus.DONE
            and resume_agent_type
        ):
            state["external_resume_session_id"] = external_resume_session_id
            state["external_resume_agent_type"] = resume_agent_type
            state[resume_agent_type] = {
                "resume_session_id": str(artifacts.get("resume_session_id", "") or external_resume_session_id).strip(),
                "provider_session_id": str(artifacts.get("provider_session_id", "") or "").strip(),
                "updated_at": datetime.now().isoformat(),
                "last_task_id": str(task.id or "").strip(),
                "last_project_id": str(task.project_id or "").strip(),
            }
            if session_scope_id:
                state["external_resume_session_scope_id"] = session_scope_id
        resume_source_session = str(artifacts.get("resume_source_session", "") or "").strip()
        if resume_source_session:
            state["resume_source_session"] = resume_source_session
        latest_external_status = str(artifacts.get("status", "") or "").strip()
        if latest_external_status:
            state["latest_external_status"] = latest_external_status
        return state
