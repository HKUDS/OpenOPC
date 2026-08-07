"""Custom/org runtime entrypoint.

Custom mode runs with company-mode semantics, but with a user-selected
organization config.  The runner owns the custom isolation boundary:

- company mode's engine/config objects are not mutated;
- the custom runtime uses its own OPCEngine instance and company executor;
- the runtime is rebound to the caller's store so UI session/task/transcript
  state remains in the same project context as the chat that started it.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any, TYPE_CHECKING

from opc.core.config import OPCConfig
from opc.core.models import UserMessage
from opc.core.org_config import (
    apply_org_config_payload_to_config,
    load_org_config_payload,
    validate_runnable_org_config,
)

if TYPE_CHECKING:
    from opc.engine import OPCEngine


class CustomRuntimeRunner:
    """Run custom/org turns through an isolated company-runtime engine."""

    def __init__(self, parent: OPCEngine) -> None:
        self.parent = parent

    def _build_org_config(self, organization_id: str | None) -> tuple[OPCConfig, str | None]:
        config_dir = self.parent.opc_home / "config"
        payload, source_path = load_org_config_payload(config_dir, organization_id)
        try:
            base_config = OPCConfig.load(config_dir) if config_dir.exists() else copy.deepcopy(self.parent.config)
        except Exception:
            base_config = copy.deepcopy(self.parent.config)
        loaded_config = apply_org_config_payload_to_config(
            base_config,
            payload,
            source_path=source_path,
        )
        resolved_org_id = str(getattr(loaded_config.org, "organization_id", "") or organization_id or "").strip() or None
        validate_runnable_org_config(loaded_config, organization_id=resolved_org_id or "")
        return loaded_config, resolved_org_id

    async def process_message(
        self,
        content: str,
        *,
        project_id: str | None,
        session_id: str | None,
        org_id: str | None,
        preferred_agent: str | None,
        domains: list[str] | None,
        origin_task_id: str | None,
        attachment_refs: list[dict[str, Any]] | None,
        message_metadata: dict[str, Any] | None,
    ) -> str:
        from opc.engine import OPCEngine
        from opc.layer2_organization.phase_hooks import unregister_dispatcher_wake
        from opc.plugins.office_ui.services.models import ServiceError

        normalized_org_id = str(org_id or "").strip()
        if not normalized_org_id:
            # Isolated org mode must carry a durable org_id; resolving the
            # active index here would silently route the run to whichever
            # organization is currently loaded.
            raise ServiceError(
                "org_id_required",
                "org_id_required",
                {
                    "project_id": project_id or self.parent.project_id or "default",
                    "reason": "custom_company_run_requires_durable_org_id",
                },
            )
        org_config, resolved_org_id = self._build_org_config(normalized_org_id)
        normalized_project_id = str(project_id or self.parent.project_id or "default").strip() or "default"
        shared_store = getattr(self.parent, "store", None)
        runtime = OPCEngine(
            config=org_config,
            opc_home=self.parent.opc_home,
            project_id=normalized_project_id,
            store=shared_store,
            owns_store=shared_store is None,
            run_startup_reconcile=shared_store is None,
            active_task_run_registry=getattr(self.parent, "_active_task_run_registry", None),
            owns_active_task_run_registry=False,
            on_progress=self.parent.on_progress,
            on_runtime_event=self.parent.on_runtime_event,
            on_escalation=self.parent.on_escalation,
        )
        runtime.on_company_runtime_children = self.parent.on_company_runtime_children
        await runtime.initialize()
        company_executor = getattr(runtime, "company_executor", None)
        if company_executor is not None:
            callback_factory = getattr(self.parent, "on_company_kanban_callback_factory", None)
            if callable(callback_factory):
                company_executor.on_kanban_changed = callback_factory(runtime)
            else:
                parent_executor = getattr(self.parent, "company_executor", None)
                company_executor.on_kanban_changed = getattr(parent_executor, "on_kanban_changed", None)

        try:
            normalized_attachment_refs = runtime._normalize_attachment_refs(attachment_refs)
            metadata = {
                "mode": "company",
                "exec_mode": "org",
                "org_id": resolved_org_id,
                "organization_id": resolved_org_id,
                "organization_name": str(getattr(org_config.org, "organization_name", "") or "").strip(),
                "organization_config_file": str(getattr(org_config.org, "organization_config_file", "") or "").strip(),
                "preferred_agent": preferred_agent,
                "domains": domains or [],
                "company_profile": "custom",
                "origin_task_id": origin_task_id,
                "attachment_refs": normalized_attachment_refs,
            }
            if message_metadata:
                metadata.update(dict(message_metadata))
                metadata["mode"] = "company"
                metadata["exec_mode"] = "org"
                metadata["company_profile"] = "custom"
                metadata["org_id"] = resolved_org_id
                metadata["organization_id"] = resolved_org_id

            message = UserMessage(
                channel="cli",
                user_id="owner",
                content=content,
                attachments=normalized_attachment_refs,
                session_id=session_id or str(uuid.uuid4()),
                project_context=normalized_project_id,
                metadata=metadata,
            )
            response = await runtime.message_bus.process_single(message)
            return response.content if response else "No response generated."
        finally:
            company_executor = getattr(runtime, "company_executor", None)
            wake = getattr(company_executor, "_signal_dispatcher_wake", None)
            if wake is not None:
                unregister_dispatcher_wake(wake)
            await runtime.shutdown()
