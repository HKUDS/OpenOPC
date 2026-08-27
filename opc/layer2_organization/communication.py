"""Inter-agent communication protocol — DM, broadcast, meeting room, async annotation."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from loguru import logger

from opc.core.events import EventBus
from opc.core.models import (
    AgentEndpointType,
    AgentMessage,
    CommsSemanticType,
    CommsState,
    CommsTransportKind,
    MeetingRoom,
    MeetingStatus,
    MessageStatus,
    MessageUrgency,
    OPCEvent,
    SessionMessageRecord,
    SessionPartRecord,
    Task,
    TaskStatus,
    WorkItemDecisionRecord,
)
from opc.core.worker_envelope import (
    classify_worker_message,
    normalize_worker_envelope_metadata,
    worker_message_is_actionable,
)
from opc.layer2_organization.work_item_links import linked_work_item_id_for_task
from opc.database.store import OPCStore
from opc.layer2_organization.collaboration_service import (
    CollaborationContext,
    CollaborationService,
)
from opc.layer2_organization.collaboration_policy import (
    effective_contact_roles,
)
from opc.layer2_organization.work_item_runtime import is_work_item_runtime_metadata
from opc.layer2_organization.work_item_identity import (
    projection_id_for_task,
    turn_type_for_task,
    work_item_identity_payload_for_task,
)
from opc.llm.provider import LLMProvider
from opc.llm.retry import LLMRetryError, call_llm_json_with_retry


class CommunicationManager:
    """Manages direct messages, broadcasts, meetings, annotations, and peer waits."""

    _MAILBOX_METADATA_KEYS: tuple[str, ...] = (
        "work_item_id",
        "source_work_item_id",
        "target_work_item_id",
        "parent_work_item_id",
        "source_message_id",
        "manager_role_id",
        "manager_seat_id",
        "origin_team_instance_id",
        "target_team_instance_id",
        "target_role_id",
        "target_seat_id",
        "source_role_id",
        "source_seat_id",
        "parent_board_scope",
        "upstream_visibility",
        "release_policy",
        "release_on_semantic_type",
        "action_hint",
    )
    _ENVELOPE_METADATA_KEYS: tuple[str, ...] = (
        "message_class",
        "protocol_type",
        "notification_kind",
        "actionable",
        "worker_id",
        "origin_task_id",
        "origin_projection_id",
        "origin_session_id",
    )

    def __init__(
        self,
        store: OPCStore,
        event_bus: EventBus,
        llm: LLMProvider | None = None,
        org_engine: Any | None = None,
        meeting_turn_runner: Callable[[MeetingRoom, str, dict[str, Any]], Awaitable[str]] | None = None,
    ) -> None:
        self.store = store
        self.event_bus = event_bus
        self.llm = llm
        self.org_engine = org_engine
        self.meeting_turn_runner = meeting_turn_runner
        self._message_queues: dict[str, asyncio.Queue[AgentMessage]] = {}
        self._meetings: dict[str, MeetingRoom] = {}
        self.task_adjustment_suggester: Any | None = None
        # Kanban-push hook: runtime state transitions (work item moves,
        # review verdict application, dependency releases) call this to
        # push a fresh UI snapshot mid-turn instead of waiting for the
        # turn boundary. CompanyWorkItemExecutor wires it up at
        # construction time.
        self.on_kanban_changed: Callable[[], Awaitable[None]] | None = None
        # Work-items-created dispatch-wake hook: fired synchronously
        # after `delegate_work` (or the runtime's auto-rework path)
        # persists new TODO items so the company-mode main loop can
        # claim+spawn them without waiting for the parent turn to drain.
        # Set by CompanyWorkItemExecutor.
        self.on_work_items_created: Callable[[], None] | None = None

    def _collaboration_service(self) -> CollaborationService:
        return CollaborationService(self)

    def set_meeting_turn_runner(
        self,
        runner: Callable[[MeetingRoom, str, dict[str, Any]], Awaitable[str]] | None,
    ) -> None:
        self.meeting_turn_runner = runner

    def _get_queue(self, agent_id: str) -> asyncio.Queue[AgentMessage]:
        return self._message_queues.setdefault(agent_id, asyncio.Queue())

    async def rehydrate_queues(self) -> int:
        """Reload unprocessed messages from the store into in-memory queues.
        Call once during engine startup to recover from process restarts."""
        if not self.store:
            return 0
        messages = await self.store.get_unprocessed_messages()
        count = 0
        for msg in messages:
            for recipient in msg.to_agents:
                self._get_queue(recipient).put_nowait(msg)
                count += 1
        if count:
            logger.info("Rehydrated {} pending messages into agent queues", count)
        return count

    def _validate_recipients(self, sender: str, recipients: list[str], task: Task | None = None) -> None:
        if not self.org_engine or not sender:
            return
        allowed = set(effective_contact_roles(sender, task=task, org_engine=self.org_engine))
        if not allowed:
            return
        invalid: list[str] = []
        for recipient in recipients:
            if recipient in allowed or recipient == sender:
                continue
            employee = self.org_engine.get_employee(recipient)
            if employee and employee.role_id in allowed:
                continue
            role_agent = self.org_engine.get_agent(recipient)
            if role_agent:
                continue
            invalid.append(recipient)
        if invalid:
            raise ValueError(f"Role `{sender}` cannot message recipient(s): {', '.join(invalid)}")

    def _serialize_message(self, message: AgentMessage) -> dict[str, Any]:
        raw_metadata = dict(message.metadata or {})
        envelope_meta = normalize_worker_envelope_metadata(
            raw_metadata,
            msg_type=message.msg_type,
            semantic_type=message.semantic_type.value,
            transport_kind=message.transport_kind.value,
            from_agent=message.from_agent,
            reply_needed=message.reply_needed,
            task_id=str(message.task_id or ""),
            projection_id=str(dict(message.refs or {}).get("projection_id", "") or ""),
            session_id=str(dict(message.refs or {}).get("session_id", "") or ""),
        )
        return {
            "msg_id": message.msg_id,
            "msg_type": message.msg_type,
            "from_agent": message.from_agent,
            "to_agents": list(message.to_agents),
            "subject": message.subject,
            "body": message.body,
            "context_ref": message.context_ref,
            "urgency": message.urgency.value,
            "reply_needed": message.reply_needed,
            "requires_ack": message.requires_ack,
            "timeout_action": message.timeout_action,
            "reply_to_msg_id": message.reply_to_msg_id,
            "task_id": message.task_id,
            "status": message.status.value,
            "timestamp": message.timestamp.isoformat(),
            "processed_at": message.processed_at.isoformat() if message.processed_at else None,
            "transport_kind": message.transport_kind.value,
            "semantic_type": message.semantic_type.value,
            "comms_state": message.comms_state.value,
            "correlation_id": message.correlation_id,
            "refs": dict(message.refs),
            "message_class": envelope_meta.get("message_class"),
            "protocol_type": envelope_meta.get("protocol_type"),
            "notification_kind": envelope_meta.get("notification_kind"),
            "actionable": bool(envelope_meta.get("actionable", True)),
            "worker_id": envelope_meta.get("worker_id"),
            "origin_task_id": envelope_meta.get("origin_task_id"),
            "origin_projection_id": envelope_meta.get("origin_projection_id"),
            "origin_session_id": envelope_meta.get("origin_session_id"),
            **{
                key: raw_metadata.get(key)
                for key in self._MAILBOX_METADATA_KEYS
                if raw_metadata.get(key) not in (None, "", [])
            },
            "metadata": {
                **envelope_meta,
                **{
                    key: raw_metadata.get(key)
                    for key in self._MAILBOX_METADATA_KEYS
                    if raw_metadata.get(key) not in (None, "", [])
                },
            },
        }

    @staticmethod
    def _enum_value(value: Any) -> str:
        return str(getattr(value, "value", value) or "").strip()

    @staticmethod
    def _infer_endpoint_type(agent_id: str, metadata: dict[str, Any] | None = None) -> AgentEndpointType:
        raw = str(agent_id or "").strip()
        meta = dict(metadata or {})
        explicit = str(
            meta.get("endpoint_type")
            or meta.get("from_endpoint_type")
            or meta.get("to_endpoint_type")
            or ""
        ).strip().lower()
        if explicit in {item.value for item in AgentEndpointType}:
            return AgentEndpointType(explicit)
        if raw.startswith("na_") or raw.startswith("subagent::"):
            return AgentEndpointType.NATIVE_SUBAGENT
        if raw.startswith("external::") or bool(meta.get("external_agent", False)):
            return AgentEndpointType.EXTERNAL_AGENT
        return AgentEndpointType.COMPANY_ROLE

    @staticmethod
    def _coerce_semantic_type(value: Any, *, fallback: CommsSemanticType = CommsSemanticType.WORK_UPDATE) -> CommsSemanticType:
        raw = CommunicationManager._enum_value(value).lower()
        for item in CommsSemanticType:
            if item.value == raw:
                return item
        return fallback

    @staticmethod
    def _coerce_transport_kind(value: Any, *, fallback: CommsTransportKind = CommsTransportKind.DM) -> CommsTransportKind:
        raw = CommunicationManager._enum_value(value).lower()
        for item in CommsTransportKind:
            if item.value == raw:
                return item
        return fallback

    @staticmethod
    def _coerce_comms_state(value: Any, *, fallback: CommsState = CommsState.OPEN) -> CommsState:
        raw = CommunicationManager._enum_value(value).lower()
        for item in CommsState:
            if item.value == raw:
                return item
        return fallback

    def _manager_mailroom_target(self, role_id: str, task: Task | None = None) -> str:
        role = str(role_id or "").strip()
        if not role:
            return "ceo"
        if self.org_engine and hasattr(self.org_engine, "get_agent"):
            agent = self.org_engine.get_agent(role)
            manager_role = str(getattr(agent, "reports_to", "") or "").strip()
            if manager_role and manager_role != role:
                return manager_role
        if task is not None:
            session_state = dict(task.metadata.get("member_session_state", {}) or task.context_snapshot.get("member_session", {}) or {})
            manager_role = str(session_state.get("manager_role_id", "") or "").strip()
            if manager_role and manager_role != role:
                return manager_role
        if self.org_engine and hasattr(self.org_engine, "get_coordinator"):
            coordinator = self.org_engine.get_coordinator()
            coordinator_role = str(getattr(coordinator, "role_id", "") or "").strip()
            if coordinator_role and coordinator_role != role:
                return coordinator_role
        return "ceo"

    def _recipient_seat_id_for_role(self, role_id: str, task: Task | None = None) -> str:
        role = str(role_id or "").strip()
        if not role or task is None:
            return ""
        metadata = dict(getattr(task, "metadata", {}) or {})
        for role_key, seat_key in (
            ("review_owner_role_id", "review_owner_seat_id"),
            ("manager_role_id", "manager_seat_id"),
        ):
            if str(metadata.get(role_key, "") or "").strip() == role:
                seat_id = str(metadata.get(seat_key, "") or "").strip()
                if seat_id:
                    return seat_id
        context_snapshot = dict(getattr(task, "context_snapshot", {}) or {})
        session_state = dict(
            metadata.get("member_session_state", {})
            or context_snapshot.get("member_session", {})
            or {}
        )
        if str(session_state.get("manager_role_id", "") or "").strip() == role:
            seat_id = str(session_state.get("manager_seat_id", "") or "").strip()
            if seat_id:
                return seat_id
        runtime_topology = dict(metadata.get("runtime_topology", {}) or {})
        from_seat_id = str(metadata.get("delegation_seat_id", "") or "").strip()
        if self.org_engine and hasattr(self.org_engine, "resolve_runtime_target_seat"):
            try:
                resolved = self.org_engine.resolve_runtime_target_seat(
                    runtime_topology,
                    from_seat_id=from_seat_id,
                    target_role_id=role,
                )
            except Exception:
                resolved = None
            if isinstance(resolved, dict):
                seat_id = str(resolved.get("seat_id", "") or "").strip()
                if seat_id:
                    return seat_id
        for seat in list(runtime_topology.get("seats", []) or []):
            if str(seat.get("role_id", "") or "").strip() == role:
                seat_id = str(seat.get("seat_id", "") or "").strip()
                if seat_id:
                    return seat_id
        if role == str(task.assigned_to or metadata.get("work_item_role_id", "") or "").strip():
            return from_seat_id
        return ""

    def _canonicalize_message(self, message: AgentMessage, task: Task | None = None) -> AgentMessage:
        message.metadata = dict(message.metadata or {})
        refs = {
            **dict(message.metadata.get("refs", {}) or {}),
            **dict(message.refs or {}),
        }
        if task is not None:
            refs.setdefault("task_id", str(task.id or "").strip())
            refs.setdefault("projection_id", projection_id_for_task(task))
            refs.setdefault("session_id", str(task.session_id or task.parent_session_id or "").strip())
        if message.task_id:
            refs.setdefault("task_id", str(message.task_id).strip())
        if message.reply_to_msg_id:
            refs.setdefault("reply_to_msg_id", str(message.reply_to_msg_id).strip())
        semantic_fallback = (
            CommsSemanticType.BLOCKED_ON_DECISION
            if message.reply_needed or message.msg_type == "decision_needed"
            else CommsSemanticType.WORK_UPDATE
        )
        message.transport_kind = self._coerce_transport_kind(
            message.transport_kind,
            fallback=CommsTransportKind.BROADCAST if len(message.to_agents) > 1 else CommsTransportKind.DM,
        )
        message.semantic_type = self._coerce_semantic_type(
            message.semantic_type or message.metadata.get("semantic_type"),
            fallback=semantic_fallback,
        )
        message.comms_state = self._coerce_comms_state(
            message.comms_state or message.metadata.get("comms_state"),
            fallback=CommsState.OPEN,
        )
        if not message.correlation_id:
            message.correlation_id = str(message.metadata.get("correlation_id", "") or message.msg_id).strip()
        message.refs = refs
        envelope_meta = normalize_worker_envelope_metadata(
            message.metadata,
            msg_type=message.msg_type,
            semantic_type=message.semantic_type.value,
            transport_kind=message.transport_kind.value,
            from_agent=message.from_agent,
            reply_needed=message.reply_needed,
            task_id=refs.get("task_id", ""),
            projection_id=refs.get("projection_id", ""),
            session_id=refs.get("session_id", ""),
            worker_id=str(message.metadata.get("worker_id", "") or message.from_agent).strip(),
        )
        message.metadata.update(
            {
                **envelope_meta,
                "transport_kind": message.transport_kind.value,
                "semantic_type": message.semantic_type.value,
                "comms_state": message.comms_state.value,
                "correlation_id": message.correlation_id,
                "refs": dict(refs),
                "from_endpoint_type": self._infer_endpoint_type(message.from_agent, message.metadata).value,
                "to_endpoint_types": [
                    self._infer_endpoint_type(target, message.metadata).value
                    for target in list(message.to_agents)
                ],
            }
        )
        return message

    def _message_frontmatter(self, message: AgentMessage, task: Task | None = None) -> dict[str, Any]:
        normalized = self._canonicalize_message(message, task=task)
        refs = dict(normalized.refs or {})
        frontmatter = {
            "transport_kind": normalized.transport_kind.value,
            "semantic_type": normalized.semantic_type.value,
            "comms_state": normalized.comms_state.value,
            "correlation_id": normalized.correlation_id,
            "from_endpoint_type": self._infer_endpoint_type(normalized.from_agent, normalized.metadata).value,
            "to_endpoint_types": [
                self._infer_endpoint_type(target, normalized.metadata).value
                for target in list(normalized.to_agents)
            ],
            "refs": refs,
            "kind": normalized.semantic_type.value,
            "task_id": refs.get("task_id", ""),
            "projection_id": refs.get("projection_id", ""),
        }
        metadata = dict(normalized.metadata or {})
        for key in (*self._ENVELOPE_METADATA_KEYS, *self._MAILBOX_METADATA_KEYS):
            value = metadata.get(key)
            if value not in (None, "", []):
                frontmatter[key] = value
        return frontmatter

    @staticmethod
    def _is_high_risk_work_item(task: Task) -> bool:
        gate = dict(task.metadata.get("work_item_gate", {}) or {})
        if bool(gate.get("requires_human", False)):
            return True
        if bool(task.metadata.get("work_item_verification_required", False)):
            return True
        turn_type = turn_type_for_task(task, fallback="")
        return turn_type in {"execute", "review"}

    async def _record_non_live_collaboration(
        self,
        task: Task,
        *,
        origin: str,
        summary: str,
    ) -> None:
        task.context_snapshot = dict(task.context_snapshot)
        provenance = list(task.context_snapshot.get("collaboration_provenance", []))
        provenance.append(
            {
                "origin": origin,
                "summary": summary,
                "recorded_at": datetime.now().isoformat(),
            }
        )
        task.context_snapshot["collaboration_provenance"] = provenance[-8:]
        task.context_snapshot["latest_collaboration_origin"] = origin
        if self._is_high_risk_work_item(task):
            warnings = list(task.metadata.get("progress_log", []))
            note = f"Non-live collaboration artifact observed: {origin}. {summary}"
            if note not in warnings:
                warnings.append(note)
                task.metadata["progress_log"] = warnings

    async def send_manager_notification(
        self,
        *,
        from_agent: str,
        task: Task | None,
        semantic_type: CommsSemanticType,
        subject: str,
        body: str,
        metadata: dict[str, Any] | None = None,
        reply_needed: bool = False,
        requires_ack: bool = False,
    ) -> AgentMessage | None:
        manager_role = self._manager_mailroom_target(from_agent, task=task)
        if not manager_role or manager_role == from_agent:
            return None
        msg_type = (
            "decision_needed"
            if semantic_type == CommsSemanticType.BLOCKED_ON_DECISION
            else "inform"
        )
        work_item_id = linked_work_item_id_for_task(task)
        metadata_payload = dict(metadata or {})
        target_seat_id = (
            self._recipient_seat_id_for_role(manager_role, task)
            or str(metadata_payload.get("target_seat_id", "") or "").strip()
            or str((getattr(task, "metadata", {}) or {}).get("manager_seat_id", "") or "").strip()
        )
        message = AgentMessage(
            msg_type=msg_type,
            from_agent=from_agent,
            to_agents=[manager_role],
            subject=subject,
            body=body,
            context_ref=task.id if task else None,
            urgency=MessageUrgency.BLOCKING if reply_needed else MessageUrgency.NORMAL,
            reply_needed=reply_needed,
            requires_ack=requires_ack,
            task_id=task.id if task else None,
            transport_kind=CommsTransportKind.SYSTEM,
            semantic_type=semantic_type,
            metadata={
                **metadata_payload,
                "work_item_id": work_item_id,
                "source_work_item_id": work_item_id,
                "target_work_item_id": str(
                    dict(metadata or {}).get("target_work_item_id")
                    or dict(metadata or {}).get("work_item_id")
                    or work_item_id
                    or ""
                ).strip(),
                "parent_work_item_id": str(
                    (getattr(task, "metadata", {}) or {}).get("delegation_parent_work_item_id", "")
                    or dict(
                        (getattr(task, "metadata", {}) or {}).get("derived_work_item_projection", {})
                        or {}
                    ).get("parent_work_item_id", "")
                    or work_item_id
                    or ""
                ).strip(),
                "manager_role_id": manager_role,
                "from_seat_id": str((getattr(task, "metadata", {}) or {}).get("delegation_seat_id", "") or "").strip(),
                "source_role_id": from_agent,
                "source_seat_id": str((getattr(task, "metadata", {}) or {}).get("delegation_seat_id", "") or "").strip(),
                "target_role_id": manager_role,
                "target_seat_id": target_seat_id,
                "team_id": str((getattr(task, "metadata", {}) or {}).get("delegation_team_id", "") or "").strip(),
            },
        )
        return await self.send_dm(message, task=task)

    @staticmethod
    def _task_workspace_root(task: Task | None) -> str:
        if task is None:
            return ""
        metadata = dict(task.metadata or {})
        return (
            str(metadata.get("workspace_root", "") or "").strip()
            or str(metadata.get("comms_workspace_root", "") or "").strip()
            or str(metadata.get("output_root", "") or "").strip()
            or str(metadata.get("target_output_dir", "") or "").strip()
        )

    @staticmethod
    def _task_output_root(task: Task | None) -> str:
        if task is None:
            return ""
        metadata = dict(task.metadata or {})
        return (
            str(metadata.get("output_root", "") or "").strip()
            or str(metadata.get("target_output_dir", "") or "").strip()
            or CommunicationManager._task_workspace_root(task)
        )

    @staticmethod
    def _task_comms_root(task: Task | None) -> str:
        if task is None:
            return ""
        metadata = dict(task.metadata or {})
        explicit = str(metadata.get("comms_root", "") or "").strip()
        if explicit:
            return explicit
        workspace_root = CommunicationManager._task_workspace_root(task)
        if not workspace_root:
            return ""
        try:
            return str(Path(workspace_root).expanduser().resolve() / ".opc-comms")
        except Exception:
            return ""

    def _task_comms_layout(self, task: Task | None):
        if task is None:
            return None
        workspace_root = self._task_workspace_root(task)
        if not workspace_root:
            return None
        try:
            from opc.layer2_organization import comms as _comms
        except Exception:
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

    async def _record_session_notice(
        self,
        task: Task,
        *,
        text: str,
        metadata: dict[str, Any] | None = None,
        agent_id: str | None = None,
    ) -> None:
        if not task.session_id:
            return
        message = SessionMessageRecord(
            session_id=task.session_id,
            role="assistant",
            task_id=task.id,
            agent_id=agent_id,
            metadata=dict(metadata or {}),
        )
        await self.store.save_session_message(message)
        await self.store.save_session_part(
            SessionPartRecord(
                message_id=message.message_id,
                session_id=task.session_id,
                part_type="text",
                payload={"text": text},
            )
        )

    async def _record_comms_failure(
        self,
        task: Task | None,
        *,
        operation: str,
        from_role: str,
        to_role: str,
        reason: str,
        attempted_path: str = "",
        attempted_command: str = "",
    ) -> dict[str, Any]:
        payload = {
            "operation": str(operation or "").strip(),
            "from_role": str(from_role or "").strip(),
            "to_role": str(to_role or "").strip(),
            "reason": str(reason or "").strip() or "unknown_error",
            "attempted_path": str(attempted_path or "").strip(),
            "attempted_command": str(attempted_command or "").strip(),
            "recorded_at": datetime.now().isoformat(),
            "can_retry": True,
        }
        if task is None:
            return payload
        task.metadata = dict(task.metadata or {})
        task.context_snapshot = dict(task.context_snapshot or {})
        failures = list(task.context_snapshot.get("comms_failures", []) or [])
        payload["attempt_count"] = len(failures) + 1
        failures.append(dict(payload))
        task.context_snapshot["comms_failures"] = failures[-12:]
        task.context_snapshot["latest_comms_failure"] = dict(payload)
        task.metadata["comms_health"] = "degraded"
        progress = list(task.metadata.get("progress_log", []) or [])
        progress.append(
            f"Comms failure [{payload['operation']}] {payload['from_role']} -> {payload['to_role']}: {payload['reason']}"
        )
        task.metadata["progress_log"] = progress[-40:]
        if task.status in {TaskStatus.DONE, TaskStatus.IDLE, TaskStatus.AWAITING_PEER}:
            task.status = TaskStatus.PENDING
        await self.store.save_task(task)
        notice = (
            "通信发送失败，需要你下一轮自行处理：\n"
            f"- operation: {payload['operation']}\n"
            f"- from: {payload['from_role']}\n"
            f"- to: {payload['to_role']}\n"
            f"- reason: {payload['reason']}"
        )
        if payload["attempted_path"]:
            notice += f"\n- path: {payload['attempted_path']}"
        if payload["attempted_command"]:
            notice += f"\n- command: {payload['attempted_command']}"
        notice += "\n请结合错误信息决定是否重发、改写、改走 meeting，或先继续可并行部分。"
        await self._record_session_notice(
            task,
            text=notice,
            metadata={
                "kind": "comms_failure",
                "comms_failure": dict(payload),
            },
            agent_id=payload["from_role"] or None,
        )
        await self.event_bus.publish(
            OPCEvent(
                event_type="comms_delivery_failed",
                payload={
                    "task_id": task.id,
                    "session_id": task.session_id,
                    **payload,
                },
            )
        )
        return payload

    @staticmethod
    def _status_from_message_bucket(path: Path) -> MessageStatus:
        parent = path.parent.name
        if parent == "seen":
            return MessageStatus.READ
        return MessageStatus.DELIVERED

    async def _project_comms_messages(
        self,
        layout: Any,
        *,
        role_id: str,
        task: Task | None,
        unread_only: bool,
        limit: int,
        mark_read: bool,
    ) -> list[dict[str, Any]]:
        from opc.layer2_organization import comms as _comms

        headers = (
            _comms.list_unread(layout, role_id, limit=limit)
            if unread_only
            else _comms.list_role_messages(
                layout,
                role_id,
                include_new=True,
                include_seen=True,
                include_outbox=False,
            )[-limit:]
        )
        serialized: list[dict[str, Any]] = []
        consumed_paths: list[Path] = []
        active_seat_id = str((getattr(task, "metadata", {}) or {}).get("delegation_seat_id", "") or "").strip()
        for header in headers:
            full_header, body = _comms.read_message(header.path)
            if full_header is None:
                continue
            frontmatter = dict(full_header.raw_frontmatter or {})
            target_seat_id = str(frontmatter.get("target_seat_id", "") or "").strip()
            if active_seat_id and target_seat_id and target_seat_id != active_seat_id:
                continue
            refs = dict(frontmatter.get("refs", {}) or {})
            transport_kind = self._coerce_transport_kind(frontmatter.get("transport_kind"))
            semantic_type = self._coerce_semantic_type(frontmatter.get("semantic_type") or frontmatter.get("kind"))
            comms_state = self._coerce_comms_state(frontmatter.get("comms_state"))
            projection = AgentMessage(
                msg_id=full_header.message_id,
                msg_type=str(frontmatter.get("msg_type", "") or "question"),
                from_agent=full_header.from_role,
                to_agents=[full_header.to_role],
                subject=full_header.subject,
                body=body,
                context_ref=task.id if task else None,
                urgency=MessageUrgency.BLOCKING if full_header.blocking else MessageUrgency.NORMAL,
                reply_needed=bool(full_header.blocking),
                requires_ack=bool(frontmatter.get("requires_ack", False)),
                reply_to_msg_id=full_header.reply_to,
                task_id=(task.id if task else str(frontmatter.get("task_id", "") or refs.get("task_id", "") or "").strip()) or None,
                status=self._status_from_message_bucket(header.path),
                transport_kind=transport_kind,
                semantic_type=semantic_type,
                comms_state=comms_state,
                correlation_id=str(frontmatter.get("correlation_id", "") or full_header.message_id).strip(),
                refs=refs,
                metadata={
                    "projection_source": "file_comms",
                    "comms_path": str(header.path),
                    "transport_kind": transport_kind.value,
                    "semantic_type": semantic_type.value,
                    "comms_state": comms_state.value,
                    "correlation_id": str(frontmatter.get("correlation_id", "") or full_header.message_id).strip(),
                    "refs": refs,
                    "from_endpoint_type": str(frontmatter.get("from_endpoint_type", "") or "").strip(),
                    "to_endpoint_types": list(frontmatter.get("to_endpoint_types", []) or []),
                    **{
                        key: frontmatter.get(key)
                        for key in (*self._ENVELOPE_METADATA_KEYS, *self._MAILBOX_METADATA_KEYS)
                        if frontmatter.get(key) not in (None, "", [])
                    },
                },
            )
            projection = self._canonicalize_message(projection, task=task)
            await self.store.save_message(projection)
            serialized.append(classify_worker_message(self._serialize_message(projection)))
            if mark_read and header.path.parent.name == "new":
                consumed_paths.append(header.path)
        if consumed_paths:
            moved = _comms.mark_seen(layout, role_id, consumed_paths)
            moved_names = {path.name for path in moved}
            for item in serialized:
                metadata = dict(item.get("metadata", {}) or {})
                path = str(metadata.get("comms_path", "") or "").strip()
                if path and Path(path).name in moved_names:
                    item["status"] = MessageStatus.READ.value
        return list(reversed(serialized))

    async def _project_comms_meetings(self, layout: Any, *, task: Task | None) -> None:
        from opc.layer2_organization import comms as _comms

        if not layout.meetings_root.is_dir():
            return
        for child in sorted(layout.meetings_root.iterdir(), key=lambda item: item.name):
            if not child.is_dir():
                continue
            state = _comms.read_meeting_state(layout, child.name)
            if state is None:
                continue
            transcript = [
                {
                    "entry_id": entry.entry_id,
                    "agent": entry.author,
                    "content": entry.content,
                    "posted_at": entry.posted_at,
                }
                for entry in _comms.read_transcript(layout, state.meeting_id)
            ]
            status = MeetingStatus.CLOSED if state.status == "closed" else MeetingStatus.OPEN
            await self.store.save_meeting(
                MeetingRoom(
                    room_id=state.meeting_id,
                    task_id=task.id if task else None,
                    topic=state.topic,
                    participants=list(state.participants),
                    shared_context="",
                    agenda=[],
                    decision_owner=state.organizer,
                    status=status,
                    outcome={"decision": state.decision} if state.decision else None,
                    transcript=transcript,
                    metadata={
                        "projection_source": "file_comms",
                        "transcript_path": str(state.transcript_path),
                    },
                )
            )

    async def rebuild_comms_projection(
        self,
        *,
        task: Task | None = None,
        layout: Any | None = None,
    ) -> dict[str, Any]:
        active_task = task
        resolved_layout = layout or self._task_comms_layout(active_task)
        if active_task is None or resolved_layout is None:
            return {"available": False}
        await self._project_comms_meetings(resolved_layout, task=active_task)
        return {
            "available": True,
            "workspace_root": self._task_workspace_root(active_task),
            "output_root": self._task_output_root(active_task),
            "comms_root": self._task_comms_root(active_task),
        }

    @staticmethod
    def _coerce_string_list(value: Any, *, limit: int = 8) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        items: list[str] = []
        for raw in list(value or []):
            text = str(raw).strip()
            if text:
                items.append(text)
        return items[:limit]

    @staticmethod
    def _extract_json_object(content: str) -> dict[str, Any] | None:
        text = str(content or "").strip()
        if not text:
            return None
        if text.startswith("```"):
            lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
            text = "\n".join(lines).strip()
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        decoder = json.JSONDecoder()
        for idx, char in enumerate(text):
            if char != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(text[idx:])
            except Exception:
                continue
            if isinstance(candidate, dict):
                return candidate
        return None

    @staticmethod
    def _normalize_meeting_stance(value: Any) -> str:
        text = str(value or "").strip().lower()
        if text in {"agree", "aligned", "support", "yes"}:
            return "agree"
        if text in {"disagree", "oppose", "no"}:
            return "disagree"
        if text in {"conditional", "partial", "mixed"}:
            return "conditional"
        if text in {"abstain", "neutral"}:
            return "abstain"
        return "conditional" if text else "abstain"

    @staticmethod
    def _normalize_meeting_vote(value: Any, stance: str = "") -> str:
        text = str(value or "").strip().lower()
        if text in {"support", "yes", "approve", "agree"}:
            return "support"
        if text in {"oppose", "no", "reject", "disagree"}:
            return "oppose"
        if text in {"abstain", "neutral"}:
            return "abstain"
        normalized_stance = CommunicationManager._normalize_meeting_stance(stance)
        if normalized_stance == "agree":
            return "support"
        if normalized_stance == "disagree":
            return "oppose"
        return "abstain"

    def _coerce_meeting_turn_payload(self, content: str) -> dict[str, Any]:
        parsed = self._extract_json_object(content)
        if not isinstance(parsed, dict):
            parsed = {}
        parsed_from_json = bool(parsed)
        lines = [
            line.strip(" -*\t")
            for line in str(content or "").splitlines()
            if line.strip(" -*\t")
        ]
        reasoning = str(parsed.get("reasoning", "") or "").strip()
        if not reasoning and lines:
            reasoning = lines[0]
        proposal = str(parsed.get("proposal", "") or "").strip()
        if not proposal and lines:
            proposal = lines[0]
        blocking_issues = self._coerce_string_list(parsed.get("blocking_issues"), limit=6)
        assumptions = self._coerce_string_list(parsed.get("assumptions"), limit=6)
        questions = self._coerce_string_list(parsed.get("questions_for_others"), limit=6)
        if not blocking_issues and not parsed_from_json:
            blocking_issues = [line for line in lines if any(token in line.lower() for token in ("block", "risk", "concern", "issue"))][:4]
        stance = self._normalize_meeting_stance(parsed.get("stance"))
        if stance == "abstain" and content:
            lowered = str(content).lower()
            if "disagree" in lowered or "oppose" in lowered:
                stance = "disagree"
            elif "agree" in lowered or "support" in lowered or "aligned" in lowered:
                stance = "agree"
        support_level = parsed.get("support_level")
        try:
            support_level_num = float(support_level)
        except (TypeError, ValueError):
            support_level_num = 0.8 if stance == "agree" else (0.2 if stance == "disagree" else 0.5)
        return {
            "stance": stance,
            "proposal": proposal,
            "support_level": max(0.0, min(1.0, support_level_num)),
            "vote": self._normalize_meeting_vote(parsed.get("vote"), stance=stance),
            "reasoning": reasoning,
            "blocking_issues": blocking_issues,
            "assumptions": assumptions,
            "questions_for_others": questions,
        }

    def _coerce_meeting_outcome(
        self,
        content: str,
        *,
        decision_method: str,
        consensus: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        parsed = self._extract_json_object(content)
        if not isinstance(parsed, dict):
            parsed = {}
        decision = str(parsed.get("decision", "") or "").strip()
        action_items = self._coerce_string_list(parsed.get("action_items"), limit=10)
        reasoning = str(parsed.get("reasoning", "") or "").strip() or str(content or "").strip()
        follow_up_questions = self._coerce_string_list(parsed.get("follow_up_questions"), limit=8)
        outcome = {
            "decision": decision,
            "action_items": action_items,
            "reasoning": reasoning,
            "decision_method": decision_method,
            "resolution_status": "resolved" if decision else "unresolved",
            "follow_up_questions": follow_up_questions,
        }
        if consensus:
            outcome["consensus_summary"] = dict(consensus)
            outcome["aligned_points"] = list(consensus.get("aligned_points", []))
            outcome["unresolved_items"] = list(consensus.get("blocking_conflicts", []))
            outcome["vote_summary"] = dict(consensus.get("vote_summary", {}))
        return outcome

    @staticmethod
    def _normalize_proposal_key(value: str) -> str:
        text = str(value or "").strip().lower()
        return " ".join(text.split())

    def _fallback_consensus_analysis(self, entries: list[dict[str, Any]], meeting: MeetingRoom) -> dict[str, Any]:
        structured_entries = []
        proposal_counts: dict[str, int] = {}
        vote_summary = {"support": 0, "oppose": 0, "abstain": 0}
        blocking_conflicts: list[str] = []
        open_questions: list[str] = []
        for entry in entries:
            structured = dict(entry.get("structured", {}) or {})
            structured_entries.append(structured)
            proposal = str(structured.get("proposal", "") or "").strip()
            proposal_key = self._normalize_proposal_key(proposal)
            if proposal_key:
                proposal_counts[proposal_key] = proposal_counts.get(proposal_key, 0) + 1
            vote = self._normalize_meeting_vote(structured.get("vote"), stance=str(structured.get("stance", "") or ""))
            vote_summary[vote] = vote_summary.get(vote, 0) + 1
            blocking_conflicts.extend(self._coerce_string_list(structured.get("blocking_issues"), limit=6))
            open_questions.extend(self._coerce_string_list(structured.get("questions_for_others"), limit=4))
        dominant_proposal_key = max(proposal_counts, key=proposal_counts.get, default="")
        proposal_lookup = {
            self._normalize_proposal_key(str((entry.get("structured", {}) or {}).get("proposal", "") or "").strip()):
            str((entry.get("structured", {}) or {}).get("proposal", "") or "").strip()
            for entry in entries
        }
        dominant_proposal = proposal_lookup.get(dominant_proposal_key, "")
        all_supportive = all(
            self._normalize_meeting_stance((entry.get("structured", {}) or {}).get("stance")) in {"agree", "conditional"}
            for entry in entries
        )
        consensus_reached = bool(
            entries
            and dominant_proposal_key
            and proposal_counts.get(dominant_proposal_key, 0) == len(entries)
            and all_supportive
            and not blocking_conflicts
        )
        aligned_points = [f"All participants converged on `{dominant_proposal}`."] if consensus_reached and dominant_proposal else []
        suggested_next = [
            str(entry.get("agent", "")).strip()
            for entry in entries
            if self._normalize_meeting_stance((entry.get("structured", {}) or {}).get("stance")) == "disagree"
        ]
        if not suggested_next:
            suggested_next = [participant for participant in meeting.participants if participant]
        return {
            "consensus_reached": consensus_reached,
            "aligned_points": aligned_points,
            "open_questions": list(dict.fromkeys(open_questions))[:8],
            "blocking_conflicts": list(dict.fromkeys(blocking_conflicts))[:8],
            "suggested_next_speakers": suggested_next[: max(1, len(suggested_next))],
            "dominant_proposal": dominant_proposal,
            "vote_summary": vote_summary,
            "recommended_decision_method": "semantic_consensus" if consensus_reached else "owner_override",
        }

    def _render_recent_meeting_transcript(self, meeting: MeetingRoom, *, limit: int = 12) -> str:
        lines: list[str] = []
        for entry in meeting.transcript[-limit:]:
            agent = str(entry.get("agent", "")).strip() or "unknown"
            round_no = entry.get("round")
            entry_type = str(entry.get("entry_type", "participant_turn") or "participant_turn").strip()
            prefix = f"[Round {round_no}] " if isinstance(round_no, int) and round_no > 0 else ""
            if entry_type == "judge_analysis":
                prefix += "[Judge] "
            elif entry_type == "final_decision":
                prefix += "[Decision] "
            lines.append(f"{prefix}{agent}: {str(entry.get('content', '')).strip()}")
        return "\n".join(lines)

    async def _judge_meeting_consensus(
        self,
        meeting: MeetingRoom,
        round_entries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        fallback = self._fallback_consensus_analysis(round_entries, meeting)
        if not self.llm:
            return fallback
        prompt = (
            "You are the semantic consensus judge for a company meeting.\n"
            "Analyze whether the participants actually converged on the same decision.\n"
            "Return ONLY valid JSON with this schema:\n"
            '{"consensus_reached": true|false, "aligned_points": ["..."], '
            '"open_questions": ["..."], "blocking_conflicts": ["..."], '
            '"suggested_next_speakers": ["role_id"], "dominant_proposal": "...", '
            '"vote_summary": {"support": 0, "oppose": 0, "abstain": 0}, '
            '"recommended_decision_method": "semantic_consensus|owner_override|majority_vote"}\n\n'
            f"Topic: {meeting.topic}\n"
            f"Agenda: {json.dumps(meeting.agenda, ensure_ascii=False)}\n"
            f"Current round: {meeting.current_round}\n"
            f"Recent transcript:\n{self._render_recent_meeting_transcript(meeting) or '(none)'}\n\n"
            f"Structured round entries:\n{json.dumps([entry.get('structured', {}) for entry in round_entries], ensure_ascii=False)}"
        )
        try:
            parsed = await call_llm_json_with_retry(
                self.llm,
                system="",
                payload=prompt,
                task_type="quick_tasks",
                label="meeting_consensus_judge",
            )
        except LLMRetryError:
            return fallback
        if not isinstance(parsed, dict):
            return fallback
        analysis = {
            "consensus_reached": bool(parsed.get("consensus_reached", False)),
            "aligned_points": self._coerce_string_list(parsed.get("aligned_points"), limit=10),
            "open_questions": self._coerce_string_list(parsed.get("open_questions"), limit=10),
            "blocking_conflicts": self._coerce_string_list(parsed.get("blocking_conflicts"), limit=10),
            "suggested_next_speakers": self._coerce_string_list(parsed.get("suggested_next_speakers"), limit=max(1, len(meeting.participants))),
            "dominant_proposal": str(parsed.get("dominant_proposal", "") or "").strip(),
            "vote_summary": dict(parsed.get("vote_summary", {}) or fallback.get("vote_summary", {})),
            "recommended_decision_method": str(parsed.get("recommended_decision_method", "") or fallback.get("recommended_decision_method", "owner_override")).strip() or "owner_override",
        }
        if not analysis["dominant_proposal"]:
            analysis["dominant_proposal"] = str(fallback.get("dominant_proposal", "") or "").strip()
        if not analysis["suggested_next_speakers"]:
            analysis["suggested_next_speakers"] = list(fallback.get("suggested_next_speakers", []))
        return analysis

    async def _run_meeting_turn(
        self,
        meeting: MeetingRoom,
        participant: str,
        *,
        mode: str,
        consensus: dict[str, Any] | None = None,
    ) -> str:
        waiting_task = await self.store.get_task(str(meeting.task_id)) if meeting.task_id else None
        transcript = self._render_recent_meeting_transcript(meeting) or "(no prior discussion)"
        role = self.org_engine.get_agent(participant) if self.org_engine else None
        role_name = str(getattr(role, "name", "") or participant).strip() or participant
        responsibility = str(getattr(role, "responsibility", "") or "").strip()
        output_schema = (
            '{"stance":"agree|disagree|conditional|abstain",'
            '"proposal":"...",'
            '"support_level":0.0,'
            '"vote":"support|oppose|abstain",'
            '"reasoning":"...",'
            '"blocking_issues":["..."],'
            '"assumptions":["..."],'
            '"questions_for_others":["..."]}'
        )
        if mode == "decision_owner":
            task_brief = (
                f"Meeting decision required for topic `{meeting.topic}`.\n"
                f"You are the decision owner: {participant}.\n"
                "Review the meeting transcript and the latest consensus analysis, then make the decision.\n"
                "Return ONLY valid JSON with this schema:\n"
                '{"decision":"...",'
                '"action_items":["..."],'
                '"reasoning":"...",'
                '"follow_up_questions":["..."]}'
            )
            prompt = (
                f"You are {role_name} ({participant}).\n"
                + (f"Responsibility: {responsibility}\n" if responsibility else "")
                + task_brief
                + "\n\nMeeting agenda:\n"
                + "\n".join(f"- {item}" for item in meeting.agenda)
                + f"\n\nRecent transcript:\n{transcript}"
                + f"\n\nConsensus analysis:\n{json.dumps(consensus or {}, ensure_ascii=False)}"
                + (
                    "\n\nResolve this decision within the company roles. Make a concrete decision when the evidence "
                    "supports one. If the evidence is genuinely insufficient, leave `decision` empty and explain "
                    "the unresolved evidence in `reasoning`; the workflow will return the issue to the owning agent "
                    "for an internal retry."
                )
            )
        else:
            task_brief = (
                f"Participate in meeting round {meeting.current_round} for topic `{meeting.topic}` as role `{participant}`.\n"
                "Think independently from your role's perspective.\n"
                "Return ONLY valid JSON with this schema:\n"
                f"{output_schema}\n"
                "Do not produce prose before or after the JSON."
            )
            prompt = (
                f"You are {role_name} ({participant}).\n"
                + (f"Responsibility: {responsibility}\n" if responsibility else "")
                + task_brief
                + "\n\nMeeting background:\n"
                + str(meeting.shared_context or "").strip()
                + "\n\nAgenda:\n"
                + "\n".join(f"- {item}" for item in meeting.agenda)
                + f"\n\nRecent transcript:\n{transcript}"
                + f"\n\nLatest consensus analysis:\n{json.dumps(consensus or {}, ensure_ascii=False)}"
                + "\n\nIf you disagree, name the blocking issue explicitly."
            )
        request = {
            "mode": mode,
            "participant": participant,
            "round": int(meeting.current_round or 1),
            "task_id": meeting.task_id,
            "project_id": str(getattr(waiting_task, "project_id", "") or ""),
            "task_brief": task_brief,
            "prompt": prompt,
            "system_addendum": (
                "## Meeting Turn Contract\n"
                "You are producing one structured meeting turn.\n"
                "Stay within the meeting topic.\n"
                "Use available read-only context/tools only when needed.\n"
                "Do not edit files, do not request user input, and do not widen scope.\n"
                "End your turn with one JSON object only."
            ),
            "meeting_context": {
                "room_id": meeting.room_id,
                "topic": meeting.topic,
                "agenda": list(meeting.agenda),
                "shared_context": meeting.shared_context,
                "current_round": int(meeting.current_round or 1),
                "decision_owner": meeting.decision_owner,
                "consensus": dict(consensus or {}),
                "recent_transcript": meeting.transcript[-12:],
            },
            "execution_scope_ids": list((waiting_task.metadata if waiting_task else {}).get("execution_task_ids", []))
            or ([str(meeting.task_id).strip()] if str(meeting.task_id or "").strip() else []),
        }
        if self.meeting_turn_runner:
            return await self.meeting_turn_runner(meeting, participant, request)
        # A missing runner means the engine wiring is broken — engine.py
        # sets ``set_meeting_turn_runner`` during initialization, and every
        # test that exercises meetings must inject its own fake runner.
        # Fail loud instead of silently returning a JSON stub (which would
        # pretend a consensus was reached) or calling the main LLM (which
        # would contradict the external-agent-voice contract).
        raise RuntimeError(
            f"meeting_turn_runner is not registered (meeting={meeting.room_id}, "
            f"participant={participant}, mode={mode}); engine wiring is incomplete."
        )

    def _ensure_meeting_round_state(self, meeting: MeetingRoom) -> None:
        if meeting.current_round <= 0:
            meeting.current_round = 1
        if not meeting.pending_participants:
            meeting.pending_participants = list(meeting.participants)

    def _meeting_round_entries(self, meeting: MeetingRoom, round_no: int) -> list[dict[str, Any]]:
        return [
            dict(entry)
            for entry in meeting.transcript
            if str(entry.get("entry_type", "") or "participant_turn") == "participant_turn"
            and int(entry.get("round") or 0) == int(round_no)
        ]

    def _append_meeting_participant_turn(
        self,
        meeting: MeetingRoom,
        *,
        from_agent: str,
        content: str,
        source: str,
        round_no: int | None = None,
    ) -> None:
        active_round = int(round_no or meeting.current_round or 1)
        meeting.transcript = [
            entry
            for entry in meeting.transcript
            if not (
                str(entry.get("entry_type", "") or "participant_turn") == "participant_turn"
                and int(entry.get("round") or 0) == active_round
                and str(entry.get("agent", "")).strip() == str(from_agent).strip()
            )
        ]
        meeting.transcript.append(
            {
                "agent": from_agent,
                "content": content,
                "timestamp": datetime.now().isoformat(),
                "round": active_round,
                "source": source,
                "entry_type": "participant_turn",
                "structured": self._coerce_meeting_turn_payload(content),
            }
        )
        meeting.pending_participants = [
            participant
            for participant in list(meeting.pending_participants or [])
            if str(participant).strip() != str(from_agent).strip()
        ]
        meeting.status = MeetingStatus.IN_PROGRESS
        meeting.updated_at = datetime.now()
        meeting.last_activity_at = datetime.now()

    async def _dispatch_meeting_round_requests(
        self,
        meeting: MeetingRoom,
        *,
        participants: list[str],
        consensus: dict[str, Any],
    ) -> None:
        unresolved = self._coerce_string_list(consensus.get("blocking_conflicts"), limit=6)
        open_questions = self._coerce_string_list(consensus.get("open_questions"), limit=6)
        body_lines = [
            f"Meeting follow-up for room `{meeting.room_id}`.",
            f"Round: {meeting.current_round}",
            f"Topic: {meeting.topic}",
            "Agenda:",
            *[f"- {item}" for item in meeting.agenda],
        ]
        if unresolved:
            body_lines.append("Unresolved conflicts:")
            body_lines.extend(f"- {item}" for item in unresolved)
        if open_questions:
            body_lines.append("Open questions:")
            body_lines.extend(f"- {item}" for item in open_questions)
        body_lines.append("Reply with `respond_meeting` using a structured JSON stance.")
        for participant in participants:
            follow_up = AgentMessage(
                msg_type="decision_needed",
                from_agent=meeting.decision_owner,
                to_agents=[participant],
                subject=f"Meeting Round {meeting.current_round}: {meeting.topic}",
                body="\n".join(body_lines),
                context_ref=meeting.room_id,
                task_id=meeting.task_id,
                urgency=MessageUrgency.HIGH,
                metadata={
                    "meeting_room_id": meeting.room_id,
                    "agenda": list(meeting.agenda),
                    "meeting_round": meeting.current_round,
                    "consensus_summary": dict(consensus),
                },
            )
            await self.send_dm(follow_up)

    async def _finalize_meeting(
        self,
        meeting: MeetingRoom,
        *,
        outcome: dict[str, Any],
        transcript_note: str = "",
    ) -> MeetingRoom:
        normalized_outcome = dict(outcome)
        decision = str(normalized_outcome.get("decision", "") or "").strip()
        resolution_status = str(normalized_outcome.get("resolution_status", "") or "").strip()
        if resolution_status not in {"resolved", "unresolved"}:
            resolution_status = "resolved" if decision else "unresolved"
        normalized_outcome["resolution_status"] = resolution_status
        meeting.outcome = normalized_outcome
        meeting.decision_method = str(outcome.get("decision_method", "") or meeting.decision_method or "").strip()
        meeting.pending_participants = []
        meeting.status = MeetingStatus.DECIDED if resolution_status == "resolved" else MeetingStatus.CLOSED
        meeting.updated_at = datetime.now()
        meeting.last_activity_at = datetime.now()
        if transcript_note:
            meeting.transcript.append(
                {
                    "agent": meeting.decision_owner or "meeting_system",
                    "content": transcript_note,
                    "timestamp": datetime.now().isoformat(),
                    "round": int(meeting.current_round or 0),
                    "entry_type": "final_decision",
                    "source": "meeting_system",
                }
        )
        await self.store.save_meeting(meeting)
        if meeting.task_id:
            task = await self.store.get_task(str(meeting.task_id))
            if task:
                await self.store.record_work_item_decision(
                    WorkItemDecisionRecord(
                        project_id=str(task.project_id or "default"),
                        task_id=task.id,
                        role_id=meeting.decision_owner,
                        projection_id=projection_id_for_task(task),
                        category="meeting",
                        summary=str(outcome.get("decision", "") or meeting.topic).strip(),
                        details={
                            "meeting_room_id": meeting.room_id,
                            "decision_method": meeting.decision_method,
                            "resolution_status": resolution_status,
                            "consensus": dict(meeting.consensus or {}),
                            "action_items": list(outcome.get("action_items", []) or []),
                        },
                    )
                )
        await self.event_bus.publish(
            OPCEvent(
                event_type="meeting_ended",
                payload={
                    "room_id": meeting.room_id,
                    "task_id": meeting.task_id,
                    "outcome": meeting.outcome,
                    "decision_method": meeting.decision_method,
                },
            )
        )
        return meeting

    def _majority_vote_outcome(self, meeting: MeetingRoom, consensus: dict[str, Any]) -> dict[str, Any] | None:
        vote_summary = dict(consensus.get("vote_summary", {}) or {})
        support = int(vote_summary.get("support", 0) or 0)
        oppose = int(vote_summary.get("oppose", 0) or 0)
        dominant = str(consensus.get("dominant_proposal", "") or "").strip()
        if support <= oppose or not dominant:
            return None
        return {
            "decision": dominant,
            "action_items": [
                "Proceed with the majority-backed proposal.",
                "Record the unresolved objections for downstream reviewers.",
            ],
            "reasoning": "Consensus was not unanimous, so the meeting used a majority vote fallback.",
            "decision_method": "majority_vote",
            "resolution_status": "resolved",
            "follow_up_questions": [],
            "consensus_summary": dict(consensus),
            "aligned_points": list(consensus.get("aligned_points", [])),
            "unresolved_items": list(consensus.get("blocking_conflicts", [])),
            "vote_summary": vote_summary,
        }

    def _unresolved_meeting_outcome(
        self,
        consensus: dict[str, Any],
        *,
        owner_reasoning: str = "",
    ) -> dict[str, Any]:
        reasoning = "The decision owner did not produce a concrete decision after the meeting failed to converge."
        if owner_reasoning:
            reasoning = f"{reasoning} Owner assessment: {owner_reasoning}"
        return {
            "decision": "",
            "action_items": [
                "Return the unresolved conflicts to the owning agent.",
                "Gather the missing evidence and retry the decision internally.",
            ],
            "reasoning": reasoning,
            "decision_method": "owner_unresolved",
            "resolution_status": "unresolved",
            "retry_recommended": True,
            "follow_up_questions": list(consensus.get("open_questions", [])),
            "consensus_summary": dict(consensus),
            "aligned_points": list(consensus.get("aligned_points", [])),
            "unresolved_items": list(consensus.get("blocking_conflicts", [])),
            "vote_summary": dict(consensus.get("vote_summary", {})),
        }

    async def _resolve_meeting_without_consensus(
        self,
        meeting: MeetingRoom,
        *,
        consensus: dict[str, Any],
    ) -> MeetingRoom:
        decision_policy = str(meeting.metadata.get("decision_policy", "") or "semantic_consensus_then_owner").strip()
        # Upgrade compatibility for meetings persisted before owner decisions
        # became fully internal. Never surface the legacy policy to prompts or
        # create a user-facing wait from it.
        if decision_policy == "owner_then_human":
            decision_policy = "owner_override"
        majority_first = decision_policy in {
            "semantic_consensus_then_vote",
            "majority_vote",
        }
        if majority_first:
            majority = self._majority_vote_outcome(meeting, consensus)
            if majority is not None:
                return await self._finalize_meeting(
                    meeting,
                    outcome=majority,
                    transcript_note=str(majority.get("decision", "") or majority.get("reasoning", "")).strip(),
                )

        owner_reasoning = ""
        if meeting.decision_owner and decision_policy in {
            "semantic_consensus_then_owner",
            "owner_override",
            "semantic_consensus_then_owner_then_vote",
            "semantic_consensus_then_vote",
            "majority_vote",
        }:
            owner_response = await self._run_meeting_turn(
                meeting,
                meeting.decision_owner,
                mode="decision_owner",
                consensus=consensus,
            )
            owner_outcome = self._coerce_meeting_outcome(
                owner_response,
                decision_method="owner_override",
                consensus=consensus,
            )
            if owner_outcome.get("decision"):
                return await self._finalize_meeting(
                    meeting,
                    outcome=owner_outcome,
                    transcript_note=str(owner_outcome.get("decision", "") or owner_outcome.get("reasoning", "")).strip(),
                )
            owner_reasoning = str(owner_outcome.get("reasoning", "") or "").strip()

        if not majority_first and decision_policy == "semantic_consensus_then_owner_then_vote":
            majority = self._majority_vote_outcome(meeting, consensus)
            if majority is not None:
                return await self._finalize_meeting(
                    meeting,
                    outcome=majority,
                    transcript_note=str(majority.get("decision", "") or majority.get("reasoning", "")).strip(),
                )

        unresolved = self._unresolved_meeting_outcome(
            consensus,
            owner_reasoning=owner_reasoning,
        )
        return await self._finalize_meeting(
            meeting,
            outcome=unresolved,
            transcript_note=str(unresolved.get("reasoning", "")).strip(),
        )

    async def _advance_meeting(
        self,
        meeting: MeetingRoom,
        *,
        run_missing_participants: bool,
    ) -> tuple[MeetingRoom, bool]:
        if meeting.status in {MeetingStatus.DECIDED, MeetingStatus.CLOSED, MeetingStatus.CANCELLED}:
            return meeting, False
        self._ensure_meeting_round_state(meeting)
        changed = False
        iteration_budget = max(1, int(meeting.max_rounds or 1)) + 1
        while iteration_budget > 0:
            iteration_budget -= 1
            round_entries = self._meeting_round_entries(meeting, meeting.current_round)
            responded = {
                str(entry.get("agent", "")).strip()
                for entry in round_entries
                if str(entry.get("agent", "")).strip()
            }
            pending = [
                participant
                for participant in list(meeting.participants or [])
                if str(participant).strip() and str(participant).strip() not in responded
            ]
            if pending != list(meeting.pending_participants or []):
                meeting.pending_participants = list(pending)
                changed = True
            if pending:
                if not run_missing_participants:
                    meeting.updated_at = datetime.now()
                    await self.store.save_meeting(meeting)
                    return meeting, changed
                for participant in pending:
                    content = await self._run_meeting_turn(
                        meeting,
                        participant,
                        mode="participant",
                        consensus=meeting.consensus,
                    )
                    self._append_meeting_participant_turn(
                        meeting,
                        from_agent=participant,
                        content=content,
                        source="meeting_runner",
                        round_no=meeting.current_round,
                    )
                    changed = True
                round_entries = self._meeting_round_entries(meeting, meeting.current_round)

            if not round_entries:
                meeting.updated_at = datetime.now()
                await self.store.save_meeting(meeting)
                return meeting, changed

            consensus = await self._judge_meeting_consensus(meeting, round_entries)
            meeting.consensus = dict(consensus)
            meeting.updated_at = datetime.now()
            meeting.last_activity_at = datetime.now()
            meeting.transcript.append(
                {
                    "agent": "meeting_judge",
                    "content": json.dumps(consensus, ensure_ascii=False),
                    "timestamp": datetime.now().isoformat(),
                    "round": int(meeting.current_round or 0),
                    "entry_type": "judge_analysis",
                    "source": "meeting_judge",
                }
            )
            changed = True

            if bool(consensus.get("consensus_reached", False)):
                semantic_outcome = {
                    "decision": str(consensus.get("dominant_proposal", "") or "").strip(),
                    "action_items": [
                        "Follow the meeting decision in downstream work.",
                        "Preserve the aligned rationale in the handoff.",
                    ],
                    "reasoning": (
                        "Semantic consensus was reached across the participant positions and blocking conflicts were resolved."
                    ),
                    "decision_method": "semantic_consensus",
                    "resolution_status": "resolved",
                    "follow_up_questions": list(consensus.get("open_questions", [])),
                    "consensus_summary": dict(consensus),
                    "aligned_points": list(consensus.get("aligned_points", [])),
                    "unresolved_items": list(consensus.get("blocking_conflicts", [])),
                    "vote_summary": dict(consensus.get("vote_summary", {})),
                }
                if semantic_outcome["decision"]:
                    return await self._finalize_meeting(
                        meeting,
                        outcome=semantic_outcome,
                        transcript_note=str(semantic_outcome.get("decision", "")).strip(),
                    ), True

            deadline_reached = bool(meeting.deadline_at and datetime.now() >= meeting.deadline_at)
            if meeting.current_round >= max(1, int(meeting.max_rounds or 1)) or deadline_reached:
                return await self._resolve_meeting_without_consensus(meeting, consensus=consensus), True

            next_speakers = [
                participant
                for participant in self._coerce_string_list(consensus.get("suggested_next_speakers"), limit=max(1, len(meeting.participants)))
                if participant in meeting.participants
            ]
            meeting.current_round += 1
            meeting.pending_participants = next_speakers or list(meeting.participants)
            meeting.status = MeetingStatus.IN_PROGRESS
            meeting.updated_at = datetime.now()
            meeting.last_activity_at = datetime.now()
            await self.store.save_meeting(meeting)
            if not run_missing_participants:
                await self._dispatch_meeting_round_requests(
                    meeting,
                    participants=list(meeting.pending_participants),
                    consensus=consensus,
                )
                return meeting, True
        await self.store.save_meeting(meeting)
        return meeting, changed

    def _mark_peer_wait(self, task: Task, message: AgentMessage, timeout_seconds: int = 300) -> dict[str, Any]:
        task.status = TaskStatus.AWAITING_PEER
        task.metadata = dict(task.metadata)
        wait = {
            "message_id": message.msg_id,
            "waiting_on_agents": list(message.to_agents),
            "timeout_action": message.timeout_action or "",
            "timeout_at": (datetime.now() + timedelta(seconds=timeout_seconds)).isoformat(),
            "kind": "peer_message",
        }
        task.metadata["peer_wait"] = wait
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["pending_peer_message"] = self._serialize_message(message)
        return wait

    @staticmethod
    def _task_dependency_keys(task: Task) -> set[str]:
        keys = {str(task.id).strip()}
        projection_id = projection_id_for_task(task)
        if projection_id:
            keys.add(projection_id)
        return keys

    @staticmethod
    def _normalize_scope_ids(raw_ids: list[str] | None) -> list[str]:
        return list(dict.fromkeys(str(item).strip() for item in list(raw_ids or []) if str(item).strip()))

    def _task_scope_ids(self, task: Task | None = None, task_id: str | None = None, task_ids: list[str] | None = None) -> list[str]:
        explicit = self._normalize_scope_ids(task_ids)
        if explicit:
            return explicit
        if task is not None:
            runtime_task_ids = self._normalize_scope_ids(task.metadata.get("execution_task_ids", []))
            if runtime_task_ids:
                return runtime_task_ids
            if str(task.id).strip():
                return [str(task.id).strip()]
            return []
        if task_id:
            return [str(task_id).strip()]
        return []

    @staticmethod
    def _role_id_for_task(task: Task) -> str:
        return str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip()

    @staticmethod
    def _is_task_runnable(task: Task, task_by_key: dict[str, Task]) -> bool:
        deps = [str(dep).strip() for dep in task.dependencies if str(dep).strip()]
        if not deps:
            return True
        return all(
            (dep_task := task_by_key.get(dep)) is not None and dep_task.status == TaskStatus.DONE
            for dep in deps
        )

    # Standby-profile helpers (_build_standby_task_summary, _render_standby_profile,
    # _build_role_standby_profile, _collect_standby_profiles) were removed along
    # with the main-key "impersonation reply" path in a prior cleanup. Standby
    # roles now wake up via CommsReactivationSweeper and reply through their
    # own agents via the runtime mailbox, so no side-channel "rendered
    # standby context" is needed.

    async def _resolve_message_project_id(self, message: AgentMessage) -> str:
        metadata_project = str(message.metadata.get("project_id", "") or "").strip()
        if metadata_project:
            return metadata_project
        for ref in (message.task_id, message.context_ref):
            ref_id = str(ref or "").strip()
            if not ref_id:
                continue
            task = await self.store.get_task(ref_id)
            if task and str(task.project_id or "").strip():
                return str(task.project_id).strip()
        return ""

    # NOTE: ``_reply_from_standby_profiles`` and ``_maybe_auto_reply_to_message``
    # used to synthesize a main-LLM "impersonation reply" whenever a blocking
    # DM landed in a standby role's inbox. They are removed: standby roles now
    # wake up via ``CommsReactivationSweeper`` (layer2_organization/
    # reactivation_sweeper.py) and reply through their own external/native
    # agent via the runtime mailbox. The role's own voice is authoritative,
    # and the main API key is not burned on impersonation.

    async def _validate_blocking_recipients_ready(self, task: Task, to_agents: list[str]) -> None:
        """Reject blocking waits on recipients that cannot reply yet.

        This prevents a work item from pausing on a downstream role whose
        task still depends on the current work item (or otherwise is not
        runnable), which would deadlock the company runtime.
        """
        project_id = str(getattr(task, "project_id", "") or "").strip()
        recipients = {str(agent).strip() for agent in to_agents if str(agent).strip()}
        work_item_turn_type = turn_type_for_task(task, fallback="")
        work_item_runtime = is_work_item_runtime_metadata(task.metadata or {})
        if not project_id or not recipients:
            return

        tasks = await self.store.get_tasks(project_id=project_id)
        dependency_keys = self._task_dependency_keys(task)
        task_by_key: dict[str, Task] = {}
        tasks_by_role: dict[str, list[Task]] = {}

        for candidate in tasks:
            task_id = str(candidate.id).strip()
            if task_id:
                task_by_key[task_id] = candidate
            projection_id = projection_id_for_task(candidate)
            if projection_id:
                task_by_key.setdefault(projection_id, candidate)

            role_id = str(candidate.assigned_to or candidate.metadata.get("work_item_role_id", "") or "").strip()
            if role_id:
                tasks_by_role.setdefault(role_id, []).append(candidate)

        blocked_reasons: list[str] = []
        for role_id in sorted(recipients):
            candidate_tasks = [
                candidate
                for candidate in tasks_by_role.get(role_id, [])
                if str(candidate.id).strip() != str(task.id).strip()
            ]
            if not candidate_tasks:
                blocked_reasons.append(
                    f"{role_id} has no active work package yet"
                    + (" during intake/dispatch startup" if work_item_turn_type in {"intake", "dispatch", "plan"} or work_item_runtime else "")
                )
                continue

            role_is_ready = False
            role_reasons: list[str] = []
            for candidate in candidate_tasks:
                deps = {str(dep).strip() for dep in candidate.dependencies if str(dep).strip()}
                unmet = [
                    dep
                    for dep in deps
                    if (dep_task := task_by_key.get(dep)) is None or dep_task.status != TaskStatus.DONE
                ]
                work_item_label = projection_id_for_task(candidate) or str(candidate.title or candidate.id).strip()

                if dependency_keys & deps:
                    role_reasons.append(f"{role_id} ({work_item_label}) depends on the current work item")
                    continue

                if candidate.status in {TaskStatus.PENDING, TaskStatus.BLOCKED} and unmet:
                    role_reasons.append(f"{role_id} ({work_item_label}) is not runnable yet")
                    continue

                role_is_ready = True
                break

            if not role_is_ready and role_reasons:
                blocked_reasons.append(role_reasons[0])

        if blocked_reasons:
            raise ValueError(
                "Cannot use blocking peer wait on recipient(s) that cannot reply yet: "
                + "; ".join(blocked_reasons)
                + ". Use `blocking=false` or finish the current work item first."
            )

    # --- Mode 1: Direct Message ---

    async def send_dm(self, message: AgentMessage, task: Task | None = None) -> AgentMessage:
        active_task = task
        if active_task is None and message.task_id:
            active_task = await self.store.get_task(str(message.task_id))
        context = CollaborationContext.from_task(active_task, role_id=message.from_agent)
        return await self._collaboration_service().send_dm(context, message, task=active_task)

    async def open_peer_wait(
        self,
        task: Task,
        to_agents: list[str],
        subject: str,
        body: str,
        timeout_action: str = "",
        timeout_seconds: int = 300,
        msg_type: str = "question",
    ) -> dict[str, Any]:
        message = AgentMessage(
            msg_type=msg_type,
            from_agent=task.assigned_to,
            to_agents=to_agents,
            subject=subject,
            body=body,
            context_ref=task.id,
            task_id=task.id,
            urgency=MessageUrgency.BLOCKING,
            reply_needed=True,
            timeout_action=timeout_action,
            metadata={
                **work_item_identity_payload_for_task(task),
                "async_mailbox": True,
                "reply_requested": True,
                "legacy_blocking_request": True,
                "timeout_seconds": timeout_seconds,
                "execution_task_ids": list(task.metadata.get("execution_task_ids", [])),
            },
        )
        message = await self.send_dm(message, task=task)
        return {
            "requires_peer_wait": False,
            "reason": (
                f"Delivered async mailbox request to {', '.join(to_agents)}. "
                "The sender continues working without pausing."
            ),
            "message": self._serialize_message(message),
            "delivery_mode": "async_mailbox",
            "blocking_deprecated": True,
        }

    async def ask_peer_and_wait(
        self,
        task: Task,
        to_agent: str,
        subject: str,
        body: str,
        timeout_action: str = "",
        timeout_seconds: int = 300,
        on_timeout: str = "continue",
    ) -> dict[str, Any]:
        context = CollaborationContext.from_task(task)
        return await self._collaboration_service().ask_peer_and_wait(
            context,
            task=task,
            to_agent=to_agent,
            subject=subject,
            body=body,
            timeout_action=timeout_action,
            timeout_seconds=timeout_seconds,
            on_timeout=on_timeout,
        )

    async def wait_for_reply(
        self,
        agent_id: str,
        original_msg_id: str,
        timeout: float = 60.0,
    ) -> AgentMessage | None:
        """Wait for an in-memory reply to a specific message."""
        queue = self._get_queue(agent_id)
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=remaining)
                if msg.reply_to_msg_id == original_msg_id or msg.context_ref == original_msg_id:
                    return msg
                queue.put_nowait(msg)
            except asyncio.TimeoutError:
                return None

    async def read_inbox(
        self,
        agent_id: str,
        task_id: str | None = None,
        task_ids: list[str] | None = None,
        task: Task | None = None,
        unread_only: bool = True,
        limit: int = 10,
        mark_read: bool = True,
    ) -> list[dict[str, Any]]:
        context = CollaborationContext.from_task(task, role_id=agent_id)
        return await self._collaboration_service().read_inbox(
            context,
            agent_id=agent_id,
            task_id=task_id,
            task_ids=task_ids,
            task=task,
            unread_only=unread_only,
            limit=limit,
            mark_read=mark_read,
        )

    async def inbox(
        self,
        *,
        agent_id: str,
        task: Task | None = None,
        action: str = "status",
        message_ids: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        context = CollaborationContext.from_task(task, role_id=agent_id)
        return await self._collaboration_service().inbox(
            context,
            agent_id=agent_id,
            action=action,
            message_ids=list(message_ids or []),
            limit=limit,
            task=task,
        )

    def format_live_inbox_interrupt(self, messages: list[dict[str, Any]]) -> str:
        if not messages:
            return ""
        lines = [
            "## Live Inbox Update",
            "New direct messages arrived while you were working. They do not pause your task.",
            "You may reply now with `reply_message`, acknowledge handled messages with `inbox(action=\"ack\")`, or continue working and respond later.",
            "If you reply, use the exact `message_id` shown below.",
        ]
        for item in messages[:6]:
            msg_id = str(item.get("msg_id", "")).strip()
            from_agent = str(item.get("from_agent", "")).strip()
            subject = str(item.get("subject", "")).strip()
            body = str(item.get("body", "")).strip()
            reply_needed = bool(item.get("reply_needed", False))
            urgency = str(item.get("urgency", "")).strip()
            message_class = str(item.get("message_class", "") or dict(item.get("metadata", {}) or {}).get("message_class", "")).strip()
            protocol_type = str(item.get("protocol_type", "") or dict(item.get("metadata", {}) or {}).get("protocol_type", "")).strip()
            lines.append(
                f"- message_id={msg_id} | from={from_agent} | urgency={urgency or 'normal'} | "
                f"reply_needed={'yes' if reply_needed else 'no'}"
                + (f" | class={message_class}" if message_class else "")
                + (f" | protocol={protocol_type}" if protocol_type else "")
            )
            if subject:
                lines.append(f"  Subject: {subject}")
            if body:
                lines.append(f"  Body: {body}")
        return "\n".join(lines)

    async def consume_live_inbox_messages(
        self,
        task: Task,
        *,
        agent_id: str | None = None,
        limit: int = 6,
    ) -> list[dict[str, Any]]:
        session_state = dict(task.metadata.get("member_session_state", {}) or task.context_snapshot.get("member_session", {}) or {})
        if str(session_state.get("status", "") or "").strip().lower() == "cold":
            return []
        active_agent = str(agent_id or task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip()
        if not active_agent:
            return []
        messages = await self.read_inbox(
            agent_id=active_agent,
            task=task,
            unread_only=True,
            limit=limit,
            mark_read=False,
        )
        if not messages:
            return []
        classified = [classify_worker_message(dict(item)) for item in messages if isinstance(item, dict)]
        actionable = [item for item in classified if worker_message_is_actionable(item)]
        injected_ids = {
            str(item).strip()
            for item in list(task.context_snapshot.get("live_inbox_injected_message_ids", []) or [])
            if str(item).strip()
        }
        fresh_actionable = [
            item for item in actionable
            if str(item.get("msg_id", "") or item.get("message_id", "") or "").strip()
            and str(item.get("msg_id", "") or item.get("message_id", "") or "").strip() not in injected_ids
        ]
        notifications = [item for item in classified if item.get("message_class") == "notification"]
        task.context_snapshot = dict(task.context_snapshot)
        live_history = list(task.context_snapshot.get("live_inbox_messages", []))
        live_history.extend(fresh_actionable)
        task.context_snapshot["live_inbox_messages"] = live_history[-10:]
        if fresh_actionable:
            task.context_snapshot["latest_live_inbox"] = fresh_actionable[-1]
            injected_ids.update(
                str(item.get("msg_id", "") or item.get("message_id", "") or "").strip()
                for item in fresh_actionable
                if str(item.get("msg_id", "") or item.get("message_id", "") or "").strip()
            )
            task.context_snapshot["live_inbox_injected_message_ids"] = sorted(injected_ids)[-50:]
        if notifications:
            task.context_snapshot["latest_live_notification"] = notifications[-1]
        await self.store.save_task(task)
        return fresh_actionable

    async def reply_to_message(
        self,
        original_msg_id: str,
        from_agent: str,
        body: str,
        subject: str = "",
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        original_metadata_updates: dict[str, Any] | None = None,
    ) -> AgentMessage:
        resolved_task_id = str(task_id or "").strip()
        if not resolved_task_id and self.store and hasattr(self.store, "get_message"):
            original = await self.store.get_message(original_msg_id)
            if original is not None:
                resolved_task_id = str(original.task_id or "").strip()
        active_task = await self.store.get_task(resolved_task_id) if resolved_task_id else None
        return await self._collaboration_service().reply_message(
            CollaborationContext.from_task(active_task, role_id=from_agent),
            original_msg_id=original_msg_id,
            from_agent=from_agent,
            body=body,
            subject=subject,
            task_id=resolved_task_id or task_id,
            metadata=metadata,
            original_metadata_updates=original_metadata_updates,
        )

    async def resolve_task_peer_wait(self, task: Task) -> bool:
        wait = dict(task.metadata.get("peer_wait", {}))
        if not wait:
            return False
        if str(wait.get("kind") or "") == "comms_blocking":
            # File-comms blocking waits are owned by the company
            # dispatcher's per-tick unpark; resolving them here would flip
            # task.status without the work-item phase and strand the run.
            return False
        if wait.get("kind") != "meeting":
            msg_id = wait.get("message_id")
            reply = await self.store.get_latest_reply(msg_id) if msg_id else None
            live_required = bool(wait.get("live_required", False))
            if reply:
                task.status = TaskStatus.PENDING
                task.metadata = dict(task.metadata)
                task.metadata.pop("peer_wait", None)
                task.context_snapshot = dict(task.context_snapshot)
                replies = list(task.context_snapshot.get("peer_replies", []))
                replies.append(self._serialize_message(reply))
                task.context_snapshot["peer_replies"] = replies[-10:]
                task.context_snapshot["latest_peer_reply"] = self._serialize_message(reply)
                reply_origin = str((reply.metadata or {}).get("reply_origin", "") or "live_reply").strip()
                task.context_snapshot["peer_wait_released"] = {
                    "kind": "peer_message",
                    "released_at": datetime.now().isoformat(),
                    "reason": "Peer wait resolved by an available reply.",
                    "reply_origin": reply_origin,
                }
                await self.store.save_task(task)
                if reply_origin != "live_reply":
                    await self._record_non_live_collaboration(
                        task,
                        origin=reply_origin,
                        summary=f"Peer wait on `{msg_id}` resumed with a non-live reply.",
                    )
                return True
            if not live_required:
                task.status = TaskStatus.PENDING
                task.metadata = dict(task.metadata)
                task.metadata.pop("peer_wait", None)
                task.context_snapshot = dict(task.context_snapshot)
                task.context_snapshot["peer_wait_released"] = {
                    "kind": "peer_message",
                    "released_at": datetime.now().isoformat(),
                    "reason": "Peer messages are now asynchronous mailbox delivery and no longer pause tasks.",
                }
                await self.store.save_task(task)
                return True
        msg_id = wait.get("message_id")
        if not msg_id:
            return False
        reply = await self.store.get_latest_reply(msg_id)
        if reply:
            task.status = TaskStatus.PENDING
            task.metadata = dict(task.metadata)
            task.metadata.pop("peer_wait", None)
            task.context_snapshot = dict(task.context_snapshot)
            replies = list(task.context_snapshot.get("peer_replies", []))
            replies.append(self._serialize_message(reply))
            task.context_snapshot["peer_replies"] = replies[-10:]
            task.context_snapshot["latest_peer_reply"] = self._serialize_message(reply)
            await self.store.save_task(task)
            reply_origin = str((reply.metadata or {}).get("reply_origin", "") or "live_reply").strip()
            if reply_origin != "live_reply":
                await self._record_non_live_collaboration(
                    task,
                    origin=reply_origin,
                    summary=f"Meeting wait on `{msg_id}` resumed with a non-live reply.",
                )
            return True

        # Previously this branch called ``_synthesize_reply_from_completed_agents``
        # to burn the main LLM on an impersonation reply when all targets
        # were DONE. That path is gone: ``CommsReactivationSweeper`` now
        # re-opens the recipient tasks so their real agents reply via the
        # runtime mailbox. The next tick of this resolver picks up the
        # genuine reply via ``get_latest_reply`` above. If no reply ever
        # arrives, the waiting task stays in AWAITING_PEER until the peer
        # wait's configured timeout fires (handled elsewhere).

        timeout_at = wait.get("timeout_at")
        if timeout_at and datetime.now() >= datetime.fromisoformat(timeout_at):
            timeout_mode = str(wait.get("on_timeout", "") or "continue").strip().lower() or "continue"
            if timeout_mode == "manager":
                return await self._escalate_peer_wait_to_manager(task, wait)
            if timeout_mode == "meeting":
                await self._escalate_peer_wait_to_meeting(task, wait)
                return False
            task.status = TaskStatus.PENDING
            task.metadata = dict(task.metadata)
            task.context_snapshot = dict(task.context_snapshot)
            task.metadata.pop("peer_wait", None)
            task.context_snapshot["peer_timeout_action"] = wait.get("timeout_action", "")
            original = await self.store.get_message(msg_id)
            if original:
                original.status = MessageStatus.TIMED_OUT
                original.processed_at = datetime.now()
                await self.store.save_message(original)
            await self.store.save_task(task)
            return True
        return False

    async def _escalate_peer_wait_to_manager(self, task: Task, wait: dict[str, Any]) -> bool:
        manager_role = str(task.metadata.get("manager_role_id", "") or "").strip()
        if not manager_role and self.org_engine:
            agent = self.org_engine.get_agent(str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip())
            manager_role = str(getattr(agent, "reports_to", "") or "").strip()
        if not manager_role:
            task.status = TaskStatus.PENDING
            task.metadata = dict(task.metadata)
            task.metadata.pop("peer_wait", None)
            task.context_snapshot = dict(task.context_snapshot)
            task.context_snapshot["peer_timeout_action"] = wait.get("timeout_action", "")
            task.context_snapshot["peer_timeout_escalation"] = {
                "mode": "manager_unavailable",
                "reason": "No manager role was available for escalation.",
            }
            await self.store.save_task(task)
            return True
        original_msg_id = str(wait.get("message_id", "") or "").strip()
        summary = (
            f"Peer wait timed out for work item `{projection_id_for_task(task)}`. "
            f"Waiting on: {', '.join(wait.get('waiting_on_agents', [])) or '(unknown)'}. "
            f"Default timeout action: {str(wait.get('timeout_action', '') or '(none)').strip()}."
        )
        escalation_message = AgentMessage(
            msg_type="decision_needed",
            from_agent=str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip(),
            to_agents=[manager_role],
            subject=f"Peer timeout escalation: {task.title}",
            body=summary,
            context_ref=task.id,
            task_id=task.id,
            urgency=MessageUrgency.HIGH,
            metadata={
                "peer_timeout_escalation": True,
                "timed_out_message_id": original_msg_id,
                "reply_origin": "live_request",
            },
        )
        escalation_message = await self.send_dm(escalation_message, task=task)
        task.status = TaskStatus.PENDING
        task.metadata = dict(task.metadata)
        task.metadata.pop("peer_wait", None)
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["peer_timeout_action"] = wait.get("timeout_action", "")
        task.context_snapshot["peer_timeout_escalation"] = {
            "mode": "manager",
            "manager_role_id": manager_role,
            "message_id": escalation_message.msg_id,
            "reason": summary,
        }
        await self.store.save_task(task)
        return True

    async def _escalate_peer_wait_to_meeting(self, task: Task, wait: dict[str, Any]) -> None:
        manager_role = str(task.metadata.get("manager_role_id", "") or "").strip()
        if not manager_role and self.org_engine:
            agent = self.org_engine.get_agent(str(task.assigned_to or task.metadata.get("work_item_role_id", "") or "").strip())
            manager_role = str(getattr(agent, "reports_to", "") or "").strip()
        participants = [str(item).strip() for item in list(wait.get("waiting_on_agents", []) or []) if str(item).strip()]
        if manager_role and manager_role not in participants and manager_role != str(task.assigned_to or "").strip():
            participants.append(manager_role)
        meeting = await self.create_meeting(
            MeetingRoom(
                task_id=task.id,
                topic=f"Escalated peer wait: {task.title}",
                participants=participants,
                shared_context=(
                    f"Peer wait timed out. Previous timeout action: {str(wait.get('timeout_action', '') or '(none)').strip()}.\n"
                    f"Original waiting agents: {', '.join(wait.get('waiting_on_agents', [])) or '(unknown)'}"
                ),
                agenda=[
                    "Resolve the blocked peer clarification",
                    "Decide whether the default timeout action is acceptable",
                ],
                decision_owner=str(task.assigned_to or task.metadata.get("work_item_role_id", "coordinator") or "coordinator").strip(),
            )
        )
        task.metadata = dict(task.metadata)
        task.metadata["peer_wait"] = {
            "kind": "meeting",
            "meeting_room_id": meeting.room_id,
            "waiting_on_agents": participants,
            "escalated_from": dict(wait),
        }
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["peer_timeout_escalation"] = {
            "mode": "meeting",
            "meeting_room_id": meeting.room_id,
            "participants": participants,
        }
        await self.store.save_task(task)

    # The former DM auto-resolve helpers (``_check_targets_done`` and
    # ``_synthesize_reply_from_completed_agents``) were removed along with
    # ``_reply_from_standby_profiles``. Blocking peer waits now resolve only
    # when a real agent replies — the sweeper in ``reactivation_sweeper.py``
    # guarantees that standby recipients are re-activated in time.

    async def refresh_waiting_tasks(self, tasks: list[Task]) -> list[Task]:
        resumed: list[Task] = []
        for task in tasks:
            if task.status != TaskStatus.AWAITING_PEER:
                continue
            wait = dict(task.metadata.get("peer_wait", {}))
            resolved = False
            if wait.get("kind") == "meeting":
                resolved = await self.resolve_task_meeting_wait(task)
            else:
                resolved = await self.resolve_task_peer_wait(task)
            if resolved:
                resumed.append(task)
        return resumed

    # --- Mode 2: Targeted Broadcast ---

    async def broadcast(self, message: AgentMessage) -> AgentMessage:
        """Send a targeted broadcast to multiple agents."""
        message.metadata = dict(message.metadata)
        message.metadata["broadcast"] = True
        return await self.send_dm(message)

    # --- Mode 3: Meeting Room ---

    async def create_meeting(self, meeting: MeetingRoom) -> MeetingRoom:
        task_record = await self.store.get_task(meeting.task_id) if meeting.task_id else None
        context = CollaborationContext.from_task(task_record, role_id=meeting.decision_owner)
        return await self._collaboration_service().create_meeting(context, meeting)

    async def respond_to_meeting(
        self,
        room_id: str,
        from_agent: str,
        content: str,
        finalize: bool = False,
        task: Task | None = None,
    ) -> MeetingRoom:
        context = CollaborationContext.from_task(task, role_id=from_agent)
        return await self._collaboration_service().respond_to_meeting(
            context,
            room_id=room_id,
            from_agent=from_agent,
            content=content,
            finalize=finalize,
            task=task,
        )

    async def open_meeting_wait(
        self,
        task: Task,
        topic: str,
        participants: list[str],
        agenda: list[str],
        shared_context: str = "",
        decision_owner: str | None = None,
        decision_policy: str = "semantic_consensus_then_owner",
        timeout_seconds: int = 900,
        risk_level: str = "normal",
    ) -> dict[str, Any]:
        context = CollaborationContext.from_task(task)
        return await self._collaboration_service().open_meeting_wait(
            context,
            task=task,
            topic=topic,
            participants=participants,
            agenda=agenda,
            shared_context=shared_context,
            decision_owner=decision_owner,
            decision_policy=decision_policy,
            timeout_seconds=timeout_seconds,
            risk_level=risk_level,
        )

    async def resolve_task_meeting_wait(self, task: Task) -> bool:
        wait = dict(task.metadata.get("peer_wait", {}))
        if wait.get("kind") != "meeting":
            return False
        room_id = wait.get("meeting_room_id")
        if not room_id:
            return False
        meeting = await self.store.get_meeting(room_id)
        if not meeting or meeting.status not in {MeetingStatus.DECIDED, MeetingStatus.CLOSED}:
            return False
        outcome = dict(meeting.outcome or {})
        resolution_status = str(outcome.get("resolution_status", "") or "").strip()
        if not resolution_status:
            resolution_status = "resolved" if str(outcome.get("decision", "") or "").strip() else "unresolved"
        task.status = TaskStatus.PENDING
        task.metadata = dict(task.metadata)
        task.metadata.pop("peer_wait", None)
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["meeting_outcome"] = outcome
        task.context_snapshot["meeting_decision_method"] = meeting.decision_method
        task.context_snapshot["meeting_resolution_status"] = resolution_status
        task.context_snapshot["meeting_consensus"] = dict(meeting.consensus or {})
        outcome_origin = str((meeting.metadata or {}).get("outcome_origin", "") or "live_meeting").strip()
        task.context_snapshot["meeting_outcome_origin"] = outcome_origin
        if resolution_status == "unresolved":
            task.context_snapshot["meeting_internal_retry"] = {
                "room_id": meeting.room_id,
                "reason": str(outcome.get("reasoning", "") or "The meeting ended without an internal decision.").strip(),
            }
        await self.store.save_task(task)
        if outcome_origin != "live_meeting":
            await self._record_non_live_collaboration(
                task,
                origin=outcome_origin,
                summary=f"Meeting `{room_id}` was resolved without a fully live discussion.",
            )
        return True

    async def run_meeting(
        self,
        meeting: MeetingRoom,
        agent_respond: Any = None,
    ) -> MeetingRoom:
        """Advance a meeting using independent participant turns."""
        meeting.metadata = dict(meeting.metadata)
        meeting.metadata.setdefault("outcome_origin", "autonomous_meeting")
        progressed, _ = await self._advance_meeting(
            meeting,
            run_missing_participants=True,
        )
        return progressed

    async def auto_resolve_stale_meetings(
        self,
        tasks: list[Task],
    ) -> list[str]:
        """Advance stalled meetings using independent participant turns."""
        progressed_rooms: list[str] = []
        for task in tasks:
            if task.status != TaskStatus.AWAITING_PEER:
                continue
            wait = dict(task.metadata.get("peer_wait", {}))
            if wait.get("kind") != "meeting":
                continue
            room_id = wait.get("meeting_room_id")
            if not room_id:
                continue
            meeting = await self.store.get_meeting(room_id)
            if not meeting or meeting.status in {MeetingStatus.DECIDED, MeetingStatus.CLOSED}:
                continue
            logger.info("Advancing meeting {} with autonomous participant turns", room_id)
            before_status = meeting.status
            before_round = meeting.current_round
            before_outcome = dict(meeting.outcome or {})
            updated = await self.run_meeting(meeting)
            if updated.status in {MeetingStatus.DECIDED, MeetingStatus.CLOSED}:
                await self.resolve_task_meeting_wait(task)
            if (
                updated.status != before_status
                or updated.current_round != before_round
                or dict(updated.outcome or {}) != before_outcome
            ):
                progressed_rooms.append(room_id)
        return progressed_rooms

    async def propose_task_adjustment(
        self,
        *,
        task: Task,
        summary: str,
        changeset: dict[str, Any],
    ) -> dict[str, Any]:
        context = CollaborationContext.from_task(task)
        return await self._collaboration_service().propose_task_adjustment(
            context,
            summary=summary,
            changeset=changeset,
        )

    async def build_agent_context(self, agent_id: str, task: Task | None = None, task_id: str | None = None) -> dict[str, Any]:
        active_task = task
        if active_task is None and task_id:
            active_task = await self.store.get_task(task_id)
        if active_task is not None:
            await self.rebuild_comms_projection(task=active_task)
        inbox = await self.read_inbox(
            agent_id=agent_id,
            task=active_task,
            task_id=task_id,
            unread_only=False,
            mark_read=False,
            limit=10,
        )
        task_comments: list[dict[str, Any]] = []
        if active_task:
            task_comments = list(active_task.comments)
        allowed_contacts: list[dict[str, Any]] = []
        if self.org_engine and agent_id and hasattr(self.org_engine, "build_contact_directory"):
            try:
                allowed_contacts = self.org_engine.build_contact_directory(agent_id, task=active_task)
            except TypeError:
                allowed_contacts = self.org_engine.build_contact_directory(agent_id)
        pending_handoffs: list[dict[str, Any]] = []
        if active_task:
            records = await self.store.get_handoff_records(
                project_id=active_task.project_id,
                task_id=active_task.id,
                limit=10,
            )
            pending_handoffs = [
                {
                    "handoff_id": record.handoff_id,
                    "from_role": record.from_role,
                    "summary": record.summary,
                    "status": record.status,
                    "requires_ack": record.requires_ack,
                }
                for record in records
                if record.status not in {"accepted", "rejected"} and record.requires_ack
            ]
        return {
            "inbox": inbox,
            "annotations": task_comments[-10:],
            "allowed_contacts": allowed_contacts,
            "pending_handoffs": pending_handoffs,
        }

    # --- Deadlock Detection ---

    async def detect_deadlocks(self, project_id: str) -> list[tuple[str, str]]:
        """Detect circular waiting patterns across ALL peer wait types.

        Covers meetings, DMs, comms-blocking, and dependency-based cycles
        (not just meetings as before).
        """
        tasks = await self.store.get_tasks(project_id=project_id)
        waiting_for: dict[str, set[str]] = {}
        # Build wait graph from all AWAITING_PEER tasks (all wait kinds)
        for task in tasks:
            if task.status != TaskStatus.AWAITING_PEER:
                continue
            role = task.assigned_to or task.metadata.get("work_item_role_id", "")
            wait = dict(task.metadata.get("peer_wait", {}))
            targets = set(wait.get("waiting_on_agents", []))
            if role and targets:
                waiting_for.setdefault(role, set()).update(targets)
        # Also detect dependency-based deadlocks: two PENDING tasks whose deps
        # form a cycle (shouldn't happen with correct DAG, but catch it)
        task_by_id = {t.id: t for t in tasks}
        for task in tasks:
            if task.status != TaskStatus.PENDING:
                continue
            role = task.assigned_to or task.metadata.get("work_item_role_id", "")
            if not role:
                continue
            for dep_id in (task.dependencies or []):
                dep_task = task_by_id.get(dep_id)
                if dep_task and dep_task.status == TaskStatus.PENDING:
                    dep_role = dep_task.assigned_to or dep_task.metadata.get("work_item_role_id", "")
                    if dep_role:
                        waiting_for.setdefault(role, set()).add(dep_role)
        # DFS cycle detection (supports cycles of any length, not just pairs)
        return self._find_wait_cycles(waiting_for)

    @staticmethod
    def _find_wait_cycles(graph: dict[str, set[str]]) -> list[tuple[str, str]]:
        """Find cycles in the wait-for graph using iterative DFS."""
        visited: set[str] = set()
        in_stack: set[str] = set()
        cycles: list[tuple[str, str]] = []
        seen_pairs: set[tuple[str, str]] = set()

        for start in graph:
            if start in visited:
                continue
            stack: list[tuple[str, list[str]]] = [(start, list(graph.get(start, set())))]
            in_stack.add(start)
            path: list[str] = [start]
            while stack:
                node, neighbors = stack[-1]
                if neighbors:
                    nxt = neighbors.pop()
                    if nxt in in_stack:
                        # Found a cycle — record pairwise edges in the cycle
                        cycle_start_idx = path.index(nxt) if nxt in path else -1
                        if cycle_start_idx >= 0:
                            cycle_nodes = path[cycle_start_idx:]
                            for i, cn in enumerate(cycle_nodes):
                                nn = cycle_nodes[(i + 1) % len(cycle_nodes)]
                                pair = tuple(sorted((cn, nn)))
                                if pair not in seen_pairs:
                                    seen_pairs.add(pair)
                                    cycles.append(pair)
                    elif nxt not in visited and nxt in graph:
                        in_stack.add(nxt)
                        path.append(nxt)
                        stack.append((nxt, list(graph.get(nxt, set()))))
                else:
                    stack.pop()
                    in_stack.discard(node)
                    if path and path[-1] == node:
                        path.pop()
                    visited.add(node)
        return cycles
