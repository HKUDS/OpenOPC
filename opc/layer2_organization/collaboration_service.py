"""Shared collaboration service for native and external agent flows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from opc.core.models import (
    AgentMessage,
    CommsSemanticType,
    MeetingRoom,
    MeetingStatus,
    MessageStatus,
    MessageUrgency,
    OPCEvent,
    Task,
    TaskStatus,
)
from opc.core.worker_envelope import classify_worker_message, worker_message_is_actionable
from opc.layer2_organization import comms as file_comms
from opc.layer2_organization.collaboration_policy import effective_contact_roles
from opc.layer2_organization.work_item_identity import work_item_identity_payload_for_task


class CommunicationDeliveryError(RuntimeError):
    """Raised when file-based collaboration delivery fails."""

    def __init__(self, message: str, *, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.payload = dict(payload or {})


@dataclass
class CollaborationContext:
    """Resolved collaboration identity and workspace scope."""

    role_id: str = ""
    task: Task | None = None
    task_id: str = ""
    project_id: str = "default"
    session_id: str = "default"
    workspace_root: str = ""
    output_root: str = ""
    comms_root: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_task(
        cls,
        task: Task | None,
        *,
        role_id: str = "",
    ) -> "CollaborationContext":
        if task is None:
            return cls(role_id=role_id)
        metadata = dict(task.metadata or {})
        workspace_root = (
            str(metadata.get("workspace_root", "") or "").strip()
            or str(metadata.get("comms_workspace_root", "") or "").strip()
            or str(metadata.get("output_root", "") or "").strip()
            or str(metadata.get("target_output_dir", "") or "").strip()
        )
        output_root = (
            str(metadata.get("output_root", "") or "").strip()
            or str(metadata.get("target_output_dir", "") or "").strip()
            or workspace_root
        )
        comms_root = str(metadata.get("comms_root", "") or "").strip()
        if not comms_root and workspace_root:
            try:
                comms_root = str(Path(workspace_root).expanduser().resolve() / ".opc-comms")
            except Exception:
                comms_root = ""
        return cls(
            role_id=str(
                role_id
                or task.assigned_to
                or metadata.get("work_item_role_id", "")
                or ""
            ).strip(),
            task=task,
            task_id=str(task.id or "").strip(),
            project_id=str(task.project_id or "default").strip() or "default",
            session_id=(
                str(task.parent_session_id or "").strip()
                or str(task.session_id or "").strip()
                or "default"
            ),
            workspace_root=workspace_root,
            output_root=output_root,
            comms_root=comms_root,
            metadata=metadata,
        )

    @classmethod
    def from_environment(
        cls,
        *,
        role_id: str,
        project_id: str,
        session_id: str,
        workspace_root: str,
        comms_root: str = "",
        task: Task | None = None,
        task_id: str = "",
    ) -> "CollaborationContext":
        resolved_comms_root = str(comms_root or "").strip()
        if not resolved_comms_root and workspace_root:
            try:
                resolved_comms_root = str(Path(workspace_root).expanduser().resolve() / ".opc-comms")
            except Exception:
                resolved_comms_root = ""
        return cls(
            role_id=str(role_id or "").strip(),
            task=task,
            task_id=str(task_id or getattr(task, "id", "") or "").strip(),
            project_id=str(project_id or "default").strip() or "default",
            session_id=str(session_id or "default").strip() or "default",
            workspace_root=str(workspace_root or "").strip(),
            output_root=str(workspace_root or "").strip(),
            comms_root=resolved_comms_root,
            metadata=dict(getattr(task, "metadata", {}) or {}),
        )


class CollaborationService:
    """Shared collaboration semantics over the file-based comms substrate."""

    def __init__(self, host: Any | None = None) -> None:
        self.host = host
        self.store = getattr(host, "store", None)
        self.event_bus = getattr(host, "event_bus", None)
        self.org_engine = getattr(host, "org_engine", None)

    async def resolve_task(self, context: CollaborationContext) -> Task | None:
        if context.task is not None:
            return context.task
        if not self.store or not context.task_id or not hasattr(self.store, "get_task"):
            return None
        task = await self.store.get_task(context.task_id)
        if task is not None:
            context.task = task
            enriched = CollaborationContext.from_task(task, role_id=context.role_id)
            context.role_id = context.role_id or enriched.role_id
            context.project_id = enriched.project_id
            context.session_id = enriched.session_id
            context.workspace_root = enriched.workspace_root
            context.output_root = enriched.output_root
            context.comms_root = enriched.comms_root
            context.metadata = dict(enriched.metadata)
        return context.task

    def _require_host(self) -> Any:
        if self.host is None:
            raise RuntimeError("CollaborationService host is required for this operation")
        return self.host

    def _layout(self, context: CollaborationContext, task: Task | None = None):
        active_task = task or context.task
        if active_task is not None and self.host is not None:
            layout = self.host._task_comms_layout(active_task)
            if layout is not None:
                return layout
        workspace_root = str(
            context.workspace_root
            or context.metadata.get("workspace_root", "")
            or context.metadata.get("comms_workspace_root", "")
            or context.metadata.get("output_root", "")
            or context.metadata.get("target_output_dir", "")
            or ""
        ).strip()
        if not workspace_root:
            return None
        try:
            return file_comms.resolve_layout(
                workspace_root,
                str(context.project_id or "default").strip() or "default",
                str(context.session_id or "default").strip() or "default",
            )
        except Exception:
            return None

    async def _record_failure(
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
        if self.host is not None and hasattr(self.host, "_record_comms_failure"):
            return await self.host._record_comms_failure(
                task,
                operation=operation,
                from_role=from_role,
                to_role=to_role,
                reason=reason,
                attempted_path=attempted_path,
                attempted_command=attempted_command,
            )
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
        if task is not None:
            task.metadata = dict(task.metadata or {})
            task.context_snapshot = dict(task.context_snapshot or {})
            failures = list(task.context_snapshot.get("comms_failures", []) or [])
            failures.append(dict(payload))
            task.context_snapshot["comms_failures"] = failures[-12:]
            task.context_snapshot["latest_comms_failure"] = dict(payload)
            task.metadata["comms_health"] = "degraded"
            if self.store and hasattr(self.store, "save_task"):
                await self.store.save_task(task)
        return payload

    async def _publish_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_bus is None or not hasattr(self.event_bus, "publish"):
            return
        await self.event_bus.publish(OPCEvent(event_type=event_type, payload=payload))

    def _validate_recipients(self, sender: str, recipients: list[str], task: Task | None = None) -> None:
        if not sender:
            return
        allowed = set(effective_contact_roles(sender, task=task, org_engine=self.org_engine))
        if not allowed:
            return
        invalid = [recipient for recipient in recipients if recipient and recipient != sender and recipient not in allowed]
        if invalid:
            raise ValueError(f"Role `{sender}` cannot message recipient(s): {', '.join(invalid)}")

    def _canonicalize_message(self, message: AgentMessage, task: Task | None = None) -> AgentMessage:
        host = self._require_host()
        return host._canonicalize_message(message, task=task)

    def _serialize_message(self, message: AgentMessage) -> dict[str, Any]:
        host = self._require_host()
        return host._serialize_message(message)

    def _message_frontmatter(self, message: AgentMessage, task: Task | None = None) -> dict[str, Any]:
        host = self._require_host()
        return host._message_frontmatter(message, task=task)

    async def send_dm(
        self,
        context: CollaborationContext,
        message: AgentMessage,
        *,
        task: Task | None = None,
        allow_policy_bypass: bool = False,
    ) -> AgentMessage:
        host = self._require_host()
        active_task = task or context.task
        if active_task is None and message.task_id and self.store and hasattr(self.store, "get_task"):
            active_task = await self.store.get_task(str(message.task_id))
        message = self._canonicalize_message(message, task=active_task)
        if (
            str(getattr(message.transport_kind, "value", message.transport_kind) or "").strip().lower() != "system"
            and not allow_policy_bypass
        ):
            self._validate_recipients(message.from_agent, message.to_agents, task=active_task)

        subject = (message.subject or "").strip()
        body = (message.body or "").strip()
        if not subject and not body:
            failure = await self._record_failure(
                active_task,
                operation="send_dm",
                from_role=message.from_agent,
                to_role=",".join(message.to_agents),
                reason="empty_message_rejected (subject and body are blank)",
            )
            raise CommunicationDeliveryError(
                "Refusing to send a blank message. Include a concrete request, decision, update, or reply before sending.",
                payload=failure,
            )

        layout = self._layout(context, active_task)
        if layout is None:
            delivered: list[AgentMessage] = []
            for recipient in list(message.to_agents):
                projected = AgentMessage(
                    msg_id=message.msg_id,
                    msg_type=message.msg_type,
                    from_agent=message.from_agent,
                    to_agents=[recipient],
                    subject=message.subject,
                    body=message.body,
                    context_ref=message.context_ref,
                    urgency=message.urgency,
                    reply_needed=message.reply_needed,
                    requires_ack=message.requires_ack,
                    timeout_action=message.timeout_action,
                    reply_to_msg_id=message.reply_to_msg_id,
                    task_id=message.task_id,
                    status=MessageStatus.DELIVERED,
                    timestamp=message.timestamp,
                    transport_kind=message.transport_kind,
                    semantic_type=message.semantic_type,
                    comms_state=message.comms_state,
                    correlation_id=message.correlation_id,
                    refs=dict(message.refs),
                    metadata={
                        **dict(message.metadata or {}),
                        "projection_source": "store_fallback",
                    },
                )
                projected = self._canonicalize_message(projected, task=active_task)
                if self.store and hasattr(self.store, "save_message"):
                    await self.store.save_message(projected)
                if hasattr(host, "_get_queue"):
                    host._get_queue(recipient).put_nowait(projected)
                delivered.append(projected)
            if delivered:
                primary = delivered[0]
                primary.metadata = dict(primary.metadata)
                primary.metadata["delivered_message_ids"] = [item.msg_id for item in delivered]
                message = primary
            await self._publish_event(
                "agent_message_sent",
                {
                    "msg_id": message.msg_id,
                    "from": message.from_agent,
                    "to": message.to_agents,
                    "type": message.msg_type,
                    "subject": message.subject,
                    "urgency": message.urgency.value,
                    "task_id": message.task_id,
                    "delivery": "store_fallback",
                },
            )
            # No main-key LLM impersonation here. If the recipient is on
            # standby (task DONE), CommsReactivationSweeper will re-open
            # that task so the recipient's own agent reads the inbox and
            # replies through the runtime mailbox. Senders that invoked
            # `ask_peer_and_wait` unblock on the real reply (or timeout).
            return message
        try:
            file_comms.ensure_layout(layout, [message.from_agent, *list(message.to_agents)])
        except Exception as exc:
            failure = await self._record_failure(
                active_task,
                operation="send_dm",
                from_role=message.from_agent,
                to_role=",".join(message.to_agents),
                reason=str(exc),
                attempted_path=str(layout.root),
            )
            raise CommunicationDeliveryError(str(exc), payload=failure) from exc

        delivered: list[AgentMessage] = []
        blocking = bool(message.reply_needed) and str((message.metadata or {}).get("wait_mode", "") or "").strip() == "ask_peer_and_wait"
        message.metadata = dict(message.metadata)
        message.metadata.setdefault("reply_origin", "live_request")

        import hashlib as _hashlib

        content_fp = _hashlib.sha1(
            f"{message.from_agent}|{subject}|{body}".encode("utf-8", errors="replace")
        ).hexdigest()[:16]

        for recipient in list(message.to_agents):
            try:
                frontmatter = {
                    **self._message_frontmatter(message, task=active_task),
                    "to_endpoint_type": host._infer_endpoint_type(recipient, message.metadata).value,
                }
                delivered_path = file_comms.send_message(
                    layout,
                    from_role=message.from_agent,
                    to_role=recipient,
                    subject=message.subject,
                    body=message.body,
                    blocking=blocking,
                    reply_to=message.reply_to_msg_id,
                    priority=message.urgency.value,
                    sent_at=message.timestamp.isoformat(),
                    idempotency_key=f"{recipient}:{content_fp}",
                    extra_frontmatter={
                        "task_id": message.task_id or "",
                        "context_ref": message.context_ref or "",
                        "requires_ack": bool(message.requires_ack),
                        "timeout_action": str(message.timeout_action or "").strip(),
                        "projection_source": "file_comms",
                        "content_fp": content_fp,
                        **frontmatter,
                    },
                )
                header = file_comms.read_header(delivered_path)
                if header is None:
                    raise RuntimeError(f"Failed to read comms header for `{delivered_path}`")
                projected = AgentMessage(
                    msg_id=header.message_id,
                    msg_type=message.msg_type,
                    from_agent=message.from_agent,
                    to_agents=[recipient],
                    subject=message.subject,
                    body=message.body,
                    context_ref=message.context_ref,
                    urgency=message.urgency,
                    reply_needed=message.reply_needed,
                    requires_ack=message.requires_ack,
                    timeout_action=message.timeout_action,
                    reply_to_msg_id=message.reply_to_msg_id,
                    task_id=message.task_id,
                    status=MessageStatus.DELIVERED,
                    timestamp=message.timestamp,
                    transport_kind=message.transport_kind,
                    semantic_type=message.semantic_type,
                    comms_state=message.comms_state,
                    correlation_id=message.correlation_id,
                    refs=dict(message.refs),
                    metadata={
                        **dict(message.metadata),
                        "projection_source": "file_comms",
                        "comms_path": str(delivered_path),
                    },
                )
                projected = self._canonicalize_message(projected, task=active_task)
                if self.store and hasattr(self.store, "save_message"):
                    await self.store.save_message(projected)
                if hasattr(host, "_get_queue"):
                    host._get_queue(recipient).put_nowait(projected)
                delivered.append(projected)
            except Exception as exc:
                failure = await self._record_failure(
                    active_task,
                    operation="send_dm",
                    from_role=message.from_agent,
                    to_role=recipient,
                    reason=str(exc),
                    attempted_path=str(layout.role_new_dir(recipient)),
                )
                raise CommunicationDeliveryError(str(exc), payload=failure) from exc

        if delivered:
            primary = delivered[0]
            primary.metadata = dict(primary.metadata)
            primary.metadata["delivered_message_ids"] = [item.msg_id for item in delivered]
            message = primary
        await self._publish_event(
            "agent_message_sent",
            {
                "msg_id": message.msg_id,
                "from": message.from_agent,
                "to": message.to_agents,
                "type": message.msg_type,
                "subject": message.subject,
                "urgency": message.urgency.value,
                "task_id": message.task_id,
            },
        )
        # No main-key LLM impersonation here either. Blocking DMs whose
        # recipient is on standby wake up via CommsReactivationSweeper
        # so the real agent can reply from its own session.
        return message

    async def ask_peer_and_wait(
        self,
        context: CollaborationContext,
        *,
        task: Task,
        to_agent: str,
        subject: str,
        body: str,
        timeout_action: str = "",
        timeout_seconds: int = 300,
        on_timeout: str = "continue",
    ) -> dict[str, Any]:
        target = str(to_agent or "").strip()
        if not target:
            raise ValueError("ask_peer_and_wait requires a recipient")
        host = self._require_host()
        await host._validate_blocking_recipients_ready(task, [target])
        message = AgentMessage(
            msg_type="question",
            from_agent=task.assigned_to,
            to_agents=[target],
            subject=subject,
            body=body,
            context_ref=task.id,
            task_id=task.id,
            urgency=MessageUrgency.BLOCKING,
            reply_needed=True,
            semantic_type=CommsSemanticType.BLOCKED_ON_DECISION,
            metadata={
                **work_item_identity_payload_for_task(task),
                "async_mailbox": False,
                "reply_requested": True,
                "wait_mode": "ask_peer_and_wait",
                "reply_origin": "live_request",
                "timeout_seconds": timeout_seconds,
                "execution_task_ids": list(task.metadata.get("execution_task_ids", [])),
            },
        )
        message = await self.send_dm(context, message, task=task)
        timeout_mode = str(on_timeout or "continue").strip().lower() or "continue"
        task.status = TaskStatus.AWAITING_PEER
        task.metadata = dict(task.metadata)
        task.metadata["peer_wait"] = {
            "kind": "peer_message",
            "message_id": message.msg_id,
            "waiting_on_agents": [target],
            "timeout_action": timeout_action,
            "timeout_at": (datetime.now() + timedelta(seconds=max(1, int(timeout_seconds or 1)))).isoformat(),
            "on_timeout": timeout_mode,
            "live_required": True,
            "reply_origin_required": "live_reply",
        }
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["pending_peer_message"] = self._serialize_message(message)
        if self.store and hasattr(self.store, "save_task"):
            await self.store.save_task(task)
        return {
            "requires_peer_wait": True,
            "reason": f"Waiting for a lightweight peer reply from `{target}`.",
            "message": self._serialize_message(message),
            "peer_wait": dict(task.metadata["peer_wait"]),
        }

    async def _generic_read_inbox(
        self,
        context: CollaborationContext,
        *,
        role_id: str,
        limit: int,
        archive: bool,
    ) -> list[dict[str, Any]]:
        layout = self._layout(context, context.task)
        if layout is None:
            return []
        active_seat_id = str((getattr(context.task, "metadata", {}) or {}).get("delegation_seat_id", "") or "").strip()
        headers = file_comms.list_unread(layout, role_id, limit=limit)
        messages: list[dict[str, Any]] = []
        paths_to_mark: list[Path] = []
        for header in headers:
            target_seat_id = str((header.raw_frontmatter or {}).get("target_seat_id", "") or "").strip()
            if active_seat_id and target_seat_id and target_seat_id != active_seat_id:
                continue
            _, body = file_comms.read_message(header.path)
            item = classify_worker_message(
                {
                    "msg_id": str(header.message_id or "").strip(),
                    "message_id": str(header.message_id or "").strip(),
                    "from_agent": str(header.from_role or "").strip(),
                    "to_agent": str(header.to_role or "").strip(),
                    "from": str(header.from_role or "").strip(),
                    "subject": str(header.subject or "").strip(),
                    "body": str(body or "").strip(),
                    "reply_needed": bool(header.blocking),
                    "requires_ack": bool((header.raw_frontmatter or {}).get("requires_ack", False)),
                    "urgency": str(header.priority or "").strip() or "normal",
                    "transport_kind": str(header.raw_frontmatter.get("transport_kind", "") or "").strip(),
                    "semantic_type": str(
                        header.raw_frontmatter.get("semantic_type")
                        or header.raw_frontmatter.get("kind")
                        or ""
                    ).strip(),
                    "metadata": {
                        **dict(header.raw_frontmatter or {}),
                        "comms_path": str(header.path),
                    },
                    "status": MessageStatus.DELIVERED.value,
                }
            )
            messages.append(item)
            paths_to_mark.append(header.path)
        if archive and paths_to_mark:
            moved = file_comms.mark_seen(layout, role_id, paths_to_mark)
            moved_names = {path.name for path in moved}
            for item in messages:
                path = str((item.get("metadata", {}) or {}).get("comms_path", "") or "").strip()
                if not path:
                    continue
                if Path(path).name in moved_names:
                    item["status"] = MessageStatus.READ.value
        return messages

    async def read_inbox(
        self,
        context: CollaborationContext,
        *,
        agent_id: str,
        task_id: str | None = None,
        task_ids: list[str] | None = None,
        task: Task | None = None,
        unread_only: bool = True,
        limit: int = 10,
        mark_read: bool = True,
    ) -> list[dict[str, Any]]:
        active_task = task or context.task
        scope_ids = []
        host = self.host
        if host is not None and hasattr(host, "_task_scope_ids"):
            scope_ids = host._task_scope_ids(task=task, task_id=task_id, task_ids=task_ids)
        if active_task is None and task_id and self.store and hasattr(self.store, "get_task"):
            active_task = await self.store.get_task(task_id)
        if active_task is None and scope_ids and self.store and hasattr(self.store, "get_task"):
            for scope_id in scope_ids:
                active_task = await self.store.get_task(scope_id)
                if active_task is not None:
                    break
        if active_task is not None:
            context.task = active_task
            context = CollaborationContext.from_task(active_task, role_id=context.role_id or agent_id)
        layout = self._layout(context, active_task)
        if host is not None and layout is not None and active_task is not None and hasattr(host, "_project_comms_messages"):
            await host.rebuild_comms_projection(task=active_task, layout=layout)
            messages = await host._project_comms_messages(
                layout,
                role_id=agent_id,
                task=active_task,
                unread_only=unread_only,
                limit=limit,
                mark_read=mark_read,
            )
            if mark_read:
                for item in messages:
                    handoff_id = str((item.get("metadata", {}) or {}).get("handoff_id", "") or "").strip()
                    if handoff_id and bool(item.get("requires_ack", False)) and self.store and hasattr(self.store, "update_handoff_record"):
                        await self.store.update_handoff_record(
                            handoff_id,
                            status="received",
                            received_at=datetime.now(),
                        )
                if self.store and hasattr(self.store, "get_handoff_records"):
                    sent_handoffs = await self.store.get_handoff_records(
                        project_id=str(active_task.project_id or "default").strip() or "default",
                        task_id=active_task.id,
                        status="sent",
                        limit=50,
                    )
                    for record in sent_handoffs:
                        if str(record.to_role or "").strip() != str(agent_id or "").strip():
                            continue
                        if not bool(record.requires_ack):
                            continue
                        await self.store.update_handoff_record(
                            record.handoff_id,
                            status="received",
                            received_at=datetime.now(),
                        )
            return messages
        if layout is not None and unread_only:
            return await self._generic_read_inbox(
                context,
                role_id=agent_id,
                limit=limit,
                archive=mark_read,
            )
        if self.store and hasattr(self.store, "get_messages_for_agent"):
            messages = await self.store.get_messages_for_agent(
                agent_id=agent_id,
                task_id=task_id,
                task_ids=scope_ids,
                unread_only=unread_only,
                limit=limit,
            )
            if mark_read:
                for message in messages:
                    if message.status in {MessageStatus.SENT, MessageStatus.DELIVERED}:
                        message.status = MessageStatus.READ
                        message.processed_at = datetime.now()
                        if hasattr(self.store, "save_message"):
                            await self.store.save_message(message)
                if active_task is not None and hasattr(self.store, "get_handoff_records"):
                    sent_handoffs = await self.store.get_handoff_records(
                        project_id=str(active_task.project_id or "default").strip() or "default",
                        task_id=active_task.id,
                        status="sent",
                        limit=50,
                    )
                    for record in sent_handoffs:
                        if str(record.to_role or "").strip() != str(agent_id or "").strip():
                            continue
                        if not bool(record.requires_ack):
                            continue
                        await self.store.update_handoff_record(
                            record.handoff_id,
                            status="received",
                            received_at=datetime.now(),
                        )
            return [self._serialize_message(message) for message in messages]
        return []

    @staticmethod
    def _message_id(item: dict[str, Any]) -> str:
        return str(item.get("msg_id", "") or item.get("message_id", "") or "").strip()

    @staticmethod
    def _inbox_summary_payload(message: dict[str, Any]) -> dict[str, Any]:
        metadata = dict(message.get("metadata", {}) or {})
        return {
            "msg_id": CollaborationService._message_id(message),
            "from_agent": str(message.get("from_agent", "") or message.get("from", "")).strip(),
            "subject": str(message.get("subject", "") or "").strip(),
            "reply_needed": bool(message.get("reply_needed", False)),
            "requires_ack": bool(message.get("requires_ack", False)),
            "urgency": str(message.get("urgency", "") or "normal").strip() or "normal",
            "message_class": str(message.get("message_class", "") or metadata.get("message_class", "") or "").strip(),
            "protocol_type": str(message.get("protocol_type", "") or metadata.get("protocol_type", "") or "").strip(),
            "notification_kind": str(message.get("notification_kind", "") or metadata.get("notification_kind", "") or "").strip(),
            "transport_kind": str(message.get("transport_kind", "") or metadata.get("transport_kind", "") or "").strip(),
            "semantic_type": str(message.get("semantic_type", "") or metadata.get("semantic_type", "") or "").strip(),
        }

    @staticmethod
    def _inbox_message_payload(message: dict[str, Any]) -> dict[str, Any]:
        payload = CollaborationService._inbox_summary_payload(message)
        payload["body"] = str(message.get("body", "") or "").strip()
        payload["status"] = str(message.get("status", "") or "").strip()
        payload["metadata"] = dict(message.get("metadata", {}) or {})
        return payload

    async def ack_inbox_messages(
        self,
        context: CollaborationContext,
        *,
        agent_id: str,
        message_ids: list[str],
        task: Task | None = None,
        ignore_active_seat: bool = False,
    ) -> dict[str, Any]:
        """Explicitly mark selected messages as handled for one role."""
        active_task = task or context.task
        if active_task is not None:
            context = CollaborationContext.from_task(active_task, role_id=context.role_id or agent_id)
        target_ids = {
            str(item).strip()
            for item in list(message_ids or [])
            if str(item).strip()
        }
        if not target_ids:
            return {"acked": [], "already_seen": [], "missing": [], "count": 0}

        acked: set[str] = set()
        already_seen: set[str] = set()
        layout = self._layout(context, active_task)
        if layout is not None:
            active_seat_id = str((getattr(active_task, "metadata", {}) or {}).get("delegation_seat_id", "") or "").strip()
            unread_headers = file_comms.list_unread(layout, agent_id)
            paths_to_mark: list[Path] = []
            header_by_name: dict[str, Any] = {}
            for header in unread_headers:
                msg_id = str(header.message_id or "").strip()
                if msg_id not in target_ids:
                    continue
                target_seat_id = str((header.raw_frontmatter or {}).get("target_seat_id", "") or "").strip()
                if not ignore_active_seat and active_seat_id and target_seat_id and target_seat_id != active_seat_id:
                    continue
                paths_to_mark.append(header.path)
                header_by_name[header.path.name] = header
            moved = file_comms.mark_seen(layout, agent_id, paths_to_mark) if paths_to_mark else []
            moved_names = {path.name for path in moved}
            for name in moved_names:
                header = header_by_name.get(name)
                if header is not None:
                    acked.add(str(header.message_id or "").strip())

            seen_headers = file_comms.list_role_messages(
                layout,
                agent_id,
                include_new=False,
                include_seen=True,
                include_outbox=False,
            )
            for header in seen_headers:
                msg_id = str(header.message_id or "").strip()
                if msg_id in target_ids and msg_id not in acked:
                    already_seen.add(msg_id)

        if self.store and hasattr(self.store, "get_message"):
            for msg_id in sorted(target_ids):
                try:
                    message = await self.store.get_message(msg_id)
                except Exception:
                    message = None
                if message is None:
                    continue
                recipients = {str(item).strip() for item in list(message.to_agents or []) if str(item).strip()}
                if str(agent_id or "").strip() not in recipients:
                    continue
                if message.status in {MessageStatus.SENT, MessageStatus.DELIVERED}:
                    message.status = MessageStatus.READ
                    message.processed_at = datetime.now()
                    if hasattr(self.store, "save_message"):
                        await self.store.save_message(message)
                    acked.add(msg_id)
                elif message.status in {MessageStatus.READ, MessageStatus.REPLIED}:
                    if msg_id not in acked:
                        already_seen.add(msg_id)

        missing = sorted(target_ids - acked - already_seen)
        return {
            "acked": sorted(acked),
            "already_seen": sorted(already_seen),
            "missing": missing,
            "count": len(acked),
        }

    @staticmethod
    def _frontmatter_ref_values(frontmatter: dict[str, Any]) -> set[str]:
        refs: set[str] = set()
        for source in (
            frontmatter,
            dict(frontmatter.get("refs", {}) or {}),
            dict(frontmatter.get("metadata", {}) or {}),
        ):
            for key in (
                "work_item_id",
                "source_work_item_id",
                "target_work_item_id",
                "parent_work_item_id",
                "projection_id",
                "origin_projection_id",
                "source_projection_id",
                "target_projection_id",
                "task_id",
                "origin_task_id",
                "context_ref",
            ):
                value = str(source.get(key, "") or "").strip()
                if value:
                    refs.add(value)
        return refs

    @staticmethod
    def _frontmatter_semantic_type(frontmatter: dict[str, Any]) -> str:
        return str(
            frontmatter.get("semantic_type")
            or frontmatter.get("kind")
            or frontmatter.get("protocol_type")
            or frontmatter.get("notification_kind")
            or ""
        ).strip().lower()

    async def ack_inbox_messages_by_refs(
        self,
        context: CollaborationContext,
        *,
        agent_id: str,
        work_item_ids: list[str] | None = None,
        projection_ids: list[str] | None = None,
        task_ids: list[str] | None = None,
        semantic_types: list[str] | None = None,
        task: Task | None = None,
    ) -> dict[str, Any]:
        """Mark lifecycle messages handled by role + work/projection/task refs.

        This is an internal lifecycle cleanup path. It intentionally bypasses
        the active-seat filter used by normal role tools, but it never leaves
        the current role's inbox and only moves messages whose frontmatter refs
        match the completed review/report work.
        """
        active_task = task or context.task
        if active_task is not None:
            context = CollaborationContext.from_task(active_task, role_id=context.role_id or agent_id)
        target_refs = {
            str(item).strip()
            for items in (work_item_ids or [], projection_ids or [], task_ids or [])
            for item in list(items or [])
            if str(item).strip()
        }
        allowed_semantics = {
            str(item).strip().lower()
            for item in list(semantic_types or [])
            if str(item).strip()
        }
        if not target_refs:
            return {"acked": [], "already_seen": [], "missing": [], "count": 0}

        acked: set[str] = set()
        layout = self._layout(context, active_task)
        if layout is not None:
            headers = file_comms.list_unread(layout, agent_id)
            paths_to_mark: list[Path] = []
            header_by_name: dict[str, Any] = {}
            for header in headers:
                frontmatter = dict(header.raw_frontmatter or {})
                if allowed_semantics:
                    semantic = self._frontmatter_semantic_type(frontmatter)
                    protocol = str(frontmatter.get("protocol_type", "") or "").strip().lower()
                    notification = str(frontmatter.get("notification_kind", "") or "").strip().lower()
                    if not ({semantic, protocol, notification} & allowed_semantics):
                        continue
                if not (self._frontmatter_ref_values(frontmatter) & target_refs):
                    continue
                paths_to_mark.append(header.path)
                header_by_name[header.path.name] = header
            moved = file_comms.mark_seen(layout, agent_id, paths_to_mark) if paths_to_mark else []
            for path in moved:
                header = header_by_name.get(path.name)
                if header is not None:
                    acked.add(str(header.message_id or "").strip())

        if self.store and hasattr(self.store, "get_messages_for_agent") and hasattr(self.store, "save_message"):
            try:
                messages = await self.store.get_messages_for_agent(
                    agent_id=agent_id,
                    unread_only=True,
                    limit=200,
                )
            except Exception:
                messages = []
            for message in messages:
                metadata = dict(getattr(message, "metadata", {}) or {})
                message_refs = self._frontmatter_ref_values({
                    **metadata,
                    "task_id": getattr(message, "task_id", "") or "",
                    "context_ref": getattr(message, "context_ref", "") or "",
                })
                if not (message_refs & target_refs):
                    continue
                semantic = str(getattr(getattr(message, "semantic_type", ""), "value", getattr(message, "semantic_type", "")) or "").strip().lower()
                if allowed_semantics and semantic not in allowed_semantics and str(metadata.get("protocol_type", "") or "").strip().lower() not in allowed_semantics:
                    continue
                if message.status in {MessageStatus.SENT, MessageStatus.DELIVERED}:
                    message.status = MessageStatus.READ
                    message.processed_at = datetime.now()
                    await self.store.save_message(message)
                    acked.add(str(message.msg_id or "").strip())

        return {
            "acked": sorted(item for item in acked if item),
            "already_seen": [],
            "missing": [],
            "count": len([item for item in acked if item]),
        }

    async def inbox(
        self,
        context: CollaborationContext,
        *,
        agent_id: str,
        action: str = "status",
        message_ids: list[str] | None = None,
        limit: int = 10,
        task: Task | None = None,
    ) -> dict[str, Any]:
        """Compact role mailbox surface: status, peek, or explicit ack."""
        normalized_action = str(action or "status").strip().lower() or "status"
        active_task = task or context.task
        if active_task is not None:
            context = CollaborationContext.from_task(active_task, role_id=context.role_id or agent_id)
        if normalized_action == "ack":
            ack = await self.ack_inbox_messages(
                context,
                agent_id=agent_id,
                message_ids=list(message_ids or []),
                task=active_task,
            )
            status = await self.inbox(
                context,
                agent_id=agent_id,
                action="status",
                limit=limit,
                task=active_task,
            )
            return {"action": "ack", **ack, "status": status}
        if normalized_action not in {"status", "peek"}:
            raise ValueError("inbox action must be one of: status, peek, ack")

        read_limit = max(1, int(limit or 10))
        messages = await self.read_inbox(
            context,
            agent_id=agent_id,
            task=active_task,
            task_id=context.task_id or None,
            unread_only=True,
            limit=max(read_limit, 50 if normalized_action == "status" else read_limit),
            mark_read=False,
        )
        classified = [classify_worker_message(dict(item)) for item in messages if isinstance(item, dict)]
        actionable = [item for item in classified if worker_message_is_actionable(item)]
        blocking = [
            item for item in actionable
            if bool(item.get("reply_needed", False))
            or str(item.get("urgency", "") or "").strip().lower() == MessageUrgency.BLOCKING.value
        ]
        if normalized_action == "peek":
            shown = actionable[:read_limit]
            return {
                "action": "peek",
                "count": len(shown),
                "unread_count": len(classified),
                "actionable_count": len(actionable),
                "blocking_count": len(blocking),
                "messages": [self._inbox_message_payload(item) for item in shown],
            }
        summaries = [self._inbox_summary_payload(item) for item in actionable[: min(3, read_limit)]]
        return {
            "action": "status",
            "unread_count": len(classified),
            "actionable_count": len(actionable),
            "blocking_count": len(blocking),
            "has_actionable_unread": bool(actionable),
            "latest_unread_summary": summaries,
        }

    async def prepare_inbox_for_resume(
        self,
        context: CollaborationContext,
        *,
        seen_ids: set[str],
        limit: int = 6,
        backlog_key: str = "broker_pending_inbox",
    ) -> list[dict[str, Any]]:
        task = context.task or await self.resolve_task(context)
        if task is None:
            return []
        role_id = str(
            context.role_id
            or task.assigned_to
            or task.metadata.get("work_item_role_id", "")
            or ""
        ).strip()
        if not role_id:
            return []
        messages = await self.read_inbox(
            CollaborationContext.from_task(task, role_id=role_id),
            agent_id=role_id,
            task=task,
            unread_only=True,
            limit=limit,
            mark_read=False,
        )
        fresh = [
            dict(item)
            for item in messages
            if str(item.get("msg_id", "")).strip()
            and str(item.get("msg_id", "")).strip() not in seen_ids
        ]
        if not fresh:
            return []
        for item in fresh:
            seen_ids.add(str(item.get("msg_id", "")).strip())
        task.context_snapshot = dict(task.context_snapshot)
        backlog = [
            dict(item)
            for item in list(task.context_snapshot.get(backlog_key, []) or [])
            if isinstance(item, dict)
        ]
        backlog.extend(fresh)
        task.context_snapshot[backlog_key] = backlog[-12:]
        task.context_snapshot["latest_broker_inbox"] = fresh[-1]
        if self.store and hasattr(self.store, "save_task"):
            await self.store.save_task(task)
        return fresh

    def _find_inbox_message(
        self,
        context: CollaborationContext,
        role_id: str,
        message_id: str,
    ) -> tuple[Any | None, str]:
        layout = self._layout(context, context.task)
        if layout is None:
            return None, ""
        headers = file_comms.list_unread(layout, role_id)
        headers += file_comms.list_role_messages(layout, role_id, include_new=False, include_seen=True)
        for header in headers:
            if str(header.message_id or "").strip() != str(message_id or "").strip():
                continue
            return file_comms.read_message(header.path)
        return None, ""

    async def reply_message(
        self,
        context: CollaborationContext,
        *,
        original_msg_id: str,
        from_agent: str,
        body: str,
        subject: str = "",
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        original_metadata_updates: dict[str, Any] | None = None,
    ) -> AgentMessage:
        original = None
        if self.store and hasattr(self.store, "get_message"):
            original = await self.store.get_message(original_msg_id)
        header = None
        original_body = ""
        if original is None:
            header, original_body = self._find_inbox_message(context, from_agent, original_msg_id)
            if header is None:
                raise ValueError(f"Original message `{original_msg_id}` not found")
        if original is not None:
            recipients = {str(item).strip() for item in list(original.to_agents or []) if str(item).strip()}
            if from_agent not in recipients:
                raise ValueError(f"Role `{from_agent}` cannot reply to message `{original_msg_id}`")
            original.metadata = dict(original.metadata)
            if original_metadata_updates:
                original.metadata.update(dict(original_metadata_updates))
            original_from = original.from_agent
            original_subject = original.subject
            original_context_ref = original.context_ref or original.task_id
            original_task_id = task_id or original.task_id
            reply_semantic_type = dict(original.metadata or {}).get("reply_semantic_type")
        else:
            if str(header.to_role or "").strip() != from_agent:
                raise ValueError(f"Role `{from_agent}` cannot reply to message `{original_msg_id}`")
            original_from = str(header.from_role or "").strip()
            original_subject = str(header.subject or "").strip()
            original_context_ref = context.task_id or None
            original_task_id = task_id or context.task_id or None
            reply_semantic_type = dict(header.raw_frontmatter or {}).get("reply_semantic_type")
        reply_metadata = {"in_reply_to_subject": original_subject}
        if metadata:
            reply_metadata.update(dict(metadata))
        reply_metadata.setdefault("reply_origin", "live_reply")
        if original_body and "original_body" not in reply_metadata:
            reply_metadata["original_body"] = original_body
        host = self._require_host()
        reply = AgentMessage(
            msg_type="answer" if bool(getattr(original, "reply_needed", header.blocking if header else False)) else "inform",
            from_agent=from_agent,
            to_agents=[original_from],
            subject=subject or f"Re: {original_subject}",
            body=body,
            context_ref=original_context_ref,
            task_id=original_task_id,
            urgency=MessageUrgency.NORMAL,
            reply_to_msg_id=original_msg_id,
            semantic_type=host._coerce_semantic_type(
                reply_semantic_type,
                fallback=CommsSemanticType.WORK_UPDATE,
            ),
            metadata=reply_metadata,
        )
        reply = await self.send_dm(
            context,
            reply,
            task=context.task,
            allow_policy_bypass=True,
        )
        if original is not None:
            original.status = MessageStatus.REPLIED
            original.processed_at = datetime.now()
            if self.store and hasattr(self.store, "save_message"):
                await self.store.save_message(original)
        await self.ack_inbox_messages(
            context,
            agent_id=from_agent,
            message_ids=[original_msg_id],
            task=context.task,
        )
        await self._publish_event(
            "agent_message_replied",
            {
                "msg_id": original_msg_id,
                "reply_msg_id": reply.msg_id,
                "task_id": reply.task_id,
            },
        )
        return reply

    async def create_meeting(
        self,
        context: CollaborationContext,
        meeting: MeetingRoom,
    ) -> MeetingRoom:
        task_record = context.task
        if task_record is None and meeting.task_id and self.store and hasattr(self.store, "get_task"):
            task_record = await self.store.get_task(meeting.task_id)
        layout = self._layout(context, task_record)
        try:
            if layout is not None:
                file_comms.ensure_layout(layout, [meeting.decision_owner, *list(meeting.participants)])
                state = file_comms.start_meeting(
                    layout,
                    meeting_id=meeting.room_id,
                    topic=meeting.topic,
                    organizer=meeting.decision_owner,
                    participants=meeting.participants,
                    extra={"task_id": meeting.task_id or ""},
                )
                meeting.room_id = state.meeting_id
        except Exception as exc:
            failure = await self._record_failure(
                task_record,
                operation="start_meeting",
                from_role=meeting.decision_owner,
                to_role=",".join(meeting.participants),
                reason=str(exc),
                attempted_path=str(layout.meeting_dir(meeting.room_id)) if layout is not None else "",
            )
            raise CommunicationDeliveryError(str(exc), payload=failure) from exc
        meeting.status = MeetingStatus.OPEN
        meeting.current_round = max(1, int(meeting.current_round or 1))
        meeting.pending_participants = list(meeting.pending_participants or meeting.participants)
        meeting.last_activity_at = datetime.now()
        meeting.updated_at = datetime.now()
        timeout_seconds = max(60, int(meeting.metadata.get("timeout_seconds", 900) or 900))
        if meeting.deadline_at is None:
            meeting.deadline_at = datetime.now() + timedelta(seconds=timeout_seconds)
        if self.host is not None and hasattr(self.host, "_meetings"):
            self.host._meetings[meeting.room_id] = meeting
        if self.store and hasattr(self.store, "save_meeting"):
            await self.store.save_meeting(meeting)
        invite_body = (
            f"{meeting.shared_context}\n\n"
            f"Meeting room: {meeting.room_id}\n"
            f"Topic: {meeting.topic}\n"
            "Agenda:\n"
            + "\n".join(f"- {item}" for item in meeting.agenda)
            + "\n\nUse `respond_meeting` with a structured JSON stance when you are ready."
        ).strip()
        invite_context = CollaborationContext.from_task(task_record, role_id=meeting.decision_owner)
        for participant in meeting.participants:
            invite = AgentMessage(
                msg_type="decision_needed",
                from_agent=meeting.decision_owner,
                to_agents=[participant],
                subject=f"Meeting Invite: {meeting.topic}",
                body=invite_body,
                context_ref=meeting.room_id,
                task_id=meeting.task_id,
                urgency=MessageUrgency.HIGH,
                metadata={
                    "meeting_room_id": meeting.room_id,
                    "agenda": list(meeting.agenda),
                    "meeting_round": meeting.current_round,
                    "decision_policy": str(meeting.metadata.get("decision_policy", "") or "semantic_consensus_then_owner"),
                },
            )
            await self.send_dm(invite_context, invite, task=task_record)
        await self._publish_event(
            "meeting_started",
            {
                "room_id": meeting.room_id,
                "task_id": meeting.task_id,
                "topic": meeting.topic,
                "participants": meeting.participants,
            },
        )
        return meeting

    async def respond_to_meeting(
        self,
        context: CollaborationContext,
        *,
        room_id: str,
        from_agent: str,
        content: str,
        finalize: bool = False,
        task: Task | None = None,
    ) -> MeetingRoom:
        if self.store is None or not hasattr(self.store, "get_meeting"):
            raise RuntimeError("Meetings require a task store")
        host = self._require_host()
        meeting = await self.store.get_meeting(room_id)
        if not meeting:
            raise ValueError(f"Meeting `{room_id}` not found")
        task_record = task or (await self.store.get_task(meeting.task_id) if meeting.task_id and hasattr(self.store, "get_task") else None)
        layout = self._layout(CollaborationContext.from_task(task_record, role_id=from_agent), task_record)
        if finalize:
            if str(from_agent).strip() != str(meeting.decision_owner).strip():
                raise ValueError("Only the meeting decision owner can finalize a meeting directly.")
            manual_outcome = host._coerce_meeting_outcome(
                content,
                decision_method="manual_owner_finalize",
                consensus=meeting.consensus,
            )
            if not manual_outcome.get("decision") and not host._extract_json_object(content):
                manual_outcome["decision"] = str(content or "").strip()
                manual_outcome["resolution_status"] = "resolved"
            meeting.metadata = dict(meeting.metadata)
            meeting.metadata["outcome_origin"] = "live_meeting"
            if layout is not None:
                try:
                    file_comms.close_meeting(
                        layout,
                        meeting_id=meeting.room_id,
                        decision=str(manual_outcome.get("decision", "") or str(content or "").strip()),
                        closed_by=from_agent,
                    )
                except Exception as exc:
                    failure = await self._record_failure(
                        task_record,
                        operation="close_meeting",
                        from_role=from_agent,
                        to_role=",".join(meeting.participants),
                        reason=str(exc),
                        attempted_path=str(layout.meeting_dir(meeting.room_id)),
                    )
                    raise CommunicationDeliveryError(str(exc), payload=failure) from exc
            return await host._finalize_meeting(
                meeting,
                outcome=manual_outcome,
                transcript_note=str(manual_outcome.get("decision", "") or manual_outcome.get("reasoning", "")).strip(),
            )
        if layout is not None:
            try:
                file_comms.append_to_transcript(
                    layout,
                    meeting_id=meeting.room_id,
                    author=from_agent,
                    content=content,
                )
            except Exception as exc:
                failure = await self._record_failure(
                    task_record,
                    operation="respond_meeting",
                    from_role=from_agent,
                    to_role=meeting.decision_owner,
                    reason=str(exc),
                    attempted_path=str(layout.meeting_dir(meeting.room_id) / "transcript.md"),
                )
                raise CommunicationDeliveryError(str(exc), payload=failure) from exc
        host._ensure_meeting_round_state(meeting)
        host._append_meeting_participant_turn(
            meeting,
            from_agent=from_agent,
            content=content,
            source="live_agent",
            round_no=meeting.current_round,
        )
        await self.store.save_meeting(meeting)
        progressed, _ = await host._advance_meeting(
            meeting,
            run_missing_participants=False,
        )
        return progressed

    async def open_meeting_wait(
        self,
        context: CollaborationContext,
        *,
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
        meeting = await self.create_meeting(
            CollaborationContext.from_task(task, role_id=context.role_id),
            MeetingRoom(
                task_id=task.id,
                topic=topic,
                participants=participants,
                shared_context=shared_context,
                agenda=agenda,
                decision_owner=decision_owner or task.assigned_to or task.metadata.get("work_item_role_id", "coordinator"),
                metadata={
                    "decision_policy": decision_policy,
                    "timeout_seconds": max(60, int(timeout_seconds or 60)),
                    "risk_level": str(risk_level or "normal").strip() or "normal",
                },
            ),
        )
        task.status = TaskStatus.AWAITING_PEER
        task.metadata = dict(task.metadata)
        task.metadata["peer_wait"] = {
            "kind": "meeting",
            "meeting_room_id": meeting.room_id,
            "waiting_on_agents": list(participants),
        }
        task.context_snapshot = dict(task.context_snapshot)
        task.context_snapshot["active_meeting"] = {
            "room_id": meeting.room_id,
            "topic": meeting.topic,
            "agenda": list(meeting.agenda),
            "decision_owner": meeting.decision_owner,
            "decision_policy": meeting.metadata.get("decision_policy", decision_policy),
        }
        if self.store and hasattr(self.store, "save_task"):
            await self.store.save_task(task)
        return {
            "requires_peer_wait": True,
            "reason": f"Waiting for meeting `{meeting.topic}` outcome.",
            "meeting_room_id": meeting.room_id,
            "peer_wait": dict(task.metadata["peer_wait"]),
        }

    async def propose_task_adjustment(
        self,
        context: CollaborationContext,
        *,
        summary: str,
        changeset: dict[str, Any],
    ) -> dict[str, Any]:
        task = context.task or await self.resolve_task(context)
        role_id = str(
            context.role_id
            or getattr(task, "assigned_to", "")
            or (task.metadata.get("work_item_role_id", "") if task else "")
            or ""
        ).strip()
        if task is None or not role_id:
            raise ValueError("propose_task_adjustment requires an active assigned task")
        suggester = getattr(self.host, "task_adjustment_suggester", None) if self.host is not None else None
        if suggester is None:
            raise RuntimeError("Runtime replan support is not configured")
        result = await suggester(
            project_id=task.project_id,
            source_role_id=role_id,
            summary=summary,
            changeset=changeset,
            session_id=task.parent_session_id or task.session_id,
            task_id=task.id,
        )
        proposal = result["proposal"]
        persisted_task = None
        if self.store and hasattr(self.store, "get_task"):
            persisted_task = await self.store.get_task(task.id)
        if persisted_task is not None:
            # The proposal/card transaction owns the pending marker, and an
            # immediate owner answer may already have cleared it.  Refresh the
            # caller's live Task view instead of saving its pre-publication
            # snapshot back over the durable decision.
            task.status = persisted_task.status
            task.result = persisted_task.result
            task.assigned_to = persisted_task.assigned_to
            task.context_snapshot = dict(persisted_task.context_snapshot or {})
            task.metadata = dict(persisted_task.metadata or {})
        if result.get("auto_applied"):
            if persisted_task is None:
                task.metadata = dict(task.metadata)
                task.metadata["reorg_proposal_id"] = proposal.proposal_id
                task.metadata.pop("pending_reorg_proposal_id", None)
                task.metadata.pop("pending_reorg_scope", None)
            if (
                persisted_task is None
                and self.store
                and hasattr(self.store, "save_task")
            ):
                await self.store.save_task(task)
            return {
                "proposal_id": proposal.proposal_id,
                "scope": proposal.scope.value,
                "status": proposal.status.value,
                "auto_applied": True,
                "result": result.get("result", {}),
            }
        if persisted_task is None:
            task.metadata = dict(task.metadata)
            task.metadata["pending_reorg_proposal_id"] = proposal.proposal_id
            task.metadata["pending_reorg_scope"] = proposal.scope.value
        if (
            persisted_task is None
            and self.store
            and hasattr(self.store, "save_task")
        ):
            await self.store.save_task(task)
        proposal_status = str(
            getattr(proposal.status, "value", proposal.status) or ""
        ).strip()
        if proposal_status != "proposed":
            return {
                "proposal_id": proposal.proposal_id,
                "scope": proposal.scope.value,
                "status": proposal_status,
                "auto_applied": False,
                "requires_user_input": False,
                "reason": (
                    f"Runtime adjustment `{proposal.proposal_id}` was already "
                    f"decided with status `{proposal_status}`."
                ),
            }
        return {
            "proposal_id": proposal.proposal_id,
            "scope": proposal.scope.value,
            "status": proposal_status,
            "auto_applied": False,
            "requires_user_input": True,
            "reason": (
                f"Proposed runtime adjustment `{proposal.proposal_id}`. "
                "Review the replan details and reply `approve` or `deny` to continue."
            ),
        }

    async def list_colleagues(self, context: CollaborationContext) -> dict[str, Any]:
        task = context.task or await self.resolve_task(context)
        if self.org_engine and context.role_id and hasattr(self.org_engine, "build_contact_directory"):
            try:
                contacts = self.org_engine.build_contact_directory(context.role_id, task=task)
            except TypeError:
                contacts = self.org_engine.build_contact_directory(context.role_id)
            roles = [str(item.get("role_id", "") or "").strip() for item in contacts if str(item.get("role_id", "") or "").strip()]
            return {"roles": roles, "self": context.role_id}
        layout = self._layout(context, task)
        if layout is None:
            return {"roles": [], "self": context.role_id}
        roles = [role for role in file_comms.list_roles(layout) if role != context.role_id]
        return {"roles": roles, "self": context.role_id}

    async def read_meeting(self, context: CollaborationContext, *, meeting_id: str) -> dict[str, Any]:
        task = context.task or await self.resolve_task(context)
        layout = self._layout(context, task)
        if layout is None:
            return {"error": "meeting layout unavailable"}
        state = file_comms.read_meeting_state(layout, meeting_id)
        if state is None:
            return {"error": f"meeting {meeting_id} not found"}
        entries = file_comms.read_transcript(layout, meeting_id)
        return {
            "meeting_id": state.meeting_id,
            "topic": state.topic,
            "status": state.status,
            "participants": state.participants,
            "entries": [
                {"author": entry.author, "posted_at": entry.posted_at, "content": entry.content}
                for entry in entries
            ],
        }
