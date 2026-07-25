"""WebSocket Handler — routes all messages between frontend and OPC.

Routes inbound UI messages and outbound event envelopes.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, TYPE_CHECKING

from loguru import logger
from opc.core.active_task_runs import (
    ActiveTaskRunAdmissionClosed,
    ActiveTaskRunRegistry,
)
from opc.core.config import (
    OPCConfig,
    get_project_workplace,
    slugify_organization_name,
    validate_organization_id,
)
from opc.core.org_config import (
    allocate_org_config_id,
    apply_org_config_payload_to_config,
    build_org_config_payload_from_config,
    list_org_config_paths,
    load_org_config_payload,
    org_config_filename,
    org_config_path,
    org_config_relative_path,
    org_configs_dir,
    read_org_index,
    validate_runnable_org_config,
    validate_saved_org_id,
    write_org_config_payload,
    write_org_index,
)
from opc.core.models import normalize_role_runtime_status
from opc.core.transcript_visibility import rendered_transcript_metadata_visible
from opc.presentation.kanban import build_company_board_columns
from opc.layer2_organization.phase import (
    kanban_column,
    should_hide_work_item_from_company_kanban,
)
from opc.layer2_organization.company_runtime_identity import (
    ACTIVE_COMPANY_RUNTIME_CHECKPOINT_STATUSES,
    COMPANY_RUNTIME_CHECKPOINT_TYPES,
    is_company_runtime_task,
    load_company_runtime_identity_index,
)
from opc.layer2_organization.work_item_identity import (
    work_item_identity_payload,
    work_item_identity_payload_for_task,
    work_item_projection_id_from_metadata,
    work_item_turn_type_from_metadata,
)
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task
from opc.layer2_organization.work_item_transition import (
    apply_task_status_transition,
)
from opc.layer2_organization.org_work_item_planner import build_custom_org_work_item_blueprint
from opc.layer4_tools.output_budget import clip_text

if TYPE_CHECKING:
    import aiohttp.web
    from opc.engine import OPCEngine
    from opc.plugins.office_ui.agent_store import AgentStore
    from opc.plugins.office_ui.chat_store import ChatStore
    from opc.plugins.office_ui.event_adapter import EventAdapter

from opc.plugins.office_ui.dispatcher import Dispatcher
from opc.plugins.office_ui.services import (
    ModeState,
    OfficeServiceContext,
    OfficeServices,
    ServiceError,
    ServiceResult,
    SessionService,
)
from opc.plugins.office_ui.snapshot_builder import (
    STATUS_TO_COLUMN,
    _build_company_runtime_control_by_task,
    collapse_adjacent_transcript_duplicates,
    _build_session_context_preview,
    _extract_markdown_text,
    _sanitize_ui_message_dict,
    _normalize_transcript_detail_level,
    _task_parent_session_link,
    build_collab_sync,
    build_project_index_sync,
    build_transcript_ui_messages,
    build_snapshot,
)
from opc.plugins.office_ui.org_architecture_snapshot import (
    apply_org_architecture_snapshot,
    build_org_architecture_snapshot,
    dump_org_architecture_snapshot,
    parse_org_architecture_snapshot,
)


def _add_execution_turn_aliases(
    payload: dict[str, Any],
    runtime_task_id: Any | None = None,
) -> dict[str, Any]:
    """Add canonical UI aliases for runtime Task / execution turn identity."""
    task_id = str(
        runtime_task_id
        or payload.get("runtime_task_id")
        or payload.get("execution_turn_id")
        or payload.get("task_id")
        or ""
    ).strip()
    if task_id:
        payload.setdefault("runtime_task_id", task_id)
        payload.setdefault("execution_turn_id", task_id)
    return payload


def _is_cjk_title_char(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x3400 <= cp <= 0x4DBF
        or 0x4E00 <= cp <= 0x9FFF
        or 0xF900 <= cp <= 0xFAFF
        or 0x3040 <= cp <= 0x30FF
        or 0xAC00 <= cp <= 0xD7AF
    )


def _compact_session_title(content: str, *, max_units: int = 10, fallback: str = "New Chat") -> str:
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    if not text:
        return fallback
    if max_units <= 0:
        return fallback

    units = 0
    index = 0
    cut_index = len(text)
    while index < len(text):
        ch = text[index]
        if ch.isspace():
            index += 1
            continue
        if _is_cjk_title_char(ch):
            units += 1
            index += 1
            if units == max_units:
                cut_index = index
                break
            continue
        if (ch.isalnum() or ch == "_") and not _is_cjk_title_char(ch):
            while (
                index < len(text)
                and (text[index].isalnum() or text[index] == "_")
                and not _is_cjk_title_char(text[index])
            ):
                index += 1
            units += 1
            if units == max_units:
                cut_index = index
                break
            continue
        index += 1

    compact = text[:cut_index].strip() or fallback
    has_more_units = any(
        _is_cjk_title_char(ch) or ((ch.isalnum() or ch == "_") and not _is_cjk_title_char(ch))
        for ch in text[cut_index:]
    )
    return f"{compact}..." if has_more_units else compact


_TASK_MODE_HIDDEN_RUNTIME_PROGRESS_TYPES: frozenset[str] = frozenset({
    "message_start",
    "message_stop",
    "tool_call_delta",
    "status_snapshot",
    "context_usage",
    "cost_update",
    "task_ledger_updated",
    "prompt_prefix_state",
    "prompt_prefix_cache_fingerprint",
    "prefetch_started",
    "prefetch_completed",
    "prefetch_consumed",
    "durable_memory_extracted",
    "durable_memory_extraction_failed",
    "tool_hook",
    "turn_started",
    "turn_completed",
    "member_idle",
})


_TASK_MODE_DEBUG_ONLY_PROGRESS_TYPES: frozenset[str] = frozenset({
    "compaction_applied",
})


# Company mode shares the task-mode noise list; runtime bookkeeping events
# (turns, status snapshots, cost ticks) carry no reviewable content and drown
# out thinking/tool entries in the per-role activity feed.
_COMPANY_MODE_HIDDEN_RUNTIME_PROGRESS_TYPES: frozenset[str] = (
    _TASK_MODE_HIDDEN_RUNTIME_PROGRESS_TYPES | frozenset({"member_inbox_updated"})
)


# These handlers can hand an accepted UI request to engine execution.  Reserve
# at the router boundary (before their first await), because the persisted task
# mode may not be known until the handler loads the task from the project store.
_EXECUTION_HANDOFF_MESSAGE_TYPES: frozenset[str] = frozenset({
    "run_task",
    "session_send",
    "session_resume",
})


_TASK_MODE_VISIBLE_RUNTIME_PROGRESS_TYPES: frozenset[str] = frozenset({
    "thinking_delta",
    "tool_started",
    "tool_progress",
    "tool_completed",
    "permission_requested",
    "permission_resolved",
    "checkpoint_saved",
    "turn_failed",
})


def _normalize_escalation_key(value: str) -> str:
    return re.sub(r"[\s\-]+", "_", value.strip()).strip("_").casefold()


def _normalize_escalation_reply(reply: str, options: list[dict[str, Any]]) -> str | None:
    raw_reply = str(reply or "").strip()
    if not raw_reply:
        return None

    normalized_map: dict[str, str] = {}
    for idx, option in enumerate(options, start=1):
        option_id = str(option.get("id", "")).strip()
        label = str(option.get("label", option_id)).strip()
        if not option_id:
            continue
        normalized_map[option_id.casefold()] = option_id
        normalized_map[_normalize_escalation_key(option_id)] = option_id
        if label:
            normalized_map[label.casefold()] = option_id
            normalized_map[_normalize_escalation_key(label)] = option_id
        normalized_map[str(idx)] = option_id

    alias_map = {
        "y": "approve_once",
        "yes": "approve_once",
        "approve": "approve_once",
        "allow": "approve_once",
        "session": "approve_session",
        "n": "deny",
        "no": "deny",
        "deny": "deny",
        "reject": "deny",
        "project": "always_project",
        "global": "always_global",
    }
    alias = alias_map.get(_normalize_escalation_key(raw_reply))
    if alias and alias in normalized_map.values():
        return alias
    return normalized_map.get(raw_reply.casefold()) or normalized_map.get(_normalize_escalation_key(raw_reply))


def _ui_message_identity_metadata(
    *,
    kind: str | None = None,
    message_id: str | None = None,
    conversation_turn_id: str | None = None,
    created_at: float | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if kind:
        metadata["kind"] = kind
    normalized_id = str(message_id or "").strip()
    if normalized_id:
        metadata["ui_message_id"] = normalized_id
    normalized_turn_id = str(conversation_turn_id or "").strip()
    if normalized_turn_id:
        metadata["conversation_turn_id"] = normalized_turn_id
        metadata["canonical_turn_id"] = normalized_turn_id
        metadata["turn_id"] = normalized_turn_id
    if created_at is not None:
        metadata["ui_created_at"] = float(created_at)
    return metadata


def _ui_conversation_turn_id(message_id: str | None) -> str:
    normalized_id = str(message_id or "").strip()
    if not normalized_id:
        return ""
    return f"ui-turn:{normalized_id}"


_GENERIC_ESCALATION_OPTIONS: list[dict[str, str]] = [
    {"id": "approve_once", "label": "Approve once"},
    {"id": "approve_session", "label": "Allow for this session"},
    {"id": "deny", "label": "Deny"},
    {"id": "always_project", "label": "Always allow for this project"},
    {"id": "always_global", "label": "Always allow globally"},
    {"id": "proceed", "label": "Proceed"},
    {"id": "abort", "label": "Abort"},
]


def _looks_like_escalation_reply(content: str) -> bool:
    return _normalize_escalation_reply(content, _GENERIC_ESCALATION_OPTIONS) is not None


_TASK_MODE_PREFERRED_AGENTS = frozenset({
    "native",
    "codex",
    "claude_code",
    "cursor",
    "opencode",
})

_PERSISTED_WORKER_NOTIFICATION_KINDS = frozenset({
    "idle",
    "task_complete",
    "blocked",
    "handoff_ready",
    "error",
    "permission_needed",
})

_RUNTIME_TASK_VISIBILITY_EVENT_TYPES = frozenset({
    "member_session_started",
    "member_claimed_work_item",
    "member_idle",
    "member_inbox_updated",
    "worker_notification",
})

_PROJECT_SCOPED_ENVELOPE_TYPES = frozenset({
    "snapshot",
    "event",
    "channel_created",
    "board_task_created",
    "board_task_moved",
    "board_task_status_changed",
    "session_runtime_control",
    "chat_new_message",
    "chat_channel_created",
    "kanban_updated",
    "kanban_board_created",
    "agent_runtime_update",
    "worker_notification",
    "execution_mode_resolved",
    "collab_sync_push",
    "project_index_push",
    "kanban_view_data",
    "session_created",
    "session_updated",
    "session_message",
    "session_title_updated",
    "session_deleted",
    "child_session_created",
    "session_progress",
    "work_item_progress",
    "project_run_updated",
    "seat_digest_updated",
    "work_item_batch_updated",
    "project_recovery_updated",
    "project_revision_created",
    "comms_state",
    "comms_message",
    "comms_state_dirty",
    "runtime_status_sync",
})


# ── Saved org architectures helpers ──────────────────────────────
# A saved org is a complete user organization config under
# .opc/config/company_orgs/org_<organization_id>_config.yaml.
_SAVED_ORG_NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
_ACTIVE_SAVED_ORG_STATE_KEY = "active_saved_org"
_SAVED_ORG_NAME_LAX_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ProjectScopeError(ValueError):
    """Raised when a project-scoped WS request is missing its explicit scope."""


def _saved_orgs_dir() -> Path:
    from opc.core.config import get_opc_home
    return org_configs_dir(get_opc_home() / "config")


def _saved_org_path(name: str, *, strict: bool = True) -> Path:
    raw = str(name or "").strip()
    pattern = _SAVED_ORG_NAME_RE if strict else _SAVED_ORG_NAME_LAX_RE
    if pattern.match(raw):
        org_id = raw.lower()
    elif not strict and raw:
        org_id = slugify_organization_name(raw)
    else:
        raise ValueError(f"Invalid saved-org name: {name!r}")
    return _saved_orgs_dir() / org_config_filename(org_id)


class WSHandler:
    """Routes all WebSocket messages between frontend and OPC."""

    def __init__(
        self,
        engine: OPCEngine,
        agent_store: AgentStore,
        chat_store: ChatStore,
        event_adapter: EventAdapter,
    ) -> None:
        self.engine = engine
        self._root_engine = engine
        self._active_project_id = str(engine.project_id or "default").strip() or "default"
        self._project_switch_lock = asyncio.Lock()
        self.agent_store = agent_store
        self.chat_store = chat_store
        self.event_adapter = event_adapter
        self._clients: set[aiohttp.web.WebSocketResponse] = set()
        self._client_project_ids: dict[Any, str] = {}
        self._client_switch_seq: dict[Any, str] = {}
        self._client_project_index_tasks: dict[Any, asyncio.Task[Any]] = {}
        self._client_initial_state_tasks: dict[Any, asyncio.Task[Any]] = {}
        self._exec_mode: str = "task"  # restored from DB in restore_persisted_mode()
        self._company_profile: str = "corporate"
        self._task_preferred_agent: str = "native"
        self._local_talent_cache: list[Any] | None = None  # invalidated on import/hire
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._task_bg_map: dict[str, set[asyncio.Task[Any]]] = {}
        self._task_bg_context: dict[asyncio.Task[Any], dict[str, Any]] = {}
        self._handoff_route_tasks: dict[asyncio.Task[Any], str] = {}
        self._broadcast_seq: int = 0
        self._broadcast_lock = asyncio.Lock()
        self._task_locks: dict[str, asyncio.Lock] = {}
        # Tracks which asyncio.Task currently holds the per-task lock. If the
        # prior holder finishes/crashes without reaching the finally block that
        # pops the lock, the next acquirer would block forever. We detect that
        # case by checking ``holder.done()`` in ``_get_task_lock`` and replace
        # the stale lock before the new acquirer blocks.
        self._task_lock_holders: dict[str, asyncio.Task[Any]] = {}
        self._config_lock = asyncio.Lock()
        self._active_runtime_children: dict[str, str] = {}
        self._secretary_session_id: str | None = None
        self._secretary_session_ids: dict[str, str] = {}
        self._session_to_task: dict[str, str] = {}
        self._ui_task_aliases: dict[str, str] = {}
        self._pending_escalations: dict[str, dict[str, Any]] = {}
        self._pending_escalation_order: list[str] = []
        self._progress_buffer: dict[str, list[dict[str, Any]]] = {}
        self._progress_project_ids: dict[str, str] = {}
        self._runtime_status_sync_task: asyncio.Task[Any] | None = None
        self._runtime_status_sync_prev: dict[str, str] = {}
        self._assistant_delta_buffers: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._assistant_delta_flush_tasks: dict[tuple[str, str, str], asyncio.Task[None]] = {}
        self._assistant_delta_seq: int = 0
        self._ASSISTANT_DELTA_FLUSH_INTERVAL_SEC = 0.05
        self._stop_requested_task_ids: set[str] = set()
        self._company_stop_intents: dict[str, dict[str, Any]] = {}
        self._company_stop_finalize_tasks: dict[str, asyncio.Task[Any]] = {}
        self._company_suspend_reply_locks: dict[str, asyncio.Lock] = {}
        self._company_delivery_feedback_reply_locks: dict[str, asyncio.Lock] = {}
        # Buffer progress entries before UPSERTing to SQLite. Raised from 1
        # so bursts (codex streaming thinking chunks, multi-line tool output)
        # don't hammer the DB once per entry, but kept small enough that a
        # task sitting idle between work items still shows up on the Activity
        # panel without waiting for 10 entries to accumulate. A periodic
        # flush in ``_periodic_flush_loop`` catches tasks that emit sparsely
        # so at most ``_PROGRESS_FLUSH_INTERVAL_SEC`` of entries are held
        # in RAM before they're persisted and visible on page refresh.
        self._PROGRESS_FLUSH_THRESHOLD = 2
        self._PROGRESS_FLUSH_INTERVAL_SEC = 3.0
        self._RUNTIME_STATUS_SYNC_INTERVAL_SEC = 12.0
        self._progress_flush_task: asyncio.Task[None] | None = None
        self._shutting_down: bool = False
        self._active_message_tasks: set[asyncio.Task[Any]] = set()
        self.dispatcher = Dispatcher(engine, chat_store)
        self.services_context = OfficeServiceContext(
            engine=engine,
            agent_store=agent_store,
            chat_store=chat_store,
            event_adapter=event_adapter,
            mode_state=ModeState(
                exec_mode=self._exec_mode,
                company_profile=self._company_profile,
                task_preferred_agent=self._task_preferred_agent,
            ),
        )
        self.services_context.config_lock = self._config_lock
        self.services_context.background_tasks = self._background_tasks
        self.services_context.task_bg_map = self._task_bg_map
        self.services_context.task_bg_context = self._task_bg_context
        self.services_context.session_to_task = self._session_to_task
        self.services_context.active_runtime_children = self._active_runtime_children
        self.services_context.stop_requested_task_ids = self._stop_requested_task_ids
        self.services_context.task_locks = self._task_locks
        self.services_context.task_lock_holders = self._task_lock_holders
        self.services_context.wire_engine_callbacks = self._wire_engine_callbacks  # type: ignore[attr-defined]
        self.services_context.load_active_org_config = lambda org_id: self._load_active_org_config_into_engine(org_id)
        self.services_context.set_active_saved_org_name = self._service_set_active_saved_org_name
        self.services_context.get_active_saved_org_name = self._service_get_active_saved_org_name
        self.services_context.project_workplace_hook = lambda project_id: get_project_workplace(project_id)  # type: ignore[attr-defined]
        self.services_context.on_engine_activated = self._on_service_engine_activated
        self.services_context.persist_runtime_config = self._persist_runtime_config
        self.services_context.rebind_engine_config = self._rebind_engine_config
        self.services_context.sync_role_map = self._sync_role_map
        self.services_context.ensure_custom_role_agents = self._ensure_custom_role_agents
        self.services_context.broadcast_snapshot = self._broadcast_snapshot
        self.services_context.cancel_session_tasks = self._cancel_session_tasks
        self.services_context.cancel_task_tree = self._cancel_task_tree
        self.services = OfficeServices(self.services_context)
        self._wire_engine_callbacks(engine)

    def _on_service_engine_activated(self, engine: Any, project_id: str) -> None:
        self.engine = engine
        self.dispatcher = Dispatcher(engine, self.chat_store)
        self._active_project_id = self._normalize_project_id(project_id)
        self._refresh_engine_attachment_store()

    def _ensure_office_services(self) -> OfficeServices:
        """Create service wiring for tests that instantiate WSHandler via __new__."""
        if hasattr(self, "services") and hasattr(self, "services_context"):
            self.services_context.mode_state.exec_mode = getattr(self, "_exec_mode", "task")
            self.services_context.mode_state.company_profile = getattr(self, "_company_profile", "corporate")
            self.services_context.mode_state.task_preferred_agent = getattr(self, "_task_preferred_agent", "native")
            return self.services
        context = OfficeServiceContext(
            engine=self.engine,
            agent_store=getattr(self, "agent_store", None),
            chat_store=getattr(self, "chat_store", None),
            event_adapter=getattr(self, "event_adapter", None),
            mode_state=ModeState(
                exec_mode=getattr(self, "_exec_mode", "task"),
                company_profile=getattr(self, "_company_profile", "corporate"),
                task_preferred_agent=getattr(self, "_task_preferred_agent", "native"),
            ),
        )
        if hasattr(self, "_config_lock"):
            context.config_lock = self._config_lock
        for attr in ("_background_tasks", "_task_bg_map", "_task_bg_context", "_session_to_task", "_task_locks", "_task_lock_holders"):
            if hasattr(self, attr):
                setattr(context, attr.removeprefix("_"), getattr(self, attr))
        if hasattr(self, "_wire_engine_callbacks"):
            context.wire_engine_callbacks = self._wire_engine_callbacks  # type: ignore[attr-defined]
        if hasattr(self, "_load_active_org_config_into_engine"):
            context.load_active_org_config = lambda org_id: self._load_active_org_config_into_engine(org_id)
        if hasattr(self, "_service_set_active_saved_org_name"):
            context.set_active_saved_org_name = self._service_set_active_saved_org_name
        if hasattr(self, "_service_get_active_saved_org_name"):
            context.get_active_saved_org_name = self._service_get_active_saved_org_name
        if hasattr(self, "_on_service_engine_activated"):
            context.on_engine_activated = self._on_service_engine_activated
        if hasattr(self, "_persist_runtime_config"):
            context.persist_runtime_config = self._persist_runtime_config
        if hasattr(self, "_rebind_engine_config"):
            context.rebind_engine_config = self._rebind_engine_config
        if hasattr(self, "_sync_role_map"):
            context.sync_role_map = self._sync_role_map
        if hasattr(self, "_ensure_custom_role_agents"):
            context.ensure_custom_role_agents = self._ensure_custom_role_agents
        if hasattr(self, "_broadcast_snapshot"):
            context.broadcast_snapshot = self._broadcast_snapshot
        if hasattr(self, "_cancel_session_tasks"):
            context.cancel_session_tasks = self._cancel_session_tasks
        if hasattr(self, "_cancel_task_tree"):
            context.cancel_task_tree = self._cancel_task_tree
        self.services_context = context
        self.services = OfficeServices(context)
        return self.services

    async def _service_set_active_saved_org_name(self, org_id: str) -> None:
        await self._set_active_saved_org_name(org_id)

    async def _service_get_active_saved_org_name(self) -> str:
        return await self._get_active_saved_org_name()

    @staticmethod
    def _normalize_project_id(project_id: str | None) -> str:
        return str(project_id or "default").strip() or "default"

    def _active_engine_project_id(self) -> str:
        return self._normalize_project_id(getattr(self.engine, "project_id", None) or self._active_project_id)

    def _client_active_project_id(self, ws: Any | None = None) -> str:
        if ws is not None:
            project_id = str(self._client_project_ids.get(ws, "") or "").strip()
            if project_id:
                return self._normalize_project_id(project_id)
        return self._active_engine_project_id()

    def _request_project_id(self, data: dict[str, Any] | None) -> str:
        raw = (data or {}).get("project_id") or (data or {}).get("projectId")
        project_id = str(raw or "").strip()
        if not project_id:
            raise ProjectScopeError("project_id required for project-scoped request")
        return self._normalize_project_id(project_id)

    @staticmethod
    def _payload_project_id(payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        for key in ("project_id", "projectId", "active_project_id", "activeProjectId"):
            value = payload.get(key)
            normalized = str(value or "").strip()
            if normalized:
                return normalized
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("project_id", "projectId"):
                value = data.get(key)
                normalized = str(value or "").strip()
                if normalized:
                    return normalized
        return ""

    async def _engine_for_request(self, data: dict[str, Any] | None) -> tuple[Any, str]:
        project_id = self._request_project_id(data)
        engine = await self._engine_for_project(project_id)
        return engine, project_id

    def _progress_callback_for_engine(self, engine: Any) -> Any:
        async def _progress(text: str, **kw: Any) -> None:
            # UI progress is a best-effort display copy: a failure here (e.g.
            # a locked ui_state.db) must never crash the agent execution that
            # emitted the progress line.
            try:
                await self.on_progress(
                    text,
                    _runtime_engine=engine,
                    _project_id=self._normalize_project_id(getattr(engine, "project_id", None)),
                    **kw,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.opt(exception=True).warning(
                    "UI progress handling failed; agent execution continues"
                )

        return _progress

    def _runtime_event_callback_for_engine(self, engine: Any) -> Any:
        async def _runtime_event(event: Any) -> None:
            try:
                await self.on_opc_event(
                    event,
                    runtime_engine=engine,
                    project_id=self._normalize_project_id(getattr(engine, "project_id", None)),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.opt(exception=True).warning(
                    "UI runtime-event handling failed; agent execution continues"
                )

        setattr(_runtime_event, "_opc_ui_handler_id", id(self))
        setattr(_runtime_event, "_opc_ui_project_id", self._normalize_project_id(getattr(engine, "project_id", None)))
        return _runtime_event

    def _escalation_callback_for_engine(self, engine: Any) -> Any:
        async def _escalation(message: str, options: list[dict]) -> str | None:
            return await self._handle_ui_escalation(
                message,
                options,
                project_id=self._normalize_project_id(getattr(engine, "project_id", None)),
            )

        setattr(_escalation, "_opc_ui_handler_id", id(self))
        setattr(_escalation, "_opc_ui_project_id", self._normalize_project_id(getattr(engine, "project_id", None)))
        return _escalation

    def _kanban_callback_for_engine(self, engine: Any) -> Any:
        async def _kanban_changed() -> None:
            try:
                await self.on_kanban_changed(engine=engine)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.opt(exception=True).warning(
                    "UI kanban refresh failed; agent execution continues"
                )

        return _kanban_changed

    def _wire_engine_callbacks(self, engine: Any) -> None:
        try:
            progress_callback = self._progress_callback_for_engine(engine)
            runtime_event_callback = self._runtime_event_callback_for_engine(engine)
            escalation_callback = self._escalation_callback_for_engine(engine)
            engine.on_company_runtime_children = self._register_company_runtime_children
            engine.on_company_kanban_callback_factory = self._kanban_callback_for_engine
            engine.on_escalation = escalation_callback
            engine.on_progress = progress_callback
            engine.on_runtime_event = runtime_event_callback
            if getattr(engine, "escalation", None):
                engine.escalation.user_reply_callback = escalation_callback
            company_executor = getattr(engine, "company_executor", None)
            if company_executor is not None:
                company_executor.progress_callback = progress_callback
                company_executor.on_kanban_changed = self._kanban_callback_for_engine(engine)
            reorg_manager = getattr(engine, "reorg_manager", None)
            if reorg_manager is not None:
                reorg_manager.progress_callback = progress_callback
            event_bus = getattr(engine, "event_bus", None)
            forward_runtime_event = getattr(engine, "_forward_runtime_event", None)
            if (
                engine is not self._root_engine
                and event_bus is not None
                and callable(getattr(event_bus, "subscribe_all", None))
            ):
                # Delegates need all events, not only runtime_event. They may
                # have inherited a typed runtime forwarder during initialize();
                # remove it so runtime_event is not delivered twice.
                listeners_by_type = getattr(event_bus, "_listeners", {})
                runtime_list = listeners_by_type.get("runtime_event", []) if listeners_by_type is not None else []
                if callable(forward_runtime_event) and isinstance(runtime_list, list):
                    runtime_list[:] = [
                        listener for listener in runtime_list
                        if listener != forward_runtime_event
                    ]
                project_marker = self._normalize_project_id(getattr(engine, "project_id", None))
                global_listeners = getattr(event_bus, "_global_listeners", [])
                already_subscribed = any(
                    getattr(listener, "_opc_ui_handler_id", None) == id(self)
                    and getattr(listener, "_opc_ui_project_id", None) == project_marker
                    for listener in list(global_listeners or [])
                )
                if not already_subscribed:
                    event_bus.subscribe_all(runtime_event_callback)
        except Exception:
            logger.opt(exception=True).debug(
                f"Failed to wire UI callbacks for project engine {getattr(engine, 'project_id', None)!r}",
            )

    @staticmethod
    def _is_real_opc_engine(engine: Any) -> bool:
        try:
            from opc.engine import OPCEngine as _OPCEngine
        except Exception:
            return False
        return isinstance(engine, _OPCEngine)

    async def _engine_for_project(self, project_id: str) -> Any:
        normalized = self._normalize_project_id(project_id)
        root = self._root_engine
        current_root_project = self._normalize_project_id(getattr(root, "project_id", None))
        if normalized == current_root_project:
            engine = root
        else:
            delegate_getter = getattr(root, "_get_project_delegate", None)
            explicit_delegate = "_get_project_delegate" in getattr(root, "__dict__", {})
            if not callable(delegate_getter) or not (self._is_real_opc_engine(root) or explicit_delegate):
                raise RuntimeError(
                    "Project switching requires OPCEngine project delegates or an explicit "
                    "_get_project_delegate test double."
                )
            maybe_engine = delegate_getter(normalized)
            engine = await maybe_engine if inspect.isawaitable(maybe_engine) else maybe_engine
        # Self-heal a closed store (project deleted then re-created while this
        # engine instance stayed bound to it — e.g. the root engine, which can
        # never be evicted from its own delegate cache).
        store = getattr(engine, "store", None)
        if store is not None and not getattr(store, "is_ready", True):
            ensure_ready = getattr(store, "ensure_ready", None)
            if callable(ensure_ready):
                logger.warning(f"Reopening closed store for project '{normalized}'")
                await ensure_ready()
        self._wire_engine_callbacks(engine)
        return engine

    async def _activate_project(self, project_id: str) -> Any:
        engine = await self._engine_for_project(project_id)
        self.engine = engine
        self.dispatcher = Dispatcher(engine, self.chat_store)
        self._active_project_id = self._normalize_project_id(getattr(engine, "project_id", None) or project_id)
        self._refresh_engine_attachment_store()
        return engine

    def _refresh_engine_attachment_store(self) -> None:
        """Ensure the engine attachment store matches the active project."""
        ensure_attachment_store = getattr(self.engine, "_ensure_attachment_store", None)
        if not callable(ensure_attachment_store):
            return
        try:
            ensure_attachment_store()
        except Exception as exc:
            logger.warning(f"Failed to refresh attachment store for project {self.engine.project_id!r}: {exc}")

    def _register_company_runtime_children(self, parent_session_id: str, child_task_ids: list[str]) -> None:
        """Called by engine when company mode creates child work-item tasks.

        Maps each child task_id to the primary task_id (the one the user
        initiated) so that ``on_progress`` can dual-route runtime events.
        """
        origin_task_id = self._session_to_task.get(parent_session_id)
        if not origin_task_id:
            # Fallback: try to find by iterating known mappings
            for sid, tid in self._session_to_task.items():
                if sid == parent_session_id or tid == parent_session_id:
                    origin_task_id = tid
                    break
        if not origin_task_id:
            logger.warning(f"Cannot map runtime children: parent_session_id={parent_session_id} not in _session_to_task")
            return
        for child_id in child_task_ids:
            self._active_runtime_children[child_id] = origin_task_id
            # Also register child task_id in _session_to_task for progress routing
            self._session_to_task[child_id] = child_id

    def _get_task_lock(self, task_id: str) -> asyncio.Lock:
        """Get or create a per-task lock for serializing messages within one session.

        Self-heals stale locks: if the previous holder coroutine is ``.done()``
        but the lock was never released (process interrupt, silent cancellation,
        unhandled exception swallowed upstream), replace it with a fresh lock so
        the next acquirer can proceed instead of blocking forever. This is what
        lets Continue / new messages work after a disconnect.
        """
        prev_holder = self._task_lock_holders.get(task_id)
        if prev_holder is not None and prev_holder.done():
            logger.warning(
                f"Replacing stale task lock for {task_id} "
                f"(prior holder done: cancelled={prev_holder.cancelled()}). "
                "Lock was not released via finally — likely disconnect or crash."
            )
            self._task_locks.pop(task_id, None)
            self._task_lock_holders.pop(task_id, None)
        lock = self._task_locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._task_locks[task_id] = lock
        return lock

    def _resolve_agent_for_idle(self, task_id: str, task: Any = None) -> str | None:
        """Resolve agent_id for a task, trying event_adapter map first, then task.assigned_to."""
        agent_id = self.event_adapter._resolve_agent_from_task(task_id)
        if agent_id:
            return agent_id
        # Fallback: resolve from task's assigned_to (opc_role_id → UI agent_id)
        assigned_to = str(getattr(task, "assigned_to", "") or "").strip() if task else ""
        if assigned_to:
            return self.event_adapter._resolve_role_to_agent(assigned_to)
        return None

    @staticmethod
    def _normalize_session_detail_timestamp(value: Any) -> float | None:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        if not numeric:
            return None
        if numeric > 1_000_000_000_000:
            numeric /= 1000.0
        return numeric

    @staticmethod
    def _message_visible_in_detail_level(message: dict[str, Any], detail_level: str) -> bool:
        metadata = dict(message.get("metadata", {}) or {})
        return rendered_transcript_metadata_visible(metadata, detail_level=detail_level)

    @classmethod
    def _filter_ui_messages_for_detail_level(
        cls,
        messages: list[dict[str, Any]],
        detail_level: str,
    ) -> list[dict[str, Any]]:
        return [
            message
            for message in messages
            if cls._message_visible_in_detail_level(message, detail_level)
        ]

    async def _load_session_transcript_page(
        self,
        task: Any,
        *,
        limit: int,
        detail_level: str = "summary",
        before_timestamp: float | None = None,
        before_message_id: str | None = None,
        engine: Any | None = None,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Load a transcript page for the requested detail level."""
        runtime_engine = engine or self.engine
        store = runtime_engine.store
        if not self._store_is_ready(store):
            return [], 0, False

        task_id = str(getattr(task, "id", "") or "").strip()
        session_id = str(getattr(task, "session_id", "") or "").strip()
        if not task_id or not session_id:
            return [], 0, False

        channel_id = f"session:{task_id}"
        page_loader = getattr(store, "get_session_transcript_page", None)
        if callable(page_loader):
            normalized_limit = max(1, min(int(limit), 500))
            normalized_detail_level = _normalize_transcript_detail_level(detail_level)
            # A database-visible transcript row can still disappear when the
            # renderer finds no content, and adjacent result surfaces can
            # collapse to one UI row.  Page raw rows in bounded chunks until
            # we have one *rendered* look-ahead row or exhaust the transcript.
            # This makes has_more describe the UI timeline rather than the SQL
            # row set and prevents an empty raw page from stalling history.
            chunk_limit = normalized_limit
            raw_before_dt = (
                datetime.fromtimestamp(before_timestamp)
                if before_timestamp is not None
                else None
            )
            raw_before_id = before_message_id
            seen_raw_cursors: set[tuple[datetime, str]] = set()
            formatted_messages: list[dict[str, Any]] = []
            total_count = 0
            raw_has_more = False

            while True:
                raw_page = page_loader(
                    session_id,
                    limit=chunk_limit,
                    before_created_at=raw_before_dt,
                    before_message_id=raw_before_id,
                    detail_level=normalized_detail_level,
                )
                page = await raw_page if inspect.isawaitable(raw_page) else raw_page
                transcript_chunk = list((page or {}).get("messages", []) or [])
                total_count = max(
                    total_count,
                    int((page or {}).get("total_count", 0) or 0),
                )
                raw_has_more = bool((page or {}).get("has_more", False))
                if not transcript_chunk:
                    break

                formatted_chunk = build_transcript_ui_messages(
                    transcript_chunk,
                    channel_id=channel_id,
                    task_id=task_id,
                    detail_level=normalized_detail_level,
                )
                formatted_messages = collapse_adjacent_transcript_duplicates([
                    *formatted_chunk,
                    *formatted_messages,
                ])
                if len(formatted_messages) > normalized_limit:
                    return (
                        formatted_messages[-normalized_limit:],
                        max(total_count, len(formatted_messages)),
                        True,
                    )
                if not raw_has_more:
                    break

                oldest_message = transcript_chunk[0].get("message")
                oldest_created_at = getattr(oldest_message, "created_at", None)
                oldest_message_id = str(
                    getattr(oldest_message, "message_id", "") or ""
                ).strip()
                if not isinstance(oldest_created_at, datetime) or not oldest_message_id:
                    break
                next_cursor = (oldest_created_at, oldest_message_id)
                if next_cursor in seen_raw_cursors:
                    break
                seen_raw_cursors.add(next_cursor)
                raw_before_dt, raw_before_id = next_cursor
                # Small client pages should not require one SQL round-trip per
                # empty row. Start at the requested size so duplicate/empty
                # boundaries are exact, then grow only while looking through
                # rows which did not fill the rendered page.
                chunk_limit = min(500, max(chunk_limit + 1, chunk_limit * 2))

            return (
                formatted_messages[-normalized_limit:],
                max(total_count, len(formatted_messages)),
                raw_has_more,
            )

        transcript_loader = getattr(store, "get_session_transcript", None)
        if not callable(transcript_loader):
            return [], 0, False

        raw_transcript = transcript_loader(session_id)
        transcript = list(await raw_transcript if inspect.isawaitable(raw_transcript) else raw_transcript)
        formatted_messages = build_transcript_ui_messages(
            transcript,
            channel_id=channel_id,
            task_id=task_id,
            detail_level=_normalize_transcript_detail_level(detail_level),
        )

        total_count = len(formatted_messages)
        if before_timestamp is None:
            has_more = total_count > limit
            return formatted_messages[-limit:], total_count, has_more

        filtered_messages: list[dict[str, Any]] = []
        normalized_before_id = str(before_message_id or "").strip()
        for message in formatted_messages:
            created_at = float(message.get("timestamp") or 0)
            message_id = str(message.get("message_id", "") or "").strip()
            if created_at < before_timestamp:
                filtered_messages.append(message)
                continue
            if (
                normalized_before_id
                and created_at == before_timestamp
                and message_id < normalized_before_id
            ):
                filtered_messages.append(message)
        has_more = len(filtered_messages) > limit
        return filtered_messages[-limit:], total_count, has_more

    async def _sync_role_map(self) -> None:
        """Sync opc_role_id → agent_id mapping to EventAdapter."""
        role_map = await self.agent_store.get_role_agent_map()
        self.event_adapter.update_role_map(role_map)

    async def restore_persisted_mode(self) -> None:
        """Restore exec_mode, company_profile, and task preferred agent from DB on startup."""
        self._exec_mode = self._normalize_session_exec_mode(
            await self.agent_store.get_server_state("exec_mode", "task")
        )
        self._company_profile = self._normalize_session_company_profile(
            await self.agent_store.get_server_state("company_profile", "corporate")
        )
        if self._exec_mode == "org":
            self._company_profile = "custom"
        self._task_preferred_agent = self._normalize_session_preferred_agent(
            await self.agent_store.get_server_state("task_preferred_agent", "native"),
        )

        # Sync in-memory org architecture to match restored exec_mode.
        # Do not save during startup restore: disk files are the source of truth.
        if self.engine.org_engine:
            async with self._config_lock:
                if self._exec_mode in {"org", "custom"}:
                    self.engine.config.org.company_profile = "custom"
                    self.engine.org_engine.config = self.engine.config
                    self.engine.org_engine.reload_from_config()
                elif self._exec_mode == "company":
                    self._restore_company_config_into_engine(self._company_profile)
                else:
                    self._restore_company_config_into_engine("")
            if self._exec_mode in {"org", "custom"}:
                await self._restore_active_saved_org_if_needed()
        if hasattr(self, "services_context"):
            self.services_context.mode_state.exec_mode = self._exec_mode
            self.services_context.mode_state.company_profile = self._company_profile
            self.services_context.mode_state.task_preferred_agent = self._task_preferred_agent

    async def _persist_mode(self) -> None:
        """Save current exec_mode, company_profile, and task preferred agent to DB."""
        await self.agent_store.set_server_state("exec_mode", self._exec_mode)
        await self.agent_store.set_server_state("company_profile", self._company_profile)
        await self.agent_store.set_server_state("task_preferred_agent", self._task_preferred_agent)

    # ══════════════════════════════════════════════════════════════════════
    # Connection lifecycle
    # ══════════════════════════════════════════════════════════════════════

    async def handle_ws(self, request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        """Handle a WebSocket connection."""
        import aiohttp.web as web
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if bool(getattr(self, "_shutting_down", False)):
            await ws.close()
            return ws
        self._clients.add(ws)
        self._ensure_progress_flush_loop()
        self._ensure_runtime_status_sync_loop()
        logger.info(f"WS client connected ({len(self._clients)} total)")

        try:
            # Ensure role→agent mapping is current
            await self._sync_role_map()

            # Send initial snapshot
            initial_project_id = self._active_engine_project_id()
            self._client_project_ids[ws] = initial_project_id
            snapshot = await build_snapshot(
                self.engine, self.agent_store, self.chat_store, self.event_adapter
            )
            snapshot["project_id"] = initial_project_id
            snapshot["exec_mode"] = self._exec_mode
            snapshot["company_profile"] = self._company_profile
            snapshot["task_preferred_agent"] = self._task_preferred_agent
            if not await self._safe_send_json(ws, {"type": "snapshot", "payload": snapshot}):
                return ws
            self._track_client_initial_state(
                ws,
                self._send_initial_project_state_for_client(
                    ws,
                    self.engine,
                    initial_project_id,
                ),
            )

            # Process messages
            async for msg in ws:
                if msg.type == 1:  # aiohttp.WSMsgType.TEXT
                    await self._route_message(ws, msg.data)
                elif msg.type == 2:  # BINARY
                    pass
                elif msg.type == 8:  # ERROR
                    logger.warning(f"WS error: {ws.exception()}")
        except Exception as e:
            if self._is_expected_shutdown_error(e) or self._is_ws_disconnect_error(e):
                logger.debug(f"WS handler closed during disconnect/shutdown: {type(e).__name__}: {e!r}")
            else:
                logger.error(f"WS handler error: {e}")
        finally:
            self._clients.discard(ws)
            try:
                self._client_project_ids.pop(ws, None)
                self._client_switch_seq.pop(ws, None)
                index_task = self._client_project_index_tasks.pop(ws, None)
                if index_task is not None and not index_task.done():
                    index_task.cancel()
                initial_task = self._client_initial_state_tasks.pop(ws, None)
                if initial_task is not None and not initial_task.done():
                    initial_task.cancel()
            except TypeError:
                pass
            logger.info(f"WS client disconnected ({len(self._clients)} total)")

        return ws

    # ══════════════════════════════════════════════════════════════════════
    # Outbound: OPC → Frontend
    # ══════════════════════════════════════════════════════════════════════

    async def broadcast(self, envelope: dict[str, Any]) -> None:
        """Broadcast an envelope to all connected clients."""
        if not self._clients:
            return
        async with self._broadcast_lock:
            self._broadcast_seq += 1
            prepared = self._prepare_outbound_envelope(envelope)
            if prepared is None:
                return
            envelope = prepared
            envelope["_seq"] = self._broadcast_seq
            data = json.dumps(envelope, default=str)
            envelope_type = str(envelope.get("type", "") or "")
            envelope_project_id = self._payload_project_id(envelope.get("payload"))
            if not envelope_project_id:
                envelope_project_id = str(envelope.get("project_id", "") or "").strip()
            # Snapshot client set to avoid iteration-during-mutation race
            clients = set(self._clients)
            disconnected = set()
            for ws in clients:
                if (
                    envelope_type in _PROJECT_SCOPED_ENVELOPE_TYPES
                    and envelope_project_id
                    and self._client_active_project_id(ws) != self._normalize_project_id(envelope_project_id)
                ):
                    continue
                try:
                    await ws.send_str(data)
                except Exception:
                    disconnected.add(ws)
            self._clients -= disconnected

    def _prepare_outbound_envelope(self, envelope: dict[str, Any]) -> dict[str, Any] | None:
        envelope_type = str(envelope.get("type", "") or "")
        explicit_project_id = str(envelope.get("project_id", "") or "").strip()
        payload = envelope.get("payload")
        if isinstance(payload, dict):
            payload_project_id = self._payload_project_id(payload)
            final_project_id = explicit_project_id or payload_project_id
            if envelope_type in _PROJECT_SCOPED_ENVELOPE_TYPES and not final_project_id:
                logger.warning(
                    "Dropping project-scoped UI envelope without project_id: type={}",
                    envelope_type,
                )
                return None
            if final_project_id and payload_project_id != final_project_id:
                payload = {**payload, "project_id": final_project_id}
                envelope = {**envelope, "payload": payload}
        elif envelope_type in _PROJECT_SCOPED_ENVELOPE_TYPES:
            final_project_id = explicit_project_id
            if not final_project_id:
                logger.warning(
                    "Dropping project-scoped UI envelope without payload/project_id: type={}",
                    envelope_type,
                )
                return None
        if envelope.get("type") in {"session_message", "chat_new_message"}:
            payload = envelope.get("payload")
            if isinstance(payload, dict):
                envelope = {
                    **envelope,
                    "payload": _sanitize_ui_message_dict(payload),
                }
        return envelope

    async def _send_envelope_to_client(self, ws: Any, envelope: dict[str, Any]) -> bool:
        async with self._broadcast_lock:
            self._broadcast_seq += 1
            prepared = self._prepare_outbound_envelope(envelope)
            if prepared is None:
                return False
            prepared["_seq"] = self._broadcast_seq
            return await self._safe_send_json(ws, prepared)

    async def _canonicalize_runtime_visual_event(
        self,
        visual_event: dict[str, Any],
        *,
        engine: Any | None = None,
    ) -> dict[str, Any]:
        payload = dict(visual_event.get("data", {}) or {})
        raw_runtime_task_id = str(payload.get("task_id", "") or "").strip()
        mapped_task_id = await self._ui_task_id_for_runtime_task_id(raw_runtime_task_id, engine=engine)
        if mapped_task_id:
            payload["task_id"] = mapped_task_id
            if raw_runtime_task_id and mapped_task_id != raw_runtime_task_id:
                payload.setdefault("runtime_task_id", raw_runtime_task_id)
        _add_execution_turn_aliases(payload, raw_runtime_task_id or mapped_task_id)
        if not str(payload.get("turn_id", "") or "").strip():
            runtime_session_id = str(payload.get("runtime_session_id", "") or "").strip()
            iteration = payload.get("iteration")
            if runtime_session_id and iteration not in (None, "", [], {}):
                payload["turn_id"] = f"{runtime_session_id}:{iteration}"
        return {
            **visual_event,
            "data": payload,
        }

    @staticmethod
    def _assistant_delta_key(payload: dict[str, Any], *, delta_type: str = "assistant_delta") -> tuple[str, str, str] | None:
        task_id = str(payload.get("task_id", "") or "").strip()
        if not task_id:
            return None
        turn_id = str(
            payload.get("turn_id")
            or payload.get("canonical_turn_id")
            or payload.get("execution_turn_id")
            or payload.get("runtime_task_id")
            or "active"
        ).strip() or "active"
        item_id = str(
            payload.get("item_id")
            or payload.get("stream_id")
            or delta_type
        ).strip() or delta_type
        return task_id, turn_id, item_id

    async def _delayed_flush_assistant_delta(self, key: tuple[str, str, str]) -> None:
        try:
            await asyncio.sleep(self._ASSISTANT_DELTA_FLUSH_INTERVAL_SEC)
            await self._flush_assistant_delta(key)
        finally:
            current = asyncio.current_task()
            if self._assistant_delta_flush_tasks.get(key) is current:
                self._assistant_delta_flush_tasks.pop(key, None)

    async def _flush_assistant_delta(self, key: tuple[str, str, str]) -> None:
        bucket = self._assistant_delta_buffers.pop(key, None)
        pending = self._assistant_delta_flush_tasks.pop(key, None)
        current = asyncio.current_task()
        if pending is not None and pending is not current and not pending.done():
            pending.cancel()
        if not bucket:
            return
        text = str(bucket.get("text", "") or "")
        if not text:
            return
        visual_event = dict(bucket.get("event", {}) or {})
        payload = dict(visual_event.get("data", {}) or {})
        self._assistant_delta_seq += 1
        payload["text"] = text
        payload.setdefault("seq", self._assistant_delta_seq)
        visual_event["data"] = payload
        await self.broadcast({"type": "event", "payload": visual_event})

    async def _flush_assistant_delta_for_payload(self, payload: dict[str, Any]) -> None:
        task_id = str(payload.get("task_id", "") or "").strip()
        if not task_id:
            return
        turn_id = str(
            payload.get("turn_id")
            or payload.get("canonical_turn_id")
            or payload.get("execution_turn_id")
            or payload.get("runtime_task_id")
            or ""
        ).strip()
        keys = [
            key for key in list(self._assistant_delta_buffers)
            if key[0] == task_id and (not turn_id or key[1] == turn_id)
        ]
        for key in keys:
            await self._flush_assistant_delta(key)

    async def _queue_assistant_delta_visual_event(self, visual_event: dict[str, Any]) -> None:
        payload = dict(visual_event.get("data", {}) or {})
        text = str(payload.get("text", "") or "")
        if not text:
            return
        delta_type = str(visual_event.get("type", "") or "assistant_delta").strip() or "assistant_delta"
        key = self._assistant_delta_key(payload, delta_type=delta_type)
        if key is None:
            await self.broadcast({"type": "event", "payload": visual_event})
            return
        bucket = self._assistant_delta_buffers.setdefault(
            key,
            {
                "event": {
                    **visual_event,
                    "data": {
                        **payload,
                        "text": "",
                    },
                },
                "text": "",
            },
        )
        bucket["event"] = {
            **visual_event,
            "data": {
                **payload,
                "text": "",
            },
        }
        bucket["text"] = f"{bucket.get('text', '')}{text}"
        if "\n" in text:
            await self._flush_assistant_delta(key)
            return
        pending = self._assistant_delta_flush_tasks.get(key)
        if pending is None or pending.done():
            self._assistant_delta_flush_tasks[key] = asyncio.create_task(
                self._delayed_flush_assistant_delta(key),
                name=f"assistant-delta-flush:{key[0]}:{key[1]}:{key[2]}",
            )

    async def _broadcast_runtime_visual_event(self, visual_event: dict[str, Any]) -> None:
        payload = dict(visual_event.get("data", {}) or {})
        runtime_type = str(visual_event.get("type", "") or "").strip()
        if runtime_type in {"assistant_delta", "thinking_delta"}:
            await self._queue_assistant_delta_visual_event(visual_event)
            return
        if runtime_type in {"turn_completed", "turn_failed", "checkpoint_saved"}:
            await self._flush_assistant_delta_for_payload(payload)
        await self.broadcast({"type": "event", "payload": visual_event})

    async def on_opc_event(
        self,
        event: Any,
        *,
        runtime_engine: Any | None = None,
        project_id: str | None = None,
    ) -> None:
        """EventBus subscriber. Translates OPC events and broadcasts."""
        engine = runtime_engine or self.engine
        pid = self._normalize_project_id(project_id or getattr(engine, "project_id", None))
        if event.event_type == "runtime_event":
            runtime_payload = dict(event.payload or {})
            runtime_type = str(runtime_payload.get("type", "") or "").strip()
            if runtime_type in _RUNTIME_TASK_VISIBILITY_EVENT_TYPES:
                await self._materialize_runtime_task_visibility(
                    runtime_payload,
                    engine=engine,
                    project_id=pid,
                )

        visual_events = self.event_adapter.translate(event)
        for ve in visual_events:
            ve = dict(ve)
            ve["project_id"] = pid
            ve_data = dict(ve.get("data", {}) or {})
            ve_data.setdefault("project_id", pid)
            ve["data"] = ve_data
            if ve.get("type") == "board_task_status_changed":
                # Kanban status update: broadcast as dedicated message type
                payload = dict(ve.get("data", {}) or {})
                raw_runtime_task_id = str(payload.get("task_id", "") or "").strip()
                mapped_task_id = await self._ui_task_id_for_runtime_task_id(raw_runtime_task_id, engine=engine)
                if mapped_task_id:
                    payload["task_id"] = mapped_task_id
                    if mapped_task_id != raw_runtime_task_id:
                        payload.setdefault("work_item_id", mapped_task_id)
                _add_execution_turn_aliases(payload, raw_runtime_task_id)
                await self.broadcast({
                    "type": "board_task_status_changed",
                    "payload": payload,
                })
            elif ve.get("type") == "execution_mode_resolved":
                await self.broadcast({
                    "type": "execution_mode_resolved",
                    "payload": ve.get("data", {}),
                })
            elif ve.get("type") == "agent_runtime_update":
                payload = dict(ve.get("data", {}) or {})
                raw_runtime_task_id = str(payload.get("task_id", "") or "").strip()
                mapped_task_id = await self._ui_task_id_for_runtime_task_id(raw_runtime_task_id, engine=engine)
                if mapped_task_id:
                    payload["task_id"] = mapped_task_id
                    if mapped_task_id != raw_runtime_task_id:
                        payload.setdefault("work_item_id", mapped_task_id)
                _add_execution_turn_aliases(payload, raw_runtime_task_id)
                await self.broadcast({
                    "type": "agent_runtime_update",
                    "payload": payload,
                })
            elif ve.get("type") == "child_session_created":
                payload = dict(ve.get("data", {}) or {})
                if event.event_type == "child_session_created" and self._store_is_ready(engine.store):
                    task_id = str(payload.get("task_id", "") or "").strip()
                    if task_id:
                        try:
                            created_task = await engine.store.get_task(task_id)
                        except Exception:
                            created_task = None
                        if created_task is not None:
                            payload.setdefault(
                                "selected_execution_agent",
                                self._resolve_task_selected_execution_agent(created_task),
                            )
                            # Enrich with role/employee metadata from task
                            meta = created_task.metadata or {}
                            identity_payload = (
                                {}
                                if self._runtime_payload_is_task_mode(dict(meta or {}))
                                else work_item_identity_payload(
                                    projection_id=str(meta.get("work_item_projection_id", "") or "").strip(),
                                    turn_type=str(meta.get("work_item_turn_type", "") or "").strip(),
                                )
                            )
                            for _key, _value in identity_payload.items():
                                if _key not in payload and _value:
                                    payload[_key] = _value
                            for _key in (
                                "work_item_role_id", "work_item_role_name",
                                "employee_assignment", "origin_task_id",
                            ):
                                if _key not in payload and meta.get(_key):
                                    payload[_key] = meta[_key]
                            role_id = str(
                                payload.get("work_item_role_id")
                                or getattr(created_task, "assigned_to", "")
                                or ""
                            ).strip()
                            if role_id and not str(payload.get("work_item_role_name", "") or "").strip():
                                payload["work_item_role_name"] = self._resolve_work_item_role_name(
                                    role_id,
                                    meta,
                                    engine=engine,
                                )
                    _add_execution_turn_aliases(payload, task_id)
                await self.broadcast({
                    "type": "child_session_created",
                    "payload": payload,
                })
            else:
                if event.event_type == "runtime_event":
                    ve = await self._canonicalize_runtime_visual_event(ve, engine=engine)
                    await self._broadcast_runtime_visual_event(ve)
                else:
                    await self.broadcast({"type": "event", "payload": ve})

        if event.event_type == "runtime_event":
            runtime_payload = dict(event.payload or {})
            if str(runtime_payload.get("type", "") or "").strip() == "worker_notification":
                await self._handle_worker_notification(runtime_payload, engine=engine, project_id=pid)
            await self._handle_runtime_event_progress(event.payload or {}, engine=engine, project_id=pid)

        # Create chat_store channel for child sessions (so messages can render)
        if event.event_type == "child_session_created":
            p = event.payload or {}
            task_id = p.get("task_id", "")
            title = p.get("title", "Sub-task")
            if task_id:
                channel_id = f"session:{task_id}"
                exec_mode = self._normalize_session_exec_mode(self._exec_mode)
                company_profile = self._normalize_session_company_profile(self._company_profile)
                org_id = ""
                preferred_agent = self._task_preferred_agent
                parent_session_id = str(p.get("parent_session_id") or "").strip()
                parent_task_id = (
                    str(self._session_to_task.get(parent_session_id) or "").strip()
                    or parent_session_id
                )
                if parent_task_id and self._store_is_ready(engine.store):
                    try:
                        parent_task = await engine.store.get_task(parent_task_id)
                    except Exception:
                        parent_task = None
                    exec_mode, company_profile = self._resolve_task_session_config(parent_task)
                    org_id = self._resolve_task_org_id(parent_task)
                    preferred_agent = self._resolve_task_preferred_agent(parent_task)
                work_item_identity: dict[str, Any] = {}
                if self._store_is_ready(engine.store):
                    try:
                        created_task = await engine.store.get_task(task_id)
                    except Exception:
                        created_task = None
                    if created_task is not None:
                        created_meta = dict(getattr(created_task, "metadata", {}) or {})
                        role_id = str(created_meta.get("work_item_role_id", "") or getattr(created_task, "assigned_to", "") or "").strip()
                        role_name = str(created_meta.get("work_item_role_name", "") or "").strip()
                        if not role_name and role_id:
                            role_name = self._resolve_work_item_role_name(
                                role_id,
                                created_meta,
                                engine=engine,
                            )
                        if self._runtime_payload_is_task_mode(created_meta):
                            work_item_identity = {
                                "employee_assignment": created_meta.get("employee_assignment"),
                                "origin_task_id": created_meta.get("origin_task_id") or parent_task_id or task_id,
                                "selected_execution_agent": self._resolve_task_selected_execution_agent(created_task),
                            }
                        else:
                            projection_id = work_item_projection_id_from_metadata(created_meta)
                            turn_type = work_item_turn_type_from_metadata(created_meta, fallback="")
                            work_item_identity = {
                                **work_item_identity_payload(projection_id=projection_id, turn_type=turn_type),
                                "work_item_role_id": role_id,
                                "work_item_role_name": role_name,
                                "employee_assignment": created_meta.get("employee_assignment"),
                                "origin_task_id": created_meta.get("origin_task_id") or parent_task_id or task_id,
                                "selected_execution_agent": self._resolve_task_selected_execution_agent(created_task),
                            }
                        preferred_agent = self._resolve_task_preferred_agent(created_task)
                        org_id = self._resolve_task_org_id(created_task) or org_id
                await self.chat_store.create_session_channel(task_id, title, project_id=pid)
                # Display counter already incremented by task_created event — use map lookup
                display_num = self.event_adapter.get_task_display_num(task_id)
                display_id = f"OPC-{display_num}"
                execution_aliases = _add_execution_turn_aliases({}, task_id)
                # Child company runtime sessions remain in the session sidebar but do not
                # become company-mode kanban cards.
                if not parent_session_id:
                    await self.broadcast({"type": "board_task_created", "payload": {
                        "project_id": pid,
                        "task_id": task_id,
                        **execution_aliases,
                        "display_id": display_id,
                        "board_id": pid,
                        "title": title,
                        # Engine event agent_id is opc_role_id; resolve to UI agent_id
                        "assignee_ids": [self.event_adapter._resolve_role_to_agent(p["agent_id"])] if p.get("agent_id") else [],
                        **work_item_identity,
                    }})
                await self.broadcast({"type": "session_created", "payload": {
                    "project_id": pid,
                    "task_id": task_id,
                    **execution_aliases,
                    "channel_id": channel_id,
                    "session_id": p.get("session_id"),
                    "parent_session_id": p.get("parent_session_id"),
                    "origin_task_id": work_item_identity.get("origin_task_id") or parent_task_id or task_id,
                    "exec_mode": exec_mode,
                    "company_profile": company_profile,
                    "org_id": org_id,
                    "preferred_agent": preferred_agent,
                    "title": title,
                    "status": "pending",
                    "created_at": time.time(),
                    **work_item_identity,
                }})

        # Update agent_store status for persistence (match by opc_role_id)
        if event.event_type == "agent_status_changed":
            await self.agent_store.update_status_by_role(
                event.payload.get("role_id", ""),
                event.payload.get("status", ""),
            )

        # Mirror agent messages to chat
        if event.event_type == "agent_message_sent":
            await self._mirror_agent_message(event, engine=engine, project_id=pid)
            # Tell every connected client that the comms tree just changed
            # so the CommsPanel can refetch immediately instead of waiting
            # for its 5s polling tick. Cheap fire-and-forget broadcast.
            try:
                await self.broadcast({
                    "type": "comms_state_dirty",
                    "payload": {
                        "project_id": pid,
                        "from": (event.payload or {}).get("from"),
                        "to": (event.payload or {}).get("to"),
                    },
                })
            except Exception:
                pass

        # Mirror escalations to chat
        if event.event_type == "escalation_created":
            await self._mirror_escalation(event, engine=engine, project_id=pid)
        if event.event_type in {"escalation_resolved", "escalation_timeout"}:
            await self._mark_escalation_event_checkpoint_terminal(event, project_id=pid)

        if event.event_type == "task_status_changed":
            payload = event.payload or {}
            task_id = str(payload.get("task_id", "") or "").strip()
            status = str(payload.get("status", "") or "").strip().lower()
            if task_id and status in {
                "done",
                "failed",
                "cancelled",
                "blocked",
                "awaiting_manager_review",
                "awaiting_human",
                "awaiting_review",
                "awaiting_peer",
            }:
                await self._sync_task_transcript_messages(task_id, engine=engine)
                if self._store_is_ready(engine.store):
                    task = await engine.store.get_task(task_id)
                    if task is not None:
                        for parent_task_id in self._related_parent_task_ids(task):
                            await self._sync_task_transcript_messages(parent_task_id, engine=engine)

    async def _handle_runtime_event_progress(
        self,
        payload: dict[str, Any],
        *,
        engine: Any | None = None,
        project_id: str | None = None,
    ) -> None:
        runtime_engine = engine or self.engine
        pid = self._normalize_project_id(project_id or getattr(runtime_engine, "project_id", None))
        payload = self._enrich_runtime_progress_payload(payload, engine=runtime_engine)
        raw_task_id = str(payload.get("task_id", "") or "").strip()
        if not raw_task_id:
            return
        task_id = await self._ui_task_id_for_runtime_task_id(raw_task_id, engine=runtime_engine)
        if not task_id:
            return
        runtime_type = str(payload.get("type", "") or "").strip()
        entry = self._runtime_event_to_progress_entry(payload)
        if not entry:
            if runtime_type in {"turn_completed", "turn_failed", "checkpoint_saved"}:
                await self._sync_task_transcript_messages(task_id, engine=runtime_engine)
                if self._store_is_ready(runtime_engine.store):
                    task = await runtime_engine.store.get_task(raw_task_id)
                    if task is not None:
                        for parent_task_id in self._related_parent_task_ids(task):
                            await self._sync_task_transcript_messages(parent_task_id, engine=runtime_engine)
            return
        entry["timestamp"] = time.time()
        _add_execution_turn_aliases(entry, raw_task_id)
        # Buffer BEFORE broadcasting: broadcast awaits can interleave a
        # session_detail read, and any entry a client has already seen must be
        # visible in buffer∪DB or the snapshot will erase it from the live log.
        buf = self._progress_buffer.setdefault(task_id, [])
        buf.append(entry)
        self._progress_project_ids[task_id] = pid
        await self.broadcast({
            "type": "session_progress",
                "payload": {
                    "project_id": pid,
                    "task_id": task_id,
                    **_add_execution_turn_aliases({}, raw_task_id),
                "entry": entry,
            },
        })
        origin = self._active_runtime_children.get(raw_task_id) or (task_id if task_id != raw_task_id else None)
        if entry.get("is_company_runtime") and origin:
            await self.broadcast({
                "type": "work_item_progress",
                "payload": {
                    "project_id": pid,
                    "task_id": origin,
                    **_add_execution_turn_aliases({}, raw_task_id),
                    "entry": entry,
                },
            })
        # Re-read the buffer: a concurrent flush during the broadcast awaits
        # may have popped it, leaving `buf` as a stale detached list.
        if len(self._progress_buffer.get(task_id, [])) >= self._PROGRESS_FLUSH_THRESHOLD:
            await self._flush_progress(task_id, project_id=pid)
        if runtime_type in {"turn_completed", "turn_failed", "checkpoint_saved"}:
            await self._sync_task_transcript_messages(task_id, engine=runtime_engine)
            if self._store_is_ready(runtime_engine.store):
                task = await runtime_engine.store.get_task(raw_task_id)
                if task is not None:
                    for parent_task_id in self._related_parent_task_ids(task):
                        await self._sync_task_transcript_messages(parent_task_id, engine=runtime_engine)

    async def _handle_worker_notification(
        self,
        payload: dict[str, Any],
        *,
        engine: Any | None = None,
        project_id: str | None = None,
    ) -> None:
        runtime_engine = engine or self.engine
        remapped = dict(payload or {})
        mapped_task_id = await self._ui_task_id_for_runtime_task_id(remapped.get("task_id"), engine=runtime_engine)
        if mapped_task_id:
            remapped["task_id"] = mapped_task_id
        remapped["project_id"] = self._normalize_project_id(project_id or getattr(runtime_engine, "project_id", None))
        await self.broadcast({
            "type": "worker_notification",
            "payload": remapped,
        })
        await self._persist_worker_notification_message(payload, engine=runtime_engine, project_id=project_id)

    async def _persist_worker_notification_message(
        self,
        payload: dict[str, Any],
        *,
        engine: Any | None = None,
        project_id: str | None = None,
    ) -> None:
        runtime_engine = engine or self.engine
        notification_kind = str(payload.get("notification_kind", "") or "").strip()
        raw_task_id = str(payload.get("task_id", "") or "").strip()
        if notification_kind not in _PERSISTED_WORKER_NOTIFICATION_KINDS or not raw_task_id:
            return
        task_id = await self._ui_task_id_for_runtime_task_id(raw_task_id, engine=runtime_engine)
        if not task_id:
            return
        project_id = self._normalize_project_id(project_id or getattr(runtime_engine, "project_id", None))
        title = "Task Update"
        if self._store_is_ready(runtime_engine.store):
            try:
                task = await runtime_engine.store.get_task(raw_task_id)
            except Exception:
                task = None
            if task is not None:
                title = str(getattr(task, "title", "") or title).strip() or title
        await self.chat_store.create_session_channel(task_id, title, project_id=project_id)
        timestamp = float(payload.get("timestamp") or time.time())
        worker_id = str(payload.get("worker_id", "") or "").strip() or "worker"
        worker_type = str(payload.get("worker_type", "") or "").strip() or "worker"
        message_id = (
            f"worker-note:{task_id}:{worker_id}:{notification_kind}:{int(timestamp * 1000)}"
        )
        summary = str(payload.get("summary", "") or "").strip()
        worker_name = str(payload.get("name", "") or "").strip()
        sender_name = worker_name or worker_type.replace("_", " ").title()
        content = summary or f"{sender_name}: {notification_kind.replace('_', ' ')}"
        metadata = {
            **_ui_message_identity_metadata(
                kind="worker_notification",
                message_id=message_id,
                created_at=timestamp,
            ),
            "source": "runtime_event",
            "role": "system",
            "worker_id": worker_id,
            "worker_type": worker_type,
            "notification_kind": notification_kind,
            "resident_status": payload.get("resident_status"),
        }
        message = await self.chat_store.insert_message(
            channel_id=f"session:{task_id}",
            sender="system",
            sender_name=sender_name,
            content=content,
            metadata=metadata,
            message_id=message_id,
            project_id=project_id,
        )
        await self.broadcast({
            "type": "session_message",
            "payload": message,
        })

    def _remember_pending_escalation(self, payload: dict[str, Any]) -> dict[str, Any]:
        escalation_id = str(payload.get("escalation_id") or f"esc_{uuid.uuid4()}")
        raw_project_id = str(payload.get("project_id") or "").strip()
        project_id = self._normalize_project_id(raw_project_id) if raw_project_id else ""
        approval_group_key = str(payload.get("approval_group_key") or "").strip() or self._approval_group_key(
            str(payload.get("message") or "")
        )
        existing = self._pending_escalations.get(escalation_id)
        if existing is not None:
            future = existing.get("future")
            if future is None or future.done():
                future = asyncio.get_running_loop().create_future()
            record = {
                **existing,
                **payload,
                "future": future,
                "escalation_id": escalation_id,
                "approval_group_key": approval_group_key,
            }
            if project_id:
                record["project_id"] = project_id
            self._pending_escalations[escalation_id] = record
            if escalation_id in self._pending_escalation_order:
                self._pending_escalation_order = [
                    item for item in self._pending_escalation_order
                    if item != escalation_id
                ]
            self._pending_escalation_order.append(escalation_id)
            return record

        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        record = {
            **payload,
            "future": future,
            "escalation_id": escalation_id,
            "approval_group_key": approval_group_key,
        }
        if project_id:
            record["project_id"] = project_id
        self._pending_escalations[escalation_id] = record
        self._pending_escalation_order.append(escalation_id)
        return record

    @staticmethod
    def _approval_group_key(message: str) -> str:
        raw = str(message or "").strip()
        if not raw:
            return ""
        normalized = WSHandler._semantic_permission_group_key(raw)
        if normalized:
            return normalized
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.lower().startswith("allowlist target:"):
                return stripped.split(":", 1)[1].strip()
        match = re.search(r"Approve\s+([a-z_]+)\s+'([^']+)'\?", raw, re.IGNORECASE)
        if match:
            return f"{match.group(1).lower()}:{match.group(2).strip()}"
        return ""

    @staticmethod
    def _semantic_permission_group_key(message: str) -> str:
        raw = str(message or "")
        tool_match = re.search(r"Approve\s+tool\s+'([^']+)'\?", raw, re.IGNORECASE)
        tool_name = tool_match.group(1).strip().casefold() if tool_match else ""
        if tool_name != "shell_exec":
            return ""

        command_match = re.search(
            r"command=(.*?)(?:\nAllowlist target:|\Z)",
            raw,
            re.IGNORECASE | re.DOTALL,
        )
        command = (command_match.group(1) if command_match else raw).strip()
        command_family = ""
        if re.match(r"^(?:python|python3)\b", command, re.IGNORECASE):
            command_family = "python"
        elif re.match(r"^node\b", command, re.IGNORECASE):
            command_family = "node"
        if not command_family:
            return ""

        domains = sorted({
            match.group(1).casefold()
            for match in re.finditer(r"https?://([^/\s'\"<>]+)", command)
        })
        domain_key = ",".join(domains) if domains else "no-domain"
        return f"tool:shell_exec/{command_family}:domain:{domain_key}"

    def _resolve_related_pending_escalations(
        self,
        record: dict[str, Any],
        reply: str,
    ) -> list[str]:
        normalized_reply = str(reply or "").strip().lower()
        if normalized_reply not in {"approve_session", "always_project", "always_global"}:
            return []
        group_key = str(record.get("approval_group_key") or "").strip()
        if not group_key:
            return []
        current_escalation_id = str(record.get("escalation_id") or "").strip()
        task_id = str(record.get("task_id") or "").strip()
        project_id = str(record.get("project_id") or "").strip()
        resolved_ids: list[str] = []
        for escalation_id in list(self._pending_escalation_order):
            if escalation_id == current_escalation_id:
                continue
            candidate = self._pending_escalations.get(escalation_id)
            if not candidate:
                continue
            future = candidate.get("future")
            if future is None or future.done():
                continue
            candidate_project_id = str(candidate.get("project_id") or "").strip()
            if project_id and candidate_project_id and candidate_project_id != project_id:
                continue
            if str(candidate.get("approval_group_key") or "").strip() != group_key:
                continue
            if normalized_reply == "approve_session" and str(candidate.get("task_id") or "").strip() != task_id:
                continue
            future.set_result(normalized_reply)
            resolved_ids.append(escalation_id)
        return resolved_ids

    @staticmethod
    def _task_mode_permission_prompt(message: str, current_turn_title: str = "") -> str:
        raw = str(message or "").strip()
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        cleaned: list[str] = []
        for line in lines:
            if line.startswith("[") and "]" in line:
                line = line.split("]", 1)[1].strip()
            if line.lower().startswith("task:"):
                continue
            cleaned.append(line)
        title = "Permission required"
        if current_turn_title:
            title = f"Permission required: {current_turn_title[:80]}"
        return "\n".join([title, *cleaned]).strip()

    async def _resolve_escalation_session_task_id(
        self,
        task_id: str | None,
        *,
        engine: Any | None = None,
    ) -> str | None:
        source_task_id = str(task_id or "").strip()
        if not source_task_id:
            return None

        runtime_engine = engine or self.engine
        store = getattr(runtime_engine, "store", None)
        get_task = getattr(store, "get_task", None)
        if callable(get_task):
            try:
                task = await get_task(source_task_id)
            except Exception as e:
                logger.warning(f"Failed to resolve escalation task mapping for {source_task_id}: {e}")
                task = None
            if task is not None:
                try:
                    identity_index = await load_company_runtime_identity_index(
                        store,
                        self._normalize_project_id(
                            getattr(task, "project_id", None)
                            or getattr(runtime_engine, "project_id", None)
                        ),
                    )
                    runtime_identity = identity_index.resolve(task_id=source_task_id)
                except Exception:
                    logger.opt(exception=True).debug(
                        "failed to resolve durable company identity for escalation"
                    )
                    runtime_identity = None
                if runtime_identity is not None:
                    # Escalations are a control decision.  Route only through
                    # the durable runtime identity; process-local progress maps
                    # are not evidence that a work-item Task is the UI parent.
                    return runtime_identity.ui_anchor_task_id or None
                if is_company_runtime_task(task):
                    return None
                internal_turn_target = self._company_internal_turn_escalation_target(task)
                if internal_turn_target is not None:
                    return internal_turn_target or None
                ui_task_id = self._ui_task_id_for_task(task)
                if ui_task_id:
                    return ui_task_id
                metadata = dict(getattr(task, "metadata", {}) or {})
                origin_task_id = str(metadata.get("origin_task_id") or "").strip()
                if origin_task_id:
                    return origin_task_id

        return source_task_id

    def _company_internal_turn_escalation_target(self, task: Any | None) -> str | None:
        """Visible routing target for escalations raised by internal
        company-mode scheduling turns.

        Review/report turn work items get composite ids (``review::<wid>::vN``),
        so their runtime tasks carry session ids shaped like
        ``<root_session>:review::<wid>::vN``. The UI deliberately hides those
        session channels, so an approval card posted to the turn's own channel
        can never be seen or answered — it silently times out and the work item
        parks on AWAITING_HUMAN.

        Returns None when ``task`` is not such an internal turn (caller keeps
        its normal resolution), the durable origin task when present, or ""
        when no visible channel is known.  Company runtime control routing is
        resolved before this helper; process-local session maps are deliberately
        not consulted here.
        """
        if task is None:
            return None
        session_id = str(getattr(task, "session_id", "") or "").strip()
        root_session_id, sep, suffix = session_id.partition(":")
        if not sep or "::" not in suffix:
            return None
        metadata = dict(getattr(task, "metadata", {}) or {})
        origin_task_id = str(metadata.get("origin_task_id") or "").strip()
        task_id = str(getattr(task, "id", "") or "").strip()
        if origin_task_id and origin_task_id != task_id:
            return origin_task_id
        return ""

    @staticmethod
    def _pending_escalation_matches_task(record: dict[str, Any], task_id: str | None) -> bool:
        task_key = str(task_id or "").strip()
        if not task_key:
            return True
        record_task_id = str(record.get("task_id") or "").strip()
        source_task_id = str(record.get("source_task_id") or "").strip()
        return task_key in {record_task_id, source_task_id}

    @staticmethod
    def _pending_escalation_matches_project(record: dict[str, Any], project_id: str | None) -> bool:
        project_key = str(project_id or "").strip()
        if not project_key:
            return True
        record_project_id = str(record.get("project_id") or "").strip()
        if not record_project_id:
            return True
        return record_project_id == project_key

    def _find_pending_escalation(
        self,
        *,
        task_id: str | None = None,
        escalation_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        explicit_escalation_id = str(escalation_id or "").strip()
        if explicit_escalation_id:
            record = self._pending_escalations.get(explicit_escalation_id)
            if not record:
                return None
            future = record.get("future")
            if future is None or future.done():
                return None
            if not self._pending_escalation_matches_project(record, project_id):
                return None
            if not self._pending_escalation_matches_task(record, task_id):
                return None
            return record

        for escalation_id in reversed(self._pending_escalation_order):
            record = self._pending_escalations.get(escalation_id)
            if not record:
                continue
            future = record.get("future")
            if future is None or future.done():
                continue
            if not self._pending_escalation_matches_project(record, project_id):
                continue
            if not self._pending_escalation_matches_task(record, task_id):
                continue
            return record
        return None

    async def _handle_ui_escalation(
        self,
        message: str,
        options: list[dict],
        *,
        project_id: str | None = None,
    ) -> str | None:
        project_key = self._normalize_project_id(project_id)
        option_ids = tuple(str(opt.get("id", "")).strip() for opt in options)
        record = None
        for escalation_id in reversed(self._pending_escalation_order):
            candidate = self._pending_escalations.get(escalation_id)
            if not candidate:
                continue
            future = candidate.get("future")
            if future is None or future.done():
                continue
            if not self._pending_escalation_matches_project(candidate, project_key):
                continue
            candidate_ids = tuple(str(opt.get("id", "")).strip() for opt in candidate.get("options", []))
            if candidate_ids == option_ids and str(candidate.get("message", "")) == message:
                record = candidate
                break
        if record is None:
            record = self._find_pending_escalation(project_id=project_key)
        if record is None:
            return None

        future = record["future"]
        try:
            return await future
        finally:
            escalation_id = str(record.get("escalation_id", ""))
            self._pending_escalations.pop(escalation_id, None)
            self._pending_escalation_order = [
                item for item in self._pending_escalation_order
                if item != escalation_id
            ]

    async def on_kanban_changed(self, *, engine: Any | None = None) -> None:
        """Callback fired by the company-mode lifecycle loop after each work item
        batch completes.  Broadcasts a full collab_sync so the frontend kanban
        reflects newly delegated / updated work items in real-time."""
        runtime_engine = engine or self.engine
        if (
            self._shutting_down
            or not self._store_is_ready(getattr(runtime_engine, "store", None))
            or not self._chat_store_is_ready(self.chat_store)
        ):
            return
        try:
            collab = await build_collab_sync(
                runtime_engine, self.agent_store, self.chat_store,
                self.event_adapter,
                exec_mode=self._exec_mode,
            )
            await self.broadcast({"type": "collab_sync_push", "payload": collab})
        except Exception as exc:
            if self._is_expected_shutdown_error(exc) or self._is_closed_database_error(exc):
                logger.debug(
                    "on_kanban_changed skipped during shutdown/closed store: {}: {}",
                    type(exc).__name__,
                    exc,
                )
                return
            # Surface the exception type + message inline AND force the full
            # traceback into the sink. loguru silently drops ``exc_info=True``
            # unless the sink was configured with ``backtrace=True``; using
            # ``.opt(exception=True)`` reliably prints the stack regardless
            # of sink configuration, which is what we want when diagnosing a
            # transient race between the company-mode loop and the UI push.
            logger.opt(exception=True).warning(
                "on_kanban_changed collab_sync broadcast failed: {}: {}",
                type(exc).__name__,
                exc,
            )

    async def on_progress(self, text: str, **kw: Any) -> None:
        """engine.on_progress callback. Routes progress to session channel.

        ``task_id`` is now supplied explicitly by the caller (NativeRuntimeV2,
        company_mode, or the engine scoped wrapper).  No global fallback.
        Optional ``agent_role_id`` / ``agent_name`` carry agent identity for
        dual-routed messages so the parent chat can display per-agent identity.
        """
        import time as _time

        runtime_engine = kw.pop("_runtime_engine", None) or self.engine
        pid = self._normalize_project_id(kw.pop("_project_id", None) or getattr(runtime_engine, "project_id", None))
        raw_task_id = str(kw.get("task_id", "") or "").strip() or None
        task_id = await self._ui_task_id_for_runtime_task_id(raw_task_id, engine=runtime_engine)
        task_id = task_id or None
        agent_role_id: str = kw.get("agent_role_id", "")
        agent_name: str = kw.get("agent_name", "")
        visual_events = self.event_adapter.parse_progress(
            text,
            task_id=task_id or raw_task_id,
            agent_role_id=agent_role_id,
        )
        for ve in visual_events:
            ve = dict(ve)
            ve_data = dict(ve.get("data", {}) or {})
            ve_data.setdefault("project_id", pid)
            ve["data"] = ve_data
            ve["project_id"] = pid
            await self.broadcast({"type": "event", "payload": ve})

        # Route to session channel if task_id is known, else activity
        target_channel = f"session:{task_id}" if task_id else f"activity:{pid}"

        # ── Resolve role label for cleaner display ─────────────────
        _role_label = (agent_name or agent_role_id or "").strip()
        if _role_label:
            _role_label = _role_label.replace("_", " ").title()

        # ── Broadcast progress entry for tool call history ──────────
        if task_id:
            entry = self._parse_progress_entry(text)
            if entry:
                entry["timestamp"] = _time.time()
                # Enrich with role name for frontend display
                if _role_label and not entry.get("role_name"):
                    entry["role_name"] = _role_label
                if entry.get("is_company_runtime"):
                    entry.update(
                        work_item_identity_payload(
                            projection_id=entry.get("work_item_projection_id") or "",
                            turn_type=entry.get("work_item_turn_type") or "",
                        )
                    )
                    if not entry.get("work_item_projection_title"):
                        entry["work_item_projection_title"] = _role_label or None
                _add_execution_turn_aliases(entry, raw_task_id or task_id)
                # Buffer BEFORE broadcasting: broadcast awaits can interleave a
                # session_detail read, and any entry a client has already seen
                # must be visible in buffer∪DB or the snapshot will erase it.
                self._progress_buffer.setdefault(task_id, []).append(entry)
                self._progress_project_ids[task_id] = pid
                await self.broadcast({"type": "session_progress", "payload": {
                    "project_id": pid,
                    "task_id": task_id,
                    **_add_execution_turn_aliases({}, raw_task_id or task_id),
                    "entry": entry,
                }})
                # ── Company runtime dual-route: broadcast to primary session ──
                origin = self._active_runtime_children.get(task_id)
                if entry.get("is_company_runtime") and origin:
                    await self.broadcast({"type": "work_item_progress", "payload": {
                        "project_id": pid,
                        "task_id": origin,
                        **_add_execution_turn_aliases({}, raw_task_id or task_id),
                        "entry": entry,
                    }})

                # ── Persist at threshold (re-read: a concurrent flush during the
                # broadcast awaits may have popped the buffer) ──
                if len(self._progress_buffer.get(task_id, [])) >= self._PROGRESS_FLUSH_THRESHOLD:
                    await self._flush_progress(task_id, project_id=pid)

        is_work_item_event = text.startswith("[Company:")

        # Clean up [Company:UUID] prefix for display — replace with role name
        display_text = text
        if is_work_item_event and _role_label:
            bracket_end = text.find("]")
            if bracket_end > 9:
                raw_projection = text[9:bracket_end]
                # If the projection looks like a UUID, replace it with the role label.
                if len(raw_projection) > 12 and raw_projection.replace("-", "").replace("_", "").isalnum():
                    display_text = f"[{_role_label}] {text[bracket_end + 1:].strip()}"

        if self._should_store_progress_message(text):
            msg_meta: dict[str, Any] = {}
            if task_id:
                msg_meta["task_id"] = task_id
            if is_work_item_event:
                msg_meta["is_work_item_event"] = True
            msg = await self.chat_store.insert_message(
                channel_id=target_channel,
                sender="system",
                sender_name="OPC",
                content=display_text,
                metadata=msg_meta or None,
                project_id=pid,
            )
            await self.broadcast({"type": "session_message", "payload": msg})

            # Dual-route: also insert into parent session channel so the
            # primary chat view shows agent output from child tasks.
            if task_id:
                parent_task_id = self._active_runtime_children.get(task_id)
                if parent_task_id and parent_task_id != task_id:
                    parent_channel = f"session:{parent_task_id}"
                    # Resolve agent identity for the forwarded message
                    fwd_sender = "system"
                    fwd_sender_name = "OPC"
                    if agent_role_id:
                        fwd_sender = self.event_adapter._resolve_role_to_agent(agent_role_id)
                        fwd_sender_name = agent_name or agent_role_id.replace("_", " ").title()
                    fwd_meta: dict[str, Any] = {
                        "task_id": task_id,
                        "forwarded_from": task_id,
                        # _should_store_progress_message already filters out noisy
                        # external streams/heartbeats. Forwarded stored messages are
                        # high-signal role launch/status records and should remain
                        # visible in the primary session summary.
                        "detail_visibility": "summary",
                    }
                    if is_work_item_event:
                        fwd_meta["is_work_item_event"] = True
                    if _role_label:
                        fwd_meta["role_name"] = _role_label
                    parent_msg = await self.chat_store.insert_message(
                        channel_id=parent_channel,
                        sender=fwd_sender,
                        sender_name=fwd_sender_name,
                        content=display_text,
                        metadata=fwd_meta,
                        project_id=pid,
                    )
                    await self.broadcast({"type": "session_message", "payload": parent_msg})

    async def _flush_progress(self, task_id: str, *, project_id: str | None = None) -> None:
        """Write buffered progress entries to ChatStore for a single task.

        Atomically pops the buffer so concurrent on_progress calls for the same
        task_id will create a fresh buffer.  Safe to call multiple times — if
        the buffer is empty (already flushed), returns immediately.
        """
        entries = self._progress_buffer.pop(task_id, [])
        if not entries:
            self._progress_project_ids.pop(task_id, None)
            return
        pid = self._normalize_project_id(project_id or self._progress_project_ids.pop(task_id, None) or self.engine.project_id)
        try:
            await self.chat_store.append_progress(task_id, entries, project_id=pid)
        except Exception:
            logger.debug(f"Failed to flush progress for task {task_id}")

    async def flush_all_progress(self) -> None:
        """Flush all buffered progress to DB. Called on graceful server shutdown."""
        task_ids = list(self._progress_buffer.keys())
        for tid in task_ids:
            await self._flush_progress(tid)

    def _ensure_progress_flush_loop(self) -> None:
        """Start the background periodic-flush coroutine if not already running.

        Called lazily on the first WS client connect so the loop exists for
        as long as the UI is in use. Flushes any task whose buffer has had
        entries sitting in RAM longer than ``_PROGRESS_FLUSH_INTERVAL_SEC``,
        so even sparsely-emitting tasks show complete Activity timelines
        without a 10-entry wait.
        """
        if self._progress_flush_task and not self._progress_flush_task.done():
            return
        self._progress_flush_task = asyncio.create_task(self._periodic_flush_loop())

    async def _periodic_flush_loop(self) -> None:
        while not self._shutting_down:
            try:
                await asyncio.sleep(self._PROGRESS_FLUSH_INTERVAL_SEC)
            except asyncio.CancelledError:
                break
            if self._shutting_down:
                break
            # Snapshot buffer keys under no-lock read; _flush_progress handles
            # the pop atomically so racing appends don't lose entries.
            pending = [tid for tid, buf in self._progress_buffer.items() if buf]
            for tid in pending:
                try:
                    await self._flush_progress(tid)
                except Exception:
                    logger.debug(
                        "Periodic progress flush error for task %s", tid,
                    )

    def _ensure_runtime_status_sync_loop(self) -> None:
        """Start the runtime-status reconciliation coroutine if not running.

        Live status deltas are one-shot, best-effort broadcasts: a delta lost
        to a disconnect window, a project-scope drop, or a not-ready store
        leaves the UI stuck on a stale status until a full refresh. This loop
        periodically re-broadcasts the authoritative status of every task with
        a live runtime (plus one final tick for tasks that just ended) so any
        missed delta converges within one interval.
        """
        if self._runtime_status_sync_task and not self._runtime_status_sync_task.done():
            return
        self._runtime_status_sync_task = asyncio.create_task(self._runtime_status_sync_loop())

    async def _runtime_status_sync_loop(self) -> None:
        while not self._shutting_down:
            try:
                await asyncio.sleep(self._RUNTIME_STATUS_SYNC_INTERVAL_SEC)
            except asyncio.CancelledError:
                break
            if self._shutting_down:
                break
            try:
                await self._runtime_status_sync_tick()
            except Exception:
                logger.opt(exception=True).debug("runtime status sync tick failed")

    def _collect_runtime_status_candidates(self) -> dict[str, str]:
        """Map task_id → project_id for every task with a live runtime.

        Sources are purely in-memory (no store scans): the background-task
        context registry covers every session run across modes; the company
        runtime-children map adds work-item child tasks of a live tree.
        """
        candidates: dict[str, str] = {}
        for bg, ctx in list(self._task_bg_context.items()):
            if bg.done():
                continue
            t_id = str(ctx.get("task_id") or "").strip()
            if t_id:
                candidates[t_id] = self._normalize_project_id(str(ctx.get("project_id") or ""))
        for child_id, root_id in list(self._active_runtime_children.items()):
            if child_id in candidates:
                continue
            pid = (
                candidates.get(root_id)
                or self._progress_project_ids.get(child_id)
                or self._progress_project_ids.get(root_id)
            )
            if pid:
                candidates[child_id] = self._normalize_project_id(pid)
        return candidates

    async def _runtime_status_sync_tick(self) -> None:
        current = self._collect_runtime_status_candidates()
        if not self._clients:
            # Nobody to correct; a connecting client gets a full snapshot.
            self._runtime_status_sync_prev = current
            return
        departed = {
            tid: pid for tid, pid in self._runtime_status_sync_prev.items()
            if tid not in current
        }
        self._runtime_status_sync_prev = current
        to_sync = {**departed, **current}
        if not to_sync:
            return
        tracker_by_task: dict[str, tuple[str, str | None]] = {}
        for tracker in getattr(self.event_adapter, "_trackers", {}).values():
            t_id = str(getattr(tracker, "task_id", "") or "").strip()
            if t_id:
                tracker_by_task[t_id] = (tracker.state.value, tracker.current_tool)
        by_project: dict[str, list[str]] = {}
        for tid, pid in to_sync.items():
            by_project.setdefault(pid, []).append(tid)
        for pid, task_ids in by_project.items():
            try:
                engine = await self._engine_for_project(pid)
            except Exception:
                continue
            store = getattr(engine, "store", None)
            if store is None or not self._store_is_ready(store):
                continue
            sessions: list[dict[str, Any]] = []
            for task_id in sorted(task_ids):
                try:
                    t = await store.get_task(task_id)
                except Exception:
                    continue
                if t is None:
                    continue
                status_val = t.status.value if hasattr(t.status, "value") else str(t.status)
                entry: dict[str, Any] = {"task_id": task_id, "status": status_val}
                tracked = tracker_by_task.get(task_id)
                if tracked is not None:
                    entry["agent_status"], entry["current_tool"] = tracked
                sessions.append(entry)
            if sessions:
                await self.broadcast({
                    "type": "runtime_status_sync",
                    "payload": {"project_id": pid, "sessions": sessions},
                })

    @staticmethod
    def _parse_progress_entry(text: str) -> dict[str, Any] | None:
        """Parse on_progress text into a ProgressEntry dict, or None to skip."""
        # [Tool: file_read] {"path": "src/api.ts"}
        if text.startswith("[Tool:"):
            bracket_end = text.find("]")
            if bracket_end > 0:
                tool_name = text[7:bracket_end].strip()
                detail = text[bracket_end + 1:].lstrip()
                return {"type": "tool_call", "summary": tool_name, "detail": detail}
            return {"type": "tool_call", "summary": text[:60]}

        # [Autonomy] tool:web_search -> auto_approve (risk=low)
        if text.startswith("[Autonomy]"):
            return {"type": "autonomy", "summary": text[11:].strip()[:100]}

        # [Delegating to claude-code] task=...
        if text.startswith("[Delegating"):
            bracket_end = text.find("]")
            target = text[15:bracket_end] if bracket_end > 15 else "agent"
            return {
                "type": "handoff",
                "summary": f"Delegating to {target}",
                "detail": text,
            }

        # [External:agent:stdout] ... / [External status] ... / failures
        if text.startswith("[External"):
            bracket_end = text.find("]")
            header = text[1:bracket_end] if bracket_end > 1 else "External"
            detail = text[bracket_end + 1:].strip() if bracket_end > 0 else text
            summary = detail[:80] if detail else header[:80]

            if header.startswith("External:"):
                parts = header.split(":")
                agent = parts[1] if len(parts) > 1 else "external"
                stream = parts[2] if len(parts) > 2 else ""
                if stream == "thinking":
                    thinking_summary = detail[:120] if detail else f"{agent} thinking"
                    if len(detail) > 120:
                        thinking_summary = thinking_summary.rstrip() + "..."
                    return {
                        "type": "thinking",
                        "summary": thinking_summary,
                        "detail": detail or None,
                    }
                if stream == "tool":
                    first_line = next((line.strip() for line in detail.splitlines() if line.strip()), "")
                    if first_line.startswith("$ "):
                        first_line = first_line[2:]
                    tool_summary = first_line[:120] if first_line else f"{agent} tool"
                    return {
                        "type": "tool_call",
                        "summary": tool_summary,
                        "detail": detail or None,
                    }
                label = f"{agent} {stream}".strip()
                if label:
                    summary = label
            elif header == "External status" and detail:
                summary = detail[:80]

            return {
                "type": "status_change",
                "summary": summary,
                "detail": detail or text,
            }

        # [CapabilityRecovery]
        if text.startswith("[CapabilityRecovery]"):
            return {"type": "status_change", "summary": "Capability recovery"}

        # [Company] (no projection) — global company runtime event (e.g. deadlock)
        if text.startswith("[Company]"):
            action = text[10:].strip()
            action_lower = action.lower()
            entry_type = "gate_result"
            if "deadlock" in action_lower:
                entry_type = "deadlock"
            elif "failed" in action_lower:
                entry_type = "work_item_failed"
            return {
                "type": entry_type,
                "summary": f"Company runtime: {action[:80]}",
                "detail": action[:200] if action else None,
                **work_item_identity_payload(projection_id="company_runtime", turn_type=""),
                "work_item_projection_title": "Company Runtime",
                "is_company_runtime": True,
            }

        # [Company:projection] ... — classify by specific action
        if text.startswith("[Company:"):
            bracket_end = text.find("]")
            projection_id = text[9:bracket_end] if bracket_end > 9 else "work_item"
            action = text[bracket_end + 2:].strip() if bracket_end > 0 else ""
            action_lower = action.lower()

            entry_type = "gate_result"
            if "starting" in action_lower or "started" in action_lower:
                entry_type = "work_item_started"
            elif "gate passed" in action_lower or "approved" in action_lower or "completed" in action_lower:
                entry_type = "gate_approved"
            elif "rejected" in action_lower or "reworking" in action_lower:
                entry_type = "gate_rejected"
            elif "awaiting manager review" in action_lower:
                entry_type = "awaiting_manager_review"
            elif "awaiting user" in action_lower or "awaiting human review" in action_lower or "awaiting review" in action_lower:
                entry_type = "awaiting_human"
            elif "awaiting peer" in action_lower:
                entry_type = "awaiting_peer"
            elif "failed" in action_lower:
                entry_type = "work_item_failed"
            elif "deadlock" in action_lower:
                entry_type = "deadlock"

            # Use projection name as title, action text as detail.
            is_uuid_like = len(projection_id) > 12 and projection_id.replace("-", "").replace("_", "").isalnum()
            projection_title = projection_id if is_uuid_like else projection_id.replace("_", " ").replace("-", " ").title()
            return {
                "type": entry_type,
                "summary": f"{projection_title}: {action[:80]}",
                "detail": action[:200] if action else None,
                **work_item_identity_payload(projection_id=projection_id, turn_type=""),
                "work_item_projection_title": projection_title,
                "is_company_runtime": True,
            }

        return None

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            numeric = float(value)
            if not math.isfinite(numeric):
                return None
            return int(numeric)
        try:
            numeric = float(str(value))
        except (TypeError, ValueError):
            return None
        if not math.isfinite(numeric):
            return None
        return int(numeric)

    @staticmethod
    def _context_usage_metrics(payload: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
        context_tokens = WSHandler._coerce_int(payload.get("context_tokens", payload.get("token_count")))
        context_window = WSHandler._coerce_int(payload.get("context_window"))
        remaining_pct = WSHandler._coerce_int(payload.get("context_remaining_pct"))

        if remaining_pct is not None:
            remaining_pct = max(0, min(remaining_pct, 100))

        if context_window is not None and context_window > 0:
            if context_tokens is not None:
                used_tokens = max(0, min(context_tokens, context_window))
                used_pct = int(round((used_tokens / context_window) * 100))
                return used_tokens, max(0, min(used_pct, 100)), context_window
            if remaining_pct is not None:
                used_pct = 100 - remaining_pct
                used_tokens = int(round((used_pct / 100) * context_window))
                return used_tokens, used_pct, context_window

        if remaining_pct is not None:
            return context_tokens, 100 - remaining_pct, context_window

        return context_tokens, None, context_window

    @staticmethod
    def _context_usage_compact_label(payload: dict[str, Any]) -> str | None:
        used_tokens, used_pct, context_window = WSHandler._context_usage_metrics(payload)
        if used_pct is not None:
            return f"{used_pct}% used"
        if used_tokens is not None and context_window is not None and context_window > 0:
            return f"{used_tokens}/{context_window} tokens"
        if used_tokens is not None:
            return f"{used_tokens} tokens"
        return None

    @staticmethod
    def _context_usage_detail(payload: dict[str, Any]) -> str | None:
        used_tokens, used_pct, context_window = WSHandler._context_usage_metrics(payload)
        parts: list[str] = []
        if used_tokens is not None:
            if context_window is not None and context_window > 0:
                parts.append(f"{used_tokens}/{context_window} tokens")
            else:
                parts.append(f"{used_tokens} tokens")
        if used_pct is not None:
            parts.append(f"{used_pct}% used")
        return " | ".join(parts) or None

    @staticmethod
    def _humanize_role_label(role_id: str) -> str:
        normalized = str(role_id or "").strip()
        if not normalized:
            return ""
        if "_" not in normalized and "-" not in normalized and normalized.isalpha() and len(normalized) <= 4:
            return normalized.upper()
        return normalized.replace("_", " ").replace("-", " ").title()

    def _resolve_work_item_role_name(
        self,
        role_id: str,
        metadata: dict[str, Any] | None = None,
        *,
        engine: Any | None = None,
    ) -> str:
        explicit = str((metadata or {}).get("work_item_role_name", "") or "").strip()
        if explicit:
            return explicit

        rid = str(role_id or "").strip()
        if not rid:
            return ""

        runtime_engine = engine or self.engine
        org_engine = getattr(runtime_engine, "org_engine", None)
        get_agent = getattr(org_engine, "get_agent", None)
        if callable(get_agent):
            try:
                agent = get_agent(rid)
            except Exception:
                agent = None
            if isinstance(agent, dict):
                name = agent.get("name")
            else:
                name = getattr(agent, "name", "")
            if isinstance(name, str) and name.strip():
                return name.strip()

        return self._humanize_role_label(rid)

    def _enrich_runtime_progress_payload(self, payload: dict[str, Any], *, engine: Any | None = None) -> dict[str, Any]:
        enriched = dict(payload or {})
        if str(enriched.get("work_item_role_name", "") or "").strip():
            return enriched

        role_id = str(
            enriched.get("role_id")
            or enriched.get("agent_role_id")
            or enriched.get("work_item_role_id")
            or ""
        ).strip()
        role_name = self._resolve_work_item_role_name(role_id, engine=engine)
        if role_name:
            enriched["work_item_role_name"] = role_name
        return enriched

    @staticmethod
    def _runtime_payload_is_task_mode(payload: dict[str, Any]) -> bool:
        execution_mode = str(payload.get("execution_mode", "") or "").strip().lower()
        if execution_mode == "company_mode":
            return False
        if execution_mode in {"task_mode", "task", "project_mode", "project"}:
            return True
        projection_id = str(payload.get("work_item_projection_id", "") or "").strip()
        if projection_id and projection_id != "task_mode_execution":
            return False
        if str(payload.get("company_profile", "") or "").strip():
            return False
        mode = str(payload.get("mode", "") or "").strip().lower()
        runtime_kind = str(payload.get("runtime_kind", "") or "").strip()
        task_mode_contract = str(payload.get("task_mode_contract", "") or "").strip()
        return (
            mode == "task"
            or runtime_kind == "task_mode_agent_turn"
            or task_mode_contract == "single_full_capability_main_agent"
            or projection_id == "task_mode_execution"
        )

    @staticmethod
    def _runtime_event_to_progress_entry(payload: dict[str, Any]) -> dict[str, Any] | None:
        runtime_type = str(payload.get("type", "") or "").strip()
        if not runtime_type:
            return None
        is_task_mode = WSHandler._runtime_payload_is_task_mode(payload)
        if is_task_mode:
            if runtime_type in _TASK_MODE_HIDDEN_RUNTIME_PROGRESS_TYPES:
                return None
            if runtime_type not in _TASK_MODE_VISIBLE_RUNTIME_PROGRESS_TYPES:
                return None
        elif runtime_type in _COMPANY_MODE_HIDDEN_RUNTIME_PROGRESS_TYPES:
            return None

        summary = runtime_type.replace("_", " ").title()
        detail = ""
        entry_type = "status_change"

        if runtime_type == "turn_started":
            entry_type = "work_item_started"
            summary = f"Turn {payload.get('iteration', '?')} started"
        elif runtime_type == "assistant_delta":
            # Company mode only (task mode is filtered out above and already
            # streams assistant text as the draft reply): surface the role's
            # narration and final reply in its progress transcript, matching
            # what external agents get via [External:*:result] parsing.
            entry_type = "assistant"
            detail = str(payload.get("text", "") or "")
            if not detail.strip():
                return None
            preview = " ".join(detail.split())
            summary = preview[:120].rstrip() + ("..." if len(preview) > 120 else "")
        elif runtime_type == "member_idle":
            return None
        elif runtime_type == "thinking_delta":
            entry_type = "thinking"
            # Keep the raw fragment: streaming deltas are token-sized, so
            # stripping them destroys the whitespace between tokens once the
            # fragments are merged back into one entry.
            detail = str(payload.get("text", "") or "")
            if not detail.strip():
                return None
            preview = " ".join(detail.split())
            summary = preview[:120].rstrip() + ("..." if len(preview) > 120 else "")
        elif runtime_type == "member_claimed_work_item":
            entry_type = "work_item_started"
            priority = str(payload.get("message_priority", "") or "").strip().lower()
            if priority == "manager":
                summary = "Work item resumed"
                detail = "Claimed from manager queue."
            else:
                summary = "Work item started"
                if priority:
                    detail = f"Claimed from {priority.replace('_', ' ')} queue."
        elif runtime_type == "tool_started":
            entry_type = "tool_call"
            summary = str(payload.get("tool_name", "") or "tool")
            if payload.get("arguments"):
                try:
                    detail = json.dumps(payload.get("arguments", {}), ensure_ascii=False, default=str)
                except TypeError:
                    detail = str(payload.get("arguments"))
        elif runtime_type == "tool_progress":
            entry_type = "tool_call"
            summary = str(payload.get("tool_name", "") or "tool")
            detail = str(payload.get("text", "") or payload.get("message", "") or "").strip()
        elif runtime_type == "tool_completed":
            entry_type = "tool_call"
            summary = str(payload.get("tool_name", "") or "tool")
            detail = str(payload.get("result_summary", "") or payload.get("result_preview", "") or "").strip()
        elif runtime_type == "status_snapshot":
            entry_type = "status_change"
            current_tool = str(payload.get("current_tool", "") or "").strip()
            turn_cost = payload.get("turn_cost_usd")
            pieces: list[str] = []
            if current_tool:
                pieces.append(f"tool={current_tool}")
            context_label = WSHandler._context_usage_compact_label(payload)
            if context_label:
                pieces.append(f"context={context_label}")
            if turn_cost not in (None, ""):
                pieces.append(f"turn=${float(turn_cost):.4f}")
            summary = "Runtime status"
            detail = " | ".join(pieces)
        elif runtime_type in {"permission_requested", "permission_resolved"}:
            entry_type = "autonomy"
            target = str(payload.get("tool_name", "") or "tool").strip()
            resolution = str(payload.get("resolution", payload.get("predicted_permission", "")) or "").strip()
            summary = f"{target}: {resolution or runtime_type.replace('_', ' ')}".strip(": ")
            detail = str(payload.get("rationale", "") or "").strip()
        elif runtime_type == "cost_update":
            entry_type = "status_change"
            summary = "Cost update"
            detail = (
                f"turn=${float(payload.get('turn_cost_usd', 0.0) or 0.0):.4f} "
                f"session=${float(payload.get('session_cost_usd', 0.0) or 0.0):.4f}"
            )
        elif runtime_type == "context_usage":
            entry_type = "status_change"
            summary = "Context usage"
            detail = WSHandler._context_usage_detail(payload) or "Context usage updated"
        elif runtime_type == "context_warning":
            entry_type = "status_change"
            summary = "Context usage high"
            detail = WSHandler._context_usage_detail(payload) or "Context window nearly full"
        elif runtime_type in {"subagent_started", "subagent_updated", "subagent_completed"}:
            if is_task_mode and str(payload.get("profile", "") or "").strip() == "verify":
                entry_type = "verification"
                profile = str(payload.get("profile", "") or "verify").strip()
                summary = f"{profile}: {runtime_type.replace('_', ' ')}"
                detail = (
                    str(payload.get("content_preview", "") or "").strip()
                    or str(payload.get("message", "") or "").strip()
                    or str(payload.get("status", "") or "").strip()
                )
            else:
                entry_type = "handoff"
                profile = str(payload.get("profile", "") or "subagent").strip()
                summary = f"{profile}: {runtime_type.replace('_', ' ')}"
                detail = (
                    str(payload.get("content_preview", "") or "").strip()
                    or str(payload.get("message", "") or "").strip()
                    or str(payload.get("status", "") or "").strip()
                )
        elif runtime_type == "member_inbox_updated":
            entry_type = "status_change"
            summary = "Resident inbox updated"
            pieces = [
                f"chat={int(payload.get('actionable_inbox_count', 0) or 0)}",
                f"protocol={int(payload.get('protocol_backlog_count', 0) or 0)}",
                f"notifications={int(payload.get('notification_backlog_count', 0) or 0)}",
            ]
            resident_status = str(payload.get("resident_status", "") or "").strip()
            if resident_status:
                pieces.append(f"status={resident_status}")
            detail = " | ".join(pieces)
        elif runtime_type == "worker_notification":
            notification_kind = str(payload.get("notification_kind", "") or "update").strip() or "update"
            if notification_kind == "blocked":
                entry_type = "awaiting_peer"
            elif notification_kind == "error":
                entry_type = "work_item_failed"
            elif notification_kind in {"task_complete", "handoff_ready"}:
                entry_type = "handoff"
            else:
                entry_type = "status_change"
            worker_label = (
                str(payload.get("name", "") or "").strip()
                or str(payload.get("worker_type", "") or "worker").strip().replace("_", " ")
            )
            summary = f"{worker_label}: {notification_kind.replace('_', ' ')}".strip(": ")
            detail = str(payload.get("summary", "") or "").strip()
        elif runtime_type == "compaction_applied":
            if is_task_mode and runtime_type in _TASK_MODE_DEBUG_ONLY_PROGRESS_TYPES:
                return None
            entry_type = "status_change"
            summary = "Context compacted"
            detail = f"message_count={payload.get('message_count', '')}".strip()
        elif runtime_type in {"verification_started", "verification_repair_requested", "verification_completed"}:
            entry_type = "verification" if is_task_mode else "status_change"
            summary = runtime_type.replace("_", " ").title()
            detail = (
                str(payload.get("verdict", "") or "").strip()
                or str(payload.get("reason", "") or "").strip()
                or str(payload.get("profile", "") or "").strip()
            )
        elif runtime_type == "checkpoint_saved":
            if is_task_mode:
                entry_type = "needs_input"
                summary = "Needs input"
                detail = str(payload.get("checkpoint_type", "") or "").strip()
            else:
                review_level = str(payload.get("review_level", "") or "").strip().lower()
                entry_type = "awaiting_manager_review" if review_level == "manager" else "awaiting_human"
                review_target = str(payload.get("review_target_role_id", "") or "").strip()
                summary = (
                    f"Awaiting {review_target or 'manager'} review"
                    if review_level == "manager"
                    else "Awaiting human review"
                )
                detail = str(payload.get("checkpoint_type", "") or "").strip()
        elif runtime_type == "turn_completed":
            entry_type = "gate_approved"
            summary = f"Turn {payload.get('iteration', '?')} completed"
            detail = str(payload.get("content_preview", "") or "").strip()
        elif runtime_type == "turn_failed":
            entry_type = "work_item_failed"
            summary = f"Turn {payload.get('iteration', '?')} failed"
            detail = str(payload.get("message", "") or "").strip()

        entry: dict[str, Any] = {
            "type": entry_type,
            "summary": summary[:160] if summary else runtime_type,
            "detail": detail[:4000] if detail else None,
        }
        tool_call_id = str(payload.get("tool_call_id", "") or "").strip()
        if tool_call_id and entry_type in {"tool_call", "autonomy"}:
            turn_id = str(payload.get("turn_id", "") or "").strip()
            prefix = "permission" if entry_type == "autonomy" else "tool"
            entry.setdefault("item_id", f"{turn_id}:{prefix}:{tool_call_id}" if turn_id else f"{prefix}:{tool_call_id}")
            entry.setdefault("stream_id", entry["item_id"])
            entry["tool_call_id"] = tool_call_id
        permission_group_key = str(payload.get("permission_group_key", "") or "").strip()
        if permission_group_key:
            entry["permission_group_key"] = permission_group_key
        for alias_key in ("turn_id", "item_id", "stream_id", "seq", "execution_mode"):
            if alias_key in payload and payload.get(alias_key) not in (None, ""):
                entry[alias_key] = payload.get(alias_key)
        work_item_projection_id = str(payload.get("work_item_projection_id") or "").strip()
        if is_task_mode and work_item_projection_id == "task_mode_execution":
            work_item_projection_id = ""
        work_item_turn_type = str(
            payload.get("work_item_turn_type")
            or payload.get("turn_type")
            or ""
        ).strip()
        work_item_projection_title = str(payload.get("work_item_projection_title", "") or "").strip()
        work_item_role_name = str(payload.get("work_item_role_name") or payload.get("role_name") or "").strip()
        if not work_item_role_name:
            role_id = str(
                payload.get("role_id")
                or payload.get("agent_role_id")
                or payload.get("work_item_role_id")
                or ""
            ).strip()
            work_item_role_name = WSHandler._humanize_role_label(role_id)
        if not is_task_mode and (work_item_projection_id or work_item_projection_title):
            entry.update(
                work_item_identity_payload(
                    projection_id=work_item_projection_id or work_item_projection_title,
                    turn_type=work_item_turn_type,
                )
            )
            entry["work_item_projection_title"] = (
                work_item_role_name
                or work_item_projection_title
                or work_item_projection_id.replace("_", " ").title()
            )
            entry["is_company_runtime"] = True
        if work_item_role_name:
            entry["role_name"] = work_item_role_name
        return entry

    @staticmethod
    def _should_store_progress_message(text: str) -> bool:
        """Route high-signal progress into chat/activity without flooding it."""
        if len(text) <= 10:
            return False
        # Plain-text progress is used for native agent final replies; the
        # authoritative assistant message comes from transcript sync. Storing
        # it here creates a duplicate system-colored chat bubble.
        if not text.startswith("["):
            return False
        if text.startswith(("[Tool:", "[Autonomy]", "[Cost:", "[Token", "[CapabilityRecovery]")):
            return False
        if text.startswith("[External:"):
            return False
        if text.startswith("[External status]"):
            lowered = text.lower()
            return (
                "started pid=" in lowered
                or "timed out" in lowered
                or "cancelled" in lowered
            )
        return True

    def _related_parent_task_ids(self, task: Any) -> list[str]:
        task_id = str(getattr(task, "id", "") or "").strip()
        metadata = dict(getattr(task, "metadata", {}) or {})
        related: list[str] = []

        for candidate in (
            self._active_runtime_children.get(task_id),
            metadata.get("origin_task_id"),
            self._session_to_task.get(str(getattr(task, "parent_session_id", "") or "").strip()),
        ):
            resolved = str(candidate or "").strip()
            if resolved and resolved != task_id and resolved not in related:
                related.append(resolved)
        return related

    def _ensure_task_display_num(self, task_id: str) -> int:
        existing = self.event_adapter._task_display_map.get(task_id)
        if existing is not None:
            return existing
        self.event_adapter._task_display_counter += 1
        self.event_adapter._task_display_map[task_id] = self.event_adapter._task_display_counter
        return self.event_adapter._task_display_counter

    async def _materialize_runtime_task_visibility(
        self,
        payload: dict[str, Any],
        *,
        engine: Any | None = None,
        project_id: str | None = None,
    ) -> None:
        """Ensure runtime-only company events still materialize a live UI session.

        Some company-mode tasks are first surfaced through runtime events such as
        ``member_session_started`` instead of the dedicated ``child_session_created``
        path. Without an explicit session/board broadcast, the frontend receives
        progress updates for an unknown task and cannot render the execution tree
        until a later full ``collab_sync`` rebuild.
        """
        runtime_engine = engine or self.engine
        task_id = str(payload.get("task_id", "") or "").strip()
        if not task_id or not self._store_is_ready(runtime_engine.store):
            return

        try:
            task = await runtime_engine.store.get_task(task_id)
        except Exception:
            task = None
        if task is None:
            return

        session_id = str(getattr(task, "session_id", "") or "").strip()
        if not session_id:
            return

        metadata = dict(getattr(task, "metadata", {}) or {})
        project_id = self._normalize_project_id(
            getattr(task, "project_id", "") or project_id or getattr(runtime_engine, "project_id", None)
        )
        ui_task_id = self._ui_task_id_for_task(task) or task_id
        ui_task = task
        if ui_task_id != task_id and self._store_is_ready(runtime_engine.store):
            try:
                resolved = await runtime_engine.store.get_task(ui_task_id)
            except Exception:
                resolved = None
            if resolved is not None:
                ui_task = resolved
        title = str(getattr(ui_task, "title", "") or getattr(task, "title", "") or payload.get("title") or "Session").strip() or "Session"
        channel_id = f"session:{ui_task_id}"
        try:
            existing_channels = await self.chat_store.get_session_channels(project_id)
        except Exception:
            existing_channels = []
        channel = next(
            (
                channel
                for channel in existing_channels
                if str(channel.get("channel_id", "") or "").strip() == channel_id
            ),
            None,
        )
        channel_already_materialized = channel is not None
        if channel is None:
            channel = await self.chat_store.create_session_channel(ui_task_id, title, project_id=project_id)
        self._session_to_task[session_id] = ui_task_id
        if channel_already_materialized:
            return

        role_id = str(getattr(task, "assigned_to", "") or metadata.get("work_item_role_id", "") or "").strip()
        assignee_ids = [self.event_adapter._resolve_role_to_agent(role_id)] if role_id else []
        work_item_role_name = str(metadata.get("work_item_role_name", "") or "").strip()
        if not work_item_role_name and role_id:
            work_item_role_name = self._resolve_work_item_role_name(role_id, metadata, engine=runtime_engine)
        shared_role_session = bool(metadata.get("shared_role_session", False))

        parent_session_id = str(
            getattr(task, "parent_session_id", "")
            or metadata.get("parent_session_id", "")
            or ""
        ).strip() or None
        if shared_role_session:
            parent_session_id = None
        origin_task_id = str(
            metadata.get("origin_task_id", "")
            or self._session_to_task.get(str(parent_session_id or ""))
            or ui_task_id
        ).strip() or ui_task_id

        display_num = self._ensure_task_display_num(ui_task_id)
        created_at = channel.get("created_at") if isinstance(channel, dict) else None
        if not isinstance(created_at, (int, float)):
            raw_created_at = getattr(ui_task, "created_at", None) or getattr(task, "created_at", None)
            created_at = raw_created_at.timestamp() if hasattr(raw_created_at, "timestamp") else time.time()

        status = getattr(getattr(task, "status", None), "value", str(getattr(task, "status", "pending")))
        exec_mode, company_profile = self._resolve_task_session_config(task)
        preferred_agent = self._resolve_task_preferred_agent(task)
        selected_execution_agent = self._resolve_task_selected_execution_agent(task)
        work_item_projection_id = work_item_projection_id_from_metadata(metadata)
        work_item_turn_type = work_item_turn_type_from_metadata(metadata, fallback="")
        work_item_identity = {
            **work_item_identity_payload(projection_id=work_item_projection_id, turn_type=work_item_turn_type),
            "work_item_role_id": role_id or None,
            "work_item_role_name": work_item_role_name or None,
            "employee_assignment": metadata.get("employee_assignment"),
            "origin_task_id": origin_task_id,
            "selected_execution_agent": selected_execution_agent,
        }
        execution_aliases = _add_execution_turn_aliases({}, task_id)

        if parent_session_id:
            await self.broadcast({
                "type": "child_session_created",
                "payload": {
                    "project_id": project_id,
                    "session_id": session_id,
                    "parent_session_id": parent_session_id,
                    "task_id": ui_task_id,
                    **execution_aliases,
                    "origin_task_id": origin_task_id,
                    "title": title,
                    "agent_id": assignee_ids[0] if assignee_ids else None,
                    **work_item_identity,
                },
            })

        if not parent_session_id:
            await self.broadcast({
                "type": "board_task_created",
                "payload": {
                    "project_id": project_id,
                    "task_id": ui_task_id,
                    **execution_aliases,
                    "display_id": f"OPC-{display_num}",
                    "board_id": project_id,
                    "title": title,
                    "assignee_ids": assignee_ids,
                    **work_item_identity,
                },
            })
        await self.broadcast({
            "type": "session_created",
            "payload": {
                "project_id": project_id,
                "task_id": ui_task_id,
                **execution_aliases,
                "channel_id": channel_id,
                "session_id": session_id,
                "parent_session_id": parent_session_id,
                "origin_task_id": origin_task_id,
                "exec_mode": exec_mode,
                "company_profile": company_profile,
                "preferred_agent": preferred_agent,
                "selected_execution_agent": selected_execution_agent,
                "title": title,
                "status": status,
                "created_at": created_at,
                "assignee_ids": assignee_ids,
                **work_item_identity,
            },
        })

    async def _ensure_reply_projected(
        self,
        *,
        channel_id: str,
        project_id: str,
        session_id: str | None,
        engine: Any | None = None,
    ) -> None:
        """Last-resort invariant: the session's newest persisted top-level reply
        must exist in the UI channel once the turn has unwound.

        The transcript sync is the normal projection path; when it is starved,
        cancelled, or misses the row (project 000, 2026-07-07 19:21/20:27), the
        engine has replied but the user sees an empty conversation forever.
        Detection is by the transcript message id, so an already-projected reply
        (any channel) is never duplicated.
        """
        if not session_id:
            return
        runtime_engine = engine or self.engine
        store = getattr(runtime_engine, "store", None)
        if not self._store_is_ready(store):
            return
        lister = getattr(store, "list_session_messages", None)
        parts_loader = getattr(store, "list_session_parts", None)
        if not callable(lister) or not callable(parts_loader):
            return
        try:
            records = await lister(session_id)
        except Exception:
            logger.opt(exception=True).debug("reply projection: failed to list session messages")
            return
        latest = None
        for record in reversed(records or []):
            if str(getattr(record, "role", "") or "").strip().lower() != "assistant":
                continue
            metadata = dict(getattr(record, "metadata", {}) or {})
            if str(metadata.get("kind", "") or "").strip() != "top_level_reply":
                continue
            latest = record
            break
        if latest is None:
            return
        message_id = str(getattr(latest, "message_id", "") or "").strip()
        if not message_id:
            return
        try:
            if await self.chat_store.message_scope(message_id) is not None:
                return
        except Exception:
            return
        try:
            parts = await parts_loader(session_id, message_id)
        except Exception:
            logger.opt(exception=True).debug("reply projection: failed to load reply parts")
            return
        text = "\n".join(
            chunk
            for part in parts or []
            if str(getattr(part, "part_type", "") or "") == "text"
            for chunk in [str(dict(getattr(part, "payload", {}) or {}).get("text", "") or "")]
            if chunk
        ).strip()
        if not text:
            return
        logger.warning(
            f"Top-level reply {message_id} missing from UI channel {channel_id} after "
            "transcript sync; projecting it directly"
        )
        reply_metadata = dict(getattr(latest, "metadata", {}) or {})
        reply_metadata.setdefault("kind", "top_level_reply")
        reply_metadata.setdefault("source", "engine")
        reply_metadata.setdefault("ui_message_id", message_id)
        reply_metadata["reply_projection_fallback"] = True
        msg = await self.chat_store.insert_message(
            channel_id=channel_id,
            sender="assistant",
            sender_name="OPC",
            content=text,
            project_id=project_id,
            metadata=reply_metadata,
            message_id=message_id,
        )
        await self.broadcast({"type": "session_message", "payload": msg})

    async def _sync_task_transcript_messages(
        self,
        task_id: str,
        *,
        engine: Any | None = None,
        broadcast: bool = True,
        detail_level: str = "summary",
        latest_assistant_metadata: dict[str, Any] | None = None,
    ) -> int:
        """Backfill persisted transcript messages into chat_store and UI."""
        runtime_engine = engine or self.engine
        store = runtime_engine.store
        if not self._store_is_ready(store):
            return 0

        task = await store.get_task(task_id)
        if not task:
            return 0

        transcript_loader = getattr(store, "get_session_transcript", None)
        session_id = str(getattr(task, "session_id", "") or "").strip()
        if not callable(transcript_loader) or not session_id:
            return 0

        project_id = getattr(task, "project_id", None) or runtime_engine.project_id or "default"
        ui_task_id = self._ui_task_id_for_task(task) or task_id
        ui_task = task
        if ui_task_id != task_id:
            try:
                resolved = await store.get_task(ui_task_id)
            except Exception:
                resolved = None
            if resolved is not None:
                ui_task = resolved
        channel_id = f"session:{ui_task_id}"
        await self.chat_store.create_session_channel(
            ui_task_id,
            getattr(ui_task, "title", "") or getattr(task, "title", "") or "Session",
            project_id=project_id,
        )

        try:
            transcript = await transcript_loader(session_id)
        except Exception:
            transcript = []

        formatted_messages = build_transcript_ui_messages(
            transcript,
            channel_id=channel_id,
            task_id=ui_task_id,
            detail_level=_normalize_transcript_detail_level(detail_level),
        )

        if latest_assistant_metadata:
            latest_assistant_metadata = (
                latest_assistant_metadata
                if self._checkpoint_metadata_targets_task(latest_assistant_metadata, task)
                else None
            )

        attached_latest_metadata = False
        if latest_assistant_metadata:
            for message in reversed(formatted_messages):
                if str(message.get("sender", "") or "").strip().lower() == "user":
                    continue
                if not self._message_can_host_checkpoint_metadata(message, latest_assistant_metadata):
                    continue
                metadata = dict(message.get("metadata", {}) or {})
                metadata.update(latest_assistant_metadata)
                message["metadata"] = metadata
                attached_latest_metadata = True
                break
            if not attached_latest_metadata:
                checkpoint_id = str(latest_assistant_metadata.get("checkpoint_id", "") or "").strip()
                synthetic_message_id = f"checkpoint::{checkpoint_id}" if checkpoint_id else str(uuid.uuid4())
                formatted_messages.append({
                    "message_id": synthetic_message_id,
                    "sender": "assistant",
                    "sender_name": str(
                        latest_assistant_metadata.get("work_item_projection_title")
                        or latest_assistant_metadata.get("requesting_role_id")
                        or "Company Member"
                    ),
                    "content": str(
                        latest_assistant_metadata.get("prompt")
                        or latest_assistant_metadata.get("summary")
                        or "Human review requested."
                    ),
                    "timestamp": time.time(),
                    "reply_to_id": None,
                    "mentions": [],
                    "metadata": dict(latest_assistant_metadata),
                })

        inserted_messages = await self.chat_store.backfill_messages(
            channel_id,
            formatted_messages,
            project_id=project_id,
        )
        if broadcast and inserted_messages:
            for message in inserted_messages:
                await self.broadcast({"type": "session_message", "payload": {
                    "project_id": project_id,
                    "message_id": message["message_id"],
                    "channel_id": channel_id,
                    "sender": message["sender"],
                    "sender_name": message["sender_name"],
                    "content": message["content"],
                    "created_at": message["timestamp"],
                    "reply_to_id": message.get("reply_to_id"),
                    "mentions": message.get("mentions", []),
                    "metadata": message.get("metadata", {}),
                }})
        return len(inserted_messages)

    @staticmethod
    def _checkpoint_metadata_targets_task(metadata: dict[str, Any], task: Any) -> bool:
        if str((metadata or {}).get("checkpoint_type", "") or "").strip() != "company_delivery_feedback":
            return True
        target_task_id = str(
            (metadata or {}).get("waiting_task_id")
            or (metadata or {}).get("task_id")
            or ""
        ).strip()
        task_id = str(getattr(task, "id", "") or "").strip()
        return not target_task_id or not task_id or target_task_id == task_id

    @staticmethod
    def _message_can_host_checkpoint_metadata(
        message: dict[str, Any],
        checkpoint_metadata: dict[str, Any],
    ) -> bool:
        if str((checkpoint_metadata or {}).get("checkpoint_type", "") or "").strip() != "company_delivery_feedback":
            return True
        metadata = dict(message.get("metadata", {}) or {})
        if str(message.get("sender", "") or "").strip().lower() == "system":
            return False
        if str(metadata.get("kind", "") or "").strip() == "worker_notification":
            return False
        if str(metadata.get("transcript_kind", "") or "").strip() == "child_result":
            return False
        return True

    # ══════════════════════════════════════════════════════════════════════
    # Inbound routing
    # ══════════════════════════════════════════════════════════════════════

    async def _route_message(self, ws: Any, raw: str) -> None:
        """Parse and route an incoming WS message."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        # ``json.loads`` succeeds for non-object frames (null/number/array/string);
        # ``data.get`` would then raise AttributeError, escape this method, and drop the
        # whole WS connection. Ignore anything that is not a JSON object.
        if not isinstance(data, dict):
            return

        msg_type = data.get("type", "")
        if self._shutting_down:
            logger.debug(f"Ignoring WS message during shutdown: {msg_type}")
            return
        handler = self._HANDLERS.get(msg_type)
        handoff_registry = None
        handoff_token: str | None = None
        if handler and msg_type in _EXECUTION_HANDOFF_MESSAGE_TYPES:
            handoff_registry = self._controller_active_task_run_registry()
            if handoff_registry is not None:
                try:
                    handoff_token = handoff_registry.reserve_handoff()
                except ActiveTaskRunAdmissionClosed:
                    await self._send_ack(
                        ws,
                        ok=False,
                        error="service_shutting_down",
                        action=msg_type,
                    )
                    return
        if handler:
            current_task = asyncio.current_task()
            if current_task is not None:
                self._active_message_tasks.add(current_task)
                if handoff_token is not None:
                    handoff_routes = getattr(self, "_handoff_route_tasks", None)
                    if not isinstance(handoff_routes, dict):
                        handoff_routes = {}
                        self._handoff_route_tasks = handoff_routes
                    handoff_routes[current_task] = handoff_token
            try:
                if handoff_registry is not None and handoff_token is not None:
                    with handoff_registry.bind_handoff(handoff_token):
                        await handler(self, ws, data)
                else:
                    await handler(self, ws, data)
            except Exception as e:
                if self._is_ws_disconnect_error(e) or self._is_expected_shutdown_error(e):
                    logger.debug(
                        f"WS handler closed for {msg_type} during disconnect/shutdown: "
                        f"{type(e).__name__}: {e!r}"
                    )
                elif isinstance(e, ProjectScopeError):
                    logger.warning(
                        "Rejected project-scoped WS request without project_id: type={} keys={} project_id={!r} projectId={!r}",
                        msg_type,
                        sorted(str(key) for key in data.keys()),
                        data.get("project_id"),
                        data.get("projectId"),
                    )
                    try:
                        await self._send_ack(ws, ok=False, error=str(e), action=msg_type)
                    except Exception:
                        pass
                else:
                    logger.opt(exception=True).error(f"WS handler error for {msg_type}: {type(e).__name__}: {e!r}")
                    try:
                        await self._send_ack(ws, ok=False, error=str(e) or type(e).__name__, action=msg_type)
                    except Exception:
                        pass  # WS may already be closed
            finally:
                if handoff_registry is not None and handoff_token is not None:
                    handoff_registry.release_handoff(handoff_token)
                if current_task is not None:
                    self._active_message_tasks.discard(current_task)
                    handoff_routes = getattr(self, "_handoff_route_tasks", None)
                    if isinstance(handoff_routes, dict):
                        handoff_routes.pop(current_task, None)
        else:
            logger.debug(f"Unknown WS message type: {msg_type}")
            await self._send_ack(
                ws,
                ok=False,
                error="unknown_message_type",
                action=msg_type,
            )

    # ── Sync ──────────────────────────────────────────────────────────

    async def _handle_ping(self, ws: Any, data: dict) -> None:
        await ws.send_json({"type": "pong"})

    async def _send_project_index_for_client(
        self,
        ws: Any,
        engine: Any,
        project_id: str,
        *,
        switch_seq: str = "",
        view_generation: Any = None,
        include_snapshot: bool = False,
        send_error_ack: bool = True,
    ) -> None:
        try:
            index_payload = await build_project_index_sync(
                engine,
                self.agent_store,
                self.chat_store,
                self.event_adapter,
                exec_mode=self._exec_mode,
            )
            index_payload["project_id"] = project_id
            index_payload["switch_seq"] = switch_seq
            if view_generation is not None:
                index_payload["view_generation"] = view_generation

            if self._client_active_project_id(ws) != project_id:
                return
            if switch_seq and self._client_switch_seq.get(ws, "") != switch_seq:
                return
            await self._send_envelope_to_client(
                ws,
                {"type": "project_index_push", "payload": index_payload},
            )

            if not include_snapshot:
                return
            try:
                snapshot = await build_snapshot(
                    engine,
                    self.agent_store,
                    self.chat_store,
                    self.event_adapter,
                )
                snapshot["project_id"] = project_id
                snapshot["exec_mode"] = self._exec_mode
                snapshot["company_profile"] = self._company_profile
                snapshot["task_preferred_agent"] = self._task_preferred_agent
                snapshot["switch_seq"] = switch_seq
                if view_generation is not None:
                    snapshot["view_generation"] = view_generation
                if self._client_active_project_id(ws) != project_id:
                    return
                if switch_seq and self._client_switch_seq.get(ws, "") != switch_seq:
                    return
                await self._send_envelope_to_client(ws, {"type": "snapshot", "payload": snapshot})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.opt(exception=True).warning(
                    f"Project index sent, but snapshot refresh failed for {project_id}: {type(exc).__name__}: {exc!r}",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.opt(exception=True).error(
                f"Failed to build project index for {project_id}: {type(exc).__name__}: {exc!r}",
            )
            if send_error_ack and self._client_active_project_id(ws) == project_id:
                await self._send_ack(
                    ws,
                    ok=False,
                    action="project_index",
                    project_id=project_id,
                    switch_seq=switch_seq,
                    error=f"Project index failed: {exc}",
                )

    async def _send_initial_project_state_for_client(
        self,
        ws: Any,
        engine: Any,
        project_id: str,
    ) -> None:
        """Send the full reconnect baseline for a newly opened websocket."""
        await self._send_project_index_for_client(
            ws,
            engine,
            project_id,
            send_error_ack=False,
        )
        if self._client_active_project_id(ws) != project_id:
            return
        try:
            collab = await build_collab_sync(
                engine,
                self.agent_store,
                self.chat_store,
                self.event_adapter,
                exec_mode=self._exec_mode,
            )
            collab["project_id"] = project_id
            if self._client_active_project_id(ws) == project_id:
                await self._send_envelope_to_client(ws, {"type": "collab_sync_push", "payload": collab})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.opt(exception=True).warning("Initial websocket collab_sync push failed")
        try:
            org_info = await self._build_org_info_payload()
            if self._client_active_project_id(ws) == project_id:
                await self._send_envelope_to_client(ws, {"type": "org_info", "payload": org_info})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.opt(exception=True).warning("Initial websocket org_info push failed")

    async def _handle_collab_sync(self, ws: Any, data: dict) -> None:
        engine, project_id = await self._engine_for_request(data)
        result = await build_collab_sync(
            engine,
            self.agent_store,
            self.chat_store,
            self.event_adapter,
            exec_mode=self._exec_mode,
        )
        result["ok"] = True
        result["project_id"] = project_id
        if data.get("switch_seq") or data.get("switchSeq"):
            result["switch_seq"] = str(data.get("switch_seq") or data.get("switchSeq") or "")
        if data.get("view_generation") is not None:
            result["view_generation"] = data.get("view_generation")
        await ws.send_json({"type": "ack", "payload": result})

    async def _handle_project_index(self, ws: Any, data: dict) -> None:
        engine, project_id = await self._engine_for_request(data)
        switch_seq = str(data.get("switch_seq") or data.get("switchSeq") or "").strip()
        view_generation = data.get("view_generation")
        self._track_client_project_index(
            ws,
            self._send_project_index_for_client(
                ws,
                engine,
                project_id,
                switch_seq=switch_seq,
                view_generation=view_generation,
                include_snapshot=bool(data.get("include_snapshot") or data.get("includeSnapshot")),
            ),
        )
        await self._send_ack(
            ws,
            ok=True,
            action="project_index",
            project_id=project_id,
            switch_seq=switch_seq,
        )

    # ── Chat ──────────────────────────────────────────────────────────

    # ── Kanban ────────────────────────────────────────────────────────

    async def _handle_kanban_create_board(self, ws: Any, data: dict) -> None:
        # We use one board per project; accept for protocol compatibility
        _engine, project_id = await self._engine_for_request(data)
        data = {**data, "project_id": project_id}
        await self._send_ack(ws, ok=True)
        await self.broadcast({"type": "kanban_board_created", "payload": data})

    async def _handle_kanban_create_task(self, ws: Any, data: dict) -> None:
        try:
            _engine, pid = await self._engine_for_request(data)
            result = await self.services.kanban.create_task(
                project_id=pid,
                title=data.get("title", "Untitled"),
                description=data.get("description", ""),
                task_id=data.get("task_id"),
                board_id=data.get("board_id", pid),
                assignee_ids=data.get("assignee_ids", []),
            )
            self._session_to_task.update(self.services_context.session_to_task)
            await self._publish_service_result(result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="kanban_create_task")

    async def _handle_kanban_update_task(self, ws: Any, data: dict) -> None:
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self.services.kanban.update_task(
                project_id=project_id,
                task_id=data.get("task_id", ""),
                updates=data.get("updates", {}),
            )
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, **result.payload)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="kanban_update_task")

    async def _handle_kanban_move_task(self, ws: Any, data: dict) -> None:
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self.services.kanban.move_task(
                project_id=project_id,
                task_id=data.get("task_id", ""),
                column_id=data.get("column_id", ""),
            )
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, **result.payload)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="kanban_move_task")

    async def _handle_kanban_delete_task(self, ws: Any, data: dict) -> None:
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self.services.kanban.delete_task(project_id=project_id, task_id=data.get("task_id", ""))
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, **result.payload)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="kanban_delete_task")

    async def _handle_kanban_delete_board(self, ws: Any, data: dict) -> None:
        # No-op for OPC's project-based boards
        _engine, project_id = await self._engine_for_request(data)
        await self._send_ack(ws, ok=True, project_id=project_id)

    async def _handle_kanban_assign(self, ws: Any, data: dict) -> None:
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self.services.kanban.assign(
                project_id=project_id,
                task_id=data.get("task_id", ""),
                agent_id=data.get("agent_id", ""),
            )
            await self._send_ack(ws, ok=True, **result.payload)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="kanban_assign")

    async def _handle_kanban_status(self, ws: Any, data: dict) -> None:
        # Alias: convert status to column_id and delegate
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self.services.kanban.status(
                project_id=project_id,
                task_id=data.get("task_id", ""),
                status=data.get("status", data.get("column_id", "")),
            )
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, **result.payload)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="kanban_status")

    async def _handle_kanban_switch_view(self, ws: Any, data: dict) -> None:
        """Return filtered kanban data for global/office/agent view levels."""
        from opc.plugins.office_ui.snapshot_builder import build_company_kanban_projection, task_to_kanban
        level = data.get("level", "global")
        target_id = data.get("target_id")
        run_engine, project_id = await self._engine_for_request(data)

        tasks: list[Any] = []
        if run_engine.store:
            try:
                tasks = await run_engine.store.get_tasks(project_id=project_id)
            except Exception:
                logger.warning("Failed to load tasks for kanban_switch_view")

        company_projection_tasks: list[dict[str, Any]] = []
        company_columns: list[dict[str, Any]] = []
        company_boards: list[dict[str, Any]] = []
        if run_engine.store:
            try:
                company_projection_tasks, company_columns, company_boards, _ = await build_company_kanban_projection(
                    run_engine,
                    project_id=project_id,
                    tasks=tasks,
                    event_adapter=self.event_adapter,
                )
            except Exception:
                logger.opt(exception=True).warning("Failed to load company kanban projection for kanban_switch_view")
        if self._exec_mode in {"company", "org", "custom"} and not company_columns:
            company_boards = [{
                "board_id": project_id,
                "name": project_id if project_id != "default" else "Main Board",
                "prefix": "OPC",
                "color": "#4f46e5",
                "next_task_num": 1,
                "created_at": time.time(),
                "updated_at": time.time(),
            }]
            company_columns = build_company_board_columns(project_id)

        if company_columns:
            filtered_tasks = list(company_projection_tasks)
            if level == "agent" and target_id:
                agent = await self.agent_store._get_one(target_id)
                role_id = agent.get("opc_role_id", target_id) if agent else target_id
                filtered_tasks = [
                    task for task in filtered_tasks
                    if str(task.get("work_item_role_id", "") or "").strip() == str(role_id or "").strip()
                ]
            elif level == "office" and target_id and getattr(run_engine, "org_engine", None) is not None:
                office_role_ids = {
                    str(agent.get("opc_role_id", agent.get("agent_id", "")) or "").strip()
                    for agent in await self.agent_store.get_all()
                    if str(agent.get("office_id", "") or "").strip() == str(target_id or "").strip()
                }
                filtered_tasks = [
                    task for task in filtered_tasks
                    if str(task.get("work_item_role_id", "") or "").strip() in office_role_ids
                ]
            boards = list(company_boards)
            if boards:
                counts_by_board: dict[str, int] = {}
                for task in filtered_tasks:
                    board_id = str(task.get("board_id", "") or "").strip()
                    if not board_id:
                        continue
                    counts_by_board[board_id] = counts_by_board.get(board_id, 0) + 1
                for board in boards:
                    board_id = str(board.get("board_id", "") or "").strip()
                    board["next_task_num"] = int(counts_by_board.get(board_id, 0) or 0) + 1
            await ws.send_json({"type": "kanban_view_data", "payload": {
                "project_id": project_id,
                "boards": boards,
                "columns": company_columns,
                "tasks": filtered_tasks,
                "work_item_projections": [],
            }})
            return

        # Filter tasks by view level (task.assigned_to stores opc_role_id)
        if level == "agent" and target_id:
            agent = await self.agent_store._get_one(target_id)
            role_id = agent.get("opc_role_id", target_id) if agent else target_id
            tasks = [t for t in tasks if t.assigned_to == role_id]
        elif level == "office" and target_id:
            agents = await self.agent_store.get_all()
            office_role_ids = {a.get("opc_role_id", a["agent_id"]) for a in agents if a.get("office_id") == target_id}
            tasks = [t for t in tasks if t.assigned_to in office_role_ids]

        formatted_tasks = [task_to_kanban(t, i + 1, self.event_adapter) for i, t in enumerate(tasks)]

        # Board and columns (same structure as collab_sync)
        now = time.time()
        boards = [{
            "board_id": project_id,
            "name": project_id if project_id != "default" else "Main Board",
            "prefix": "OPC",
            "color": "#4f46e5",
            "next_task_num": len(tasks) + 1,
            "created_at": now,
            "updated_at": now,
        }]
        columns = [
            {"column_id": "todo", "board_id": project_id, "name": "Todo",
             "color": "#6b7280", "sort_order": 0, "is_terminal": False},
            {"column_id": "in-progress", "board_id": project_id, "name": "In Progress",
             "color": "#eab308", "sort_order": 1, "is_terminal": False},
            {"column_id": "done", "board_id": project_id, "name": "Done",
             "color": "#22c55e", "sort_order": 2, "is_terminal": True},
        ]
        await ws.send_json({"type": "kanban_view_data", "payload": {
            "project_id": project_id,
            "boards": boards,
            "columns": columns,
            "tasks": formatted_tasks,
            "work_item_projections": [],
        }})

    # ── Agent Management ──────────────────────────────────────────────

    async def _handle_create_agent(self, ws: Any, data: dict) -> None:
        role = data.get("role", {})
        role_id = role.get("id", "executor")
        # Resolve name: explicit name → org_engine role name → role_id
        name = role.get("name")
        if not name or name == role_id:
            if self.engine.org_engine:
                org_role = self.engine.org_engine.get_agent(role_id)
                if org_role:
                    name = org_role.name
            if not name:
                name = role_id.replace("_", " ").replace("-", " ").title()
        office_id = role.get("office_id", "office-0")
        # Collect optional custom fields from frontend
        description = role.get("description", "")
        specialties = role.get("specialties", [])
        if isinstance(specialties, str):
            specialties = [s.strip() for s in specialties.split(",") if s.strip()]
        tools = role.get("tools", [])
        system_prompt = role.get("system_prompt", "")
        appearance = role.get("appearance", {})
        palette = appearance.get("palette") if isinstance(appearance, dict) else None
        seat_zone = appearance.get("seat_zone", "workspace") if isinstance(appearance, dict) else "workspace"

        # Custom mode: create full three-layer data (RoleConfig + EmployeeConfig + Agent)
        employee_id = None
        if self._exec_mode in {"org", "custom"} and self.engine.org_engine:
            async with self._config_lock:
                from opc.core.config import RoleConfig, EmployeeConfig

                org = self.engine.org_engine
                role_created = False

                # 1. Create RoleConfig if role doesn't exist yet
                if not org.get_agent(role_id):
                    org.add_role(RoleConfig(
                        id=role_id,
                        name=name,
                        responsibility=description,
                        tools=tools or list(specialties or []),
                    ))
                    role_created = True

                # 2. Generate unique employee_id (uuid suffix prevents collisions)
                slug = f"{role_id}-{name.lower().replace(' ', '-')}"
                if any(e.employee_id == slug for e in self.engine.config.org.employees):
                    slug = f"{slug}-{uuid.uuid4().hex[:8]}"
                employee_id = slug

                # 3. Write custom prompt file if system_prompt provided
                prompt_refs: list[str] = []
                if system_prompt:
                    prompt_ref = self._write_custom_prompt(employee_id, name, system_prompt)
                    prompt_refs.append(prompt_ref)

                # 4. Create EmployeeConfig and append to config
                all_tools = tools or list(specialties or [])
                emp = EmployeeConfig(
                    employee_id=employee_id,
                    name=name,
                    role_id=role_id,
                    description=description,
                    category=description[:60] if description else "",
                    domains=all_tools,
                    tags=list(specialties or []),
                    prompt_refs=prompt_refs,
                )
                self.engine.config.org.employees = [
                    *self.engine.config.org.employees,
                    emp,
                ]

                # 5. Persist under lock to prevent concurrent save races
                try:
                    self._persist_runtime_config()
                except Exception:
                    # Rollback in-memory state
                    self.engine.config.org.employees = [
                        e for e in self.engine.config.org.employees
                        if e.employee_id != employee_id
                    ]
                    if role_created:
                        org.remove_role(role_id)
                    # Clean up prompt file
                    if prompt_refs:
                        prompt_path = Path(self.engine.opc_home) / prompt_refs[0]
                        prompt_path.unlink(missing_ok=True)
                    employee_id = None
                    logger.warning("Failed to persist config after create_agent, rolled back")

        agent = await self.agent_store.create_agent(
            name=name, opc_role_id=role_id, office_id=office_id,
            org_engine=self.engine.org_engine,
            description=description,
            specialties=specialties,
            tools=tools,
            palette=palette,
            seat_zone=seat_zone,
            employee_id=employee_id,
        )

        # Custom mode: broadcast org panel refresh to ALL clients
        if self._exec_mode in {"org", "custom"}:
            await self._broadcast_org_info()
            await self.agent_store.sync_custom_shadow()

        # Sync role map + broadcast agent_spawned event
        await self._sync_role_map()
        await self.broadcast({"type": "event", "payload": {
            "event_id": str(uuid.uuid4()),
            "type": "agent_spawned",
            "agent_id": agent["agent_id"],
            "data": {"role_name": agent["name"]},
            "timestamp": time.time(),
        }})
        agents = await self.agent_store.get_all()
        await self._send_ack(ws, ok=True, agents=agents)

    def _write_custom_prompt(self, employee_id: str, name: str, prompt_text: str) -> str:
        """Write a custom prompt file and return its relative path as a prompt_ref."""
        prompts_dir = Path(self.engine.opc_home) / "prompts" / "custom"
        prompts_dir.mkdir(parents=True, exist_ok=True)
        # ``employee_id`` is derived from user-supplied role id/name and previously flowed
        # unchecked into the path, enabling traversal (e.g. "../../tmp/pwn"). Reduce it to
        # a single safe path component and confirm containment before writing.
        safe_id = Path(str(employee_id or "")).name.replace("..", "")
        safe_id = safe_id.replace("/", "").replace("\\", "").strip() or "agent"
        filename = f"{safe_id}.md"
        filepath = (prompts_dir / filename).resolve()
        base = prompts_dir.resolve()
        if base not in filepath.parents and filepath != base:
            raise ValueError(f"Custom prompt filename escapes prompts directory: {employee_id!r}")
        filepath.write_text(f"# {name}\n\n{prompt_text}\n", encoding="utf-8")
        return f"prompts/custom/{filename}"

    async def _build_org_info_payload(self) -> dict[str, Any]:
        """Build the full org_info payload via OrgService."""
        result = await self._ensure_office_services().org.info()
        return result.payload

    async def _broadcast_org_info(self) -> None:
        """Build org_info payload and broadcast to ALL connected clients."""
        result = await self._ensure_office_services().org.info(include_events=True)
        await self._publish_service_result(result)

    async def _broadcast_snapshot(self) -> None:
        snapshot = await build_snapshot(
            self.engine, self.agent_store, self.chat_store, self.event_adapter
        )
        snapshot["exec_mode"] = self._exec_mode
        snapshot["company_profile"] = self._company_profile
        snapshot["task_preferred_agent"] = self._task_preferred_agent
        await self.broadcast({"type": "snapshot", "payload": snapshot})

    async def _ensure_custom_role_agents(self) -> list[dict[str, Any]]:
        if self._exec_mode not in {"org", "custom"} or not self.engine.org_engine:
            return await self.agent_store.get_all()
        agents = await self.agent_store.ensure_custom_role_agents(self.engine.org_engine)
        await self._sync_role_map()
        return agents

    async def _handle_delete_agent(self, ws: Any, data: dict) -> None:
        try:
            result = await self._ensure_office_services().agent.delete(data.get("agent_id", ""))
            await self._publish_service_result(result)
            if self._exec_mode in {"org", "custom"} and data.get("agent_id"):
                if hasattr(self, "chat_store") and hasattr(self, "event_adapter"):
                    await self._broadcast_snapshot()
                await self._broadcast_org_info()
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="delete_agent")

    async def _handle_list_agents(self, ws: Any, data: dict) -> None:
        result = await self._ensure_office_services().agent.list()
        await self._send_service_ack(ws, result)

    async def _handle_move_agent(self, ws: Any, data: dict) -> None:
        try:
            await self._ensure_office_services().agent.move(
                agent_id=data.get("agent_id", ""),
                office_id=data.get("office_id", "office-0"),
                seat_zone=data.get("seat_zone"),
                desk_id=data.get("desk_id"),
            )
            await self._broadcast_snapshot()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="move_agent")

    async def _handle_get_agent_detail(self, ws: Any, data: dict) -> None:
        """Return detailed info for a single agent (agent-level kanban view)."""
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self._ensure_office_services().agent.detail(
                project_id=project_id,
                agent_id=data.get("agent_id", ""),
            )
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="get_agent_detail")

    # ── Company Runtime Mode ─────────────────────────────────────────────────

    async def _handle_set_mode(self, ws: Any, data: dict) -> None:
        new_mode = data.get("mode", "task")
        new_profile = data.get("profile", "corporate")
        org_id = self._normalize_session_org_id(
            data.get("org_id") or data.get("organization_id")
        )
        new_preferred_agent = self._normalize_session_preferred_agent(
            data.get("preferred_agent", self._task_preferred_agent),
            default=self._task_preferred_agent,
        )
        ok = await self._apply_mode_switch(new_mode, new_profile, new_preferred_agent, org_id=org_id)
        if not ok:
            await self._send_ack(
                ws,
                ok=False,
                error=getattr(self, "_last_org_load_error", "") or "org_not_found",
                org_id=org_id,
            )
            return
        active_org_id = (org_id or await self._get_active_saved_org_name()) if self._exec_mode == "org" else ""
        await self._send_ack(
            ws,
            ok=True,
            mode=self._exec_mode,
            profile=self._company_profile,
            org_id=active_org_id,
            preferred_agent=self._task_preferred_agent,
        )

    async def _apply_mode_switch(
        self,
        new_mode: str,
        new_profile: str,
        new_preferred_agent: str,
        *,
        sync_config: bool = True,
        org_id: str | None = None,
    ) -> bool:
        # Mode is a default for new turns. Existing sessions carry their own
        # persisted mode/profile, so switching the toolbar must not interrupt or
        # rewrite running task state.
        previous_mode = getattr(self, "_exec_mode", "task")
        previous_profile = getattr(self, "_company_profile", "corporate")
        previous_preferred_agent = getattr(self, "_task_preferred_agent", "native")
        new_mode = self._normalize_session_exec_mode(new_mode)
        self._last_org_load_error = ""
        if sync_config and new_mode == "org" and org_id:
            config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
            if not org_config_path(config_dir, org_id).exists():
                self._last_org_load_error = "org_not_found"
                return False
        new_profile = "custom" if new_mode == "org" else self._normalize_session_company_profile(new_profile)
        self._exec_mode = new_mode
        self._company_profile = new_profile
        self._task_preferred_agent = new_preferred_agent
        if hasattr(self, "services_context"):
            self.services_context.mode_state.exec_mode = self._exec_mode
            self.services_context.mode_state.company_profile = self._company_profile
            self.services_context.mode_state.task_preferred_agent = self._task_preferred_agent
        await self._persist_mode()

        # Sync company_profile in config so _effective_roles() uses the right mode
        if sync_config and self.engine.org_engine:
            async with self._config_lock:
                if new_mode == "org":
                    loaded = self._load_active_org_config_into_engine(org_id)
                    if not loaded:
                        self._exec_mode = previous_mode
                        self._company_profile = previous_profile
                        self._task_preferred_agent = previous_preferred_agent
                        if hasattr(self, "services_context"):
                            self.services_context.mode_state.exec_mode = self._exec_mode
                            self.services_context.mode_state.company_profile = self._company_profile
                            self.services_context.mode_state.task_preferred_agent = self._task_preferred_agent
                        await self._persist_mode()
                        return False
                    elif org_id:
                        await self._set_active_saved_org_name(org_id)
                elif new_mode == "company":
                    self._restore_company_config_into_engine(new_profile)
                else:
                    self._restore_company_config_into_engine("")
                # Mode switching persists only the selected UI mode. It must not
                # rewrite company/org architecture files.

        # Reload agents for the new mode
        if self.engine.org_engine:
            preset = self._resolve_preset_name()
            await self.agent_store.load_preset(preset, self.engine.org_engine)

        await self._prune_stale_agent_store_entries()
        if self._exec_mode in {"org", "custom"}:
            await self._ensure_custom_role_agents()

        # Sync role→agent mapping for EventAdapter
        await self._sync_role_map()

        # B4: Clean orphan references — clear assigned_to on pending tasks
        # whose role no longer exists, and prune stale DM channels
        new_agents = await self.agent_store.get_all()
        valid_agent_ids = {a["agent_id"] for a in new_agents}
        valid_role_ids = {a.get("opc_role_id", a["agent_id"]) for a in new_agents}
        if self.engine.store:
            pending_tasks = await self.engine.store.get_tasks(project_id=self.engine.project_id or "default")
            for task in pending_tasks:
                if task.assigned_to and task.assigned_to not in valid_role_ids:
                    task.assigned_to = ""
                    await self.engine.store.save_task(task)
        await self.chat_store.prune_stale_channels(valid_agent_ids, project_id=self.engine.project_id or "default")
        # Ensure activity channel exists (session channels are on-demand)
        await self.chat_store.ensure_activity_channel(project_id=self.engine.project_id or "default")

        # Broadcast snapshot + full collab_sync so frontend refreshes everything
        snapshot = await build_snapshot(
            self.engine, self.agent_store, self.chat_store, self.event_adapter
        )
        snapshot["exec_mode"] = self._exec_mode
        snapshot["company_profile"] = self._company_profile
        snapshot["task_preferred_agent"] = self._task_preferred_agent
        await self.broadcast({"type": "snapshot", "payload": snapshot})

        collab = await build_collab_sync(
            self.engine,
            self.agent_store,
            self.chat_store,
            self.event_adapter,
            exec_mode=self._exec_mode,
        )
        await self.broadcast({"type": "collab_sync_push", "payload": collab})
        await self._broadcast_org_info()
        return True

    async def _prune_stale_agent_store_entries(self) -> None:
        if not self.engine.org_engine or not hasattr(self, "agent_store"):
            return

        effective_role_ids = {agent.role_id for agent in self.engine.org_engine.list_agents()}
        try:
            effective_employee_ids = {employee.employee_id for employee in self.engine.config.org.employees}
        except Exception:
            effective_employee_ids = set()

        for stale in await self.agent_store.get_all():
            emp_id = str(stale.get("employee_id") or "").strip()
            role_id = str(stale.get("opc_role_id") or stale.get("agent_id") or "").strip()
            is_stale = (
                (emp_id and emp_id not in effective_employee_ids)
                or (not emp_id and role_id and role_id not in effective_role_ids)
            )
            if is_stale:
                try:
                    await self.agent_store.remove_agent(stale["agent_id"])
                except Exception:
                    logger.debug(f"Failed to prune stale agent {stale.get('agent_id')}")

        if self._exec_mode in {"org", "custom"}:
            await self.agent_store.sync_custom_shadow()

    def _target_mode_for_profile(self, profile: str | None) -> tuple[str | None, str | None]:
        if profile == "custom":
            return "org", "custom"
        if profile == "corporate":
            return "company", "corporate"
        return None, None

    def _rebind_engine_config(self, config: Any) -> None:
        self.engine.config = config
        org_engine = getattr(self.engine, "org_engine", None)
        if org_engine is not None:
            org_engine.config = config
        talent_market = getattr(self.engine, "talent_market", None)
        if talent_market is not None:
            talent_market.config = config
        if hasattr(self.engine, "_runtime_config_signature"):
            self.engine._runtime_config_signature = None

    def _restore_company_config_into_engine(self, company_profile: str) -> None:
        config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
        try:
            loaded_config = OPCConfig.load(config_dir) if config_dir.exists() else OPCConfig()
        except Exception as exc:
            logger.warning(f"Failed to reload company architecture after leaving org mode: {exc}")
            loaded_config = self.engine.config
        loaded_config.org.company_profile = company_profile
        self._rebind_engine_config(loaded_config)
        if self.engine.org_engine:
            self.engine.org_engine.reload_from_config()
            configure_tools = getattr(self.engine.org_engine, "configure_task_mode_tools", None)
            task_tools = getattr(self.engine, "_task_mode_tool_names", None)
            if callable(configure_tools) and callable(task_tools):
                configure_tools(task_tools())

    def _load_active_org_config_into_engine(self, organization_id: str | None = None) -> bool:
        config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
        self._last_org_load_error = ""
        try:
            payload, source_path = load_org_config_payload(config_dir, organization_id)
            loaded_config = apply_org_config_payload_to_config(
                self.engine.config,
                payload,
                source_path=source_path,
            )
            validate_runnable_org_config(loaded_config, organization_id=organization_id or "")
        except FileNotFoundError:
            self._last_org_load_error = "org_not_found"
            return False
        except Exception as exc:
            self._last_org_load_error = str(exc)
            logger.warning(f"Failed to load org architecture for org mode: {exc}")
            return False
        self._rebind_engine_config(loaded_config)
        if self.engine.org_engine:
            self.engine.org_engine.reload_from_config()
        return True

    def _resolve_preset_name(self) -> str:
        """Map current mode to agent_store preset name.

        task    → "single" (1 executor)
        company → profile name ("corporate")
        org     → "custom" (user-managed agents)
        """
        if self._exec_mode in {"org", "custom"}:
            return "custom"
        if self._exec_mode == "company":
            return self._company_profile  # "corporate"
        return "single"  # task mode uses single-agent preset

    async def _handle_run_task(self, ws: Any, data: dict) -> None:
        title = data.get("title", "")
        description = data.get("description", "")
        mode = data.get("mode", self._exec_mode)
        profile = data.get("profile", self._company_profile)
        org_id = self._normalize_session_org_id(data.get("org_id") or data.get("organization_id"))
        task_id = data.get("task_id")
        run_engine, run_project_id = await self._engine_for_request(data)
        self._track(self._run_task(
            title,
            description,
            mode,
            profile,
            task_id=task_id,
            run_engine=run_engine,
            run_project_id=run_project_id,
            org_id=org_id,
        ))
        await self._send_ack(ws, ok=True)

    # ── Cross-Office ──────────────────────────────────────────────────

    async def _handle_cross_office(self, ws: Any, data: dict) -> None:
        """Visual-only: broadcasts collab event for frontend animation. No engine dispatch."""
        await self.broadcast({"type": "cross_office_collab", "payload": {
            "agent_ids": data.get("agent_ids", []),
            "task_id": data.get("task_id", ""),
            "action": data.get("action", ""),
        }})

    # ── Workload ─────────────────────────────────────────────────────

    async def _handle_agent_workload(self, ws: Any, data: dict) -> None:
        """Return per-agent task counts (active, pending, done, failed)."""
        if not self.engine.store:
            await self._send_ack(ws, ok=True, workload={})
            return
        agents = await self.agent_store.get_all()
        # Fetch all tasks once, then group by assigned_to (opc_role_id)
        all_tasks = await self.engine.store.get_tasks(
            project_id=self.engine.project_id or "default",
        )
        # Build role_id → task list mapping
        role_tasks: dict[str, list] = {}
        for t in all_tasks:
            role_tasks.setdefault(t.assigned_to, []).append(t)

        workload: dict[str, dict[str, int]] = {}
        for agent in agents:
            agent_id = agent.get("agent_id", "")
            if not agent_id:
                continue
            # Tasks are assigned by opc_role_id, not agent_id
            role_id = agent.get("opc_role_id", agent_id)
            counts = {"active": 0, "pending": 0, "done": 0, "failed": 0}
            for t in role_tasks.get(role_id, []):
                sv = t.status.value if hasattr(t.status, "value") else str(t.status)
                if sv == "running":
                    counts["active"] += 1
                elif sv == "pending":
                    counts["pending"] += 1
                elif sv == "done":
                    counts["done"] += 1
                elif sv == "failed":
                    counts["failed"] += 1
            workload[agent_id] = counts
        await self._send_ack(ws, ok=True, workload=workload)

    # ══════════════════════════════════════════════════════════════════════
    # Private helpers
    # ══════════════════════════════════════════════════════════════════════

    def _controller_active_task_run_registry(self) -> Any | None:
        root_engine = getattr(self, "_root_engine", None) or getattr(self, "engine", None)
        registry = getattr(root_engine, "_active_task_run_registry", None)
        return registry if isinstance(registry, ActiveTaskRunRegistry) else None

    def _release_current_execution_handoff(self) -> None:
        registry = self._controller_active_task_run_registry()
        release = getattr(registry, "release_current_handoff", None)
        if callable(release):
            release()

    def _track(self, coro: Any) -> asyncio.Task[Any]:
        """Create a tracked background task that auto-removes itself on completion."""
        handoff_registry = self._controller_active_task_run_registry()
        handoff_token = (
            handoff_registry.retain_current_handoff()
            if handoff_registry is not None
            else None
        )
        try:
            task = asyncio.create_task(coro)
        except BaseException:
            if handoff_registry is not None and handoff_token is not None:
                handoff_registry.release_handoff(handoff_token)
            if inspect.iscoroutine(coro):
                coro.close()
            raise
        if handoff_registry is not None and handoff_token is not None:
            def _release_execution_handoff(
                done_task: asyncio.Task[Any],
                *,
                registry: ActiveTaskRunRegistry = handoff_registry,
                token: str = handoff_token,
            ) -> None:
                registry.release_handoff(token)
                task_context = getattr(self, "_task_bg_context", None)
                if isinstance(task_context, dict):
                    context = task_context.get(done_task)
                    if isinstance(context, dict):
                        context.pop("execution_handoff_token", None)
                        if not context:
                            task_context.pop(done_task, None)

            task.add_done_callback(
                _release_execution_handoff
            )
            # This task owns the accepted engine handoff even before callers
            # such as _track_session add their richer session context.  Keep it
            # in the existing execution-owned task index so shutdown cannot
            # close the store while its cancellation cleanup is still running.
            task_context = getattr(self, "_task_bg_context", None)
            if isinstance(task_context, dict):
                context = task_context.setdefault(task, {})
                context["execution_handoff_token"] = handoff_token
        # Work scheduled after the gate closes is rejected.  A coroutine
        # retained by a pre-shutdown reservation is the one exception: it must
        # reach registry.register (or exit) before checkpoint snapshotting.
        if self._shutting_down and handoff_token is None:
            task.cancel()
        self._background_tasks.add(task)
        task.add_done_callback(self._on_bg_task_done)
        return task

    def _cancel_client_project_index(self, ws: Any) -> None:
        task = self._client_project_index_tasks.pop(ws, None)
        if task is not None and not task.done():
            task.cancel()

    def _track_client_project_index(
        self,
        ws: Any,
        coro: Any,
    ) -> asyncio.Task[Any]:
        self._cancel_client_project_index(ws)
        task = self._track(coro)
        self._client_project_index_tasks[ws] = task

        def _cleanup(done: asyncio.Task[Any]) -> None:
            if self._client_project_index_tasks.get(ws) is done:
                self._client_project_index_tasks.pop(ws, None)

        task.add_done_callback(_cleanup)
        return task

    def _track_client_initial_state(
        self,
        ws: Any,
        coro: Any,
    ) -> asyncio.Task[Any]:
        prior = self._client_initial_state_tasks.pop(ws, None)
        if prior is not None and not prior.done():
            prior.cancel()
        task = self._track(coro)
        self._client_initial_state_tasks[ws] = task

        def _cleanup(done: asyncio.Task[Any]) -> None:
            if self._client_initial_state_tasks.get(ws) is done:
                self._client_initial_state_tasks.pop(ws, None)

        task.add_done_callback(_cleanup)
        return task

    def _track_session(
        self,
        task_id: str,
        coro: Any,
        *,
        project_id: str | None = None,
        engine: Any | None = None,
    ) -> asyncio.Task[Any]:
        """Like _track but keeps all live tasks for explicit cancellation."""
        bg = self._track(coro)
        context = dict(self._task_bg_context.get(bg) or {})
        context.update({
            "task_id": task_id,
            "project_id": self._normalize_project_id(project_id or getattr(engine, "project_id", None)),
            "engine": engine,
        })
        self._task_bg_context[bg] = context
        task_group = self._task_bg_map.setdefault(task_id, set())
        task_group.add(bg)
        bg.add_done_callback(lambda t: self._discard_session_bg_task(task_id, t))
        return bg

    def _discard_session_bg_task(self, task_id: str, task: asyncio.Task[Any]) -> None:
        self._task_bg_context.pop(task, None)
        task_group = self._task_bg_map.get(task_id)
        if task_group is None:
            return
        task_group.discard(task)
        if not task_group:
            self._task_bg_map.pop(task_id, None)

    def _cancel_session_tasks(self, task_id: str) -> None:
        task_group = self._task_bg_map.pop(task_id, set())
        for bg_task in list(task_group):
            try:
                if not bg_task.done():
                    bg_task.cancel()
            except Exception:
                logger.opt(exception=True).debug(f"Failed to cancel background task for {task_id}")

    @staticmethod
    def _is_ws_disconnect_error(exc: BaseException) -> bool:
        message = str(exc or "").strip().lower()
        if type(exc).__name__ in {"ClientConnectionResetError", "ConnectionResetError"}:
            return True
        return any(
            token in message
            for token in (
                "cannot write to closing transport",
                "closing transport",
                "websocket connection is closed",
                "connection reset by peer",
                "broken pipe",
            )
        )

    @staticmethod
    def _is_closed_database_error(exc: BaseException) -> bool:
        message = str(exc or "").strip().lower()
        return any(
            token in message
            for token in (
                "cannot operate on a closed database",
                "closed database",
                "no active connection",
            )
        )

    def _is_expected_shutdown_error(self, exc: BaseException) -> bool:
        return self._shutting_down and (
            self._is_ws_disconnect_error(exc) or self._is_closed_database_error(exc)
        )

    @staticmethod
    def _ws_flag_is_set(value: Any) -> bool:
        return isinstance(value, bool) and value

    @classmethod
    def _ws_is_open(cls, ws: Any) -> bool:
        return not (
            cls._ws_flag_is_set(getattr(ws, "closed", False))
            or cls._ws_flag_is_set(getattr(ws, "closing", False))
        )

    async def _safe_send_json(self, ws: Any, payload: dict[str, Any]) -> bool:
        if not self._ws_is_open(ws):
            self._clients.discard(ws)
            return False
        try:
            result = ws.send_json(payload)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as exc:
            if self._is_ws_disconnect_error(exc) or self._shutting_down:
                self._clients.discard(ws)
                logger.debug(f"Skipped WS send during disconnect/shutdown: {type(exc).__name__}: {exc!r}")
                return False
            raise

    async def shutdown(self, timeout: float = 15.0) -> None:
        """Checkpoint owned runtimes, then stop all WS work before DB shutdown."""
        import aiohttp

        self._shutting_down = True
        pending_handlers = [
            task
            for task in self._active_message_tasks
            if task is not asyncio.current_task() and not task.done()
        ]

        def execution_owned_tasks() -> set[asyncio.Task[Any]]:
            owned = set(self._task_bg_context)
            for task_group in self._task_bg_map.values():
                owned.update(task_group)
            return {
                task
                for task in owned
                if task is not asyncio.current_task() and not task.done()
            }

        # A preaccepted handler can replace its handoff reservation with the
        # real execution task while the admission barrier drains.  Union this
        # pre-barrier view with the post-checkpoint view so neither owner can
        # disappear from the cancellation set during that handoff.
        execution_tasks_before_prepare = execution_owned_tasks()

        # Atomically close execution admission, then wait only for requests
        # accepted before that close to either register their first real engine
        # coroutine or exit.  There is intentionally no timeout window here:
        # registration drains the reservation, so long execution is not part
        # of this wait.
        active_run_registry = self._controller_active_task_run_registry()
        close_and_wait = getattr(
            active_run_registry,
            "close_admission_and_wait_for_handoffs",
            None,
        )
        if callable(close_and_wait):
            # Close synchronously, then cancel accepted requests which are
            # still queued before their first engine registration.  In
            # particular, a duplicate Continue can be waiting behind the
            # per-runtime reply lock for the entire first execution; waiting
            # for that reservation here would deadlock shutdown before it can
            # checkpoint and cancel the live owner.
            active_run_registry.close_admission()
            is_handoff_pending = getattr(
                active_run_registry,
                "is_handoff_pending",
                None,
            )
            pending_handoff_tasks: set[asyncio.Task[Any]] = set()
            pending_handoff_tokens: set[str] = set()
            if callable(is_handoff_pending):
                for task, context in list(self._task_bg_context.items()):
                    token = (
                        context.get("execution_handoff_token")
                        if isinstance(context, dict)
                        else None
                    )
                    if (
                        task is not asyncio.current_task()
                        and not task.done()
                        and is_handoff_pending(token)
                    ):
                        pending_handoff_tasks.add(task)
                        pending_handoff_tokens.add(str(token))
                handoff_routes = getattr(self, "_handoff_route_tasks", {})
                if isinstance(handoff_routes, dict):
                    for task, token in list(handoff_routes.items()):
                        if (
                            task is not asyncio.current_task()
                            and not task.done()
                            and is_handoff_pending(token)
                        ):
                            pending_handoff_tasks.add(task)
                            pending_handoff_tokens.add(token)
            for task in pending_handoff_tasks:
                task.cancel()
            revoke_handoff = getattr(active_run_registry, "revoke_handoff", None)
            if callable(revoke_handoff):
                for token in pending_handoff_tokens:
                    revoke_handoff(token)
            await close_and_wait()

        # The controller registry is the only source of truth for executions
        # owned by this process.  Persist one checkpoint per active company
        # scope while every execution coroutine and its store are still alive.
        root_engine = getattr(self, "_root_engine", None) or self.engine
        try:
            await root_engine.prepare_active_company_runtimes_for_shutdown()
        except Exception:
            logger.opt(exception=True).error(
                "Refusing to cancel WS execution because company runtime checkpointing failed"
            )
            raise

        # No execution coroutine may get another scheduling turn after its
        # checkpoint/holds are durable.  Build the cancellation set and set
        # every cancellation flag synchronously, before awaiting progress
        # flushing, client close handshakes, or any other UI cleanup.
        execution_tasks = execution_tasks_before_prepare | execution_owned_tasks()
        tracked_tasks = set(self._background_tasks)
        tracked_tasks.update(execution_tasks)
        tracked_tasks.update(pending_handlers)
        tracked_tasks = {
            task
            for task in tracked_tasks
            if task is not asyncio.current_task() and not task.done()
        }
        for task in tracked_tasks:
            task.cancel()
        if self._progress_flush_task and not self._progress_flush_task.done():
            self._progress_flush_task.cancel()

        # Await execution cleanup first so broker finally blocks reap process
        # groups while the engine/store are guaranteed to remain available.
        if execution_tasks:
            _done, still_cleaning = await asyncio.wait(
                execution_tasks,
                timeout=max(0.0, float(timeout)),
            )
            if still_cleaning:
                message = (
                    "Timed out waiting for "
                    f"{len(still_cleaning)} execution task(s) to finish cancellation cleanup"
                )
                logger.error(message)
                raise RuntimeError(message)

        if self._progress_flush_task:
            try:
                await self._progress_flush_task
            except (asyncio.CancelledError, Exception):
                pass
            self._progress_flush_task = None

        if self._runtime_status_sync_task:
            # Cancel rather than await: the sync interval is long enough that
            # waiting for a natural wake-up would stall shutdown.
            self._runtime_status_sync_task.cancel()
            try:
                await self._runtime_status_sync_task
            except (asyncio.CancelledError, Exception):
                pass
            self._runtime_status_sync_task = None

        clients = list(self._clients)
        for ws in clients:
            try:
                await ws.close(
                    code=aiohttp.WSCloseCode.GOING_AWAY,
                    message=b"server shutting down",
                )
            except Exception as exc:
                if not self._is_ws_disconnect_error(exc):
                    logger.debug(f"Failed to close WS client cleanly: {type(exc).__name__}: {exc!r}")

        ui_background_tasks = tracked_tasks - execution_tasks
        if ui_background_tasks:
            _done, still_running = await asyncio.wait(
                ui_background_tasks,
                timeout=max(0.0, float(timeout)),
            )
            if still_running:
                logger.warning(
                    "Timed out waiting for {} non-execution WS background task(s) to stop",
                    len(still_running),
                )

    @staticmethod
    def _store_is_ready(store: Any | None) -> bool:
        """Treat initialized stores as ready, even without an explicit flag."""
        if not store:
            return False
        ready = getattr(store, "is_ready", None)
        if callable(ready):
            return bool(ready())
        if ready is None:
            return True
        return bool(ready)

    @staticmethod
    def _chat_store_is_ready(chat_store: Any | None) -> bool:
        if chat_store is None:
            return False
        ready = getattr(chat_store, "is_ready", None)
        if callable(ready):
            try:
                if not bool(ready()):
                    return False
            except Exception:
                return False
        elif ready is not None and not bool(ready):
            return False
        db = getattr(chat_store, "_db", None)
        if db is None:
            return False
        connection = getattr(db, "_connection", True)
        return connection is not None

    @staticmethod
    def _task_has_comms_workspace(task: Any | None) -> bool:
        if task is None:
            return False
        metadata = dict(getattr(task, "metadata", {}) or {})
        return bool(
            str(metadata.get("comms_workspace_root") or "").strip()
            or str(metadata.get("target_output_dir") or "").strip()
            or str(metadata.get("setup_workspace_prepared") or "").strip()
        )

    @staticmethod
    def _shared_root_ui_task_id(task: Any | None) -> str:
        if task is None:
            return ""
        metadata = dict(getattr(task, "metadata", {}) or {})
        if not bool(metadata.get("shared_role_session", False)):
            return ""
        session_id = str(getattr(task, "session_id", "") or "").strip()
        root_session_id = str(metadata.get("company_runtime_root_session_id", "") or "").strip()
        origin_task_id = str(metadata.get("origin_task_id", "") or "").strip()
        if origin_task_id and root_session_id and session_id == root_session_id:
            return origin_task_id
        return ""

    @staticmethod
    def _task_mode_origin_ui_task_id(task: Any | None) -> str:
        if task is None:
            return ""
        metadata = dict(getattr(task, "metadata", {}) or {})
        task_id = str(getattr(task, "id", "") or "").strip()
        origin_task_id = str(metadata.get("origin_task_id", "") or "").strip()
        if not origin_task_id or origin_task_id == task_id:
            return ""
        mode = str(metadata.get("mode", "") or "").strip().lower()
        exec_mode = str(metadata.get("exec_mode", "") or "").strip().lower()
        execution_mode = str(metadata.get("execution_mode", "") or "").strip().lower()
        task_mode_contract = str(metadata.get("task_mode_contract", "") or "").strip()
        if (
            mode == "task"
            or exec_mode in {"task", "project", "single"}
            or execution_mode in {"task", "task_mode", "project"}
            or task_mode_contract == "single_full_capability_main_agent"
        ):
            return origin_task_id
        return ""

    def _ui_task_id_for_task(self, task: Any | None) -> str:
        if task is None:
            return ""
        task_id = str(getattr(task, "id", "") or "").strip()
        ui_task_id = self._shared_root_ui_task_id(task) or self._task_mode_origin_ui_task_id(task) or task_id
        if task_id and ui_task_id and ui_task_id != task_id:
            self._ui_task_aliases[task_id] = ui_task_id
        return ui_task_id

    async def _ui_task_id_for_runtime_task_id(self, task_id: str | None, *, engine: Any | None = None) -> str:
        raw_task_id = str(task_id or "").strip()
        if not raw_task_id:
            return ""
        mapped = str(self._ui_task_aliases.get(raw_task_id) or "").strip()
        if mapped:
            return mapped
        runtime_engine = engine or self.engine
        store = getattr(runtime_engine, "store", None)
        get_task = getattr(store, "get_task", None)
        if callable(get_task):
            try:
                task = await get_task(raw_task_id)
            except Exception:
                task = None
            mapped = self._ui_task_id_for_task(task)
            if mapped:
                return mapped
        return raw_task_id

    def _on_bg_task_done(self, task: asyncio.Task[Any]) -> None:
        """Callback for tracked background tasks: cleanup + log unhandled errors.

        For session tasks, also broadcasts an error message and idle status
        so the frontend doesn't stay stuck on "thinking" forever.
        """
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.opt(exception=exc).error(f"Background task failed: {exc}")
            # Notify frontend for session tasks so UI doesn't stay stuck on "thinking"
            context = self._task_bg_context.get(task) or {}
            task_id = self._find_task_id_for_bg_task(task)
            if task_id:
                asyncio.ensure_future(self._notify_session_bg_failure(
                    task_id,
                    exc,
                    project_id=str(context.get("project_id") or "").strip() or None,
                ))

    def _find_task_id_for_bg_task(self, bg_task: asyncio.Task[Any]) -> str | None:
        """Find the task_id associated with a background task."""
        context = self._task_bg_context.get(bg_task)
        if context:
            task_id = str(context.get("task_id", "") or "").strip()
            if task_id:
                return task_id
        for tid, task_group in self._task_bg_map.items():
            if bg_task in task_group:
                return tid
        return None

    async def _notify_session_bg_failure(
        self,
        task_id: str,
        exc: BaseException,
        *,
        project_id: str | None = None,
    ) -> None:
        """Broadcast error message + idle status for a failed session background task."""
        try:
            channel_id = f"session:{task_id}"
            pid = self._normalize_project_id(project_id or self.engine.project_id)
            msg = await self.chat_store.insert_message(
                channel_id=channel_id,
                sender="system",
                sender_name="OPC",
                content=f"Error: {exc}",
                project_id=pid,
            )
            await self.broadcast({"type": "session_message", "payload": msg})
            await self.broadcast({"type": "board_task_status_changed", "payload": {
                "project_id": pid, "task_id": task_id, "column_id": "in-progress", "status": "failed",
            }})
            idle_payload: dict[str, Any] = {
                "project_id": pid,
                "task_id": task_id, "status": "idle", "current_tool": None, "iteration": 0,
            }
            resolved_agent = self._resolve_agent_for_idle(task_id)
            if resolved_agent:
                idle_payload["agent_id"] = resolved_agent
            await self.broadcast({"type": "agent_runtime_update", "payload": idle_payload})
        except Exception as notify_exc:
            logger.debug(f"Failed to notify frontend of session bg failure: {notify_exc}")

    async def _send_ack(self, ws: Any, ok: bool = True, **extra: Any) -> None:
        payload: dict[str, Any] = {"ok": ok, **extra}
        await self._safe_send_json(ws, {"type": "ack", "payload": payload})

    async def _publish_service_result(self, result: ServiceResult) -> None:
        for event in result.events:
            await self.broadcast({"type": event.type, "payload": event.payload})

    async def _send_service_ack(self, ws: Any, result: ServiceResult, **extra: Any) -> None:
        payload = dict(result.payload or {})
        payload.update(extra)
        ok = bool(payload.pop("ok", True))
        await self._send_ack(ws, ok=ok, **payload)

    async def _send_service_error(self, ws: Any, exc: ServiceError, *, action: str | None = None) -> None:
        payload = exc.to_payload()
        payload.pop("ok", None)
        if action:
            payload["action"] = action
        await self._send_ack(ws, ok=False, **payload)

    async def _refresh_runtime_control_for_client(
        self,
        ws: Any,
        *,
        engine: Any,
        project_id: str,
    ) -> None:
        """Replace optimistic Continue state with a durable snapshot."""
        try:
            collab = await build_collab_sync(
                engine,
                self.agent_store,
                self.chat_store,
                self.event_adapter,
                exec_mode=self._exec_mode,
            )
            collab["project_id"] = self._normalize_project_id(project_id)
            await self._safe_send_json(ws, {"type": "collab_sync_push", "payload": collab})
        except Exception:
            logger.opt(exception=True).debug("failed to refresh runtime control after rejected Continue")

    # UI profile name → engine CompanyProfile name
    _PROFILE_TO_ENGINE: dict[str, str] = {"classic": "corporate"}

    def _normalize_session_exec_mode(self, value: Any) -> str:
        return SessionService.normalize_exec_mode(value)

    def _normalize_session_company_profile(self, value: Any) -> str:
        return self._ensure_office_services().session.normalize_company_profile(value)

    def _normalize_session_preferred_agent(self, value: Any, default: str = "native") -> str:
        return SessionService.normalize_preferred_agent(value, default=default)

    def _normalize_session_org_id(self, value: Any) -> str:
        return SessionService.normalize_org_id(value)

    def _resolve_task_session_config(self, task: Any | None) -> tuple[str, str]:
        return self._ensure_office_services().session.resolve_task_session_config(task)

    def _resolve_task_identity(self, task: Any | None, **defaults: Any) -> Any:
        return self._ensure_office_services().session.resolve_task_identity(task, **defaults)

    def _resolve_task_org_id(self, task: Any | None) -> str:
        return self._ensure_office_services().session.resolve_task_org_id(task)

    @staticmethod
    def _is_company_session_exec_mode(exec_mode: Any) -> bool:
        return str(exec_mode or "").strip().lower() in {"company", "org", "custom"}

    def _resolve_task_preferred_agent(self, task: Any | None) -> str:
        return self._ensure_office_services().session.resolve_task_preferred_agent(task)

    def _resolve_task_selected_execution_agent(self, task: Any | None) -> str:
        return self._ensure_office_services().session.resolve_task_selected_execution_agent(task)

    async def _session_config_lock_reason(self, task: Any, project_id: str) -> str:
        """Return a reason when a session's execution config can no longer change."""
        return await self._ensure_office_services().session.session_config_lock_reason(task, project_id)

    async def _persist_session_config(
        self,
        task: Any,
        *,
        exec_mode: str,
        company_profile: str,
        preferred_agent: str,
        org_id: str = "",
        engine: Any | None = None,
    ) -> None:
        await self._ensure_office_services().session.persist_session_config(
            task,
            exec_mode=exec_mode,
            company_profile=company_profile,
            preferred_agent=preferred_agent,
            org_id=org_id,
            engine=engine,
        )
    def _resolve_engine_mode(self, mode: str | None = None, profile: str | None = None) -> tuple[str, str | None]:
        """Resolve UI execution mode into (engine_mode, company_profile).

        UI modes → engine API:
          "task"    → mode="project", company_profile=None
          "company" → mode="company", company_profile=profile
          "org"     → mode="org", company_profile="custom"
        UI profile "classic" maps to engine CompanyProfile "corporate".
        """
        mode = mode or self._exec_mode
        profile = profile or self._company_profile
        if mode == "company":
            engine_profile = self._PROFILE_TO_ENGINE.get(profile, profile) if profile else None
            return "company", engine_profile
        if mode in {"org", "custom"}:
            return "org", "custom"
        return "project", None

    async def _run_task(
        self,
        title: str,
        description: str,
        mode: str,
        profile: str,
        task_id: str | None = None,
        *,
        run_engine: Any | None = None,
        run_project_id: str | None = None,
        org_id: str | None = None,
    ) -> None:
        """Execute a task with the selected mode."""
        engine = run_engine or self.engine
        pid = self._normalize_project_id(run_project_id or getattr(engine, "project_id", None))
        error_channel = f"session:{task_id}" if task_id else f"activity:{pid}"

        # Look up session_id from task
        session_id: str | None = None
        task = None
        preferred_agent = self._task_preferred_agent
        session_org_id = self._normalize_session_org_id(org_id)
        if task_id and getattr(engine, "store", None):
            task = await engine.store.get_task(task_id)
            if task:
                session_id = task.session_id
                identity = self._resolve_task_identity(
                    task,
                    default_exec_mode=mode,
                    default_company_profile=profile,
                    default_preferred_agent=preferred_agent,
                    default_org_id=session_org_id,
                )
                mode = identity.exec_mode
                profile = identity.company_profile
                session_org_id = identity.org_id
                preferred_agent = identity.preferred_agent

        company_runtime_target: dict[str, Any] | None = None
        try:
            content = f"{title}\n{description}".strip()
            engine_mode, company_profile = self._resolve_engine_mode(mode, profile)
            engine_preferred_agent = preferred_agent if engine_mode == "project" else None
            response = None

            if task_id:
                # Per-task lock: same session serialized, different sessions concurrent
                async with self._get_task_lock(task_id):
                    if self._is_company_session_exec_mode(mode) and task is not None and self._store_is_ready(engine.store):
                        from opc.core.models import TaskStatus
                        await apply_task_status_transition(
                            engine.store,
                            task,
                            target_status_or_phase=TaskStatus.RUNNING,
                            reason="run_task_started",
                        )
                    await self.broadcast({"type": "board_task_status_changed", "payload": {
                        "project_id": pid, "task_id": task_id, "column_id": "in-progress", "status": "running",
                    }})
                    if task is not None:
                        await self._persist_session_config(
                            task,
                            exec_mode=mode,
                            company_profile=profile,
                            preferred_agent=preferred_agent,
                            org_id=session_org_id,
                            engine=engine,
                        )
                    if self._is_company_session_exec_mode(mode):
                        try:
                            company_runtime_target = await self._resolve_company_runtime_target(task_id, engine=engine)
                            await self._set_company_runtime_control(company_runtime_target, state="running")
                        except Exception:
                            logger.opt(exception=True).debug("failed to mark run_task company runtime running")
                    response = await engine.process_message(
                        content,
                        project_id=pid,
                        session_id=session_id,
                        mode=engine_mode,
                        org_id=session_org_id or None,
                        company_profile=company_profile,
                        preferred_agent=engine_preferred_agent,
                        origin_task_id=task_id,
                    )
                await self._sync_task_transcript_messages(task_id, engine=engine)
            else:
                response = await engine.process_message(
                    content,
                    project_id=pid,
                    mode=engine_mode,
                    org_id=session_org_id or None,
                    company_profile=company_profile,
                    preferred_agent=engine_preferred_agent,
                )

            # Broadcast: task idle (agent responded, waiting for user)
            if task_id:
                await self.broadcast({"type": "board_task_status_changed", "payload": {
                    "project_id": pid, "task_id": task_id, "column_id": "in-progress", "status": "idle",
                }})
                if getattr(engine, "store", None):
                    from opc.core.models import TaskStatus as TS
                    t = await engine.store.get_task(task_id)
                    if t:
                        t.status = TS.IDLE
                        await engine.store.save_task(t)
                if self._is_company_session_exec_mode(mode):
                    try:
                        idle_target = await self._resolve_company_runtime_target(task_id, engine=engine)
                        await self._set_company_runtime_control(idle_target or company_runtime_target, state="idle")
                    except Exception:
                        logger.opt(exception=True).debug("failed to mark run_task company runtime idle")
        except Exception as e:
            logger.opt(exception=True).error(f"Task execution error: {e}")
            # Broadcast: task failed (stays in in-progress, user can retry)
            if task_id:
                await self.broadcast({"type": "board_task_status_changed", "payload": {
                    "project_id": pid, "task_id": task_id, "column_id": "in-progress", "status": "failed",
                }})
                if self._is_company_session_exec_mode(mode):
                    try:
                        failed_target = await self._resolve_company_runtime_target(task_id, engine=engine)
                        await self._set_company_runtime_control(failed_target or company_runtime_target, state="idle")
                    except Exception:
                        logger.opt(exception=True).debug("failed to clear run_task company runtime after error")
            err_meta: dict[str, Any] = {}
            if task_id:
                err_meta["task_id"] = task_id
            msg = await self.chat_store.insert_message(
                channel_id=error_channel,
                sender="system",
                sender_name="OPC",
                content=f"Task error: {e}",
                metadata=err_meta or None,
                project_id=pid,
            )
            await self.broadcast({"type": "session_message", "payload": msg})
        finally:
            # Always refresh the frontend after a task run so new delegation
            # runs / work items are reflected on the kanban immediately.
            try:
                collab = await build_collab_sync(
                    engine, self.agent_store, self.chat_store,
                    self.event_adapter,
                    exec_mode=mode,
                )
                await self.broadcast({"type": "collab_sync_push", "payload": collab})
            except Exception:
                logger.opt(exception=True).warning("Post-run collab_sync broadcast failed (non-fatal)")

    async def _mirror_agent_message(
        self,
        event: Any,
        *,
        engine: Any | None = None,
        project_id: str | None = None,
    ) -> None:
        """Mirror agent_message_sent events into session channel or activity."""
        runtime_engine = engine or self.engine
        pid = self._normalize_project_id(project_id or getattr(runtime_engine, "project_id", None))
        p = event.payload or {}
        from_role = p.get("from", "")
        content = p.get("subject", "") or p.get("body", "") or "Message"
        task_id = p.get("task_id")

        # Resolve opc_role_id → agent_id for consistent sender identity
        from_agent = self.event_adapter._resolve_role_to_agent(from_role) if from_role else ""

        # Resolve human-readable name: agent store name → org role name → raw ID
        display_name = from_role or from_agent
        if from_agent:
            agents = await self.agent_store.get_all()
            match = next((a for a in agents if a["agent_id"] == from_agent), None)
            if match:
                display_name = match["name"]
        org_engine = getattr(runtime_engine, "org_engine", None)
        if display_name == from_role and org_engine:
            org_role = org_engine.get_agent(from_role)
            if org_role:
                display_name = org_role.name

        # Route to session channel if task_id is known, else activity
        target_channel = f"session:{task_id}" if task_id else f"activity:{pid}"
        mirror_meta: dict[str, Any] = {}
        if task_id:
            mirror_meta["task_id"] = task_id
        msg = await self.chat_store.insert_message(
            channel_id=target_channel,
            sender=from_agent,
            sender_name=display_name,
            content=content,
            metadata=mirror_meta or None,
            project_id=pid,
        )
        await self.broadcast({"type": "session_message", "payload": msg})

    async def _mirror_escalation(
        self,
        event: Any,
        *,
        engine: Any | None = None,
        project_id: str | None = None,
    ) -> None:
        """Mirror escalation_created events into session channel or activity."""
        runtime_engine = engine or self.engine
        pid = self._normalize_project_id(project_id or getattr(runtime_engine, "project_id", None))
        p = event.payload or {}
        message = p.get("message", "Escalation required")
        source_task_id = str(p.get("task_id") or "").strip() or None
        source_task = None
        if source_task_id and getattr(runtime_engine, "store", None):
            getter = getattr(runtime_engine.store, "get_task", None)
            if callable(getter):
                try:
                    source_task = await getter(source_task_id)
                except Exception:
                    source_task = None
        source_metadata = dict(getattr(source_task, "metadata", {}) or {}) if source_task is not None else {}
        is_task_mode = self._runtime_payload_is_task_mode(source_metadata)
        current_turn_title = str(
            source_metadata.get("original_message")
            or getattr(source_task, "description", "")
            or ""
        ).strip()
        display_message = self._task_mode_permission_prompt(message, current_turn_title) if is_task_mode else message
        session_task_id = await self._resolve_escalation_session_task_id(source_task_id, engine=runtime_engine)
        target_channel = f"session:{session_task_id}" if session_task_id else f"activity:{pid}"
        options = p.get("options", []) or []
        esc_record = self._remember_pending_escalation({
            "escalation_id": str(p.get("escalation_id") or ""),
            "project_id": pid,
            "task_id": session_task_id,
            "source_task_id": source_task_id,
            "message": message,
            "display_message": display_message,
            "options": options,
            "default_action": p.get("default_action"),
            "escalation_type": p.get("type", "decision_needed"),
            "approval_group_key": p.get("approval_group_key") or self._approval_group_key(message),
        })
        esc_meta: dict[str, Any] = {
            "checkpoint_type": "human_escalation",
            "checkpoint_id": esc_record.get("escalation_id"),
            "escalation_id": esc_record.get("escalation_id"),
            "escalation_type": esc_record.get("escalation_type"),
            "prompt": display_message,
            "summary": display_message,
            "options": options,
            "default_action": esc_record.get("default_action"),
            "source": "engine",
            "ui_message_id": f"escalation::{esc_record.get('escalation_id')}",
            "project_id": pid,
            "approval_group_key": esc_record.get("approval_group_key"),
        }
        approval_context = dict(p.get("approval_context") or {})
        if approval_context:
            # Persisted with the card so a click AFTER the inline wait expired
            # (or after a restart) can still apply the same allowlist grant and
            # resume the parked task.
            esc_meta["approval_context"] = approval_context
        if is_task_mode:
            esc_meta["execution_mode"] = "task_mode"
            esc_meta["permission_group_key"] = esc_record.get("approval_group_key")
            esc_meta["current_turn_title"] = current_turn_title
        if session_task_id:
            esc_meta["task_id"] = session_task_id
        if source_task_id and source_task_id != session_task_id:
            esc_meta["source_task_id"] = source_task_id
        msg = await self.chat_store.insert_message(
            channel_id=target_channel,
            sender="assistant",
            sender_name="OPC",
            content=display_message,
            metadata=esc_meta or None,
            message_id=f"escalation::{esc_record.get('escalation_id')}",
            project_id=pid,
        )
        await self.broadcast({"type": "session_message", "payload": msg})

    async def _recent_identical_helper_exists(
        self,
        channel_id: str,
        content: str,
        *,
        project_id: str,
        window_seconds: float = 120.0,
        scan_limit: int = 10,
    ) -> bool:
        """True when an identical assistant helper was posted very recently.

        Used to collapse rapid duplicate user clicks into a single helper
        reply instead of one warning per click.
        """
        try:
            recent = await self.chat_store.get_channel_messages(
                channel_id, limit=scan_limit, project_id=project_id,
            )
        except Exception:
            return False
        now = time.time()
        for item in reversed(recent):
            if str(item.get("sender", "")) != "assistant":
                continue
            if str(item.get("content", "")) != content:
                continue
            try:
                created_at = float(item.get("created_at", 0) or 0)
            except (TypeError, ValueError):
                continue
            if now - created_at <= window_seconds:
                return True
        return False

    async def _find_pending_approval_park_checkpoint(
        self,
        engine: Any,
        task_id: str,
        project_id: str,
    ) -> Any | None:
        """Locate the pending checkpoint a tool-approval timeout parked on.

        When an approval card's inline wait expires, the blocked runtime task
        returns AWAITING_HUMAN and the engine saves a durable pause checkpoint
        (task mode: ``task_user_input``; company mode: ``company_work_item_gate``).
        A later click on the card resumes execution through that checkpoint.
        """
        source_task_id = str(task_id or "").strip()
        if not source_task_id:
            return None
        store = getattr(engine, "store", None)
        getter = getattr(store, "get_pending_checkpoints", None)
        if not callable(getter):
            return None
        try:
            pending = await getter(project_id=project_id)
        except Exception:
            logger.opt(exception=True).debug(
                "Failed to load pending checkpoints for deferred escalation resume"
            )
            return None
        candidates = []
        for checkpoint in pending or []:
            if str(getattr(checkpoint, "checkpoint_type", "") or "") not in {
                "task_user_input",
                "company_work_item_gate",
            }:
                continue
            payload = dict(getattr(checkpoint, "payload", {}) or {})
            linked_ids = {
                str(payload.get("task_id") or "").strip(),
                str(payload.get("waiting_task_id") or "").strip(),
                str(getattr(checkpoint, "task_id", "") or "").strip(),
            }
            linked_ids.update(str(item or "").strip() for item in list(payload.get("task_ids", []) or []))
            if source_task_id in linked_ids:
                candidates.append(checkpoint)
        if not candidates:
            return None

        def _checkpoint_timestamp(checkpoint: Any) -> float:
            created = getattr(checkpoint, "created_at", None)
            try:
                return float(created.timestamp())
            except (AttributeError, TypeError, ValueError, OSError):
                return 0.0

        return max(candidates, key=_checkpoint_timestamp)

    async def _resolve_deferred_escalation_click(
        self,
        *,
        engine: Any,
        project_id: str,
        channel_id: str,
        checkpoint_id: str,
        card_meta: dict[str, Any],
        option_id: str,
    ) -> dict[str, Any]:
        """Apply a decision clicked on an approval card whose inline wait has
        expired: persist the allowlist grant, resolve the card, and hand back
        either a flow-through rewrite (resume the parked task through the
        normal message pipeline) or a helper reply when nothing is parked."""
        approval_context = dict(card_meta.get("approval_context") or {})
        summary: dict[str, Any] = {
            "approved": option_id in {"approve_once", "approve_session", "always_project", "always_global"},
            "scope": None,
        }
        approval_engine = getattr(engine, "approval_engine", None)
        apply_decision = getattr(approval_engine, "apply_deferred_escalation_decision", None)
        if callable(apply_decision):
            try:
                summary = apply_decision(option_id, approval_context)
            except Exception:
                logger.opt(exception=True).warning(
                    "Deferred approval grant failed; resuming the parked task without a new allowlist entry"
                )
        await self._mark_human_escalation_checkpoint_status(
            checkpoint_id,
            status="resolved",
            project_id=project_id,
            channel_id=channel_id,
            reply=option_id,
            reason="deferred_decision",
        )

        source_task_id = str(
            card_meta.get("source_task_id") or card_meta.get("task_id") or ""
        ).strip()
        park_checkpoint = await self._find_pending_approval_park_checkpoint(
            engine, source_task_id, project_id
        )
        action_name = str(approval_context.get("action_name", "") or "").strip() or "action"
        approved = bool(summary.get("approved"))
        scope = str(summary.get("scope") or "").strip()
        if park_checkpoint is None:
            return {
                "action": "reply",
                "text": (
                    f"Decision `{option_id}` recorded"
                    + (f"; allowlist updated ({scope})" if approved and scope else "")
                    + ". No parked task is currently waiting on this approval — if the runtime "
                    "is still transitioning, the grant applies on its next attempt."
                ),
            }
        if approved:
            scope_note = f" (allowlisted: {scope})" if scope else ""
            crafted = (
                f"Approval decision: {option_id}. The previously blocked `{action_name}` action "
                f"is now permitted{scope_note}. Re-run it and continue the task."
            )
        else:
            crafted = (
                f"Approval decision: deny. Do not run the blocked `{action_name}` action; "
                "choose an alternative approach or report the limitation to your manager."
            )
        return {
            "action": "flow_through",
            "content": crafted,
            "reply_metadata": {
                "response_to_checkpoint_id": str(getattr(park_checkpoint, "checkpoint_id", "") or ""),
                "response_to_checkpoint_type": str(getattr(park_checkpoint, "checkpoint_type", "") or ""),
            },
        }

    async def _mark_human_escalation_checkpoint_status(
        self,
        escalation_id: str,
        *,
        status: str,
        project_id: str,
        channel_id: str | None = None,
        reply: str | None = None,
        default_action: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        normalized_escalation_id = str(escalation_id or "").strip()
        if not normalized_escalation_id:
            return None
        update_status = getattr(self.chat_store, "update_checkpoint_status", None)
        if not callable(update_status):
            return None
        status_metadata: dict[str, Any] = {
            "checkpoint_resolution_source": "escalation_lifecycle",
        }
        if reply is not None:
            status_metadata["checkpoint_resolution_reply"] = reply
        if default_action is not None:
            status_metadata["checkpoint_timeout_default_action"] = default_action
        if reason:
            status_metadata["checkpoint_resolution_reason"] = reason
        try:
            updated = await update_status(
                normalized_escalation_id,
                channel_id=channel_id,
                checkpoint_type="human_escalation",
                status=status,
                status_metadata=status_metadata,
                project_id=project_id,
            )
        except Exception:
            logger.opt(exception=True).debug(
                f"Failed to update human escalation checkpoint status for {normalized_escalation_id}",
            )
            return None
        if updated is not None:
            await self.broadcast({"type": "session_message", "payload": updated})
        return updated

    async def _mark_escalation_event_checkpoint_terminal(
        self,
        event: Any,
        *,
        project_id: str,
    ) -> None:
        payload = dict(getattr(event, "payload", {}) or {})
        escalation_id = str(payload.get("escalation_id", "") or "").strip()
        if not escalation_id:
            return
        if event.event_type == "escalation_timeout":
            default_action = str(payload.get("default_action", "") or "").strip() or None
            if default_action is None:
                # No default was applied on timeout — the decision is still the
                # user's to make. The task parks on AWAITING_HUMAN and the card
                # stays pending; clicking it later applies the decision and
                # resumes the parked task (deferred approval path).
                return
            await self._mark_human_escalation_checkpoint_status(
                escalation_id,
                status="timeout",
                project_id=project_id,
                default_action=default_action,
                reason="timeout",
            )
            return
        if event.event_type == "escalation_resolved":
            await self._mark_human_escalation_checkpoint_status(
                escalation_id,
                status="resolved",
                project_id=project_id,
                reply=str(payload.get("reply", "") or "").strip() or None,
                reason="resolved",
            )

    async def _reconcile_inactive_human_escalation_cards(
        self,
        channel_id: str,
        *,
        task_id: str,
        project_id: str,
    ) -> list[dict[str, Any]]:
        """Mark legacy human escalation cards stale when no live approval exists.

        Older Office UI builds persisted approval cards without a terminal
        ``checkpoint_status`` when the runtime timed out or auto-approved. On a
        later reload there is no in-memory escalation future for those cards, so
        session detail is the authoritative place to reconcile persisted UI
        state with runtime state.
        """
        getter = getattr(self.chat_store, "get_unresolved_checkpoint_messages", None)
        if not callable(getter):
            return []
        try:
            cards = await getter(
                channel_id,
                checkpoint_type="human_escalation",
                project_id=project_id,
            )
        except Exception:
            logger.opt(exception=True).debug(
                f"Failed to load unresolved human escalation cards for {channel_id}",
            )
            return []

        updated_cards: list[dict[str, Any]] = []
        for card in cards:
            metadata = dict(card.get("metadata", {}) or {})
            escalation_id = str(
                metadata.get("escalation_id")
                or metadata.get("checkpoint_id")
                or ""
            ).strip()
            if not escalation_id:
                continue
            if self._find_pending_escalation(
                task_id=task_id,
                escalation_id=escalation_id,
                project_id=project_id,
            ):
                continue
            if isinstance(metadata.get("approval_context"), dict) and metadata.get("approval_context"):
                # Deferred-capable approval card: it stays answerable after the
                # inline wait expired or across restarts, so a missing pending
                # future does NOT make it stale.
                continue
            updated = await self._mark_human_escalation_checkpoint_status(
                escalation_id,
                status="stale",
                project_id=project_id,
                channel_id=channel_id,
                reason="session_detail_reconcile_inactive_escalation",
            )
            if updated is not None:
                updated_cards.append(updated)
        return updated_cards

    async def _reconcile_execution_checkpoint_cards(
        self,
        channel_id: str,
        *,
        project_id: str,
        engine: Any,
    ) -> list[dict[str, Any]]:
        """Mark non-human checkpoint cards terminal once the engine checkpoint is terminal."""
        getter = getattr(self.chat_store, "get_unresolved_checkpoint_messages", None)
        if not callable(getter):
            return []
        try:
            cards = await getter(channel_id, project_id=project_id)
        except Exception:
            logger.opt(exception=True).debug(
                f"Failed to load unresolved execution checkpoint cards for {channel_id}",
            )
            return []

        updated_cards: list[dict[str, Any]] = []
        for card in cards:
            metadata = dict(card.get("metadata", {}) or {})
            checkpoint_type = str(metadata.get("checkpoint_type", "") or "").strip()
            if checkpoint_type in {"", "human_escalation"}:
                continue
            checkpoint_id = str(metadata.get("checkpoint_id", "") or "").strip()
            if not checkpoint_id:
                continue
            status = await self._execution_checkpoint_status(
                engine=engine,
                project_id=project_id,
                checkpoint_id=checkpoint_id,
                checkpoint_type=checkpoint_type,
            )
            if not status or status == "pending":
                continue
            try:
                updated = await self.chat_store.update_checkpoint_status(
                    checkpoint_id,
                    channel_id=channel_id,
                    checkpoint_type=checkpoint_type,
                    status=status,
                    status_metadata={
                        "checkpoint_resolution_source": "execution_checkpoint_lifecycle",
                    },
                    project_id=project_id,
                )
            except Exception:
                logger.opt(exception=True).debug(
                    f"Failed to reconcile execution checkpoint card {checkpoint_id}",
                )
                continue
            if updated is not None:
                updated_cards.append(updated)
                await self.broadcast({"type": "session_message", "payload": updated})
        return updated_cards

    # ── Session Handlers ──────────────────────────────────────────────

    async def _handle_create_session(self, ws: Any, data: dict) -> None:
        """Create a new Task (PENDING) + engine session. Broadcasts session_created."""
        try:
            run_engine, pid = await self._engine_for_request(data)
            result = await self.services.session.create(
                project_id=pid,
                title=data.get("title", "New Chat"),
                exec_mode=data.get("exec_mode", self._exec_mode),
                company_profile=data.get("company_profile"),
                preferred_agent=data.get("preferred_agent", self._task_preferred_agent),
                org_id=data.get("org_id") or data.get("organization_id"),
                interface="office_ui",
            )
            self._session_to_task.update(self.services_context.session_to_task)
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, action="create_session", **result.payload)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="create_session")

    async def _handle_session_update_config(self, ws: Any, data: dict) -> None:
        """Update a session's persisted execution configuration."""
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self.services.session.update_config(
                project_id=project_id,
                task_id=str(data.get("task_id", "") or ""),
                exec_mode=data.get("exec_mode"),
                company_profile=data.get("company_profile"),
                preferred_agent=data.get("preferred_agent"),
                org_id=data.get("org_id") or data.get("organization_id"),
            )
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, **result.payload)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="session_update_config")

    async def _handle_session_detail(self, ws: Any, data: dict) -> None:
        """Return a paginated persisted transcript/context for a single session."""
        if self._shutting_down:
            return

        task_id = str(data.get("task_id", "") or "").strip()
        if not task_id:
            await self._send_ack(ws, ok=False, error="task_id required")
            return

        run_engine, request_project_id = await self._engine_for_request(data)
        view_generation = data.get("view_generation")
        store = run_engine.store
        if not self._store_is_ready(store):
            await self._send_ack(ws, ok=False, error="store_not_ready", project_id=request_project_id, view_generation=view_generation)
            return

        task = await store.get_task(task_id)
        if not task:
            await self._send_ack(ws, ok=False, error="task_not_found", project_id=request_project_id, task_id=task_id, view_generation=view_generation, action="session_detail")
            return

        channel_id = f"session:{task_id}"
        session_id = getattr(task, "session_id", None)
        project_id = getattr(task, "project_id", None) or run_engine.project_id or request_project_id
        request_limit = max(1, min(int(data.get("limit", 200) or 200), 500))
        detail_level = _normalize_transcript_detail_level(data.get("detail_level", "summary"))
        raw_include = data.get("include")
        if isinstance(raw_include, list):
            include_set = {str(item or "").strip() for item in raw_include if str(item or "").strip()}
        else:
            include_set = {"messages", "session_state"}
            if detail_level == "full":
                include_set.update({"progress", "work_items", "runtime_context"})
        before_timestamp = self._normalize_session_detail_timestamp(data.get("before_created_at"))
        before_message_id = str(data.get("before_message_id", "") or "").strip() or None

        try:
            await self.chat_store.create_session_channel(
                task_id,
                getattr(task, "title", "") or "Session",
                project_id=project_id,
            )
        except Exception as exc:
            if self._is_expected_shutdown_error(exc):
                logger.debug(f"session_detail: channel bootstrap skipped during shutdown for {task_id}")
                return
            raise
        try:
            transcript_page, transcript_total_count, transcript_has_more = await self._load_session_transcript_page(
                task,
                limit=request_limit,
                detail_level=detail_level,
                before_timestamp=before_timestamp,
                before_message_id=before_message_id,
                engine=run_engine,
            )
            if transcript_page:
                await self.chat_store.backfill_messages(
                    channel_id,
                    transcript_page,
                    project_id=project_id,
                )
        except Exception:
            logger.opt(exception=True).debug(f"session_detail: transcript page load failed for {task_id}")
            transcript_total_count = 0
            transcript_has_more = False

        await self._reconcile_inactive_human_escalation_cards(
            channel_id,
            task_id=task_id,
            project_id=project_id,
        )
        await self._reconcile_execution_checkpoint_cards(
            channel_id,
            project_id=project_id,
            engine=run_engine,
        )

        try:
            cache_page = await self.chat_store.get_channel_messages_page_info(
                channel_id,
                limit=request_limit,
                before_timestamp=before_timestamp,
                before_message_id=before_message_id,
                detail_level=detail_level,
                project_id=project_id,
            )
            messages = list(cache_page.get("messages", []) or [])
            visible_cache_count = int(cache_page.get("total_count", len(messages)) or 0)
            cache_has_more = bool(cache_page.get("has_more", False))
            messages = self._filter_ui_messages_for_detail_level(messages, detail_level)
            messages = [_sanitize_ui_message_dict(message) for message in messages]
        except Exception as exc:
            if self._is_expected_shutdown_error(exc):
                logger.debug(f"session_detail: cache page skipped during shutdown for {task_id}")
                return
            messages = []
            visible_cache_count = len(messages)
            cache_has_more = False
        total_message_count = max(transcript_total_count, visible_cache_count, len(messages))
        has_more = transcript_has_more or cache_has_more

        task_meta = task.metadata if isinstance(getattr(task, "metadata", None), dict) else {}
        handoff_context = _extract_markdown_text(task_meta.get("handoff_context"), max_chars=None)
        task_description_context = _extract_markdown_text(getattr(task, "description", ""), max_chars=None)
        parent_session_link = _task_parent_session_link(task, task_meta)
        if not handoff_context and parent_session_link:
            handoff_context = task_description_context or await _build_session_context_preview(
                run_engine,
                session_id,
                max_chars=None,
            )
        handoff_to = _extract_markdown_text(task_meta.get("handoff_to"), max_chars=None)
        status_val = task.status.value if hasattr(task.status, "value") else str(task.status)
        created_at = getattr(task, "created_at", None)
        created_ts = created_at.timestamp() if hasattr(created_at, "timestamp") else time.time()
        updated_at = getattr(task, "updated_at", None)
        updated_ts = updated_at.timestamp() if hasattr(updated_at, "timestamp") else created_ts
        assigned_to = str(getattr(task, "assigned_to", "") or "").strip()
        assignee_ids: list[str] = []
        if assigned_to:
            try:
                assignee_ids = [self.event_adapter._resolve_role_to_agent(assigned_to)] if self.event_adapter else [assigned_to]
            except Exception:
                assignee_ids = [assigned_to]
        identity = self._resolve_task_identity(task)
        exec_mode_val = identity.exec_mode
        company_profile_val = identity.company_profile
        org_id_val = identity.org_id
        project_tasks = [task]
        if self._is_company_session_exec_mode(exec_mode_val):
            try:
                project_tasks = await store.get_tasks(project_id=project_id)
            except Exception:
                logger.opt(exception=True).debug("session_detail: failed to load project tasks for runtime control")
                project_tasks = [task]
        try:
            runtime_control_meta = (
                await _build_company_runtime_control_by_task(run_engine, project_tasks, project_id)
            ).get(task_id, {})
        except Exception:
            logger.opt(exception=True).debug("session_detail: failed to build runtime control payload")
            runtime_control_meta = {}
        runtime_meta = dict(task_meta.get("runtime_v2", {}) or {})
        member_session_meta = dict(task_meta.get("member_session_state", {}) or {})
        session_state: dict[str, Any] = {
            "project_id": project_id,
            "task_id": task_id,
            "runtime_task_id": task_id,
            "execution_turn_id": task_id,
            "session_id": session_id,
            "parent_session_id": parent_session_link or None,
            "mode": "child" if parent_session_link else "primary",
            "exec_mode": exec_mode_val,
            "company_profile": company_profile_val,
            "org_id": org_id_val,
            "channel_id": channel_id,
            "title": getattr(task, "title", "") or "Session",
            "status": status_val,
            "column_id": STATUS_TO_COLUMN.get(status_val, "todo"),
            "assignee_ids": assignee_ids,
            "priority": None,
            "tags": list(getattr(task, "tags", []) or []),
            "created_at": created_ts,
            "updated_at": updated_ts,
            "message_count": total_message_count,
            "handoff_context": handoff_context,
            "handoff_to": handoff_to,
            "origin_task_id": task_meta.get("origin_task_id") or task_id,
            "artifacts": task_meta.get("artifacts"),
            "runtime_session_id": runtime_meta.get("runtime_session_id"),
            "resume_cursor": runtime_meta.get("resume_cursor"),
            "active_subagents": list(runtime_meta.get("active_subagents", []) or []),
            "permission_requests": list(runtime_meta.get("permission_requests", []) or []),
            "worktree_path": runtime_meta.get("worktree_path"),
            "context_tokens": runtime_meta.get("context_tokens"),
            "context_window": runtime_meta.get("context_window"),
            "context_remaining_pct": runtime_meta.get("context_remaining_pct"),
            "input_tokens": runtime_meta.get("input_tokens"),
            "output_tokens": runtime_meta.get("output_tokens"),
            "total_tokens": runtime_meta.get("total_tokens"),
            "turn_cost_usd": runtime_meta.get("turn_cost_usd"),
            "session_cost_usd": runtime_meta.get("session_cost_usd"),
            "pending_permission_count": runtime_meta.get("pending_permission_count"),
            "drain_mode": runtime_meta.get("drain_mode"),
            "resident_status": normalize_role_runtime_status(
                member_session_meta.get("status") or member_session_meta.get("resident_status"),
                member_session_meta.get("focused_work_item_id"),
            ),
            "actionable_inbox_count": member_session_meta.get("actionable_inbox_count"),
            "protocol_backlog_count": member_session_meta.get("protocol_backlog_count"),
            "notification_backlog_count": member_session_meta.get("notification_backlog_count"),
            "latest_notification": member_session_meta.get("latest_notification"),
            "detail_loaded": True,
            "full_loaded": detail_level == "full" and not has_more,
            "has_more": has_more,
            "detail_loading": False,
            "view_generation": view_generation,
            **runtime_control_meta,
        }
        if "progress" in include_set or "work_items" in include_set or detail_level == "full":
            try:
                # Flush the in-memory progress buffer first: entries are
                # broadcast to clients before they reach the DB, so a snapshot
                # built from the DB alone would erase freshly streamed entries
                # from the client's live log (visible as flickering rows).
                await self._flush_progress(task_id, project_id=project_id)
                progress_log = await self.chat_store.get_progress(task_id, project_id=project_id)
            except Exception:
                progress_log = []
            session_state["progress_log"] = progress_log
        if "work_items" in include_set or detail_level == "full":
            session_state["work_item_log"] = list(task_meta.get("work_item_log", []) or [])
            if task_meta.get("role_work_items"):
                session_state["role_work_items"] = task_meta.get("role_work_items")
            if task_meta.get("executor_role_work_items"):
                session_state["executor_role_work_items"] = task_meta.get("executor_role_work_items")

        await self._send_ack(
            ws,
            ok=True,
            action="session_detail",
            project_id=project_id,
            view_generation=view_generation,
            task_id=task_id,
            channel_id=channel_id,
            session_id=session_id,
            detail_level=detail_level,
            include=sorted(include_set),
            message_count=total_message_count,
            loaded_count=len(messages),
            has_more=has_more,
            messages=messages,
            session_state=session_state,
            handoff_context=handoff_context,
            handoff_to=handoff_to,
        )

    async def _handle_session_send(self, ws: Any, data: dict) -> None:
        """Handle user message in a session. Auto-titles from first message."""
        task_id = data.get("task_id", "")
        content = data.get("content", "")
        if not content or not task_id:
            return
        run_engine, run_project_id = await self._engine_for_request(data)

        # Process file attachments via AttachmentStore (disk storage, lightweight refs)
        raw_attachments = data.get("attachments", [])
        attachment_refs: list[dict] = []
        attachment_errors: list[str] = []
        if raw_attachments:
            ensure_attachment_store = getattr(run_engine, "_ensure_attachment_store", None)
            if callable(ensure_attachment_store):
                try:
                    ensure_attachment_store()
                except Exception as exc:
                    logger.warning(f"Failed to refresh attachment store for project {run_project_id!r}: {exc}")
        att_store = getattr(run_engine, "attachment_store", None)
        if raw_attachments and att_store:
            for att in raw_attachments:
                filename = str(att.get("filename", "upload") or "upload").strip() or "upload"
                mime_type = str(att.get("mime_type", "") or "").strip() or None
                try:
                    ref = await att_store.save_from_base64(
                        filename,
                        att.get("data", ""),
                        mime_type=mime_type,
                    )
                    attachment_refs.append(ref.to_dict())
                except Exception as exc:
                    logger.warning(f"Attachment save failed: {exc}")
                    attachment_errors.append(f"{filename}: {exc}")
        elif raw_attachments and not att_store:
            attachment_errors.append("Attachment store is not available for the active project.")

        channel_id = f"session:{task_id}"
        pid = run_project_id

        # Look up session_id from task
        session_id: str | None = None
        task = None
        task_session_exec_mode = self._normalize_session_exec_mode(self._exec_mode)
        store = run_engine.store
        if self._store_is_ready(store):
            task = await store.get_task(task_id)
            if task:
                session_id = task.session_id
                task_session_exec_mode, _ = self._resolve_task_session_config(task)
            else:
                await self._send_ack(ws, ok=False, error="task_not_found", project_id=run_project_id, task_id=task_id)
                return

        # Only cancelled/deleted sessions are terminal — their session data has
        # been torn down, so no further input can be accepted. A DONE session is
        # NOT terminal: task-mode reuses the same primary task across follow-up
        # turns (engine `_find_reusable_task_mode_task` reopens a DONE task in the
        # same session), and company sessions accept follow-up text after final
        # delivery. Blocking DONE here made task mode one-shot — the second user
        # message was rejected before it ever reached the engine.
        if task:
            from opc.core.models import TaskStatus
            if task.status == TaskStatus.CANCELLED:
                try:
                    cancelled_target = await self._resolve_company_runtime_target(
                        task_id,
                        engine=run_engine,
                    )
                except Exception:
                    logger.opt(exception=True).debug(
                        "failed to resolve cancelled company UI anchor"
                    )
                    cancelled_target = None
                identity = (cancelled_target or {}).get("identity")
                active_cancelled_anchor = bool(
                    identity is not None
                    and str(getattr(identity, "ui_anchor_task_id", "") or "") == task_id
                    and str(getattr(identity, "pending_checkpoint_id", "") or "").strip()
                    and str(getattr(identity, "pending_checkpoint_type", "") or "").strip()
                    in COMPANY_RUNTIME_CHECKPOINT_TYPES
                    and str(getattr(identity, "pending_checkpoint_status", "") or "").strip().lower()
                    in ACTIVE_COMPANY_RUNTIME_CHECKPOINT_STATUSES
                )
                if not active_cancelled_anchor:
                    await self._send_ack(ws, ok=False, error="session_ended")
                    return

        if attachment_errors:
            helper_lines = [
                "Some attachments could not be included, so they were not sent to the model:",
                *[f"- {item}" for item in attachment_errors],
            ]
            helper = await self.chat_store.insert_message(
                channel_id=channel_id,
                sender="assistant",
                sender_name="OPC",
                content="\n".join(helper_lines),
                project_id=pid,
                metadata={"type": "system"},
            )
            await self.broadcast({"type": "session_message", "payload": helper})
            if raw_attachments and not attachment_refs and content.strip() == "Sent with attachments":
                return

        pending_escalation: dict[str, Any] | None = None
        background_pending_escalation: dict[str, Any] | None = None
        normalized_pending_reply: str | None = None

        # Insert user message to chat_store (UI rendering layer)
        reply_metadata: dict[str, Any] = {}
        raw_metadata = data.get("metadata")
        if isinstance(raw_metadata, dict):
            for key in (
                "response_to_checkpoint_id",
                "response_to_checkpoint_type",
                "response_to_escalation_id",
            ):
                value = raw_metadata.get(key)
                if value is None:
                    continue
                normalized_value = str(value).strip()
                if normalized_value:
                    reply_metadata[key] = normalized_value
            raw_checkpoint_reply_kind = str(raw_metadata.get("checkpoint_reply_kind", "") or "").strip().lower()
            if raw_checkpoint_reply_kind in {"approve", "deny", "feedback", "ignore"}:
                reply_metadata["checkpoint_reply_kind"] = raw_checkpoint_reply_kind
            raw_ui_message_id = str(raw_metadata.get("ui_message_id", "") or "").strip()
            if raw_ui_message_id:
                reply_metadata["ui_message_id"] = raw_ui_message_id
            raw_role_agents = raw_metadata.get("recruitment_role_agents")
            if isinstance(raw_role_agents, dict):
                normalized_role_agents: dict[str, str] = {}
                for raw_role_id, raw_agent in raw_role_agents.items():
                    role_id = str(raw_role_id or "").strip()
                    agent_name = str(raw_agent or "").strip().lower()
                    if role_id and agent_name in _TASK_MODE_PREFERRED_AGENTS:
                        normalized_role_agents[role_id] = agent_name
                if normalized_role_agents:
                    reply_metadata["recruitment_role_agents"] = normalized_role_agents
            raw_recruitment_agent = str(raw_metadata.get("recruitment_agent", "") or "").strip().lower().replace("-", "_")
            if raw_recruitment_agent in _TASK_MODE_PREFERRED_AGENTS:
                reply_metadata["recruitment_agent"] = raw_recruitment_agent
            raw_staffing_action = str(raw_metadata.get("staffing_action", "") or "").strip().lower()
            if raw_staffing_action in {"manual_approve", "approve", "auto_recruit", "deny"}:
                reply_metadata["staffing_action"] = (
                    "manual_approve" if raw_staffing_action == "approve" else raw_staffing_action
                )
            raw_staffing_selections = raw_metadata.get("staffing_selections")
            if isinstance(raw_staffing_selections, dict):
                normalized_selections: dict[str, dict[str, str]] = {}
                for raw_role_id, raw_selection in raw_staffing_selections.items():
                    role_id = str(raw_role_id or "").strip()
                    if not role_id or not isinstance(raw_selection, dict):
                        continue
                    kind = str(raw_selection.get("kind", "") or "").strip().lower()
                    selected_id = str(
                        raw_selection.get("id")
                        or raw_selection.get("employee_id")
                        or raw_selection.get("template_id")
                        or ""
                    ).strip()
                    if kind in {"employee", "template"} and selected_id:
                        normalized_selections[role_id] = {"kind": kind, "id": selected_id}
                    elif kind == "fallback":
                        normalized_selections[role_id] = {"kind": "fallback", "id": ""}
                if normalized_selections:
                    reply_metadata["staffing_selections"] = normalized_selections
            raw_user_input_answers = raw_metadata.get("user_input_answers")
            if isinstance(raw_user_input_answers, dict):
                normalized_answers: dict[str, dict[str, Any]] = {}
                for raw_question_id, raw_answer in raw_user_input_answers.items():
                    question_id = str(raw_question_id or "").strip()
                    if not question_id:
                        continue
                    if isinstance(raw_answer, dict):
                        answer: dict[str, Any] = {}
                        for field in (
                            "question_id",
                            "question",
                            "selected_option_id",
                            "selected_label",
                            "freeform_text",
                            "answer_text",
                        ):
                            value = raw_answer.get(field)
                            if value is None:
                                continue
                            normalized_value = str(value).strip()
                            if normalized_value:
                                answer[field] = normalized_value
                        if answer:
                            answer.setdefault("question_id", question_id)
                            normalized_answers[question_id] = answer
                    else:
                        normalized_value = str(raw_answer or "").strip()
                        if normalized_value:
                            normalized_answers[question_id] = {
                                "question_id": question_id,
                                "answer_text": normalized_value,
                            }
                if normalized_answers:
                    reply_metadata["user_input_answers"] = normalized_answers

        # Idempotency on the client-generated message id: the WS client queues
        # sends while disconnected and flushes the queue after a reconnect, so
        # one typed message can be delivered more than once. The first delivery
        # persisted a row under this id in this channel; later copies are
        # acknowledged and dropped instead of dispatching a duplicate turn.
        client_message_id = str(reply_metadata.get("ui_message_id", "") or "").strip()
        if client_message_id:
            existing_scope = await self.chat_store.message_scope(client_message_id)
            if existing_scope == (channel_id, pid):
                logger.info(
                    f"session_send deduplicated re-delivered client message {client_message_id} "
                    f"for task {task_id}"
                )
                await self._send_ack(
                    ws,
                    ok=True,
                    action="session_send",
                    task_id=task_id,
                    project_id=pid,
                    deduplicated=True,
                    message_id=client_message_id,
                )
                return
            if existing_scope is not None:
                # Same id already used in another channel/project: never reuse it
                # as a row id there (insert_message REPLACEs by primary key).
                client_message_id = ""

        explicit_checkpoint_id = str(reply_metadata.get("response_to_checkpoint_id", "")).strip()
        explicit_checkpoint_type = str(reply_metadata.get("response_to_checkpoint_type", "")).strip()
        explicit_escalation_id = str(reply_metadata.get("response_to_escalation_id", "")).strip()
        explicit_human_escalation = (
            explicit_checkpoint_type == "human_escalation"
            or bool(explicit_escalation_id)
        )
        if explicit_human_escalation:
            pending_escalation_id = explicit_escalation_id
            if (
                not pending_escalation_id
                and reply_metadata.get("response_to_checkpoint_type") == "human_escalation"
            ):
                pending_escalation_id = reply_metadata.get("response_to_checkpoint_id")

            pending_escalation = self._find_pending_escalation(
                task_id=task_id,
                escalation_id=pending_escalation_id,
                project_id=pid,
            )
        elif not explicit_checkpoint_id and not explicit_checkpoint_type:
            background_pending_escalation = self._find_pending_escalation(
                task_id=task_id,
                project_id=pid,
            )

        stale_human_escalation = (
            explicit_human_escalation
            and not pending_escalation
            and bool(explicit_checkpoint_id or explicit_escalation_id)
        )
        if stale_human_escalation:
            handled_as_deferred = False
            if _looks_like_escalation_reply(content):
                stale_checkpoint_id = explicit_escalation_id or explicit_checkpoint_id
                # Duplicate clicks on an approval card that was JUST resolved
                # (e.g. the user's own first click) are a normal occurrence
                # when the server is slow: answer idempotently instead of
                # flipping the card to "stale" and spamming inactive warnings.
                card = None
                try:
                    card = await self.chat_store.get_checkpoint_message(
                        stale_checkpoint_id,
                        channel_id=channel_id,
                        checkpoint_type="human_escalation",
                        project_id=pid,
                    )
                except Exception:
                    logger.opt(exception=True).debug(
                        "Failed to load checkpoint card for stale escalation reply"
                    )
                card_meta = dict((card or {}).get("metadata", {}) or {})
                card_status = str(card_meta.get("checkpoint_status", "") or "").strip().lower()
                helper_text: str | None = None
                if card_status in {"resolved", "responded"}:
                    resolution_reply = str(
                        card_meta.get("checkpoint_resolution_reply", "") or ""
                    ).strip()
                    helper_text = (
                        "This approval was already handled"
                        + (f" (decision: {resolution_reply})" if resolution_reply else "")
                        + ". No further action is needed."
                    )
                else:
                    approval_context = card_meta.get("approval_context")
                    deferred_option = (
                        _normalize_escalation_reply(content, list(card_meta.get("options") or []))
                        if isinstance(approval_context, dict) and approval_context
                        else None
                    )
                    if deferred_option:
                        # The inline wait expired (or the server restarted), but
                        # the decision is still the user's to make: apply the
                        # grant, resolve the card, and resume the parked task.
                        outcome = await self._resolve_deferred_escalation_click(
                            engine=run_engine,
                            project_id=pid,
                            channel_id=channel_id,
                            checkpoint_id=stale_checkpoint_id,
                            card_meta=card_meta,
                            option_id=deferred_option,
                        )
                        if outcome.get("action") == "flow_through":
                            content = str(outcome.get("content") or content)
                            for key in (
                                "response_to_checkpoint_id",
                                "response_to_checkpoint_type",
                                "response_to_escalation_id",
                            ):
                                reply_metadata.pop(key, None)
                            reply_metadata.update(dict(outcome.get("reply_metadata") or {}))
                            handled_as_deferred = True
                        else:
                            helper_text = str(outcome.get("text") or "Decision recorded.")
                    else:
                        await self._mark_human_escalation_checkpoint_status(
                            stale_checkpoint_id,
                            status="stale",
                            project_id=pid,
                            channel_id=channel_id,
                            reason="reply_to_inactive_escalation",
                        )
                        helper_text = (
                            "That approval request is no longer active. "
                            "The approval card has been marked inactive in the session history."
                        )
                if helper_text is not None:
                    if await self._recent_identical_helper_exists(
                        channel_id, helper_text, project_id=pid
                    ):
                        return
                    helper = await self.chat_store.insert_message(
                        channel_id=channel_id,
                        sender="assistant",
                        sender_name="OPC",
                        content=helper_text,
                        project_id=pid,
                        metadata={"type": "system"},
                    )
                    await self.broadcast({"type": "session_message", "payload": helper})
                    return
            if not handled_as_deferred:
                for key in (
                    "response_to_checkpoint_id",
                    "response_to_checkpoint_type",
                    "response_to_escalation_id",
                ):
                    reply_metadata.pop(key, None)

        if (
            explicit_checkpoint_type == "company_delivery_feedback"
            and str(reply_metadata.get("checkpoint_reply_kind", "") or "").strip().lower() == "ignore"
        ):
            if await self._route_company_delivery_feedback_reply_if_pending(
                task_id=task_id,
                content=content,
                session_id=session_id,
                task=task,
                attachment_refs=attachment_refs or None,
                message_metadata=reply_metadata if reply_metadata else None,
                user_message_id=None,
                user_message_created_at=None,
                run_engine=run_engine,
                run_project_id=run_project_id,
                reply_channel_id=channel_id,
            ):
                return

        msg_metadata: dict = dict(reply_metadata)
        if attachment_refs:
            msg_metadata["attachment_refs"] = attachment_refs
        msg = await self.chat_store.insert_message(
            channel_id=channel_id,
            sender="user",
            sender_name="You",
            content=content,
            project_id=pid,
            metadata=msg_metadata if msg_metadata else None,
            # Persist under the client-generated id so re-deliveries of the same
            # send are detectable and the optimistic bubble merges with the echo.
            message_id=client_message_id or None,
        )
        await self.broadcast({"type": "session_message", "payload": msg})

        if (
            explicit_checkpoint_id
            and explicit_checkpoint_type
            and explicit_checkpoint_type not in {"human_escalation", "company_delivery_feedback"}
        ):
            updated_checkpoint_msg = await self.chat_store.mark_checkpoint_responded(
                channel_id,
                explicit_checkpoint_id,
                checkpoint_type=explicit_checkpoint_type,
                response_message_id=str(msg.get("message_id") or "").strip() or None,
                response_metadata=reply_metadata if reply_metadata else None,
                project_id=pid,
            )
            if updated_checkpoint_msg is not None:
                await self.broadcast({"type": "session_message", "payload": updated_checkpoint_msg})

        # Auto-generate title from first message if task title is still default
        store = run_engine.store
        if self._store_is_ready(store) and task:
            if task.title in ("New Chat", ""):
                auto_title = _compact_session_title(content)
                task.title = auto_title
                await store.save_task(task)
                await self.chat_store.update_channel_name(channel_id, auto_title, project_id=pid)
                # Also update engine session title
                if run_engine.memory and session_id:
                    await run_engine.memory.update_session_title(session_id, auto_title)
                await self.broadcast({"type": "session_title_updated", "payload": {
                    "project_id": pid,
                    "task_id": task_id,
                    "title": auto_title,
                }})

        if background_pending_escalation:
            escalation_key = str(background_pending_escalation.get("escalation_id") or "").strip()
            helper_text = (
                "This approval is waiting for a card action. "
                "Please use the approval card buttons to approve or deny."
            )
            if not _looks_like_escalation_reply(content):
                allowed = [
                    str(opt.get("label") or opt.get("id") or "").strip()
                    for opt in background_pending_escalation.get("options", [])
                    if str(opt.get("id", "")).strip()
                ]
                if allowed:
                    helper_text = (
                        "This task is waiting for an approval decision. "
                        f"Use the approval card buttons: {', '.join(allowed)}."
                    )
            helper = await self.chat_store.insert_message(
                channel_id=channel_id,
                sender="assistant",
                sender_name="OPC",
                content=helper_text,
                project_id=pid,
                metadata={
                    "type": "system",
                    "pending_checkpoint_type": "human_escalation",
                    "pending_escalation_id": escalation_key,
                },
            )
            await self.broadcast({"type": "session_message", "payload": helper})
            return

        if pending_escalation:
            normalized = normalized_pending_reply
            if normalized is None:
                normalized = _normalize_escalation_reply(content, pending_escalation.get("options", []))
            if normalized is None:
                allowed = [
                    str(opt.get("label") or opt.get("id") or "").strip()
                    for opt in pending_escalation.get("options", [])
                    if str(opt.get("id", "")).strip()
                ]
                helper = await self.chat_store.insert_message(
                    channel_id=channel_id,
                    sender="assistant",
                    sender_name="OPC",
                    content=(
                        "This task is waiting for your escalation decision. "
                        f"Choose one of: {', '.join(allowed)}."
                    ),
                    metadata={
                        "checkpoint_type": "human_escalation",
                        "checkpoint_id": pending_escalation.get("escalation_id"),
                        "escalation_id": pending_escalation.get("escalation_id"),
                        "escalation_type": pending_escalation.get("escalation_type"),
                        "prompt": pending_escalation.get("message", ""),
                        "summary": pending_escalation.get("message", ""),
                        "options": pending_escalation.get("options", []),
                        "default_action": pending_escalation.get("default_action"),
                    },
                    project_id=pid,
                )
                await self.broadcast({"type": "session_message", "payload": helper})
                return
            future = pending_escalation.get("future")
            if future and not future.done():
                future.set_result(normalized)
            auto_resolved_ids = self._resolve_related_pending_escalations(pending_escalation, normalized)
            for escalation_id in auto_resolved_ids:
                updated = await self.chat_store.mark_checkpoint_responded(
                    channel_id,
                    escalation_id,
                    checkpoint_type="human_escalation",
                    response_message_id=msg.get("message_id"),
                    response_metadata=reply_metadata if reply_metadata else None,
                    project_id=pid,
                )
                if updated is not None:
                    await self.broadcast({"type": "session_message", "payload": updated})
            return

        if await self._route_company_delivery_feedback_reply_if_pending(
            task_id=task_id,
            content=content,
            session_id=session_id,
            task=task,
            attachment_refs=attachment_refs or None,
            message_metadata=reply_metadata if reply_metadata else None,
            user_message_id=str(msg.get("message_id") or "").strip() or None,
            user_message_created_at=float(msg.get("created_at")) if msg.get("created_at") is not None else None,
            run_engine=run_engine,
            run_project_id=run_project_id,
            reply_channel_id=channel_id,
        ):
            return

        if (
            task
            and self._is_company_session_exec_mode(task_session_exec_mode)
            and not explicit_checkpoint_id
            and not explicit_checkpoint_type
            and not explicit_escalation_id
        ):
            await self._supersede_pending_delivery_feedback_for_new_company_turn(
                task_id=task_id,
                session_id=session_id,
                run_engine=run_engine,
                run_project_id=run_project_id,
            )

        if await self._route_company_suspend_reply_if_pending(
            task_id=task_id,
            content=content,
            session_id=session_id,
            task=task,
            attachment_refs=attachment_refs or None,
            message_metadata=reply_metadata if reply_metadata else None,
            user_message_id=str(msg.get("message_id") or "").strip() or None,
            user_message_created_at=float(msg.get("created_at")) if msg.get("created_at") is not None else None,
            run_engine=run_engine,
            run_project_id=run_project_id,
        ):
            return

        if task and self._is_company_session_exec_mode(task_session_exec_mode):
            self._track_session(
                task_id,
                self._process_session_message(
                    task_id,
                    content,
                    session_id=session_id,
                    attachment_refs=attachment_refs or None,
                    message_metadata=reply_metadata if reply_metadata else None,
                    user_message_id=str(msg.get("message_id") or "").strip() or None,
                    user_message_created_at=float(msg.get("created_at")) if msg.get("created_at") is not None else None,
                    run_engine=run_engine,
                    run_project_id=run_project_id,
                ),
                project_id=run_project_id,
                engine=run_engine,
            )
            return

        # Route task-mode sessions through Dispatcher: classify intent, then either engine or direct reply.
        self._track_session(
            task_id,
            self._dispatch_session_message(
                task_id,
                content,
                session_id=session_id,
                attachment_refs=attachment_refs or None,
                message_metadata=reply_metadata if reply_metadata else None,
                user_message_id=str(msg.get("message_id") or "").strip() or None,
                user_message_created_at=float(msg.get("created_at")) if msg.get("created_at") is not None else None,
                run_engine=run_engine,
                run_project_id=run_project_id,
            ),
            project_id=run_project_id,
            engine=run_engine,
        )

    async def _cancel_task_tree(
        self,
        task_id: str,
        *,
        hard: bool = False,
        preserve_history: bool = False,
        store: Any | None = None,
    ) -> list[str]:
        """Cancel a task and its children, clean up engine store data.

        Args:
            hard: If True, hard-delete task rows and all lifecycle data
                  (used by session_delete).  If False, soft-cancel and
                  partial cleanup (used by session_stop).

        Returns all affected task_ids (parent + children).
        """
        all_task_ids: list[str] = [task_id]
        store = store or self.engine.store
        if not store:
            return all_task_ids
        from opc.core.models import TaskStatus
        task = await store.get_task(task_id)
        if not task:
            return all_task_ids

        # Collect child tasks first (before any deletion)
        child_tasks: list[tuple[str, str | None]] = []
        parent_sid = task.session_id or task_id
        project_id = task.project_id or getattr(store, "project_id", None) or self.engine.project_id or "default"
        try:
            siblings = await store.get_tasks(project_id=project_id)
            for sib in siblings:
                if sib.id == task_id:
                    continue
                if getattr(sib, "parent_session_id", None) == parent_sid:
                    child_tasks.append((sib.id, sib.session_id))
                    all_task_ids.append(sib.id)
        except Exception:
            logger.debug(f"Failed to find children of {task_id}")

        async def _cancel_child_via_phase(child_task: Any) -> None:
            """Cascade CANCELLED to the child's work_item.phase so the UI
            column + all projection layers update. Plain tasks keep legacy
            Task.status behavior through the shared transition fallback.
            """
            child_task.metadata = dict(getattr(child_task, "metadata", {}) or {})
            child_task.metadata["last_stop_reason"] = "user_stop"
            await apply_task_status_transition(
                store,
                child_task,
                target_status_or_phase=TaskStatus.CANCELLED,
                reason="user_stop",
                metadata_updates={"last_stop_reason": "user_stop"},
                release_claim=True,
            )

        if hard:
            # Hard-delete: children first, then parent (preserves session ref-count accuracy)
            for child_id, child_sid in child_tasks:
                try:
                    await store.hard_delete_task(child_id, child_sid)
                except Exception:
                    logger.debug(f"hard_delete_task failed for child {child_id}")
            try:
                await store.hard_delete_task(task_id, task.session_id)
            except Exception:
                logger.debug(f"hard_delete_task failed for {task_id}")
        elif preserve_history:
            # Parent stays IDLE (suspend-for-resume — user hit Stop, may resume
            # later). We do NOT transition parent's work_item here; resuming
            # should pick it back up in whatever phase it was parked in.
            task.metadata["last_stop_reason"] = "user_stop"
            task.status = TaskStatus.IDLE
            await store.save_task(task)
            for child_id, _child_sid in child_tasks:
                try:
                    child_task = await store.get_task(child_id)
                    if child_task:
                        await _cancel_child_via_phase(child_task)
                except Exception:
                    logger.debug(f"Failed to preserve stop state for child task {child_id}")
        else:
            # Soft-cancel: cascade CANCELLED phase to parent + children
            await apply_task_status_transition(
                store,
                task,
                target_status_or_phase=TaskStatus.CANCELLED,
                reason="user_cancel",
                release_claim=True,
            )
            try:
                await store.delete_session_data(task_id, task.session_id)
            except Exception:
                logger.debug(f"Failed to clean session data for {task_id}")
            for child_id, child_sid in child_tasks:
                try:
                    child_task = await store.get_task(child_id)
                    if child_task:
                        await _cancel_child_via_phase(child_task)
                    await store.delete_session_data(child_id, child_sid)
                except Exception:
                    pass

        # Clean progress buffers + cancel background tasks for all affected
        for tid in all_task_ids:
            self._progress_buffer.pop(tid, None)
            self._progress_project_ids.pop(tid, None)
            if hard:
                try:
                    await self.chat_store.delete_progress(tid, project_id=project_id)
                except Exception:
                    pass
            self._cancel_session_tasks(tid)

        return all_task_ids

    async def _resolve_company_runtime_target(
        self,
        task_id: str,
        *,
        engine: Any | None = None,
        runtime_session_id: str = "",
        checkpoint_id: str = "",
    ) -> dict[str, Any] | None:
        """Resolve a Task/UI channel to the canonical session-first scope."""
        runtime_engine = engine or self.engine
        store = runtime_engine.store
        if not task_id or not self._store_is_ready(store):
            return None
        task = await store.get_task(task_id)
        if task is None:
            return None
        project_id = self._normalize_project_id(
            getattr(task, "project_id", None) or getattr(runtime_engine, "project_id", None)
        )
        identity_index = await load_company_runtime_identity_index(store, project_id)
        identity = identity_index.resolve(
            task_id=task_id,
            runtime_session_id=runtime_session_id,
            checkpoint_id=checkpoint_id,
        )
        if identity is None:
            return None
        ui_anchor_task_id = identity.ui_anchor_task_id
        config_task = identity_index.task(identity.config_source_task_id) or task

        return {
            "task": task,
            "engine": runtime_engine,
            "identity": identity,
            "runtime_session_id": identity.runtime_session_id,
            "ui_channel_task_id": task_id,
            "ui_anchor_task_id": ui_anchor_task_id,
            "config_source_task_id": identity.config_source_task_id,
            "config_task": config_task,
            "checkpoint": identity.checkpoint,
            "origin_task_id": ui_anchor_task_id,
            "affected_task_ids": list(identity.runtime_task_ids),
        }

    async def _broadcast_company_runtime_control(
        self,
        target: dict[str, Any],
        *,
        state: str,
        checkpoint_id: str = "",
        stop_intent_id: str = "",
    ) -> None:
        affected_task_ids = [
            str(item).strip()
            for item in list(target.get("affected_task_ids", []) or [])
            if str(item).strip()
        ]
        runtime_engine = target.get("engine") or self.engine
        project_id = self._normalize_project_id(getattr(runtime_engine, "project_id", None))
        payload = {
            "project_id": project_id,
            "runtime_control_state": state,
            "can_stop": state == "running",
            "can_resume": state == "suspended",
            "resume_parent_session_id": str(target.get("runtime_session_id", "") or "").strip(),
            "pending_runtime_checkpoint_id": checkpoint_id,
            "stop_intent_id": stop_intent_id,
            "task_ids": affected_task_ids,
        }
        await self.broadcast({"type": "session_runtime_control", "payload": payload})

    async def _set_company_runtime_control(
        self,
        target: dict[str, Any] | None,
        *,
        state: str,
        checkpoint_id: str = "",
        stop_intent_id: str = "",
    ) -> None:
        if target is None:
            return
        await self._broadcast_company_runtime_control(
            target,
            state=state,
            checkpoint_id=checkpoint_id,
            stop_intent_id=stop_intent_id,
        )

    async def _finalize_company_runtime_stop(self, target: dict[str, Any], *, stop_intent_id: str) -> None:
        runtime_engine = target.get("engine") or self.engine
        parent_session_id = str(target.get("runtime_session_id", "") or "").strip()
        origin_task_id = str(target.get("origin_task_id", "") or "").strip()
        affected_task_ids = [
            str(item).strip()
            for item in list(target.get("affected_task_ids", []) or [])
            if str(item).strip()
        ]
        suspended: dict[str, Any] | None = None
        try:
            suspended = await runtime_engine.suspend_company_runtime(
                origin_task_id=origin_task_id,
                session_id=parent_session_id or None,
                reason="user_stop",
                checkpoint_type="company_runtime_suspended",
                stop_intent_id=stop_intent_id,
            )
        except Exception:
            logger.opt(exception=True).warning(f"suspend_company_runtime failed for {origin_task_id}")

        if suspended is not None:
            for candidate in list(suspended.get("task_ids", []) or []):
                candidate_id = str(candidate or "").strip()
                if candidate_id and candidate_id not in affected_task_ids:
                    affected_task_ids.append(candidate_id)
            target["affected_task_ids"] = affected_task_ids
            self._stop_requested_task_ids.update(affected_task_ids)
            for tid in affected_task_ids:
                self._progress_buffer.pop(tid, None)
                self._progress_project_ids.pop(tid, None)
                self._cancel_session_tasks(tid)
            try:
                await self._set_company_runtime_control(
                    target,
                    state="suspended",
                    checkpoint_id=str(suspended.get("checkpoint_id", "") or ""),
                    stop_intent_id=stop_intent_id,
                )
            except Exception:
                logger.opt(exception=True).debug("failed to broadcast company runtime suspended state")
            channel_id = f"session:{target.get('ui_channel_task_id', '')}"
            pid = self._normalize_project_id(getattr(runtime_engine, "project_id", None))
            try:
                msg = await self.chat_store.insert_message(
                    channel_id=channel_id,
                    sender="system",
                    sender_name="System",
                    content="Company runtime suspended by user",
                    project_id=pid,
                    metadata={
                        "type": "system",
                        "stop_reason": "user_stop",
                        "checkpoint_type": suspended.get("checkpoint_type"),
                        "checkpoint_id": suspended.get("checkpoint_id"),
                        "stop_intent_id": stop_intent_id,
                    },
                )
                await self.broadcast({"type": "session_message", "payload": msg})
            except Exception:
                logger.opt(exception=True).warning(f"Failed to insert suspend message for {origin_task_id}")
        else:
            try:
                await self._set_company_runtime_control(
                    target,
                    state="running" if affected_task_ids else "idle",
                    stop_intent_id=stop_intent_id,
                )
            except Exception:
                pass
        self._company_stop_intents.pop(parent_session_id, None)
        self._company_stop_finalize_tasks.pop(parent_session_id, None)

    async def _handle_session_stop(self, ws: Any, data: dict) -> None:
        """Stop a running task: suspend company runtime, cancel legacy task mode."""
        task_id = data.get("task_id", "")
        if not task_id:
            return

        run_engine, run_project_id = await self._engine_for_request(data)
        store = run_engine.store
        task = None
        if self._store_is_ready(store):
            try:
                task = await store.get_task(task_id)
            except Exception:
                logger.opt(exception=True).debug(f"Failed to load task for stop: {task_id}")
            if task is None:
                await self._send_ack(ws, ok=False, error="task_not_found", project_id=run_project_id, task_id=task_id)
                return

        target = None
        if task is not None:
            try:
                target = await self._resolve_company_runtime_target(task_id, engine=run_engine)
            except Exception:
                logger.opt(exception=True).warning(
                    f"failed to resolve company runtime stop target for {task_id}"
                )
            if target is not None:
                parent_session_id = str(target.get("runtime_session_id", "") or "").strip()
                existing_checkpoint = None
                try:
                    existing_checkpoint = await run_engine.get_pending_company_runtime_suspend_checkpoint(parent_session_id)
                except Exception:
                    logger.opt(exception=True).debug("failed to check existing company suspend checkpoint")
                existing_intent = self._company_stop_intents.get(parent_session_id)
                existing_finalizer = self._company_stop_finalize_tasks.get(parent_session_id)
                if existing_checkpoint is not None:
                    await self._set_company_runtime_control(
                        target,
                        state="suspended",
                        checkpoint_id=existing_checkpoint.checkpoint_id,
                        stop_intent_id=str((existing_checkpoint.payload or {}).get("stop_intent_id", "") or ""),
                    )
                    await self._send_ack(ws, ok=True, idempotent=True)
                    return
                if existing_intent or (existing_finalizer is not None and not existing_finalizer.done()):
                    await self._set_company_runtime_control(
                        target,
                        state="suspending",
                        stop_intent_id=str((existing_intent or {}).get("stop_intent_id", "") or ""),
                    )
                    await self._send_ack(ws, ok=True, idempotent=True)
                    return

                stop_intent_id = str(uuid.uuid4())
                self._company_stop_intents[parent_session_id] = {
                    "stop_intent_id": stop_intent_id,
                    "requested_at": datetime.now().isoformat(),
                    "origin_task_id": target.get("origin_task_id", task_id),
                }
                await self._set_company_runtime_control(
                    target,
                    state="suspending",
                    stop_intent_id=stop_intent_id,
                )
                finalizer = self._track(
                    self._finalize_company_runtime_stop(target, stop_intent_id=stop_intent_id)
                )
                self._company_stop_finalize_tasks[parent_session_id] = finalizer
                await self._send_ack(ws, ok=True, stop_intent_id=stop_intent_id)
                return

            if is_company_runtime_task(task):
                await self._send_ack(
                    ws,
                    ok=False,
                    error="company_runtime_identity_mismatch",
                    project_id=run_project_id,
                    task_id=task_id,
                )
                return

        try:
            all_task_ids = await self._cancel_task_tree(task_id, preserve_history=True, store=store)
        except Exception:
            logger.opt(exception=True).warning(f"_cancel_task_tree failed for {task_id}")
            all_task_ids = [task_id]
        self._stop_requested_task_ids.update(all_task_ids)

        # Broadcast status updates for all affected tasks
        for tid in all_task_ids:
            is_primary_task = tid == task_id
            try:
                await self.broadcast({"type": "board_task_status_changed", "payload": {
                    "project_id": run_project_id,
                    "task_id": tid,
                    "column_id": "in-progress" if is_primary_task else "done",
                    "status": "idle" if is_primary_task else "cancelled",
                }})
                stop_payload: dict[str, Any] = {
                    "project_id": run_project_id,
                    "task_id": tid, "status": "idle", "current_tool": None, "iteration": 0,
                }
                # Resolve agent_id from event_adapter map or task.assigned_to
                stop_task = None
                if self._store_is_ready(store):
                    try:
                        stop_task = await store.get_task(tid)
                    except Exception:
                        pass
                resolved_agent = self._resolve_agent_for_idle(tid, stop_task)
                if resolved_agent:
                    stop_payload["agent_id"] = resolved_agent
                await self.broadcast({"type": "agent_runtime_update", "payload": stop_payload})
            except Exception:
                logger.opt(exception=True).warning(f"Failed to broadcast stop status for {tid}")

        # Insert system message (only for the primary task — children are internal)
        channel_id = f"session:{task_id}"
        pid = run_project_id
        try:
            msg = await self.chat_store.insert_message(
                channel_id=channel_id,
                sender="system",
                sender_name="System",
                content="Task stopped by user",
                project_id=pid,
                metadata={"type": "system", "stop_reason": "user_stop"},
            )
            await self.broadcast({"type": "session_message", "payload": msg})
        except Exception:
            logger.opt(exception=True).warning(f"Failed to insert stop message for {task_id}")
        await self._send_ack(ws, ok=True)

    async def _handle_session_resume(self, ws: Any, data: dict) -> None:
        """Resume by durable runtime-session/checkpoint identity."""
        task_id = str(data.get("task_id", "") or "").strip()
        if not task_id:
            await self._send_ack(ws, ok=False, error="missing_task_id")
            return
        run_engine, run_project_id = await self._engine_for_request(data)

        async def reject(error: str, **extra: Any) -> None:
            await self._send_ack(ws, ok=False, error=error, **extra)
            await self._refresh_runtime_control_for_client(
                ws,
                engine=run_engine,
                project_id=run_project_id,
            )

        task = None
        session_id_override = str(data.get("session_id", "") or "").strip() or None
        if self._store_is_ready(run_engine.store):
            try:
                task = await run_engine.store.get_task(task_id)
            except Exception:
                logger.opt(exception=True).warning(f"session_resume: get_task failed for {task_id}")
            if task is None:
                await reject("task_not_found", project_id=run_project_id, task_id=task_id)
                return
        session_id = session_id_override
        if not session_id and task is not None:
            session_id = str(task.session_id or task.parent_session_id or "").strip() or None
        exec_mode, _company_profile = self._resolve_task_session_config(task)
        runtime_session_id = str(data.get("runtime_session_id", "") or "").strip()
        checkpoint_id = str(data.get("checkpoint_id", "") or "").strip()
        is_company_resume = bool(runtime_session_id or checkpoint_id) or exec_mode in {
            "company",
            "org",
            "custom",
        }
        if is_company_resume and task is not None:
            if not runtime_session_id:
                await reject("missing_runtime_session_id")
                return
            if not checkpoint_id:
                await reject("missing_checkpoint_id")
                return
            try:
                target = await self._resolve_company_runtime_target(
                    task_id,
                    engine=run_engine,
                    runtime_session_id=runtime_session_id,
                    checkpoint_id=checkpoint_id,
                )
            except Exception:
                logger.opt(exception=True).warning(f"session_resume: failed to resolve company runtime target for {task_id}")
                target = None
            if target is None:
                await reject(
                    "company_runtime_identity_mismatch",
                    project_id=run_project_id,
                    task_id=task_id,
                )
                return
            checkpoint = target.get("checkpoint")
            if checkpoint is None or str(getattr(checkpoint, "status", "") or "").strip().lower() != "pending":
                await reject("checkpoint_not_pending", checkpoint_id=checkpoint_id)
                return
            session_id = runtime_session_id
            finalizer = self._company_stop_finalize_tasks.get(runtime_session_id)
            if finalizer is not None and not finalizer.done():
                try:
                    await asyncio.wait_for(asyncio.shield(finalizer), timeout=10.0)
                except asyncio.TimeoutError:
                    await reject("stop_finalize_in_progress")
                    return
                except Exception:
                    logger.opt(exception=True).debug("session_resume: stop finalizer ended with error")
            self._company_stop_intents.pop(runtime_session_id, None)
            content = str(data.get("content", "") or "").strip() or "Resume the existing runtime."
            lock = self._company_suspend_reply_locks.get(runtime_session_id)
            if lock is not None:
                self._release_current_execution_handoff()
                await reject(
                    "checkpoint_handoff_in_progress",
                    checkpoint_id=checkpoint_id,
                )
                return
            lock = asyncio.Lock()
            self._company_suspend_reply_locks[runtime_session_id] = lock
            try:
                bg = self._track_session(
                    task_id,
                    self._process_company_suspend_reply(
                        ui_task_id=task_id,
                        runtime_session_id=runtime_session_id,
                        content=content,
                        attachment_refs=None,
                        message_metadata={
                            "ui_force_resume": True,
                            "response_to_checkpoint_id": checkpoint_id,
                            "response_to_checkpoint_type": str(getattr(checkpoint, "checkpoint_type", "") or ""),
                        },
                        user_message_id=None,
                        user_message_created_at=None,
                        run_engine=run_engine,
                        run_project_id=run_project_id,
                        target=target,
                        checkpoint=checkpoint,
                        lock=lock,
                    ),
                    project_id=run_project_id,
                    engine=run_engine,
                )
            except BaseException:
                self._company_suspend_reply_locks.pop(runtime_session_id, None)
                raise
            self._task_bg_context[bg]["company_suspend_reply"] = True
            await self._send_ack(
                ws,
                ok=True,
                runtime_session_id=runtime_session_id,
                checkpoint_id=checkpoint_id,
            )
            return
        if not session_id:
            await reject("session_not_found")
            return

        content = str(data.get("content", "") or "").strip() or "Resume the existing runtime."

        self._track_session(
            task_id,
            self._process_session_message(
                task_id,
                content,
                session_id=session_id,
                message_metadata={"ui_force_resume": True},
                run_engine=run_engine,
                run_project_id=run_project_id,
            ),
            project_id=run_project_id,
            engine=run_engine,
        )
        await self._send_ack(ws, ok=True)

    async def _load_execution_checkpoint_for_reply(
        self,
        *,
        engine: Any,
        project_id: str,
        checkpoint_id: str,
        checkpoint_type: str,
    ) -> Any | None:
        normalized_checkpoint_id = str(checkpoint_id or "").strip()
        normalized_checkpoint_type = str(checkpoint_type or "").strip()
        if not normalized_checkpoint_id:
            return None

        store = getattr(engine, "store", None)
        getter = getattr(store, "get_execution_checkpoints", None)
        if callable(getter):
            try:
                checkpoints = await getter(project_id=project_id)
            except TypeError:
                checkpoints = await getter(project_id)
            except Exception:
                logger.opt(exception=True).debug("failed to list checkpoints for explicit reply routing")
                checkpoints = []
            for checkpoint in list(checkpoints or []):
                if str(getattr(checkpoint, "checkpoint_id", "") or "").strip() != normalized_checkpoint_id:
                    continue
                if (
                    normalized_checkpoint_type
                    and str(getattr(checkpoint, "checkpoint_type", "") or "").strip() != normalized_checkpoint_type
                ):
                    return None
                return checkpoint

        direct_lookup = getattr(engine, "_load_execution_checkpoint_by_id", None)
        if callable(direct_lookup):
            try:
                maybe_checkpoint = direct_lookup(normalized_checkpoint_id)
                checkpoint = await maybe_checkpoint if inspect.isawaitable(maybe_checkpoint) else maybe_checkpoint
            except Exception:
                logger.opt(exception=True).debug("failed to load checkpoint by id for explicit reply routing")
                checkpoint = None
            if checkpoint is not None:
                if str(getattr(checkpoint, "checkpoint_id", "") or "").strip() != normalized_checkpoint_id:
                    return None
                if (
                    normalized_checkpoint_type
                    and str(getattr(checkpoint, "checkpoint_type", "") or "").strip() != normalized_checkpoint_type
                ):
                    return None
                return checkpoint
        return None

    async def _company_delivery_feedback_parent_target(
        self,
        *,
        task_id: str,
        waiting_task_id: str,
        waiting_task: Any | None,
        checkpoint: Any,
        payload: dict[str, Any],
        engine: Any,
    ) -> dict[str, str]:
        expected_runtime_session_id = str(
            payload.get("parent_session_id")
            or getattr(waiting_task, "parent_session_id", "")
            or getattr(checkpoint, "session_id", "")
            or ""
        ).strip()

        for candidate_task_id in (waiting_task_id, task_id):
            if not candidate_task_id:
                continue
            try:
                target = await self._resolve_company_runtime_target(candidate_task_id, engine=engine)
            except Exception:
                logger.opt(exception=True).debug("failed to resolve company runtime target for delivery feedback")
                target = None
            if not target:
                continue
            runtime_session_id = str(target.get("runtime_session_id", "") or "").strip()
            if expected_runtime_session_id and runtime_session_id != expected_runtime_session_id:
                continue
            ui_anchor_task_id = str(target.get("ui_anchor_task_id", "") or "").strip()
            if runtime_session_id and ui_anchor_task_id:
                return {
                    "parent_task_id": ui_anchor_task_id,
                    "parent_session_id": runtime_session_id,
                }

        # Delivery feedback is a company checkpoint action, not a generic
        # chat operation.  Ambiguous/missing durable identity must be rejected
        # instead of selecting the first Task sharing the root session.
        return {"parent_task_id": "", "parent_session_id": ""}

    async def _delivery_feedback_checkpoint_visible_to_session(
        self,
        checkpoint: Any,
        *,
        task_id: str,
        session_id: str | None,
        engine: Any,
    ) -> bool:
        requested_session_id = str(session_id or "").strip()
        if not requested_session_id:
            return False

        payload = dict(getattr(checkpoint, "payload", {}) or {})
        review_level = str(payload.get("review_level", "") or "").strip().lower()
        if review_level == "manager":
            return False

        checker = getattr(engine, "_checkpoint_visible_to_reply_session", None)
        if callable(checker):
            try:
                maybe_visible = checker(checkpoint, requested_session_id)
                visible = await maybe_visible if inspect.isawaitable(maybe_visible) else maybe_visible
                if visible is True:
                    return True
            except Exception:
                logger.opt(exception=True).debug("failed to evaluate delivery feedback checkpoint visibility")

        checkpoint_session_id = str(getattr(checkpoint, "session_id", "") or "").strip()
        if checkpoint_session_id == requested_session_id:
            return True

        for key in ("parent_session_id", "runtime_session_id", "origin_session_id"):
            if str(payload.get(key, "") or "").strip() == requested_session_id:
                return True

        normalized_task_id = str(task_id or "").strip()
        raw_task_ids = payload.get("task_ids", [])
        task_ids = {
            str(item or "").strip()
            for item in (raw_task_ids if isinstance(raw_task_ids, list) else [])
            if str(item or "").strip()
        }
        payload_task_id = str(payload.get("task_id", "") or "").strip()
        waiting_task_id = str(payload.get("waiting_task_id", "") or payload_task_id or "").strip()
        if normalized_task_id and normalized_task_id in ({payload_task_id, waiting_task_id} | task_ids):
            return True

        store = getattr(engine, "store", None)
        get_task = getattr(store, "get_task", None) if self._store_is_ready(store) else None
        if callable(get_task) and waiting_task_id:
            try:
                waiting_task = await get_task(waiting_task_id)
            except Exception:
                logger.opt(exception=True).debug("failed to load waiting task for delivery feedback visibility")
                waiting_task = None
            if waiting_task is not None:
                waiting_session_id = str(getattr(waiting_task, "session_id", "") or "").strip()
                parent_session_id = str(getattr(waiting_task, "parent_session_id", "") or "").strip()
                if requested_session_id in {waiting_session_id, parent_session_id}:
                    return True
                if normalized_task_id and normalized_task_id == str(getattr(waiting_task, "id", "") or "").strip():
                    return True

        return False

    @staticmethod
    def _checkpoint_created_timestamp(checkpoint: Any | None) -> float | None:
        raw_value = getattr(checkpoint, "created_at", None) if checkpoint is not None else None
        if raw_value is None:
            return None
        if isinstance(raw_value, (int, float)):
            return float(raw_value)
        if isinstance(raw_value, datetime):
            return raw_value.timestamp()
        if isinstance(raw_value, str):
            try:
                return datetime.fromisoformat(raw_value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return None
        return None

    def _delivery_feedback_checkpoint_channel_id(
        self,
        checkpoint: Any | None,
        *,
        fallback_channel_id: str | None = None,
    ) -> str:
        normalized_fallback = str(fallback_channel_id or "").strip()
        if normalized_fallback:
            return normalized_fallback
        payload = dict(getattr(checkpoint, "payload", {}) or {}) if checkpoint is not None else {}
        for key in ("parent_task_id", "ui_task_id", "origin_task_id", "task_id", "waiting_task_id"):
            task_id = str(payload.get(key, "") or "").strip()
            if task_id:
                return f"session:{task_id}"
        checkpoint_task_id = str(getattr(checkpoint, "task_id", "") or "").strip() if checkpoint is not None else ""
        return f"session:{checkpoint_task_id}" if checkpoint_task_id else ""

    async def _update_or_emit_checkpoint_card_status(
        self,
        checkpoint_id: str,
        *,
        checkpoint_type: str,
        status: str,
        project_id: str,
        channel_id: str | None = None,
        checkpoint: Any | None = None,
        response_message_id: str | None = None,
        response_metadata: dict[str, Any] | None = None,
        status_metadata: dict[str, Any] | None = None,
        broadcast_update: bool = True,
    ) -> dict[str, Any] | None:
        """Update a persisted checkpoint card, or persist a terminal synthetic card.

        Snapshot-built delivery review cards can be synthetic-only. In that case
        there is no chat_store row for update_checkpoint_status() to mutate, so
        the client keeps the old pending card in the floating Pending Actions
        section. Persisting the terminal synthetic card with the same checkpoint
        identity lets the frontend merge it and move it back into the timeline.
        """
        normalized_checkpoint_id = str(checkpoint_id or "").strip()
        normalized_checkpoint_type = str(checkpoint_type or "").strip()
        normalized_status = str(status or "resolved").strip().lower() or "resolved"
        normalized_project_id = self._normalize_project_id(project_id)
        if not normalized_checkpoint_id or not normalized_checkpoint_type:
            return None

        updated = await self.chat_store.update_checkpoint_status(
            normalized_checkpoint_id,
            channel_id=channel_id,
            checkpoint_type=normalized_checkpoint_type,
            status=normalized_status,
            response_message_id=response_message_id,
            response_metadata=response_metadata,
            status_metadata=status_metadata,
            project_id=normalized_project_id,
        )
        if updated is not None:
            if broadcast_update:
                await self.broadcast({"type": "session_message", "payload": updated})
            return updated

        if normalized_checkpoint_type != "company_delivery_feedback":
            return None

        fallback_channel_id = self._delivery_feedback_checkpoint_channel_id(
            checkpoint,
            fallback_channel_id=channel_id,
        )
        if not fallback_channel_id:
            return None

        if checkpoint is not None:
            base_meta = self._build_delivery_feedback_meta(checkpoint)
        else:
            base_meta = {
                "checkpoint_type": normalized_checkpoint_type,
                "checkpoint_id": normalized_checkpoint_id,
                "summary": "Human review requested.",
                "prompt": "Human review requested.",
            }

        now = time.time()
        metadata = dict(base_meta)
        metadata["checkpoint_type"] = normalized_checkpoint_type
        metadata["checkpoint_id"] = normalized_checkpoint_id
        metadata["checkpoint_status"] = normalized_status
        if normalized_status == "responded":
            metadata["checkpoint_responded_at"] = now
        else:
            metadata["checkpoint_resolved_at"] = now
        if response_message_id:
            metadata["checkpoint_response_message_id"] = response_message_id
        if isinstance(status_metadata, dict):
            for key, value in status_metadata.items():
                metadata[str(key)] = value
        if isinstance(response_metadata, dict):
            raw_reply_kind = str(response_metadata.get("checkpoint_reply_kind", "") or "").strip().lower()
            if raw_reply_kind in {"approve", "deny", "feedback", "ignore"}:
                metadata["checkpoint_reply_kind"] = raw_reply_kind

        content = str(
            metadata.get("prompt")
            or metadata.get("summary")
            or "Human review requested."
        ).strip() or "Human review requested."
        sender_name = str(
            metadata.get("work_item_projection_title")
            or metadata.get("requesting_role_id")
            or "Company Member"
        ).strip() or "Company Member"
        message_id = f"checkpoint::{normalized_checkpoint_id}"
        try:
            created = await self.chat_store.insert_message(
                channel_id=fallback_channel_id,
                sender="assistant",
                sender_name=sender_name,
                content=content,
                metadata=metadata,
                message_id=message_id,
                project_id=normalized_project_id,
                created_at=self._checkpoint_created_timestamp(checkpoint),
            )
        except Exception:
            logger.opt(exception=True).debug(
                "failed to insert terminal synthetic checkpoint card; retrying status update: checkpoint_id={}",
                normalized_checkpoint_id,
            )
            updated = await self.chat_store.update_checkpoint_status(
                normalized_checkpoint_id,
                channel_id=None,
                checkpoint_type=normalized_checkpoint_type,
                status=normalized_status,
                response_message_id=response_message_id,
                response_metadata=response_metadata,
                status_metadata=status_metadata,
                project_id=normalized_project_id,
            )
            if updated is not None:
                if broadcast_update:
                    await self.broadcast({"type": "session_message", "payload": updated})
                return updated
            return None
        if broadcast_update:
            await self.broadcast({"type": "session_message", "payload": created})
        return created

    async def _supersede_pending_delivery_feedback_for_new_company_turn(
        self,
        *,
        task_id: str,
        session_id: str | None,
        run_engine: Any,
        run_project_id: str,
    ) -> list[str]:
        project_id = self._normalize_project_id(run_project_id or getattr(run_engine, "project_id", None))
        store = getattr(run_engine, "store", None)
        if not session_id or not self._store_is_ready(store):
            return []
        getter = getattr(store, "get_pending_checkpoints", None)
        resolver = getattr(store, "resolve_execution_checkpoint", None)
        if not callable(getter) or not callable(resolver):
            return []
        try:
            checkpoints = await getter(
                project_id=project_id,
                checkpoint_types=["company_delivery_feedback"],
            )
        except Exception:
            logger.opt(exception=True).debug("failed to list pending delivery feedback checkpoints")
            return []

        superseded_ids: list[str] = []
        for checkpoint in list(checkpoints or []):
            checkpoint_id = str(getattr(checkpoint, "checkpoint_id", "") or "").strip()
            if (
                not checkpoint_id
                or str(getattr(checkpoint, "checkpoint_type", "") or "").strip() != "company_delivery_feedback"
                or str(getattr(checkpoint, "status", "") or "").strip().lower() != "pending"
            ):
                continue
            visible = await self._delivery_feedback_checkpoint_visible_to_session(
                checkpoint,
                task_id=task_id,
                session_id=session_id,
                engine=run_engine,
            )
            if not visible:
                continue

            try:
                terminalizer = getattr(run_engine, "_terminalize_company_delivery_feedback_checkpoint", None)
                if inspect.iscoroutinefunction(terminalizer):
                    superseded_at = datetime.now().isoformat()
                    await terminalizer(
                        checkpoint,
                        status="superseded",
                        resolution="superseded_by_new_company_turn",
                        payload_updates={
                            "feedback_superseded": True,
                            "feedback_superseded_at": superseded_at,
                            "feedback_resolution": "superseded_by_new_company_turn",
                        },
                        task_metadata_updates={
                            "feedback_superseded": True,
                            "feedback_superseded_at": superseded_at,
                        },
                    )
                else:
                    await resolver(checkpoint_id, status="superseded")
                    waiting_task_id = str(
                        dict(getattr(checkpoint, "payload", {}) or {}).get("waiting_task_id")
                        or getattr(checkpoint, "task_id", "")
                        or ""
                    ).strip()
                    if waiting_task_id and hasattr(store, "get_task") and hasattr(store, "save_task"):
                        from opc.core.models import TaskStatus
                        waiting_task = await store.get_task(waiting_task_id)
                        if waiting_task is not None:
                            superseded_at = datetime.now().isoformat()
                            waiting_task.metadata = dict(getattr(waiting_task, "metadata", {}) or {})
                            waiting_task.metadata.update({
                                "requires_user_feedback": False,
                                "human_review_closed": True,
                                "human_review_closed_at": superseded_at,
                                "human_review_resolution": "superseded_by_new_company_turn",
                                "feedback_closed": True,
                                "feedback_resolved": True,
                                "feedback_superseded": True,
                                "feedback_superseded_at": superseded_at,
                                "feedback_resolution": "superseded_by_new_company_turn",
                            })
                            await apply_task_status_transition(
                                store,
                                waiting_task,
                                target_status_or_phase=TaskStatus.DONE,
                                reason="superseded_by_new_company_turn",
                            )
            except Exception:
                logger.opt(exception=True).debug("failed to mark delivery feedback checkpoint superseded")
                continue

            superseded_ids.append(checkpoint_id)
            await self._update_or_emit_checkpoint_card_status(
                checkpoint_id,
                checkpoint_type="company_delivery_feedback",
                status="superseded",
                checkpoint=checkpoint,
                channel_id=f"session:{task_id}",
                status_metadata={"checkpoint_resolution_reason": "new_company_turn_started"},
                project_id=project_id,
            )
        return superseded_ids

    async def _route_company_delivery_feedback_reply_if_pending(
        self,
        *,
        task_id: str,
        content: str,
        session_id: str | None,
        task: Any | None,
        attachment_refs: list[dict] | None,
        message_metadata: dict[str, Any] | None,
        user_message_id: str | None,
        user_message_created_at: float | None,
        run_engine: Any,
        run_project_id: str,
        reply_channel_id: str,
    ) -> bool:
        metadata = dict(message_metadata or {})
        checkpoint_id = str(metadata.get("response_to_checkpoint_id", "") or "").strip()
        checkpoint_type = str(metadata.get("response_to_checkpoint_type", "") or "").strip()
        reply_kind = str(metadata.get("checkpoint_reply_kind", "") or "").strip().lower()
        if checkpoint_type != "company_delivery_feedback" or not checkpoint_id:
            return False

        checkpoint = await self._load_execution_checkpoint_for_reply(
            engine=run_engine,
            project_id=run_project_id,
            checkpoint_id=checkpoint_id,
            checkpoint_type=checkpoint_type,
        )
        if checkpoint is None:
            await self._update_or_emit_checkpoint_card_status(
                checkpoint_id,
                channel_id=reply_channel_id,
                checkpoint_type=checkpoint_type,
                status="stale",
                response_message_id=user_message_id,
                response_metadata=metadata,
                project_id=run_project_id,
            )
            if reply_kind == "ignore":
                return True
            helper = await self.chat_store.insert_message(
                channel_id=reply_channel_id,
                sender="assistant",
                sender_name="OPC",
                content=(
                    "This delivery self-evolution review is no longer active. "
                    "The review card has been marked inactive in the session history."
                ),
                project_id=run_project_id,
                metadata={"type": "system"},
            )
            await self.broadcast({"type": "session_message", "payload": helper})
            return True

        status = str(getattr(checkpoint, "status", "") or "").strip().lower()
        if status and status != "pending":
            await self._update_or_emit_checkpoint_card_status(
                checkpoint_id,
                channel_id=reply_channel_id,
                checkpoint_type=checkpoint_type,
                status=status,
                checkpoint=checkpoint,
                response_message_id=user_message_id,
                response_metadata=metadata,
                project_id=run_project_id,
            )
            if reply_kind == "ignore":
                return True
            if status == "superseded":
                helper_text = (
                    "This delivery self-evolution review was superseded by a newer company turn. "
                    "The review card has been marked inactive in the session history."
                )
            else:
                helper_text = (
                    "This delivery self-evolution review is no longer active. "
                    "The review card has been updated in the session history."
                )
            helper = await self.chat_store.insert_message(
                channel_id=reply_channel_id,
                sender="assistant",
                sender_name="OPC",
                content=helper_text,
                project_id=run_project_id,
                metadata={"type": "system"},
            )
            await self.broadcast({"type": "session_message", "payload": helper})
            return True

        payload = dict(getattr(checkpoint, "payload", {}) or {})
        waiting_task_id = str(payload.get("waiting_task_id", "") or payload.get("task_id", "") or "").strip()
        if reply_kind == "ignore":
            lock_key = checkpoint_id
            lock = self._company_delivery_feedback_reply_locks.get(lock_key)
            if lock is None:
                lock = asyncio.Lock()
                self._company_delivery_feedback_reply_locks[lock_key] = lock

            async with lock:
                checkpoint = await self._load_execution_checkpoint_for_reply(
                    engine=run_engine,
                    project_id=run_project_id,
                    checkpoint_id=checkpoint_id,
                    checkpoint_type=checkpoint_type,
                )
                if checkpoint is None:
                    await self._update_or_emit_checkpoint_card_status(
                        checkpoint_id,
                        channel_id=reply_channel_id,
                        checkpoint_type=checkpoint_type,
                        status="stale",
                        response_message_id=user_message_id,
                        response_metadata=metadata,
                        project_id=run_project_id,
                    )
                    return True

                status = str(getattr(checkpoint, "status", "") or "").strip().lower()
                if status and status != "pending":
                    await self._update_or_emit_checkpoint_card_status(
                        checkpoint_id,
                        channel_id=reply_channel_id,
                        checkpoint_type=checkpoint_type,
                        status=status,
                        checkpoint=checkpoint,
                        response_message_id=user_message_id,
                        response_metadata=metadata,
                        project_id=run_project_id,
                    )
                    return True

                started = time.monotonic()
                await self._update_or_emit_checkpoint_card_status(
                    checkpoint_id,
                    channel_id=reply_channel_id,
                    checkpoint_type="company_delivery_feedback",
                    status="ignored",
                    checkpoint=checkpoint,
                    response_message_id=user_message_id,
                    response_metadata=metadata,
                    status_metadata={"checkpoint_resolution_reason": "ignored_by_user"},
                    project_id=run_project_id,
                )

                payload = dict(getattr(checkpoint, "payload", {}) or {})
                ignored_at = datetime.now().isoformat()
                try:
                    runner = getattr(run_engine, "ignore_company_delivery_feedback_checkpoint", None)
                    if callable(runner):
                        result = runner(checkpoint, reply_metadata=metadata or None)
                        if inspect.isawaitable(result):
                            await result
                    else:
                        terminalizer = getattr(run_engine, "_terminalize_company_delivery_feedback_checkpoint", None)
                        if callable(terminalizer):
                            result = terminalizer(
                                checkpoint,
                                status="ignored",
                                resolution="self_evolution_review_ignored",
                                payload_updates={
                                    **payload,
                                    "feedback_ignored": True,
                                    "feedback_ignored_at": ignored_at,
                                    "feedback_resolution": "self_evolution_review_ignored",
                                },
                                task_metadata_updates={
                                    "self_evolution_review_ignored": True,
                                    "self_evolution_review_ignored_at": ignored_at,
                                    "feedback_ignored": True,
                                    "feedback_ignored_at": ignored_at,
                                },
                            )
                            if inspect.isawaitable(result):
                                await result
                        else:
                            resolver = getattr(getattr(run_engine, "store", None), "resolve_execution_checkpoint", None)
                            if callable(resolver):
                                result = resolver(checkpoint_id, status="ignored")
                                if inspect.isawaitable(result):
                                    await result
                except Exception:
                    logger.exception(
                        "failed to terminalize ignored delivery feedback checkpoint: checkpoint_id={}",
                        checkpoint_id,
                    )
                logger.info(
                    "delivery feedback ignore handled: checkpoint_id={} elapsed_ms={:.1f}",
                    checkpoint_id,
                    (time.monotonic() - started) * 1000,
                )
            return True
        waiting_task = None
        store = getattr(run_engine, "store", None)
        if waiting_task_id and self._store_is_ready(store):
            try:
                waiting_task = await store.get_task(waiting_task_id)
            except Exception:
                logger.opt(exception=True).debug("failed to load delivery feedback waiting task")
        target = await self._company_delivery_feedback_parent_target(
            task_id=task_id,
            waiting_task_id=waiting_task_id,
            waiting_task=waiting_task,
            checkpoint=checkpoint,
            payload=payload,
            engine=run_engine,
        )
        parent_task_id = str(target.get("parent_task_id", "") or "").strip()
        parent_session_id = str(target.get("parent_session_id", "") or "").strip()
        if not parent_task_id or not parent_session_id:
            # This is already known to be an explicit delivery-feedback reply.
            # Consuming it here prevents a missing/ambiguous durable identity
            # from falling through as an ordinary turn on the work-item Task.
            if self._chat_store_is_ready(self.chat_store):
                try:
                    helper = await self.chat_store.insert_message(
                        channel_id=reply_channel_id,
                        sender="assistant",
                        sender_name="OPC",
                        content=(
                            "This review no longer matches an active company runtime. "
                            "Refresh the session before replying again."
                        ),
                        project_id=run_project_id,
                        metadata={
                            "type": "system",
                            "reason": "company_runtime_identity_mismatch",
                        },
                    )
                    await self.broadcast({"type": "session_message", "payload": helper})
                except Exception:
                    logger.opt(exception=True).debug(
                        "failed to emit delivery feedback identity rejection"
                    )
            return True

        lock_key = checkpoint_id or parent_session_id
        lock = self._company_delivery_feedback_reply_locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            self._company_delivery_feedback_reply_locks[lock_key] = lock

        self._track_session(
            parent_task_id,
            self._process_company_delivery_feedback_reply(
                parent_task_id=parent_task_id,
                parent_session_id=parent_session_id,
                reply_channel_id=reply_channel_id,
                content=content,
                attachment_refs=attachment_refs,
                message_metadata=metadata,
                user_message_id=user_message_id,
                user_message_created_at=user_message_created_at,
                run_engine=run_engine,
                run_project_id=run_project_id,
                checkpoint=checkpoint,
                waiting_task_id=waiting_task_id,
                lock=lock,
            ),
            project_id=run_project_id,
            engine=run_engine,
        )
        return True

    async def _process_company_delivery_feedback_reply(
        self,
        *,
        parent_task_id: str,
        parent_session_id: str,
        reply_channel_id: str,
        content: str,
        attachment_refs: list[dict] | None,
        message_metadata: dict[str, Any] | None,
        user_message_id: str | None,
        user_message_created_at: float | None,
        run_engine: Any,
        run_project_id: str,
        checkpoint: Any,
        waiting_task_id: str,
        lock: asyncio.Lock,
    ) -> None:
        pid = self._normalize_project_id(run_project_id or getattr(run_engine, "project_id", None))
        async with lock:
            try:
                parent_task = None
                if self._store_is_ready(run_engine.store):
                    try:
                        parent_task = await run_engine.store.get_task(parent_task_id)
                    except Exception:
                        logger.opt(exception=True).debug("failed to load parent task for delivery feedback reply")
                session_exec_mode = self._normalize_session_exec_mode(self._exec_mode)
                session_company_profile = self._normalize_session_company_profile(self._company_profile)
                session_org_id = ""
                if parent_task is not None:
                    session_exec_mode, session_company_profile = self._resolve_task_session_config(parent_task)
                    session_org_id = self._resolve_task_org_id(parent_task)
                engine_mode, company_profile = self._resolve_engine_mode(
                    session_exec_mode,
                    session_company_profile,
                )
                engine_message_metadata = dict(message_metadata or {})
                engine_message_metadata.update(_ui_message_identity_metadata(
                    message_id=user_message_id,
                    conversation_turn_id=_ui_conversation_turn_id(user_message_id),
                    created_at=user_message_created_at,
                ))
                self._active_runtime_children[parent_task_id] = parent_task_id
                self._session_to_task[parent_session_id] = parent_task_id
                payload = dict(getattr(checkpoint, "payload", {}) or {})
                for child_task_id in list(payload.get("task_ids", []) or []):
                    child_id = str(child_task_id or "").strip()
                    if child_id:
                        self._active_runtime_children[child_id] = parent_task_id
                if waiting_task_id:
                    self._active_runtime_children[waiting_task_id] = parent_task_id

                reply_kind = str(engine_message_metadata.get("checkpoint_reply_kind", "") or "").strip().lower()
                if reply_kind not in {"approve", "feedback"}:
                    normalized_content = str(content or "").strip().lower()
                    reply_kind = "approve" if normalized_content in {"approve", "approved", "i approve this delivery."} else "feedback"
                feedback_text = str(content or "").strip() if reply_kind == "feedback" else ""
                runner = getattr(run_engine, "run_company_delivery_self_evolution_checkpoint", None)
                if not callable(runner):
                    raise RuntimeError("Company delivery self-evolution is not available in this runtime.")
                result_text = await runner(
                    checkpoint,
                    action=reply_kind,
                    feedback=feedback_text,
                    reply_metadata=engine_message_metadata or None,
                )
                assistant_msg = await self.chat_store.insert_message(
                    channel_id=reply_channel_id or f"session:{parent_task_id}",
                    sender="assistant",
                    sender_name="OPC",
                    content=str(result_text or "Self-evolution completed.").strip(),
                    project_id=pid,
                    metadata={
                        "type": "system",
                        "kind": "company_self_evolution_result",
                        "response_to_checkpoint_type": "company_delivery_feedback",
                        "response_to_checkpoint_id": str(getattr(checkpoint, "checkpoint_id", "") or "").strip(),
                        "checkpoint_reply_kind": reply_kind,
                        "self_evolution_completed": True,
                    },
                )
                await self.broadcast({"type": "session_message", "payload": assistant_msg})
                updated_checkpoint_msg = await self._mark_checkpoint_card_after_engine_response(
                    channel_id=reply_channel_id,
                    project_id=pid,
                    engine=run_engine,
                    message_metadata=engine_message_metadata,
                    response_message_id=user_message_id,
                )
                if updated_checkpoint_msg is not None:
                    await self.broadcast({"type": "session_message", "payload": updated_checkpoint_msg})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Company delivery feedback reply processing error: {}", exc)
                if self._chat_store_is_ready(self.chat_store):
                    try:
                        msg = await self.chat_store.insert_message(
                            channel_id=reply_channel_id or f"session:{parent_task_id}",
                            sender="system",
                            sender_name="OPC",
                            content=f"Error: {exc}",
                            project_id=pid,
                        )
                        await self.broadcast({"type": "session_message", "payload": msg})
                    except Exception:
                        logger.opt(exception=True).debug("failed to write delivery feedback reply error")
            finally:
                if self._chat_store_is_ready(self.chat_store):
                    await self._flush_progress(parent_task_id, project_id=pid)
                if waiting_task_id:
                    await self._flush_progress(waiting_task_id, project_id=pid)

    async def _route_company_suspend_reply_if_pending(
        self,
        *,
        task_id: str,
        content: str,
        session_id: str | None,
        task: Any | None,
        attachment_refs: list[dict] | None,
        message_metadata: dict[str, Any] | None,
        user_message_id: str | None,
        user_message_created_at: float | None,
        run_engine: Any,
        run_project_id: str,
    ) -> bool:
        """Route text after company Stop without waiting on the parent task lock.

        A live company run can still hold the parent session lock while Stop is
        finalizing or already suspended. Plain text after Stop must reach the
        engine's company-runtime suspend checkpoint immediately so the
        CEO/final-decider can arbitrate with edit/delete/delegate tools. The
        Continue button uses ``session_resume`` and keeps the original forced
        resume path.
        """
        explicit_checkpoint_type = str((message_metadata or {}).get("response_to_checkpoint_type", "") or "").strip()
        explicit_runtime_handoff = (
            explicit_checkpoint_type in COMPANY_RUNTIME_CHECKPOINT_TYPES
        )
        if explicit_checkpoint_type and not explicit_runtime_handoff:
            return False
        if task is None:
            return False
        explicit_checkpoint_id = str(
            (message_metadata or {}).get("response_to_checkpoint_id", "") or ""
        ).strip()

        async def reject(reason: str, content: str) -> bool:
            if self._chat_store_is_ready(self.chat_store):
                try:
                    helper = await self.chat_store.insert_message(
                        channel_id=f"session:{task_id}",
                        sender="assistant",
                        sender_name="OPC",
                        content=content,
                        project_id=run_project_id,
                        metadata={"type": "system", "reason": reason},
                    )
                    await self.broadcast({"type": "session_message", "payload": helper})
                except Exception:
                    logger.opt(exception=True).debug(
                        "failed to emit company runtime handoff rejection"
                    )
            return True

        if explicit_runtime_handoff and not explicit_checkpoint_id:
            return await reject(
                "missing_runtime_checkpoint_id",
                "This Continue request has no runtime checkpoint identity. Refresh the session and try again.",
            )

        try:
            base_target = await self._resolve_company_runtime_target(
                task_id,
                engine=run_engine,
            )
        except Exception:
            logger.opt(exception=True).debug("failed to resolve company suspend reply target")
            base_target = None
        if base_target is None:
            if explicit_runtime_handoff or is_company_runtime_task(task):
                return await reject(
                    "company_runtime_identity_mismatch",
                    "This message no longer matches an active company runtime. Refresh the session before retrying.",
                )
            return False

        target = base_target
        if explicit_checkpoint_id:
            try:
                target = await self._resolve_company_runtime_target(
                    task_id,
                    engine=run_engine,
                    checkpoint_id=explicit_checkpoint_id,
                )
            except Exception:
                logger.opt(exception=True).debug(
                    "failed to resolve explicit company runtime checkpoint"
                )
                target = None
            if target is None:
                if explicit_runtime_handoff or base_target.get("checkpoint") is not None:
                    return await reject(
                        "company_runtime_identity_mismatch",
                        "This runtime checkpoint does not match the current company session. Refresh before retrying.",
                    )
                return False

        runtime_session_id = str(target.get("runtime_session_id", "") or "").strip()
        if not runtime_session_id:
            return await reject(
                "company_runtime_identity_mismatch",
                "The active company runtime has no canonical session identity. Refresh before retrying.",
            )

        finalizer = self._company_stop_finalize_tasks.get(runtime_session_id)
        if finalizer is not None and not finalizer.done():
            try:
                await asyncio.wait_for(asyncio.shield(finalizer), timeout=10.0)
            except asyncio.TimeoutError:
                helper = await self.chat_store.insert_message(
                    channel_id=f"session:{task_id}",
                    sender="assistant",
                    sender_name="OPC",
                    content="Stop is still finalizing. Send your update again after the runtime reaches Suspended.",
                    project_id=run_project_id,
                    metadata={"type": "system", "reason": "company_stop_finalize_in_progress"},
                )
                await self.broadcast({"type": "session_message", "payload": helper})
                return True
            except Exception:
                logger.opt(exception=True).debug("company stop finalizer failed before follow-up routing")

            # Stop finalization may have created the checkpoint after the first
            # index load.  Reload durable identity before deciding whether this
            # message is an ordinary turn.
            try:
                target = await self._resolve_company_runtime_target(
                    task_id,
                    engine=run_engine,
                    checkpoint_id=explicit_checkpoint_id,
                )
            except Exception:
                logger.opt(exception=True).debug(
                    "failed to reload company runtime identity after stop finalization"
                )
                target = None
            if target is None:
                return await reject(
                    "company_runtime_identity_mismatch",
                    "The stopped runtime could not be matched to this session. Refresh before retrying.",
                )

        checkpoint = target.get("checkpoint")
        if checkpoint is None:
            if explicit_runtime_handoff:
                return await reject(
                    "company_runtime_checkpoint_not_found",
                    "This company runtime checkpoint is no longer active. Refresh before retrying.",
                )
            return False
        checkpoint_status = str(
            getattr(checkpoint, "status", "") or ""
        ).strip().lower()
        if checkpoint_status != "pending":
            return await reject(
                "company_runtime_checkpoint_not_pending",
                (
                    "This company runtime is already being resumed. Wait for the current handoff to finish."
                    if checkpoint_status == "resuming"
                    else "This company runtime checkpoint is no longer pending. Refresh before retrying."
                ),
            )

        lock = self._company_suspend_reply_locks.get(runtime_session_id)
        if lock is not None:
            self._release_current_execution_handoff()
            return await reject(
                "checkpoint_handoff_in_progress",
                "This company runtime is already being resumed by another request.",
            )
        lock = asyncio.Lock()
        self._company_suspend_reply_locks[runtime_session_id] = lock

        try:
            bg = self._track_session(
                task_id,
                self._process_company_suspend_reply(
                    ui_task_id=task_id,
                    runtime_session_id=runtime_session_id,
                    content=content,
                    attachment_refs=attachment_refs,
                    message_metadata=message_metadata,
                    user_message_id=user_message_id,
                    user_message_created_at=user_message_created_at,
                    run_engine=run_engine,
                    run_project_id=run_project_id,
                    target=target,
                    checkpoint=checkpoint,
                    lock=lock,
                ),
                project_id=run_project_id,
                engine=run_engine,
            )
        except BaseException:
            self._company_suspend_reply_locks.pop(runtime_session_id, None)
            raise
        self._task_bg_context[bg]["company_suspend_reply"] = True
        return True

    async def _process_company_suspend_reply(
        self,
        *,
        ui_task_id: str,
        runtime_session_id: str,
        content: str,
        attachment_refs: list[dict] | None,
        message_metadata: dict[str, Any] | None,
        user_message_id: str | None,
        user_message_created_at: float | None,
        run_engine: Any,
        run_project_id: str,
        target: dict[str, Any],
        checkpoint: Any,
        lock: asyncio.Lock,
    ) -> None:
        channel_id = f"session:{ui_task_id}"
        pid = self._normalize_project_id(run_project_id or getattr(run_engine, "project_id", None))
        async with lock:
            try:
                try:
                    await self._set_company_runtime_control(
                        target,
                        state="resuming",
                        checkpoint_id=str(
                            getattr(checkpoint, "checkpoint_id", "") or ""
                        ).strip(),
                    )
                except Exception:
                    logger.opt(exception=True).debug("failed to broadcast company suspend reply routing state")

                config_task = target.get("config_task")
                session_exec_mode = self._normalize_session_exec_mode(self._exec_mode)
                session_company_profile = self._normalize_session_company_profile(self._company_profile)
                session_org_id = ""
                if config_task is not None:
                    session_exec_mode, session_company_profile = self._resolve_task_session_config(config_task)
                    session_org_id = self._resolve_task_org_id(config_task)
                engine_mode, company_profile = self._resolve_engine_mode(
                    session_exec_mode,
                    session_company_profile,
                )
                engine_message_metadata = dict(message_metadata or {})
                engine_message_metadata.update({
                    "response_to_checkpoint_id": str(getattr(checkpoint, "checkpoint_id", "") or ""),
                    "response_to_checkpoint_type": str(getattr(checkpoint, "checkpoint_type", "") or ""),
                })
                engine_message_metadata.update(_ui_message_identity_metadata(
                    message_id=user_message_id,
                    conversation_turn_id=_ui_conversation_turn_id(user_message_id),
                    created_at=user_message_created_at,
                ))
                execution_anchor_task_id = str(
                    target.get("ui_anchor_task_id", "") or ""
                ).strip()
                if execution_anchor_task_id:
                    self._active_runtime_children[execution_anchor_task_id] = execution_anchor_task_id
                self._session_to_task[runtime_session_id] = (
                    execution_anchor_task_id or ui_task_id
                )
                await run_engine.process_message(
                    content,
                    project_id=pid,
                    session_id=runtime_session_id,
                    mode=engine_mode,
                    org_id=session_org_id or None,
                    company_profile=company_profile,
                    preferred_agent=None,
                    origin_task_id=execution_anchor_task_id or None,
                    attachment_refs=attachment_refs,
                    message_metadata=engine_message_metadata or None,
                )
                checkpoint_meta = await self._extract_checkpoint_metadata(
                    ui_task_id,
                    session_id=runtime_session_id,
                    engine=run_engine,
                )
                await self._sync_task_transcript_messages(
                    ui_task_id,
                    engine=run_engine,
                    latest_assistant_metadata=checkpoint_meta if checkpoint_meta else None,
                )
                # Replace the optimistic ``resuming`` projection with the
                # checkpoint state committed by the engine.  Successful and
                # already-consumed handoffs need the same durable refresh as
                # failed handoffs so every client converges on snapshot state.
                try:
                    await self.on_kanban_changed(engine=run_engine)
                except Exception:
                    logger.opt(exception=True).debug(
                        "failed to refresh company runtime control after handoff"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._is_expected_shutdown_error(exc) or self._is_closed_database_error(exc):
                    logger.debug(
                        "Company suspend reply skipped during shutdown/closed store: {}: {}",
                        type(exc).__name__,
                        exc,
                    )
                else:
                    logger.exception("Company suspend reply processing error: {}", exc)
                    if self._chat_store_is_ready(self.chat_store):
                        try:
                            msg = await self.chat_store.insert_message(
                                channel_id=channel_id,
                                sender="system",
                                sender_name="OPC",
                                content=f"Error: {exc}",
                                project_id=pid,
                            )
                            await self.broadcast({"type": "session_message", "payload": msg})
                        except Exception as chat_exc:
                            if self._is_expected_shutdown_error(chat_exc) or self._is_closed_database_error(chat_exc):
                                logger.debug(
                                    "Skipped company suspend reply error message during shutdown/closed store: {}: {}",
                                    type(chat_exc).__name__,
                                    chat_exc,
                                )
                            else:
                                logger.opt(exception=True).debug(
                                    "Failed to write company suspend reply error message",
                                )
                    # The engine restores a failed handoff checkpoint to
                    # pending.  Replace the optimistic ``resuming`` projection
                    # for every client with that durable state immediately.
                    await self.on_kanban_changed(engine=run_engine)
            finally:
                if self._chat_store_is_ready(self.chat_store):
                    await self._flush_progress(ui_task_id, project_id=pid)
                self._task_bg_context.pop(asyncio.current_task(), None)
                self._company_suspend_reply_locks.pop(runtime_session_id, None)

    def _resolve_task_comms_dir(self, task: Any) -> Path | None:
        """Resolve `<workspace>/.opc-comms/<project>/<session>` for a task.

        Returns None when the task lacks enough metadata to locate its
        on-disk comms tree (e.g. workspace never resolved, no session id).
        """
        md = task.metadata or {}
        workspace_root = (
            str(md.get("comms_workspace_root") or "").strip()
            or str(md.get("target_output_dir") or "").strip()
            or str(md.get("setup_workspace_prepared") or "").strip()
        )
        comms_root = str(md.get("comms_root") or "").strip()
        session_id = (
            str(getattr(task, "parent_session_id", "") or "").strip()
            or str(getattr(task, "session_id", "") or "").strip()
        )
        project_id = (
            str(getattr(task, "project_id", "") or "").strip()
            or self.engine.project_id
            or "default"
        )
        if not session_id:
            return None
        try:
            from opc.layer2_organization import comms as _comms
            if workspace_root:
                return _comms.resolve_layout(workspace_root, project_id, session_id).root
            if comms_root:
                # comms_root is `<ws>/.opc-comms`; its parent is workspace_root.
                inferred_ws = str(Path(comms_root).parent)
                return _comms.resolve_layout(inferred_ws, project_id, session_id).root
        except Exception:
            logger.opt(exception=True).debug(f"_resolve_task_comms_dir failed for task {getattr(task, 'id', '?')}")
        return None

    async def _handle_session_delete(self, ws: Any, data: dict) -> None:
        """Delete task/session data and notify frontend."""
        task_id = data.get("task_id", "")
        if not task_id:
            return
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self._ensure_office_services().session.delete(project_id=project_id, task_id=task_id)
            self._session_to_task.update(self.services_context.session_to_task)
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, project_id=project_id)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="session_delete")

    async def _handle_session_complete(self, ws: Any, data: dict) -> None:
        """Mark a session's task as DONE (user explicitly completes it)."""
        task_id = data.get("task_id", "")
        if not task_id:
            return
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self._ensure_office_services().session.complete(project_id=project_id, task_id=task_id)
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, project_id=project_id)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="session_complete")

    async def _handle_session_update_title(self, ws: Any, data: dict) -> None:
        """Update session/task title."""
        task_id = data.get("task_id", "")
        title = data.get("title", "")
        if not task_id or not title:
            return
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self._ensure_office_services().session.rename(
                project_id=project_id,
                task_id=task_id,
                title=title,
            )
            await self._publish_service_result(result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="session_update_title")

    async def _dispatch_session_message(
        self, task_id: str, content: str, *, session_id: str | None = None,
        attachment_refs: list[dict] | None = None,
        message_metadata: dict[str, Any] | None = None,
        user_message_id: str | None = None,
        user_message_created_at: float | None = None,
        run_engine: Any | None = None,
        run_project_id: str | None = None,
    ) -> None:
        """Route through Dispatcher: classify → engine pipeline or direct reply."""
        captured_context = run_engine is not None or run_project_id is not None
        engine = run_engine or self.engine
        pid = self._normalize_project_id(run_project_id or getattr(engine, "project_id", None))
        channel_id = f"session:{task_id}"
        dispatcher = self.dispatcher if engine is self.engine else Dispatcher(engine, self.chat_store)
        try:
            result = await dispatcher.handle(
                task_id,
                content,
                session_id=session_id,
                has_attachments=bool(attachment_refs),
            )
            if result.route == "engine":
                process_kwargs: dict[str, Any] = {
                    "session_id": session_id,
                    "attachment_refs": attachment_refs,
                }
                if message_metadata:
                    process_kwargs["message_metadata"] = message_metadata
                if user_message_id:
                    process_kwargs["user_message_id"] = user_message_id
                if user_message_created_at is not None:
                    process_kwargs["user_message_created_at"] = user_message_created_at
                if captured_context:
                    process_kwargs["run_engine"] = engine
                    process_kwargs["run_project_id"] = pid
                await self._process_session_message(task_id, content, **process_kwargs)
            else:
                # Direct reply from Dispatcher (status query, conversation, session control)
                msg = await self.chat_store.insert_message(
                    channel_id=channel_id,
                    sender="assistant",
                    sender_name="OPC",
                    content=result.response,
                    project_id=pid,
                )
                await self.broadcast({"type": "session_message", "payload": msg})
                # Record exchange in engine memory for context continuity
                if engine.memory and session_id:
                    user_turn_meta = _ui_message_identity_metadata(
                        kind="top_level_user_turn",
                        message_id=user_message_id,
                        conversation_turn_id=_ui_conversation_turn_id(user_message_id),
                        created_at=user_message_created_at,
                    )
                    await engine.memory.record_user_turn(
                        session_id, content,
                        project_id=pid,
                        metadata=user_turn_meta or None,
                    )
                    assistant_turn_meta = _ui_message_identity_metadata(
                        kind="top_level_reply",
                        message_id=str(msg.get("message_id") or "").strip() or None,
                        created_at=float(msg.get("created_at")) if msg.get("created_at") is not None else None,
                    )
                    await engine.memory.record_assistant_turn(
                        session_id, result.response,
                        project_id=pid,
                        metadata=assistant_turn_meta or None,
                    )
        except Exception as e:
            logger.opt(exception=True).error(f"Dispatcher error, falling back to engine: {e}")
            process_kwargs = {
                "session_id": session_id,
                "attachment_refs": attachment_refs,
            }
            if message_metadata:
                process_kwargs["message_metadata"] = message_metadata
            if user_message_id:
                process_kwargs["user_message_id"] = user_message_id
            if user_message_created_at is not None:
                process_kwargs["user_message_created_at"] = user_message_created_at
            if captured_context:
                process_kwargs["run_engine"] = engine
                process_kwargs["run_project_id"] = pid
            await self._process_session_message(task_id, content, **process_kwargs)

    async def _execution_checkpoint_status(
        self,
        *,
        engine: Any,
        project_id: str,
        checkpoint_id: str,
        checkpoint_type: str | None = None,
    ) -> str:
        normalized_checkpoint_id = str(checkpoint_id or "").strip()
        normalized_checkpoint_type = str(checkpoint_type or "").strip()
        if not normalized_checkpoint_id:
            return ""
        direct_lookup = getattr(engine, "_load_execution_checkpoint_by_id", None)
        checkpoint = None
        if callable(direct_lookup):
            try:
                checkpoint = await direct_lookup(normalized_checkpoint_id)
            except Exception:
                logger.opt(exception=True).debug("failed to load checkpoint by id from engine")
        if checkpoint is None:
            store = getattr(engine, "store", None)
            getter = getattr(store, "get_execution_checkpoints", None)
            if callable(getter):
                try:
                    checkpoints = await getter(project_id=project_id)
                except TypeError:
                    checkpoints = await getter(project_id)
                for item in checkpoints:
                    if str(getattr(item, "checkpoint_id", "") or "").strip() == normalized_checkpoint_id:
                        checkpoint = item
                        break
        if checkpoint is None:
            return ""
        if normalized_checkpoint_type and str(getattr(checkpoint, "checkpoint_type", "") or "").strip() != normalized_checkpoint_type:
            return ""
        return str(getattr(checkpoint, "status", "") or "").strip().lower()

    async def _mark_checkpoint_card_after_engine_response(
        self,
        *,
        channel_id: str,
        project_id: str,
        engine: Any,
        message_metadata: dict[str, Any] | None,
        response_message_id: str | None,
    ) -> dict[str, Any] | None:
        metadata = dict(message_metadata or {})
        checkpoint_id = str(metadata.get("response_to_checkpoint_id", "") or "").strip()
        checkpoint_type = str(metadata.get("response_to_checkpoint_type", "") or "").strip()
        if not checkpoint_id or checkpoint_type == "human_escalation":
            return None
        status = await self._execution_checkpoint_status(
            engine=engine,
            project_id=project_id,
            checkpoint_id=checkpoint_id,
            checkpoint_type=checkpoint_type,
        )
        if not status or status == "pending":
            return None
        try:
            if status == "resolved":
                updated = await self.chat_store.mark_checkpoint_responded(
                    channel_id,
                    checkpoint_id,
                    checkpoint_type=checkpoint_type,
                    response_message_id=response_message_id,
                    response_metadata=metadata,
                    project_id=project_id,
                )
                if updated is not None:
                    return updated
                updated = await self.chat_store.update_checkpoint_status(
                    checkpoint_id,
                    channel_id=None,
                    checkpoint_type=checkpoint_type,
                    status="responded",
                    response_message_id=response_message_id,
                    response_metadata=metadata,
                    project_id=project_id,
                )
                if updated is not None:
                    return updated
                checkpoint = await self._load_execution_checkpoint_for_reply(
                    engine=engine,
                    project_id=project_id,
                    checkpoint_id=checkpoint_id,
                    checkpoint_type=checkpoint_type,
                )
                return await self._update_or_emit_checkpoint_card_status(
                    checkpoint_id,
                    channel_id=channel_id,
                    checkpoint_type=checkpoint_type,
                    status="responded",
                    checkpoint=checkpoint,
                    response_message_id=response_message_id,
                    response_metadata=metadata,
                    project_id=project_id,
                    broadcast_update=False,
                )
            updated = await self.chat_store.update_checkpoint_status(
                checkpoint_id,
                channel_id=channel_id,
                checkpoint_type=checkpoint_type,
                status=status,
                response_message_id=response_message_id,
                response_metadata=metadata,
                project_id=project_id,
            )
            if updated is not None:
                return updated
            updated = await self.chat_store.update_checkpoint_status(
                checkpoint_id,
                channel_id=None,
                checkpoint_type=checkpoint_type,
                status=status,
                response_message_id=response_message_id,
                response_metadata=metadata,
                project_id=project_id,
            )
            if updated is not None:
                return updated
            checkpoint = await self._load_execution_checkpoint_for_reply(
                engine=engine,
                project_id=project_id,
                checkpoint_id=checkpoint_id,
                checkpoint_type=checkpoint_type,
            )
            return await self._update_or_emit_checkpoint_card_status(
                checkpoint_id,
                channel_id=channel_id,
                checkpoint_type=checkpoint_type,
                status=status,
                checkpoint=checkpoint,
                response_message_id=response_message_id,
                response_metadata=metadata,
                project_id=project_id,
                broadcast_update=False,
            )
        except Exception:
            logger.opt(exception=True).debug("failed to persist checkpoint card terminal state")
            return None

    async def _process_session_message(
        self, task_id: str, content: str, *,
        session_id: str | None = None,
        attachment_refs: list[dict] | None = None,
        message_metadata: dict[str, Any] | None = None,
        user_message_id: str | None = None,
        user_message_created_at: float | None = None,
        run_engine: Any | None = None,
        run_project_id: str | None = None,
    ) -> None:
        """Process user message in session context via engine.

        Passes session_id to engine.process_message so the engine can:
        - Record user/assistant turns to session memory
        - Build session-aware context for agent execution

        Temporarily overrides engine.on_progress so that all progress
        during this call routes to session:{task_id}.
        """
        engine = run_engine or self.engine
        pid = self._normalize_project_id(run_project_id or getattr(engine, "project_id", None))
        channel_id = f"session:{task_id}"

        # Register session→task mapping for company runtime child resolution
        if session_id:
            self._session_to_task[session_id] = task_id

        session_exec_mode = self._normalize_session_exec_mode(self._exec_mode)
        session_company_profile = self._normalize_session_company_profile(self._company_profile)
        session_preferred_agent = self._task_preferred_agent
        session_org_id = ""
        task = None
        store = engine.store
        if self._store_is_ready(store):
            from opc.core.models import TaskStatus
            task = await store.get_task(task_id)
            if task:
                session_exec_mode, session_company_profile = self._resolve_task_session_config(task)
                session_org_id = self._resolve_task_org_id(task)
                session_preferred_agent = self._resolve_task_preferred_agent(task)

        # Per-task lock: same session serialized, different sessions concurrent
        async with self._get_task_lock(task_id):
            current_task = asyncio.current_task()
            if current_task is not None:
                self._task_lock_holders[task_id] = current_task
            if self._store_is_ready(store):
                from opc.core.models import TaskStatus
                task = await store.get_task(task_id)
                if task:
                    session_exec_mode, session_company_profile = self._resolve_task_session_config(task)
                    session_org_id = self._resolve_task_org_id(task)
                    session_preferred_agent = self._resolve_task_preferred_agent(task)
                    if task.status == TaskStatus.DONE and self._is_company_session_exec_mode(session_exec_mode):
                        task.status = TaskStatus.IDLE
                        task.metadata = dict(getattr(task, "metadata", {}) or {})
                        task.metadata["company_session_reopened_at"] = datetime.now().isoformat()
                        await store.save_task(task)
                    await apply_task_status_transition(
                        store,
                        task,
                        target_status_or_phase=TaskStatus.RUNNING,
                        reason="session_message_started",
                    )
            await self.broadcast({"type": "board_task_status_changed", "payload": {
                "project_id": pid, "task_id": task_id, "column_id": "in-progress", "status": "running",
            }})
            terminal_board_broadcast_sent = False
            # Register company runtime origin so child-task progress can dual-route
            company_runtime_target: dict[str, Any] | None = None
            if session_exec_mode in ("company", "org", "custom"):
                self._active_runtime_children[task_id] = task_id  # primary maps to self
                try:
                    company_runtime_target = await self._resolve_company_runtime_target(task_id, engine=engine)
                    await self._set_company_runtime_control(company_runtime_target, state="running")
                except Exception:
                    logger.opt(exception=True).debug("failed to mark company session runtime running")
            try:
                engine_mode, company_profile = self._resolve_engine_mode(
                    session_exec_mode,
                    session_company_profile,
                )
                engine_preferred_agent = session_preferred_agent if session_exec_mode == "task" else None
                if task is not None:
                    await self._persist_session_config(
                        task,
                        exec_mode=session_exec_mode,
                        company_profile=session_company_profile,
                        preferred_agent=session_preferred_agent,
                        org_id=session_org_id,
                        engine=engine,
                    )
                engine_message_metadata = dict(message_metadata or {})
                engine_message_metadata.update(_ui_message_identity_metadata(
                    message_id=user_message_id,
                    conversation_turn_id=_ui_conversation_turn_id(user_message_id),
                    created_at=user_message_created_at,
                ))
                response = await engine.process_message(
                    content,
                    project_id=pid,
                    session_id=session_id,
                    mode=engine_mode,
                    org_id=session_org_id or None,
                    company_profile=company_profile,
                    preferred_agent=engine_preferred_agent,
                    origin_task_id=task_id,
                    attachment_refs=attachment_refs,
                    message_metadata=engine_message_metadata or None,
                )
                updated_checkpoint_msg = await self._mark_checkpoint_card_after_engine_response(
                    channel_id=channel_id,
                    project_id=pid,
                    engine=engine,
                    message_metadata=engine_message_metadata,
                    response_message_id=user_message_id,
                )
                if updated_checkpoint_msg is not None:
                    await self.broadcast({"type": "session_message", "payload": updated_checkpoint_msg})
                # ── Check for pending checkpoint → attach structured metadata ──
                checkpoint_meta = await self._extract_checkpoint_metadata(
                    task_id, session_id=session_id, engine=engine,
                )
                await self._sync_task_transcript_messages(
                    task_id,
                    engine=engine,
                    latest_assistant_metadata=checkpoint_meta if checkpoint_meta else None,
                )
                await self._ensure_reply_projected(
                    channel_id=channel_id,
                    project_id=pid,
                    session_id=session_id or (str(getattr(task, "session_id", "") or "").strip() if task else None),
                    engine=engine,
                )

                # ── Status: idle only while the engine left the task active ──
                store = engine.store
                final_status = "idle"
                final_column_id = "in-progress"
                if self._store_is_ready(store):
                    from opc.core.models import TaskStatus as TS
                    t = await store.get_task(task_id)
                    if t:
                        try:
                            current_status = t.status if isinstance(t.status, TS) else TS(str(t.status))
                        except ValueError:
                            current_status = TS.IDLE
                        if current_status in {TS.PENDING, TS.RUNNING}:
                            t.status = TS.IDLE
                            await store.save_task(t)
                            current_status = TS.IDLE
                        final_status = current_status.value
                        if current_status in {TS.DONE, TS.CANCELLED}:
                            final_column_id = "done"
                await self.broadcast({"type": "board_task_status_changed", "payload": {
                    "project_id": pid, "task_id": task_id, "column_id": final_column_id, "status": final_status,
                }})
                terminal_board_broadcast_sent = True
                if session_exec_mode in ("company", "org", "custom"):
                    try:
                        idle_target = await self._resolve_company_runtime_target(task_id, engine=engine)
                        await self._set_company_runtime_control(idle_target or company_runtime_target, state="idle")
                    except Exception:
                        logger.opt(exception=True).debug("failed to mark company session runtime idle")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"Session processing error: {e}")
                msg = await self.chat_store.insert_message(
                    channel_id=channel_id,
                    sender="system",
                    sender_name="OPC",
                    content=f"Error: {e}",
                    project_id=pid,
                )
                await self.broadcast({"type": "session_message", "payload": msg})

                # ── Status: failed ─────────────────────────────────────
                store = engine.store
                if self._store_is_ready(store):
                    from opc.core.models import TaskStatus as TS
                    t = await store.get_task(task_id)
                    if t:
                        t.status = TS.FAILED
                        await store.save_task(t)
                await self.broadcast({"type": "board_task_status_changed", "payload": {
                    "project_id": pid, "task_id": task_id, "column_id": "in-progress", "status": "failed",
                }})
                terminal_board_broadcast_sent = True
                if session_exec_mode in ("company", "org", "custom"):
                    try:
                        failed_target = await self._resolve_company_runtime_target(task_id, engine=engine)
                        await self._set_company_runtime_control(failed_target or company_runtime_target, state="idle")
                    except Exception:
                        logger.opt(exception=True).debug("failed to clear company session runtime after error")
            finally:
                # Flush progress buffers before clearing company runtime mappings
                child_ids = [k for k, v in self._active_runtime_children.items() if v == task_id and k != task_id]
                for cid in child_ids:
                    await self._flush_progress(cid, project_id=pid)
                await self._flush_progress(task_id, project_id=pid)
                # Clean up per-task state
                self._active_runtime_children.pop(task_id, None)
                for k in child_ids:
                    self._active_runtime_children.pop(k, None)
                    self._stop_requested_task_ids.discard(k)
                self._task_locks.pop(task_id, None)
                self._task_lock_holders.pop(task_id, None)
                self._stop_requested_task_ids.discard(task_id)
                if session_id:
                    self._session_to_task.pop(session_id, None)
                if not terminal_board_broadcast_sent:
                    # A cancelled run (or a failure inside the failure handler)
                    # never reaches the terminal broadcasts above, leaving the
                    # board stuck on "running" until a full refresh. Mirror the
                    # persisted status best-effort; never write engine state here.
                    try:
                        store = engine.store
                        if self._store_is_ready(store):
                            t = await store.get_task(task_id)
                            if t is not None:
                                status_val = t.status.value if hasattr(t.status, "value") else str(t.status)
                                await self.broadcast({"type": "board_task_status_changed", "payload": {
                                    "project_id": pid,
                                    "task_id": task_id,
                                    "column_id": "done" if status_val in ("done", "cancelled") else "in-progress",
                                    "status": status_val,
                                }})
                    except Exception:
                        logger.opt(exception=True).debug("failed to mirror terminal board status")
                # Clear agent runtime indicator — include agent_id so the
                # frontend can also clear the swarm agent's reflecting/tool_active state.
                idle_payload: dict[str, Any] = {
                    "project_id": pid,
                    "task_id": task_id, "status": "idle", "current_tool": None, "iteration": 0,
                }
                resolved_agent = self._resolve_agent_for_idle(task_id, task)
                if resolved_agent:
                    idle_payload["agent_id"] = resolved_agent
                await self.broadcast({"type": "agent_runtime_update", "payload": idle_payload})

    # ── Checkpoint metadata extraction ──────────────────────────────────

    async def _extract_checkpoint_metadata(
        self,
        task_id: str,
        *,
        session_id: str | None = None,
        engine: Any | None = None,
    ) -> dict[str, Any] | None:
        """Query pending checkpoint from engine store and build structured metadata.

        Called after engine.process_message() returns.  The engine saves the
        checkpoint *before* returning its summary string, so a pending
        checkpoint is guaranteed to exist here if the response represents an
        interactive confirmation prompt.

        Returns a dict suitable for ChatStore message ``metadata``, or None.
        """
        runtime_engine = engine or self.engine
        checkpoint = await runtime_engine.get_latest_pending_checkpoint_for_session(session_id)
        if not checkpoint:
            return None

        if checkpoint.checkpoint_type == "task_user_input":
            return await self._build_task_user_input_meta(checkpoint, engine=runtime_engine)
        if checkpoint.checkpoint_type == "company_work_item_gate":
            return self._build_company_work_item_gate_meta(checkpoint)
        if checkpoint.checkpoint_type == "company_staffing_selection":
            return self._build_staffing_selection_meta(checkpoint)
        if checkpoint.checkpoint_type == "company_recruitment_confirmation":
            return self._build_recruitment_meta(checkpoint, engine=runtime_engine)
        if checkpoint.checkpoint_type == "company_reorg_pending":
            return await self._build_reorg_meta(checkpoint, engine=runtime_engine)
        if checkpoint.checkpoint_type == "company_delivery_feedback":
            return self._build_delivery_feedback_meta(checkpoint)
        return None

    async def _build_task_user_input_meta(
        self,
        cp: Any,
        *,
        engine: Any | None = None,
    ) -> dict[str, Any] | None:
        payload = dict(cp.payload or {})
        pause_request = dict(payload.get("pause_request", {}) or {})
        task_id = str(payload.get("task_id", "")).strip()
        work_item_projection_id = ""
        work_item_projection_title = ""
        runtime_engine = engine or self.engine
        store = runtime_engine.store
        work_item_turn_type = ""
        if store and task_id:
            task = await store.get_task(task_id)
            if task:
                task_metadata = dict(getattr(task, "metadata", {}) or {})
                work_item_projection_id = str(task_metadata.get("work_item_projection_id", "") or "").strip()
                work_item_turn_type = str(task_metadata.get("work_item_turn_type", "") or "").strip()
                work_item_projection_title = str(task.title or "").strip()

        prompt = str(payload.get("prompt", "") or "").strip()
        summary = str(pause_request.get("reason", "") or prompt).strip()
        questions = [str(item).strip() for item in list(pause_request.get("questions", []) or []) if str(item).strip()]
        input_questions = [
            dict(item)
            for item in list(pause_request.get("input_questions", []) or [])
            if isinstance(item, dict) and str(item.get("question", "") or item.get("header", "") or "").strip()
        ]
        if not input_questions:
            input_questions = [
                {
                    "id": f"question_{index + 1}",
                    "header": "",
                    "question": question,
                    "options": [],
                    "allow_freeform": True,
                    "required": True,
                }
                for index, question in enumerate(questions)
            ]
        required_fields = [
            str(item).strip()
            for item in list(pause_request.get("required_fields", []) or [])
            if str(item).strip()
        ]
        resume_hint = str(pause_request.get("resume_hint", "") or "").strip()
        if not resume_hint and "blocked by autonomy policy" in f"{prompt} {summary}".lower():
            # This park came from a tool-approval timeout. The approval card
            # posted earlier stays pending and clickable indefinitely, so point
            # the user at it instead of leaving typed input as the only path.
            resume_hint = (
                "Tip: the tool-approval card above is still active — choose an option "
                "there (e.g. Approve) to grant the permission and resume this task "
                "automatically. Reply here only to give different instructions."
            )

        return {
            "checkpoint_type": "task_user_input",
            "checkpoint_id": cp.checkpoint_id,
            "task_id": task_id,
            **work_item_identity_payload(projection_id=work_item_projection_id, turn_type=work_item_turn_type),
            "work_item_projection_title": work_item_projection_title,
            "summary": summary,
            "prompt": prompt,
            "questions": questions,
            "input_questions": input_questions,
            "required_fields": required_fields,
            "context_note": str(pause_request.get("context_note", "") or "").strip(),
            "resume_hint": resume_hint,
            "requesting_role_id": str(
                payload.get("requesting_role_id") or pause_request.get("requesting_role_id") or ""
            ).strip(),
            "requesting_task_id": str(
                payload.get("requesting_task_id") or pause_request.get("requesting_task_id") or ""
            ).strip(),
            "requesting_work_item_id": str(
                payload.get("requesting_work_item_id") or pause_request.get("requesting_work_item_id") or ""
            ).strip(),
            "seat_id": str(payload.get("seat_id") or pause_request.get("seat_id") or "").strip(),
            "runtime_session_id": str(payload.get("runtime_session_id", "") or "").strip(),
            "resume_cursor": payload.get("resume_cursor"),
            "active_subagents": list(payload.get("active_subagents", []) or []),
            "permission_requests": list(payload.get("permission_requests", []) or []),
            "worktree_path": str(payload.get("worktree_path", "") or "").strip(),
        }

    def _build_company_work_item_gate_meta(self, cp: Any) -> dict[str, Any]:
        payload = dict(cp.payload or {})
        gate = dict(payload.get("gate", {}) or {})
        projection_id = str(payload.get("work_item_projection_id") or "").strip()
        turn_type = str(payload.get("work_item_turn_type") or "").strip()
        projection_title = str(
            payload.get("work_item_projection_title") or projection_id or "Work item gate"
        ).strip()
        gate_type = str(gate.get("type", "") or "review").strip()
        prompt_lines = [
            f"{projection_title} requires confirmation.",
            f"Gate type: {gate_type}",
        ]
        instructions = str(gate.get("instructions", "") or "").strip()
        if instructions:
            prompt_lines.append(f"Instructions: {instructions}")
        return {
            "checkpoint_type": "company_work_item_gate",
            "checkpoint_id": cp.checkpoint_id,
            **work_item_identity_payload(projection_id=projection_id, turn_type=turn_type),
            "work_item_projection_title": projection_title,
            "company_profile": payload.get("company_profile", ""),
            "summary": instructions or f"Pending {gate_type} confirmation",
            "prompt": "\n".join(prompt_lines),
            "options": [
                {"id": "approve", "label": "Approve"},
                {"id": "deny", "label": "Deny"},
            ],
            "default_action": "deny",
            "runtime_session_id": str(payload.get("runtime_session_id", "") or "").strip(),
            "resume_cursor": payload.get("resume_cursor"),
            "active_subagents": list(payload.get("active_subagents", []) or []),
            "permission_requests": list(payload.get("permission_requests", []) or []),
            "worktree_path": str(payload.get("worktree_path", "") or "").strip(),
        }

    def _build_recruitment_meta(self, cp: Any, *, engine: Any | None = None) -> dict[str, Any]:
        """Extract recruitment plan data into frontend-friendly metadata."""
        runtime_engine = engine or self.engine
        payload = cp.payload
        rp = payload.get("recruitment_plan", {})
        plan_metadata = dict(rp.get("metadata", {}) or {})
        recruitment_agent = self._normalize_session_preferred_agent(
            payload.get("recruitment_agent") or plan_metadata.get("recruitment_agent") or "native",
            default="native",
        )
        proposals_raw = rp.get("proposals", [])
        proposals = []
        payload_role_agents: dict[str, str] = {}
        raw_payload_role_agents = payload.get("recruitment_role_agents")
        if isinstance(raw_payload_role_agents, dict):
            payload_role_agents = {
                str(raw_role_id or "").strip(): self._normalize_session_preferred_agent(raw_agent, default="codex")
                for raw_role_id, raw_agent in raw_payload_role_agents.items()
                if str(raw_role_id or "").strip()
            }
        recruitment_role_agents: dict[str, str] = {}
        employee_payloads: list[dict[str, Any]] = []
        template_payloads: list[dict[str, Any]] = []
        org_engine = getattr(runtime_engine, "org_engine", None)
        talent_market = getattr(runtime_engine, "talent_market", None)
        is_placeholder = getattr(runtime_engine, "_is_placeholder_staffing_employee", lambda _employee: False)
        employee_payload = getattr(runtime_engine, "_staffing_employee_payload", None)
        template_payload = getattr(runtime_engine, "_staffing_template_payload", None)
        if org_engine and callable(employee_payload):
            try:
                employee_payloads = [
                    employee_payload(employee)
                    for employee in org_engine.list_employees()
                    if not is_placeholder(employee)
                ]
            except Exception:
                employee_payloads = []
        if talent_market and callable(template_payload):
            try:
                template_payloads = [
                    template_payload(template)
                    for template in talent_market.list_available_templates()
                    if str(getattr(template, "id", "") or "").strip()
                ]
            except Exception:
                template_payloads = []
        employees_by_id = {
            str(item.get("employee_id", "") or "").strip(): item
            for item in employee_payloads
            if str(item.get("employee_id", "") or "").strip()
        }
        templates_by_id = {
            str(item.get("template_id", "") or "").strip(): item
            for item in template_payloads
            if str(item.get("template_id", "") or "").strip()
        }
        employees_by_role: dict[str, list[dict[str, Any]]] = {}
        for item in employee_payloads:
            role_id = str(item.get("role_id", "") or "").strip()
            if role_id:
                employees_by_role.setdefault(role_id, []).append(item)
        staffing_roles: list[dict[str, Any]] = []
        staffing_selections: dict[str, dict[str, str]] = {}
        recruitment_rationales: list[dict[str, Any]] = []
        for p in proposals_raw:
            role_id = str(p.get("role_id", "") or "").strip()
            proposal_metadata = dict(p.get("metadata", {}) or {})
            entry: dict[str, Any] = {
                "role_id": role_id,
                "status": p.get("status", ""),
                "rationale": p.get("rationale", ""),
                "role_labels": p.get("role_labels", []),
            }
            cand = p.get("candidate")
            if cand:
                entry["candidate"] = {
                    "template_id": cand.get("template_id", ""),
                    "template_name": cand.get("template_name", ""),
                    "category": cand.get("category", ""),
                    "domains": cand.get("domains", []),
                    "proposed_name": cand.get("proposed_employee_name", ""),
                    "rationale": cand.get("rationale", ""),
                }
                template_id = str(cand.get("template_id", "") or "").strip()
                if template_id and template_id not in templates_by_id:
                    templates_by_id[template_id] = {
                        "kind": "template",
                        "template_id": template_id,
                        "template_name": cand.get("template_name", "") or template_id,
                        "category": cand.get("category", ""),
                        "domains": cand.get("domains", []),
                        "tags": [],
                        "description": "",
                        "preferred_external_agent": cand.get("preferred_external_agent"),
                        "source_path": cand.get("source_path", ""),
                    }
            emp = p.get("existing_employee")
            if emp:
                entry["existing_employee"] = {
                    "employee_id": emp.get("employee_id", ""),
                    "employee_name": emp.get("employee_name", ""),
                    "role_id": emp.get("role_id", ""),
                    "domains": emp.get("domains", []),
                    "experience_score": emp.get("experience_score", 0),
                    "rationale": emp.get("rationale", ""),
                }
                employee_id = str(emp.get("employee_id", "") or "").strip()
                if employee_id and employee_id not in employees_by_id:
                    employee_payload_item = {
                        "kind": "employee",
                        "employee_id": employee_id,
                        "employee_name": emp.get("employee_name", "") or employee_id,
                        "role_id": emp.get("role_id", "") or role_id,
                        "category": emp.get("category", ""),
                        "domains": emp.get("domains", []),
                        "tags": [],
                        "description": "",
                        "preferred_external_agent": None,
                        "experience_score": emp.get("experience_score", 0),
                    }
                    employees_by_id[employee_id] = employee_payload_item
                    if role_id:
                        employees_by_role.setdefault(role_id, []).append(employee_payload_item)
            default_agent = self._normalize_session_preferred_agent("codex", default="codex")
            entry["default_agent"] = default_agent
            entry["selected_agent"] = self._normalize_session_preferred_agent(
                payload_role_agents.get(role_id) or proposal_metadata.get("selected_execution_agent"),
                default=default_agent,
            )
            if role_id:
                recruitment_role_agents[role_id] = entry["selected_agent"]
            proposals.append(entry)
            role_label = str((p.get("role_labels", []) or [role_id])[0] or role_id)
            default_selection: dict[str, str] = {"kind": "fallback", "id": ""}
            selection_label = "Fallback role-only"
            cand = p.get("candidate")
            emp = p.get("existing_employee")
            if emp and str(emp.get("employee_id", "") or "").strip():
                employee_id = str(emp.get("employee_id", "") or "").strip()
                default_selection = {"kind": "employee", "id": employee_id, "employee_id": employee_id}
                selection_label = str(emp.get("employee_name", "") or employee_id)
            elif cand and str(cand.get("template_id", "") or "").strip():
                template_id = str(cand.get("template_id", "") or "").strip()
                default_selection = {"kind": "template", "id": template_id, "template_id": template_id}
                selection_label = str(cand.get("proposed_employee_name") or cand.get("template_name") or template_id)
            if role_id:
                staffing_selections[role_id] = dict(default_selection)
                same_role_ids = {
                    str(item.get("employee_id", "") or "").strip()
                    for item in employees_by_role.get(role_id, [])
                    if str(item.get("employee_id", "") or "").strip()
                }
                same_role_ids.update(
                    str(item or "").strip()
                    for item in list(p.get("existing_employee_ids", []) or [])
                    if str(item or "").strip()
                )
                staffing_roles.append(
                    {
                        "role_id": role_id,
                        "role_label": role_label,
                        "role_responsibility": "",
                        "default_selection": default_selection,
                        "same_role_employee_ids": sorted(same_role_ids),
                        "fallback_available": True,
                        "default_agent": default_agent,
                        "selected_agent": entry["selected_agent"],
                        "default_source": "recruitment",
                    }
                )
                reason_parts = [
                    str((cand or {}).get("rationale", "") or "").strip(),
                    str((emp or {}).get("rationale", "") or "").strip(),
                    str(p.get("rationale", "") or "").strip(),
                ]
                rationale = next((item for item in reason_parts if item), "")
                recruitment_rationales.append(
                    {
                        "role_id": role_id,
                        "role_label": role_label,
                        "status": p.get("status", ""),
                        "selection_label": selection_label,
                        "rationale": rationale,
                    }
                )
        employee_payloads = list(employees_by_id.values())
        template_payloads = list(templates_by_id.values())
        return {
            "checkpoint_type": "company_recruitment_confirmation",
            "checkpoint_id": cp.checkpoint_id,
            "company_profile": rp.get("company_profile", "corporate"),
            "previous_checkpoint_id": payload.get("previous_checkpoint_id", ""),
            "recruitment_revision": payload.get("recruitment_revision") or dict(rp.get("metadata", {}) or {}).get("recruitment_revision"),
            "recruiter_feedback": list(rp.get("recruiter_feedback", []) or []),
            "recruitment_agent": recruitment_agent,
            "recruitment_role_agents": recruitment_role_agents,
            "proposals": proposals,
            "summary": rp.get("summary", ""),
            "recruitment_rationales": recruitment_rationales,
            "staffing_roles": staffing_roles,
            "staffing_pool": {
                "employees": employee_payloads,
                "templates": template_payloads,
            },
            "staffing_selections": staffing_selections,
        }

    def _build_staffing_selection_meta(self, cp: Any) -> dict[str, Any]:
        """Extract manual staffing data into frontend-friendly metadata."""
        payload = dict(cp.payload or {})
        raw_role_agents = payload.get("recruitment_role_agents")
        payload_role_agents = raw_role_agents if isinstance(raw_role_agents, dict) else {}
        recruitment_agent = self._normalize_session_preferred_agent(
            payload.get("recruitment_agent") or "native",
            default="native",
        )
        staffing_roles: list[dict[str, Any]] = []
        recruitment_role_agents: dict[str, str] = {}
        for raw_role in list(payload.get("staffing_roles", []) or []):
            if not isinstance(raw_role, dict):
                continue
            role = dict(raw_role)
            role_id = str(role.get("role_id", "") or "").strip()
            selected_agent = self._normalize_session_preferred_agent(
                payload_role_agents.get(role_id) or role.get("selected_agent") or role.get("default_agent") or "codex",
                default="codex",
            )
            if role_id:
                role["selected_agent"] = selected_agent
                recruitment_role_agents[role_id] = selected_agent
            staffing_roles.append(role)
        return {
            "checkpoint_type": "company_staffing_selection",
            "checkpoint_id": cp.checkpoint_id,
            "company_profile": payload.get("company_profile", "corporate"),
            "summary": payload.get("summary") or "Select staff manually, or run automatic recruitment.",
            "staffing_strategy": payload.get("staffing_strategy", ""),
            "recommended_action": payload.get("recommended_action", ""),
            "staffing_defaults": dict(payload.get("staffing_defaults", {}) or {}),
            "staffing_roles": staffing_roles,
            "recruitment_agent": recruitment_agent,
            "recruitment_role_agents": recruitment_role_agents,
            "staffing_pool": dict(payload.get("staffing_pool", {}) or {}),
        }

    async def _build_reorg_meta(
        self,
        cp: Any,
        *,
        engine: Any | None = None,
    ) -> dict[str, Any] | None:
        """Extract reorg proposal data into frontend-friendly metadata."""
        runtime_engine = engine or self.engine
        store = runtime_engine.store
        if not store:
            return None
        proposal_id = cp.payload.get("proposal_id", "")
        if not proposal_id:
            return None
        proposal = await store.get_reorg_proposal(proposal_id)
        if not proposal:
            return None
        changeset = proposal.changeset
        role_changes = []
        if hasattr(changeset, "role_changes"):
            for rc in changeset.role_changes:
                role_changes.append({
                    "action": rc.action,
                    "role_id": rc.role_id,
                    "replacement_role_id": rc.replacement_role_id,
                    "reason": rc.reason,
                })
        return {
            "checkpoint_type": "company_reorg_pending",
            "checkpoint_id": cp.checkpoint_id,
            "proposal_id": proposal.proposal_id,
            "scope": proposal.scope.value,
            "risk_level": proposal.risk_level.value,
            "status": proposal.status.value,
            "title": proposal.title,
            "summary": proposal.summary,
            "rationale": proposal.rationale,
            "role_changes": role_changes,
            "impact_summary": proposal.impact_summary,
            "user_confirmation_required": proposal.user_confirmation_required,
        }

    def _build_delivery_feedback_meta(self, cp: Any) -> dict[str, Any]:
        payload = dict(cp.payload or {})
        prompt = str(payload.get("prompt", "") or "").strip()
        projection_title = str(payload.get("work_item_projection_title", "") or "").strip()
        feedback_scope = str(payload.get("feedback_scope", "work_item") or "work_item").strip()
        delivery_package = payload.get("delivery_package")
        if not isinstance(delivery_package, dict):
            delivery_package = {}
        result_content = str(payload.get("result_content", "") or "").strip()
        summary = str(
            delivery_package.get("executive_summary")
            or delivery_package.get("summary")
            or payload.get("work_item_summary")
            or payload.get("work_item_summary_for_downstream")
            or result_content
            or projection_title
            or ("Final delivery review" if feedback_scope == "final" else "Work item review")
        ).strip()
        waiting_task_id = str(
            payload.get("waiting_task_id")
            or payload.get("task_id")
            or getattr(cp, "task_id", "")
            or ""
        ).strip()
        return {
            "checkpoint_type": "company_delivery_feedback",
            "checkpoint_id": cp.checkpoint_id,
            "waiting_task_id": waiting_task_id,
            "task_id": waiting_task_id,
            **work_item_identity_payload(
                projection_id=str(payload.get("work_item_projection_id", "") or "").strip(),
                turn_type=str(payload.get("work_item_turn_type", "") or "").strip(),
            ),
            "work_item_projection_title": projection_title,
            "company_profile": payload.get("company_profile", ""),
            "feedback_scope": feedback_scope,
            "summary": summary,
            "prompt": prompt,
            "options": [
                {"id": "approve", "label": "Fully Agree / 完全同意"},
                {"id": "ignore", "label": "Ignore / 忽略"},
                {"id": "feedback", "label": "Feedback / 反馈"},
            ],
            "delivery_package": delivery_package,
            "result_content": result_content,
            "delivery_revision": payload.get("delivery_revision", ""),
            "owner_directive_revision": payload.get("owner_directive_revision", ""),
            "latest_user_directive": str(payload.get("latest_user_directive", "") or "").strip(),
            "waiting_work_item_id": str(payload.get("waiting_work_item_id", "") or "").strip(),
            "runtime_session_id": str(payload.get("runtime_session_id", "") or "").strip(),
            "resume_cursor": payload.get("resume_cursor"),
            "active_subagents": list(payload.get("active_subagents", []) or []),
            "permission_requests": list(payload.get("permission_requests", []) or []),
            "worktree_path": str(payload.get("worktree_path", "") or "").strip(),
        }

    # ── Secretary ──────────────────────────────────────────────────────

    async def _handle_secretary_send(self, ws: Any, data: dict) -> None:
        """Handle user message in the secretary channel (no kanban task linkage)."""
        content = data.get("content", "")
        if not content:
            return

        run_engine, pid = await self._engine_for_request(data)

        # Ensure secretary channel exists
        await self.chat_store.ensure_secretary_channel(project_id=pid)
        secretary_channel = f"secretary:{pid}"

        # Lazily resolve or create a persistent secretary session
        secretary_session_id = self._secretary_session_ids.get(pid)
        if secretary_session_id is None:
            if getattr(run_engine, "secretary", None):
                sessions = await run_engine.secretary.list_sessions(
                    pid, limit=1,
                )
                if sessions:
                    secretary_session_id = sessions[0].session_id
            if secretary_session_id is None:
                import uuid as _uuid
                secretary_session_id = str(_uuid.uuid4())
            self._secretary_session_ids[pid] = secretary_session_id
            if self._active_engine_project_id() == pid:
                self._secretary_session_id = secretary_session_id

        # Insert user message to chat_store for UI rendering
        msg = await self.chat_store.insert_message(
            channel_id=secretary_channel,
            sender="user",
            sender_name="You",
            content=content,
            project_id=pid,
        )
        await self.broadcast({"type": "session_message", "payload": msg})

        # Process via SecretaryService (shared with CLI `opc secretary`)
        self._track(self._process_secretary_message(content, engine=run_engine, project_id=pid, session_id=secretary_session_id))

    async def _process_secretary_message(
        self,
        content: str,
        *,
        engine: Any,
        project_id: str,
        session_id: str,
    ) -> None:
        """Call engine.process_secretary_message and write reply to secretary channel."""
        pid = project_id
        secretary_channel = f"secretary:{pid}"
        try:
            result = await engine.process_secretary_message(
                content,
                project_id=pid,
                session_id=session_id,
            )
            reply_text = result.get("response", "") if isinstance(result, dict) else str(result)

            # Show applied updates if any
            applied = result.get("applied_updates", []) if isinstance(result, dict) else []
            if applied:
                reply_text += "\n\n**Applied updates:**\n" + "\n".join(f"- {u}" for u in applied)

            msg = await self.chat_store.insert_message(
                channel_id=secretary_channel,
                sender="assistant",
                sender_name="Secretary",
                content=(reply_text or "No response."),
                project_id=pid,
            )
            await self.broadcast({"type": "session_message", "payload": msg})
        except Exception as e:
            logger.opt(exception=True).error(f"Secretary processing error: {e}")
            msg = await self.chat_store.insert_message(
                channel_id=secretary_channel,
                sender="system",
                sender_name="Secretary",
                content=f"Error: {e}",
                project_id=pid,
            )
            await self.broadcast({"type": "session_message", "payload": msg})

    # ── Project Management ──────────────────────────────────────────────

    async def _handle_list_projects(self, ws: Any, data: dict) -> None:
        """List available projects by scanning the projects directory."""
        result = await self.services.project.list(active_project_id=self._client_active_project_id(ws))
        await self._send_ack(ws, ok=True, **result.payload)

    async def _handle_create_project(self, ws: Any, data: dict) -> None:
        """Create a new project directory."""
        try:
            result = await self.services.project.create(
                data.get("project_id", ""),
                active_project_id=self._client_active_project_id(ws),
            )
            await self._send_ack(ws, ok=True, **result.payload)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="create_project")

    async def _handle_delete_project(self, ws: Any, data: dict) -> None:
        """Delete a project and all its data. Switches to 'default' if active."""
        try:
            result = await self.services.project.delete(data.get("project_id", ""))
            self.engine = self.services_context.engine
            if result.payload.get("active_project_id") == "default":
                self._secretary_session_id = None
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, project_id=result.payload.get("project_id"))
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="delete_project")

    async def _handle_switch_project(self, ws: Any, data: dict) -> None:
        """Switch the active project view without rebinding in-flight runtimes."""
        new_id = data.get("project_id", "").strip()
        switch_seq = str(data.get("switch_seq") or data.get("switchSeq") or "").strip()
        try:
            await self.services.project.switch(new_id, switch_seq=switch_seq, include_snapshot=False)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="switch_project")
            return
        except Exception as exc:
            logger.opt(exception=True).error(
                f"Failed to prepare project switch for {new_id}: {type(exc).__name__}: {exc!r}",
            )
            await self._send_ack(
                ws,
                ok=False,
                project_id=new_id,
                switch_seq=switch_seq,
                error=f"Failed to switch project `{new_id}`: {exc}",
            )
            return

        project_engine = self.services_context.engine
        self._client_project_ids[ws] = new_id
        self._client_switch_seq[ws] = switch_seq
        self._secretary_session_id = None
        await self._send_envelope_to_client(
            ws,
            {"type": "project_switched", "payload": {"project_id": new_id, "switch_seq": switch_seq}},
        )
        self._track_client_project_index(
            ws,
            self._send_project_index_for_client(
                ws,
                project_engine,
                new_id,
                switch_seq=switch_seq,
                include_snapshot=True,
            ),
        )
        await self._send_ack(ws, ok=True, project_id=new_id, switch_seq=switch_seq)

    # ── Org Info handler ─────────────────────────────────────────────

    async def _handle_org_info(self, ws: Any, data: dict) -> None:
        """Return org structure, employees, runtime topology, and channel statuses."""
        result = await self._ensure_office_services().org.info()
        await ws.send_json({"type": "org_info", "payload": result.payload})

    # ── Phase 4: Talent Market ──────────────────────────────────────────

    async def _handle_talent_import(self, ws: Any, data: dict) -> None:
        """Import talent templates from a local repo directory."""
        try:
            result = await self._ensure_office_services().talent.import_repo(data.get("repo_path", ""))
            self._local_talent_cache = None
            await self._send_service_ack(ws, result)
            await self._send_talent_list(ws)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="talent_import")
        except Exception as exc:
            logger.warning(f"Failed to import talent templates: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_talent_scan_local(self, ws: Any, data: dict) -> None:
        """Scan local talent directory and return unregistered templates for selection."""
        try:
            result = await self._ensure_office_services().talent.scan()
            await ws.send_json({"type": "talent_scan_local", "payload": result.payload})
        except Exception:
            await ws.send_json({"type": "talent_scan_local", "payload": {"templates": []}})

    async def _handle_talent_import_selected(self, ws: Any, data: dict) -> None:
        """Import user-selected templates from the local talent directory."""
        try:
            result = await self._ensure_office_services().talent.import_selected(list(data.get("template_ids", []) or []))
            self._local_talent_cache = None
            await self._send_service_ack(ws, result)
            await self._send_talent_list(ws)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="talent_import_selected")
        except Exception as exc:
            logger.warning(f"Failed to import selected templates: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _send_talent_list(self, ws: Any) -> None:
        result = await self._ensure_office_services().talent.list()
        await ws.send_json({"type": "talent_list", "payload": result.payload})

    async def _handle_talent_list(self, ws: Any, data: dict) -> None:
        """List all talent templates."""
        try:
            await self._send_talent_list(ws)
        except Exception:
            logger.debug("Failed to list talent templates")
            await ws.send_json({"type": "talent_list", "payload": {"templates": []}})

    async def _handle_talent_hire(self, ws: Any, data: dict) -> None:
        """Hire a talent template into an existing role."""
        try:
            result = await self._ensure_office_services().talent.hire(
                template_id=data.get("template_id", ""),
                role_id=data.get("role_id", ""),
                employee_name=data.get("employee_name"),
                employee_id=data.get("employee_id"),
                organization_id=data.get("org_id") or data.get("organization_id"),
            )
            self._local_talent_cache = None
            await self._publish_service_result(result)
            await self._send_service_ack(ws, result)
            await self._broadcast_org_info()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="talent_hire")
        except Exception as exc:
            logger.warning(f"Unexpected error hiring template: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_import_employee_as_agent(self, ws: Any, data: dict) -> None:
        """Import an existing org employee as a visual office agent."""
        try:
            result = await self._ensure_office_services().talent.import_employee_as_agent(
                employee_id=data.get("employee_id", ""),
                office_id=data.get("office_id", "office-0"),
            )
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, action="employee_imported", imported_employee_id=result.payload.get("imported_employee_id"))
            await self._handle_org_info(ws, {})
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="import_employee_as_agent")
        except Exception as exc:
            logger.warning(f"Failed to import employee: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    # ── Phase 4: Employee Detail ────────────────────────────────────────

    async def _handle_employee_detail(self, ws: Any, data: dict) -> None:
        """Return detailed evolution profile for an employee."""
        employee_id = data.get("employee_id", "")
        if not employee_id:
            await ws.send_json({"type": "employee_detail", "payload": {"employee_id": "", "error": "employee_id required"}})
            return
        try:
            result = await self._ensure_office_services().talent.employee_detail(employee_id)
            payload = dict(result.payload.get("employee", {}) or {})
            await ws.send_json({"type": "employee_detail", "payload": payload})
        except ServiceError as exc:
            await ws.send_json({"type": "employee_detail", "payload": {"employee_id": employee_id, "error": exc.message}})

    # ── Phase 4: Reorg Management ───────────────────────────────────────

    async def _handle_reorg_list(self, ws: Any, data: dict) -> None:
        """List recent reorg proposals for the current project."""
        store = self.engine.store
        if not store:
            await ws.send_json({"type": "reorg_list", "payload": {"proposals": []}})
            return
        project_id = self.engine.project_id or "default"
        try:
            proposals = await store.list_reorg_proposals(project_id, limit=20)
            result = []
            for p in proposals:
                changeset_summary: dict[str, Any] = {
                    "role_changes": [],
                    "task_adjustments_count": 0,
                }
                if p.changeset:
                    changeset_summary = {
                        "role_changes": [
                            {"action": rc.action, "role_id": rc.role_id, "reason": rc.reason}
                            for rc in (p.changeset.role_changes or [])
                        ],
                        "task_adjustments_count": len(p.changeset.task_adjustments or []),
                    }
                result.append({
                    "proposal_id": p.proposal_id,
                    "title": p.title,
                    "summary": p.summary,
                    "rationale": p.rationale,
                    "scope": p.scope.value if hasattr(p.scope, "value") else str(p.scope),
                    "risk_level": p.risk_level.value if hasattr(p.risk_level, "value") else str(p.risk_level),
                    "status": p.status.value if hasattr(p.status, "value") else str(p.status),
                    "initiated_by": p.initiated_by,
                    "changeset": changeset_summary,
                    "impact_summary": p.impact_summary or {},
                    "created_at": p.created_at.timestamp() if hasattr(p.created_at, "timestamp") else 0,
                    "updated_at": p.updated_at.timestamp() if hasattr(p.updated_at, "timestamp") else 0,
                })
            await ws.send_json({"type": "reorg_list", "payload": {"proposals": result}})
        except Exception:
            logger.debug("Failed to list reorg proposals")
            await ws.send_json({"type": "reorg_list", "payload": {"proposals": []}})

    async def _handle_reorg_decide(self, ws: Any, data: dict) -> None:
        """Approve or deny a reorg proposal from the UI."""
        proposal_id = data.get("proposal_id", "")
        approved = data.get("approved", False)
        notes = data.get("notes", "")
        rm = getattr(self.engine, "reorg_manager", None)
        if not rm or not proposal_id:
            await ws.send_json({"type": "ack", "payload": {"ok": False, "error": "reorg_manager not available or missing proposal_id"}})
            return
        try:
            await rm.set_reorg_approval(proposal_id, approved=approved, notes=notes)
            if approved:
                result = await rm.apply_reorg(proposal_id)
                await ws.send_json({"type": "ack", "payload": {"ok": True, "action": "reorg_applied", "result": result}})
                # Refresh org info for all clients
                await self._handle_org_info(ws, {})
            else:
                await ws.send_json({"type": "ack", "payload": {"ok": True, "action": "reorg_denied"}})
        except Exception as exc:
            logger.warning(f"Failed to decide reorg {proposal_id}: {exc}")
            await ws.send_json({"type": "ack", "payload": {"ok": False, "error": str(exc)}})

    # ------------------------------------------------------------------
    # Org config import / export
    # ------------------------------------------------------------------

    async def _ack_saved_err(self, ws: Any, msg_type: str, name: str, error: str) -> None:
        """Shared error-ack helper for the saved-org handlers."""
        await ws.send_json({"type": msg_type, "payload": {
            "ok": False, "name": name, "error": error,
        }})

    async def _get_active_saved_org_name(self) -> str:
        config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
        try:
            active_id = read_org_index(config_dir)
            if active_id and org_config_path(config_dir, active_id).exists():
                return active_id
        except Exception:
            pass
        if not hasattr(self, "agent_store"):
            return ""
        try:
            name = await self.agent_store.get_server_state(_ACTIVE_SAVED_ORG_STATE_KEY, "")
        except Exception:
            return ""
        try:
            path = _saved_org_path(name, strict=False)
        except ValueError:
            return ""
        org_id = path.stem.removeprefix("org_").removesuffix("_config")
        if path.exists():
            try:
                write_org_index(config_dir, validate_saved_org_id(org_id))
            except Exception:
                pass
            return org_id
        return ""

    async def _set_active_saved_org_name(self, name: str | None) -> None:
        value = str(name or "").strip()
        config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
        if value:
            org_id = validate_saved_org_id(value)
            write_org_index(config_dir, org_id)
        if not hasattr(self, "agent_store"):
            return
        try:
            await self.agent_store.set_server_state(_ACTIVE_SAVED_ORG_STATE_KEY, value)
        except Exception:
            logger.debug("Failed to persist active saved org name")

    def _write_active_org_config(self, config: Any) -> None:
        config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
        org = getattr(config, "org", None)
        raw_org_id = str(getattr(org, "organization_id", "") or "").strip()
        raw_name = str(
            getattr(org, "organization_name", "")
            or getattr(org, "company_name", "")
            or raw_org_id
            or "org"
        ).strip()
        try:
            organization_id = validate_saved_org_id(raw_org_id)
        except ValueError:
            active_id = read_org_index(config_dir)
            organization_id = active_id or allocate_org_config_id(config_dir, raw_name)
        if org is not None:
            org.organization_id = organization_id
            org.organization_name = raw_name
            org.organization_config_file = org_config_relative_path(organization_id)
            org.company_profile = "custom"
            try:
                from opc.core.employee_registry import write_employee_registry

                org.employees, _ = write_employee_registry(
                    Path(config_dir).parent,
                    organization_id,
                    list(getattr(org, "employees", []) or []),
                )
            except Exception:
                logger.opt(exception=True).debug("Failed to write employee registry for active org")
        payload = build_org_config_payload_from_config(
            config,
            organization_id=organization_id,
            organization_name=raw_name,
        )
        write_org_config_payload(config_dir, organization_id, payload)
        write_org_index(config_dir, organization_id)

    def _persist_runtime_config(self) -> None:
        if str(getattr(self, "_exec_mode", "") or "").strip().lower() in {"org", "custom"}:
            self._write_active_org_config(self.engine.config)
            return
        self.engine.config.save()

    async def _restore_active_saved_org_if_needed(self) -> None:
        """Recover org mode from the last loaded saved architecture.

        Org mode owns its active index, so startup restore must not consult or
        mutate company_index.yaml.
        """
        if str(getattr(self, "_exec_mode", "") or "").strip().lower() not in {"org", "custom"}:
            return

        name = await self._get_active_saved_org_name()
        if not name:
            return
        try:
            config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
            payload, path = load_org_config_payload(config_dir, name)
            validated_config = apply_org_config_payload_to_config(
                self.engine.config,
                payload,
                source_path=path,
            )
        except Exception as exc:
            logger.warning(f"Failed to restore saved org '{name}' during startup: {exc}")
            return

        async with self._config_lock:
            self._rebind_engine_config(validated_config)
            if self.engine.org_engine:
                self.engine.org_engine.reload_from_config()
        logger.info(f"Restored org architecture from saved org '{name}'")
        try:
            await self._broadcast_org_info()
        except Exception:
            pass

    async def _handle_org_saved_list(self, ws: Any, data: dict) -> None:
        """Enumerate saved organization configs."""
        result = await self._ensure_office_services().org.saved_list()
        await ws.send_json({"type": "org_saved_list", "payload": result.payload})

    async def _handle_org_saved_save_as(self, ws: Any, data: dict) -> None:
        """Snapshot current engine config.org as a saved organization."""
        try:
            result = await self._ensure_office_services().org.saved_save_as(
                str(data.get("organization_name") or data.get("name") or "").strip(),
                overwrite=bool(data.get("overwrite", False)),
            )
            await ws.send_json({"type": "org_saved_save_as", "payload": result.payload})
        except ServiceError as exc:
            await self._ack_saved_err(ws, "org_saved_save_as", str(data.get("name") or ""), exc.message)

    async def _handle_org_saved_create(self, ws: Any, data: dict) -> None:
        """Create, save, and activate a new custom organization."""
        name = str(data.get("organization_name") or data.get("name") or "").strip()
        try:
            members = data.get("members")
            result = await self._ensure_office_services().org.saved_create(
                organization_name=name,
                members=members if isinstance(members, list) else [],
            )
            org_id = str(result.payload.get("organization_id") or result.payload.get("name") or "").strip()
            ok = await self._apply_mode_switch(
                "org",
                "custom",
                getattr(self, "_task_preferred_agent", "native"),
                org_id=org_id,
            )
            if not ok:
                await ws.send_json({"type": "org_saved_create", "payload": {
                    "ok": False,
                    "name": org_id or name,
                    "error": getattr(self, "_last_org_load_error", "") or "org_activation_failed",
                }})
                return
            await ws.send_json({"type": "org_saved_create", "payload": result.payload})
        except ServiceError as exc:
            await self._ack_saved_err(ws, "org_saved_create", name, exc.message)

    async def _handle_org_saved_load(self, ws: Any, data: dict) -> None:
        """Activate a saved org. Uses _apply_org_config so errors surface
        correctly (the previous delegation to _handle_org_config_import
        sent a spurious org_config_import response and unconditionally
        acked ok=True even on failure)."""
        name = str(data.get("organization_id") or data.get("name") or "")
        try:
            organization_id = validate_saved_org_id(name)
            path = org_config_path(Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config", organization_id)
        except ValueError as exc:
            return await self._ack_saved_err(ws, "org_saved_load", name, str(exc))
        if not path.exists():
            return await self._ack_saved_err(ws, "org_saved_load", name, "not_found")
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return await self._ack_saved_err(ws, "org_saved_load", name, f"read_failed: {exc}")
        ok, payload = await self._apply_org_config(raw, dry_run=False, allow_mode_transition=True)
        if ok:
            await self._set_active_saved_org_name(organization_id)
            await ws.send_json({"type": "org_saved_load", "payload": {
                "ok": True,
                "name": organization_id,
                "organization_id": organization_id,
                "filename": path.name,
                **payload,
            }})
        else:
            await ws.send_json({"type": "org_saved_load", "payload": {
                "ok": False, "name": name, **payload,
            }})

    async def _handle_org_saved_delete(self, ws: Any, data: dict) -> None:
        """Remove a saved-org file. Never touches the active config file."""
        name = str(data.get("organization_id") or data.get("name") or "")
        try:
            result = await self._ensure_office_services().org.saved_delete(name)
            await ws.send_json({"type": "org_saved_delete", "payload": result.payload})
        except ServiceError as exc:
            await self._ack_saved_err(ws, "org_saved_delete", name, exc.message)

    async def _handle_org_config_export(self, ws: Any, data: dict) -> None:
        """Build and return current org config as a YAML string from live engine state."""
        result = await self._ensure_office_services().org.export_config()
        await ws.send_json({"type": "org_config_export", "payload": {"yaml": result.payload.get("yaml", "")}})

    async def _apply_org_config(
        self,
        raw_yaml: str,
        *,
        dry_run: bool,
        allow_mode_transition: bool = False,
    ) -> tuple[bool, dict]:
        """Parse, validate and (optionally) apply an org architecture YAML string.

        Pure function with no WS I/O — callers format their own response.
        The apply path takes self._config_lock and broadcasts org_info on
        success; dry-run and error paths do neither.

        Returns (ok, payload_dict):
          Success: (True, {"dry_run": bool, "preview": {roles_added, roles_removed, employees_changed}})
          Error:   (False, {"error": str, "validation_errors": list[str]})
        """
        try:
            snapshot = parse_org_architecture_snapshot(raw_yaml)
            existing = self.engine.config
            validated_config = apply_org_architecture_snapshot(existing, snapshot)
            try:
                validate_saved_org_id(getattr(validated_config.org, "organization_id", ""))
            except ValueError:
                if "organization_id" in snapshot:
                    raise
                raw_name = str(
                    getattr(validated_config.org, "organization_name", "")
                    or getattr(validated_config.org, "company_name", "")
                    or "org"
                ).strip()
                config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
                organization_id = allocate_org_config_id(config_dir, raw_name)
                validated_config.org.organization_id = organization_id
                validated_config.org.organization_name = raw_name
                validated_config.org.organization_config_file = org_config_relative_path(organization_id)
            validated_config.org.company_profile = "custom"
            try:
                from opc.core.employee_registry import load_company_employees

                config_dir = Path(getattr(self.engine, "opc_home", None) or Path.cwd() / ".opc") / "config"
                validated_config.org.employees = load_company_employees(
                    Path(config_dir).parent,
                    validated_config.org.organization_id,
                    list(validated_config.org.employees),
                )
            except Exception:
                logger.opt(exception=True).debug("Failed to load employee registry for applied org config")
            roles_before = {r.id for r in existing.org.roles}
            roles_after = {r.id for r in validated_config.org.roles}
            employees_before = len(existing.org.employees)
            preview = {
                "roles_added": len(roles_after - roles_before),
                "roles_removed": len(roles_before - roles_after),
                "employees_changed": abs(
                    len(validated_config.org.employees) - employees_before
                ),
            }
            if dry_run:
                return True, {"dry_run": True, "preview": preview}
            validate_runnable_org_config(validated_config)
            current_mode = getattr(self, "_exec_mode", None)
            if not allow_mode_transition and current_mode is not None and str(current_mode or "").strip().lower() not in {"org", "custom"}:
                return False, {
                    "error": "Corporate organization is read-only. Select or create a saved custom org before editing.",
                    "code": "org_read_only",
                    "validation_errors": [],
                }
            async with self._config_lock:
                self._write_active_org_config(validated_config)
                self._rebind_engine_config(validated_config)
                self.engine.org_engine.reload_from_config()

            target_mode, target_profile = self._target_mode_for_profile(
                validated_config.org.company_profile
            )
            current_mode = getattr(self, "_exec_mode", None)
            current_profile = getattr(self, "_company_profile", "corporate")
            mode_changed = target_mode is not None and (
                target_mode != current_mode
                or (target_mode == "company" and target_profile != current_profile)
            )
            runtime_ready = all(
                hasattr(self, attr)
                for attr in ("agent_store", "chat_store", "event_adapter")
            )
            if mode_changed and runtime_ready:
                await self._apply_mode_switch(
                    target_mode,
                    target_profile or current_profile,
                    getattr(self, "_task_preferred_agent", "native"),
                    sync_config=False,
                )
            else:
                if hasattr(self, "agent_store"):
                    await self._prune_stale_agent_store_entries()
                    if getattr(self, "_exec_mode", "") in {"org", "custom"}:
                        await self._ensure_custom_role_agents()
                        if runtime_ready:
                            await self._broadcast_snapshot()
            await self._broadcast_org_info()
            return True, {"dry_run": False, "preview": preview}
        except Exception as exc:
            validation_errors: list[str] = []
            try:
                from pydantic import ValidationError
                if isinstance(exc, ValidationError):
                    validation_errors = [str(e) for e in exc.errors()]
            except ImportError:
                pass
            return False, {"error": str(exc), "validation_errors": validation_errors}

    async def _handle_org_config_import(self, ws: Any, data: dict) -> None:
        """WS endpoint: user-initiated YAML import. Thin adapter around
        _apply_org_config. Saved-org Load uses _handle_org_saved_load
        which also calls _apply_org_config directly (not this handler),
        so the two flows don't cross-pollute their WS response types."""
        raw_yaml: str = data.get("yaml", "")
        dry_run: bool = data.get("dry_run", True)
        ok, payload = await self._apply_org_config(raw_yaml, dry_run=dry_run)
        await ws.send_json({"type": "org_config_import", "payload": {"ok": ok, **payload}})

    # ------------------------------------------------------------------
    # OPC Market handlers
    # ------------------------------------------------------------------

    async def _handle_market_browse(self, ws: Any, data: dict) -> None:
        """Return all available architecture presets for browsing."""
        result = await self._ensure_office_services().market.browse()
        await ws.send_json({"type": "market_browse", "payload": result.payload})

    async def _handle_market_preview(self, ws: Any, data: dict) -> None:
        """Return full details of a single architecture preset."""
        try:
            result = await self._ensure_office_services().market.preview(data.get("preset_id", ""))
            await ws.send_json({"type": "market_preview", "payload": result.payload})
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="market_preview")

    async def _handle_market_apply_preset(self, ws: Any, data: dict) -> None:
        """Apply a built-in architecture preset to the current org."""
        try:
            result = await self._ensure_office_services().market.apply_preset(
                preset_id=data.get("preset_id", ""),
                strategy=data.get("strategy", "namespace"),
            )
            await self._publish_service_result(result)
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="market_apply_preset")
        except Exception as exc:
            logger.warning(f"Market apply preset failed: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_market_list_installed(self, ws: Any, data: dict) -> None:
        """Return list of installed market packages."""
        result = await self._ensure_office_services().market.list_installed()
        await ws.send_json({"type": "market_list_installed", "payload": result.payload})

    async def _handle_market_export(self, ws: Any, data: dict) -> None:
        """Export current org as an .opcpkg package."""
        try:
            result = await self._ensure_office_services().market.export(
                package_id=data.get("package_id", ""),
                name=data.get("name", ""),
                description=data.get("description", ""),
                version=data.get("version", "1.0.0"),
                output_dir=str(Path(getattr(self.engine, "opc_home", Path.cwd() / ".opc")) / "exports"),
            )
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="market_export")
        except Exception as exc:
            logger.warning(f"Market export failed: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_market_install(self, ws: Any, data: dict) -> None:
        """Install an .opcpkg package from a local path."""
        try:
            result = await self._ensure_office_services().market.install(
                path=data.get("path", ""),
                strategy=data.get("strategy", "namespace"),
            )
            await self._publish_service_result(result)
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="market_install")
        except Exception as exc:
            logger.warning(f"Market install failed: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_market_uninstall(self, ws: Any, data: dict) -> None:
        """Uninstall a market package and clean related role/agent state."""
        try:
            result = await self._ensure_office_services().market.uninstall(data.get("package_id", ""))
            await self._publish_service_result(result)
            await self._send_service_ack(ws, result)
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="market_uninstall")
        except Exception as exc:
            logger.warning(f"Market uninstall failed: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    # ------------------------------------------------------------------
    # Org editing handlers (custom mode)
    # ------------------------------------------------------------------

    async def _handle_bulk_add_roles(self, ws: Any, data: dict) -> None:
        """Add multiple roles atomically in a single transaction."""
        try:
            roles_data = data.get("roles", [])
            if not roles_data or not isinstance(roles_data, list):
                await self._send_ack(ws, ok=False, error="roles list required")
                return
            result = await self._ensure_office_services().org.bulk_add_roles(roles_data)
            await self._publish_service_result(result)
            await self._send_ack(
                ws,
                ok=True,
                action="roles_added",
                role_ids=result.payload.get("role_ids", []),
                count=result.payload.get("count", 0),
            )
            await self._broadcast_org_info()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="bulk_add_roles")
        except Exception as exc:
            logger.warning(f"Failed to bulk add roles: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_add_role(self, ws: Any, data: dict) -> None:
        """Add a new role to the organisation."""
        try:
            result = await self._ensure_office_services().org.add_role(data)
            await self._publish_service_result(result)
            role = dict(result.payload.get("role", {}) or {})
            await self._send_ack(ws, ok=True, action="role_added", role_id=role.get("id") or role.get("role_id"))
            await self._broadcast_org_info()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="add_role")
        except Exception as exc:
            logger.warning(f"Failed to add role: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_update_role(self, ws: Any, data: dict) -> None:
        """Update a role's relationships and runtime fields."""
        try:
            role_id = str(data.get("role_id", "") or "").strip()
            if not role_id:
                await self._send_ack(ws, ok=False, error="role_id required")
                return
            result = await self._ensure_office_services().org.update_role(role_id, data)
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, action="role_updated", role_id=role_id)
            await self._broadcast_org_info()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="update_role")
        except Exception as exc:
            logger.warning(f"Failed to update role: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_update_org_strategy(self, ws: Any, data: dict) -> None:
        try:
            result = await self._ensure_office_services().org.update_org_strategy(
                final_decider_role_id=str(data.get("final_decider_role_id", "") or "").strip() or None,
            )
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, action="org_strategy_updated")
            await self._broadcast_org_info()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="update_org_strategy")
        except Exception as exc:
            logger.warning(f"Failed to update org strategy: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_delete_role(self, ws: Any, data: dict) -> None:
        """Delete a role and its employees from the organisation."""
        try:
            role_id = str(data.get("role_id", "") or "").strip()
            if not role_id:
                await self._send_ack(ws, ok=False, error="role_id required")
                return
            result = await self._ensure_office_services().org.delete_role(role_id)
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, action="role_deleted", role_id=role_id)
            await self._broadcast_org_info()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="delete_role")
        except Exception as exc:
            logger.warning(f"Failed to delete role: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_update_runtime_policy(self, ws: Any, data: dict) -> None:
        """Update the runtime policy for custom mode."""
        try:
            result = await self._ensure_office_services().org.update_runtime_policy(data.get("policy", {}) or {})
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, action="runtime_policy_updated")
            await self._broadcast_org_info()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="update_runtime_policy")
        except Exception as exc:
            logger.warning(f"Failed to update runtime policy: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    async def _handle_reset_architecture(self, ws: Any, data: dict) -> None:
        """Clear all custom roles, employees, runtime, and installed packages."""
        try:
            result = await self._ensure_office_services().org.reset_architecture()
            await self._publish_service_result(result)
            await self._send_ack(ws, ok=True, action="architecture_reset")
            await self._broadcast_org_info()
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="reset_architecture")
        except Exception as exc:
            logger.warning(f"Failed to reset architecture: {exc}")
            await self._send_ack(ws, ok=False, error=str(exc))

    # Handler routing table
    _HANDLERS: dict[str, Any] = {
        "ping":                _handle_ping,
        "collab_sync":         _handle_collab_sync,
        "project_index":       _handle_project_index,
        "kanban_create_board": _handle_kanban_create_board,
        "kanban_create_task":  _handle_kanban_create_task,
        "kanban_update_task":  _handle_kanban_update_task,
        "kanban_move_task":    _handle_kanban_move_task,
        "kanban_delete_task":  _handle_kanban_delete_task,
        "kanban_delete_board": _handle_kanban_delete_board,
        "kanban_assign":       _handle_kanban_assign,
        "kanban_status":       _handle_kanban_status,
        "create_agent":        _handle_create_agent,
        "delete_agent":        _handle_delete_agent,
        "list_agents":         _handle_list_agents,
        "move_agent":          _handle_move_agent,
        "set_execution_mode":  _handle_set_mode,
        "run_task":            _handle_run_task,
        "cross_office_collab": _handle_cross_office,
        "agent_workload":      _handle_agent_workload,
        "kanban_switch_view":  _handle_kanban_switch_view,
        "get_agent_detail":    _handle_get_agent_detail,
        # Session handlers
        "create_session":      _handle_create_session,
        "session_update_config": _handle_session_update_config,
        "session_detail":      _handle_session_detail,
        "session_send":        _handle_session_send,
        "session_stop":        _handle_session_stop,
        "session_resume":      _handle_session_resume,
        "session_delete":      _handle_session_delete,
        "session_complete":    _handle_session_complete,
        "session_update_title": _handle_session_update_title,
        # Secretary handler
        "secretary_send":      _handle_secretary_send,
        # Project management
        "list_projects":       _handle_list_projects,
        "create_project":      _handle_create_project,
        "delete_project":      _handle_delete_project,
        "switch_project":      _handle_switch_project,
        # Org info
        "org_info":            _handle_org_info,
        # Phase 4: Talent Market, Employee Detail, Reorg
        "talent_import":       _handle_talent_import,
        "talent_list":         _handle_talent_list,
        "talent_scan_local":   _handle_talent_scan_local,
        "talent_import_selected": _handle_talent_import_selected,
        "talent_hire":         _handle_talent_hire,
        "import_employee_as_agent": _handle_import_employee_as_agent,
        "employee_detail":     _handle_employee_detail,
        "reorg_list":          _handle_reorg_list,
        "reorg_decide":        _handle_reorg_decide,
        # OPC Market
        "market_browse":       _handle_market_browse,
        "market_preview":      _handle_market_preview,
        "market_apply_preset": _handle_market_apply_preset,
        "market_list_installed": _handle_market_list_installed,
        "market_export":       _handle_market_export,
        "market_install":      _handle_market_install,
        "market_uninstall":    _handle_market_uninstall,
        # Org config import/export
        "org_config_export":   _handle_org_config_export,
        "org_config_import":   _handle_org_config_import,
        # Saved org architectures (named snapshots)
        "org_saved_list":      _handle_org_saved_list,
        "org_saved_save_as":   _handle_org_saved_save_as,
        "org_saved_create":    _handle_org_saved_create,
        "org_saved_load":      _handle_org_saved_load,
        "org_saved_delete":    _handle_org_saved_delete,
        # Org editing (custom mode)
        "bulk_add_roles":      _handle_bulk_add_roles,
        "add_role":            _handle_add_role,
        "update_role":         _handle_update_role,
        "update_org_strategy": _handle_update_org_strategy,
        "delete_role":         _handle_delete_role,
        "update_runtime_policy": _handle_update_runtime_policy,
        "reset_architecture":  _handle_reset_architecture,
    }

    async def _handle_comms_state(self, ws: Any, data: dict) -> None:
        """Return a snapshot of the file-based comms layout for a session."""
        if self._shutting_down:
            return
        try:
            _engine, project_id = await self._engine_for_request(data)
            result = await self._ensure_office_services().comms.state(
                project_id=project_id,
                task_id=str(data.get("task_id", "") or ""),
                session_id=str(data.get("session_id", "") or ""),
            )
            await ws.send_json({"type": "comms_state", "payload": result.payload})
        except ServiceError as exc:
            await ws.send_json({"type": "comms_state", "payload": {"available": False, "reason": exc.message, **exc.payload}})
        except Exception as exc:
            await ws.send_json({"type": "comms_state", "payload": {"available": False, "reason": str(exc)}})

    async def _handle_comms_read_message(self, ws: Any, data: dict) -> None:
        """Read the body of a single comms message file for the UI viewer."""
        if self._shutting_down:
            return
        try:
            result = await self._ensure_office_services().comms.read(
                project_id=self._request_project_id(data),
                task_id=str(data.get("task_id", "") or ""),
                path=str(data.get("path", "") or ""),
            )
            await ws.send_json({"type": "comms_message", "payload": result.payload})
        except ServiceError as exc:
            await self._send_service_error(ws, exc, action="comms_read_message")

    # Register handlers defined after _HANDLERS class-level dict
    _HANDLERS["comms_state"] = _handle_comms_state
    _HANDLERS["comms_read_message"] = _handle_comms_read_message
