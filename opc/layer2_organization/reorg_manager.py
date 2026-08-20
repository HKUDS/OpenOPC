"""Runtime company reorganization orchestration."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import uuid
from datetime import datetime
from typing import Any, Callable, Coroutine

from opc.core.models import (
    OrgSnapshot,
    ExecutionCheckpoint,
    ReorgChangeSet,
    ReorgEventKind,
    ReorgEventRecord,
    ReorgMigrationPlan,
    ReorgProposal,
    ReorgProposalStatus,
    ReorgRiskLevel,
    ReorgRoleChange,
    ReorgScope,
    ReorgTaskAdjustment,
    Task,
    TaskStatus,
)
from opc.database.store import OPCStore
from opc.core.interaction_protocol import OWNER_INTERACTION_CHECKPOINT_TYPES
from opc.layer0_interaction.coordinator import (
    InteractionCoordinator,
    InteractionDecisionLease,
)
from opc.layer2_organization.communication import CommunicationManager
from opc.layer2_organization.company_runtime_identity import (
    resolve_company_interaction_ownership,
)
from opc.layer2_organization.org_engine import OrgEngine
from opc.layer2_organization.work_item_identity import work_item_identity_payload_for_task


_SYSTEM_REORG_DECISION_AUTHORITY = object()


class ReorgManager:
    """Coordinates proposal, approval, application, and migration of runtime org changes."""

    ACTIVE_TASK_STATUSES = {
        TaskStatus.PENDING,
        TaskStatus.BLOCKED,
        TaskStatus.AWAITING_PEER,
        TaskStatus.AWAITING_MANAGER_REVIEW,
        TaskStatus.AWAITING_HUMAN,
        TaskStatus.AWAITING_REVIEW,
    }

    def __init__(
        self,
        store: OPCStore,
        org_engine: OrgEngine,
        communication: CommunicationManager | None,
        progress_callback: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        interaction_coordinator: InteractionCoordinator | None = None,
    ) -> None:
        self.store = store
        self.org_engine = org_engine
        self.communication = communication
        self.progress_callback = progress_callback
        self.interaction_coordinator = interaction_coordinator

    async def _emit_progress(self, message: str) -> None:
        if self.progress_callback:
            await self.progress_callback(message)

    async def build_org_snapshot(self, project_id: str) -> OrgSnapshot:
        tasks = await self.store.get_tasks(project_id=project_id)
        active_tasks = [
            {
                "task_id": task.id,
                "title": task.title,
                "status": task.status.value,
                "assigned_to": task.assigned_to,
                **work_item_identity_payload_for_task(task),
                "org_version": task.metadata.get("org_version", self.org_engine.current_org_version()),
                "runtime_topology_version": task.metadata.get("runtime_topology_version", self.org_engine.current_runtime_topology_version()),
            }
            for task in tasks
            if task.status in self.ACTIVE_TASK_STATUSES | {TaskStatus.RUNNING}
        ]
        return self.org_engine.snapshot_org(project_id=project_id, active_tasks=active_tasks)

    async def propose_reorg(
        self,
        *,
        project_id: str,
        summary: str,
        rationale: str = "",
        title: str = "",
        initiated_by: str = "owner",
        source_role_id: str = "",
        changeset: ReorgChangeSet | dict[str, Any] | None = None,
        scope: ReorgScope | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ReorgProposal:
        """Create a proposal governed by an owner interaction."""

        return await self._propose_reorg(
            project_id=project_id,
            summary=summary,
            rationale=rationale,
            title=title,
            initiated_by=initiated_by,
            source_role_id=source_role_id,
            changeset=changeset,
            scope=scope,
            session_id=session_id,
            task_id=task_id,
            metadata=metadata,
            _system_authority=None,
        )

    async def _propose_reorg(
        self,
        *,
        project_id: str,
        summary: str,
        rationale: str = "",
        title: str = "",
        initiated_by: str = "owner",
        source_role_id: str = "",
        changeset: ReorgChangeSet | dict[str, Any] | None = None,
        scope: ReorgScope | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        _system_authority: object | None,
    ) -> ReorgProposal:
        if (
            _system_authority is not None
            and _system_authority is not _SYSTEM_REORG_DECISION_AUTHORITY
        ):
            raise ValueError("invalid system reorg decision authority")
        if isinstance(changeset, dict):
            changeset = ReorgChangeSet(**changeset)
        changeset = self._normalize_changeset(changeset or ReorgChangeSet())
        scope = scope or self._infer_scope(changeset)
        risk_level = self._classify_risk(scope, changeset)
        system_decision_authorized = (
            _system_authority is _SYSTEM_REORG_DECISION_AUTHORITY
        )
        if system_decision_authorized and not (
            scope == ReorgScope.TASK_ADJUSTMENT
            and risk_level == ReorgRiskLevel.LOW
        ):
            raise ValueError(
                "system reorg decisions are limited to low-risk task adjustments"
            )
        snapshot = await self.build_org_snapshot(project_id)
        await self.store.save_org_snapshot(snapshot)

        migration_plan = await self._build_migration_plan(
            project_id=project_id,
            changeset=changeset,
            snapshot=snapshot,
            target_org_version=snapshot.org_version + (1 if scope == ReorgScope.ORG_MUTATION else 0),
        )

        proposal = ReorgProposal(
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            initiated_by=initiated_by,
            source_role_id=source_role_id,
            scope=scope,
            risk_level=risk_level,
            status=ReorgProposalStatus.PROPOSED,
            title=title or summary[:120],
            summary=summary,
            rationale=rationale or summary,
            # Risk alone never grants authority.  Only the explicitly
            # authorized system path may omit the owner interaction.
            user_confirmation_required=not system_decision_authorized,
            old_org_version=snapshot.org_version,
            new_org_version=migration_plan.metadata.get("target_org_version", snapshot.org_version),
            old_runtime_topology_version=snapshot.runtime_topology_version,
            new_runtime_topology_version=snapshot.runtime_topology_version,
            changeset=changeset,
            migration_plan=migration_plan,
            impact_summary={
                "affected_tasks": len(migration_plan.affected_task_ids),
                "affected_checkpoints": len(migration_plan.affected_checkpoint_ids),
                "role_mapping": migration_plan.role_mapping,
            },
            metadata={
                **dict(metadata or {}),
                "system_decision_authorized": system_decision_authorized,
            },
        )
        if proposal.user_confirmation_required:
            if self.interaction_coordinator is None:
                # Standalone domain consumers (tests/embedded use) still use
                # the same coordinator protocol; Engine injects its shared
                # instance so UI notifications and recovery remain owned by
                # the root controller.
                self.interaction_coordinator = InteractionCoordinator(
                    store=self.store,
                    project_id=proposal.project_id,
                )
            proposal, created, _checkpoint, _checkpoint_created = (
                await self.interaction_coordinator.publish_reorg_proposal(
                    proposal,
                    await self.build_reorg_owner_checkpoint(proposal),
                )
            )
        else:
            proposal, created, _checkpoint, _checkpoint_created = (
                await self.store._create_system_task_adjustment_proposal(proposal)
            )
        if created:
            await self.store.record_reorg_event(
                ReorgEventRecord(
                    proposal_id=proposal.proposal_id,
                    project_id=project_id,
                    event_kind=ReorgEventKind.PROPOSED,
                    summary=proposal.summary,
                    details={
                        "scope": proposal.scope.value,
                        "risk_level": proposal.risk_level.value,
                        "changeset": proposal.changeset.__dict__,
                    },
                )
            )
        return proposal

    async def build_reorg_owner_checkpoint(
        self,
        proposal: ReorgProposal,
    ) -> ExecutionCheckpoint:
        """Build the canonical, deterministic owner card for one proposal."""

        domain_key = hashlib.sha256(
            json.dumps(
                {
                    "project_id": proposal.project_id,
                    "checkpoint_type": "company_reorg_pending",
                    "proposal_id": proposal.proposal_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        supersession_key = hashlib.sha256(
            f"{proposal.project_id}:company_reorg_pending:{proposal.proposal_id}".encode(
                "utf-8"
            )
        ).hexdigest()
        interaction: dict[str, Any] = {
            "kind": "company_reorg_pending",
            "prompt": proposal.summary,
            "options": [
                {"id": "approve", "label": "Approve"},
                {"id": "deny", "label": "Deny"},
            ],
            "domain_key": domain_key,
            "supersession_key": supersession_key,
            "supersession_order": [0, 0],
            # The task id is execution identity, not necessarily the owner UI
            # anchor (company child tasks resolve to their root).  Canonical
            # ownership resolution derives that anchor when publishing/viewing;
            # taskless proposals use the explicit project-owner capability.
            "ownership": {},
        }
        proposal_metadata = dict(proposal.metadata or {})
        task = (
            await self.store.get_task(proposal.task_id)
            if proposal.task_id
            else None
        )
        task_metadata = dict(getattr(task, "metadata", {}) or {})
        profile = str(
            proposal_metadata.get("company_profile")
            or task_metadata.get("company_profile")
            or (
                "custom"
                if str(task_metadata.get("exec_mode", "") or "").lower()
                in {"org", "custom"}
                else ""
            )
            or ""
        ).strip().lower()
        org_id = str(
            proposal_metadata.get("org_id")
            or getattr(task, "org_id", None)
            or task_metadata.get("org_id")
            or task_metadata.get("organization_id")
            or ""
        ).strip()
        scope = {"company_profile": profile, "org_id": org_id}
        if any(scope.values()):
            interaction["execution_scope"] = scope
        continuation_payload: dict[str, Any] = {}
        if task is not None:
            parent_session_id = str(
                getattr(task, "parent_session_id", "")
                or task_metadata.get("parent_session_id")
                or ""
            ).strip()
            interaction["ownership"] = await resolve_company_interaction_ownership(
                self.store,
                proposal.project_id,
                waiting_task_id=task.id,
                waiting_session_id=str(task.session_id or "").strip(),
                execution_parent_task_id=str(
                    getattr(task, "parent_id", "") or ""
                ).strip(),
                execution_parent_session_id=parent_session_id,
                origin_task_id=str(
                    task_metadata.get("origin_task_id", "") or ""
                ).strip(),
                root_session_id_hint=str(
                    task_metadata.get("company_runtime_root_session_id")
                    or proposal.session_id
                    or ""
                ).strip(),
            )
            continuation_payload = {
                # The atomic proposal/card pair is the first durable card.  It
                # must already contain enough execution identity to resume if
                # the owner answers before the task-pause projection runs.
                "waiting_task_id": task.id,
                "task_ids": list(
                    task_metadata.get("execution_task_ids", [task.id]) or [task.id]
                ),
                "parent_session_id": parent_session_id,
                "company_work_item_plan": task_metadata.get(
                    "company_work_item_plan"
                ),
                "original_message": str(
                    task_metadata.get("original_message")
                    or task_metadata.get("original_request")
                    or ""
                ),
            }
        checkpoint_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"openopc:reorg:{proposal.project_id}:{proposal.proposal_id}",
            )
        )
        return ExecutionCheckpoint(
            checkpoint_id=checkpoint_id,
            project_id=proposal.project_id,
            session_id=proposal.session_id,
            checkpoint_type="company_reorg_pending",
            task_id=proposal.task_id,
            payload={
                "proposal_id": proposal.proposal_id,
                "org_version": proposal.old_org_version,
                "runtime_topology_version": proposal.old_runtime_topology_version,
                "source_event_id": proposal.proposal_id,
                **continuation_payload,
                "interaction": interaction,
            },
        )

    async def set_reorg_approval(
        self,
        proposal_id: str,
        approved: bool,
        notes: str = "",
        *,
        interaction_lease: InteractionDecisionLease | None = None,
    ) -> ReorgProposal:
        """Apply a decision carried by the proposal's claimed owner card."""

        return await self._set_reorg_approval(
            proposal_id,
            approved=approved,
            notes=notes,
            interaction_lease=interaction_lease,
            _system_authority=None,
        )

    async def _set_reorg_approval(
        self,
        proposal_id: str,
        approved: bool,
        notes: str = "",
        *,
        interaction_lease: InteractionDecisionLease | None = None,
        _system_authority: object | None,
    ) -> ReorgProposal:
        if (
            _system_authority is not None
            and _system_authority is not _SYSTEM_REORG_DECISION_AUTHORITY
        ):
            raise ValueError("invalid system reorg decision authority")
        system_decision = (
            _system_authority is _SYSTEM_REORG_DECISION_AUTHORITY
        )
        if system_decision:
            receipt = await self.store._decide_system_task_adjustment_proposal(
                proposal_id,
                approved=approved,
                notes=notes,
            )
        elif self.interaction_coordinator is not None:
            receipt = await self.interaction_coordinator.decide_reorg_proposal(
                proposal_id,
                approved=approved,
                notes=notes,
                lease=interaction_lease,
            )
        else:
            raise RuntimeError("required reorg decision needs interaction lease")
        proposal = receipt.proposal
        if proposal is None:
            raise ValueError(f"Unknown reorg proposal: {proposal_id}")
        if receipt.outcome == "conflict":
            if (
                proposal.status == ReorgProposalStatus.PROPOSED
                and proposal.user_confirmation_required
            ):
                raise ValueError(
                    "Confirmation-required reorg decisions need the matching "
                    "claimed owner interaction."
                )
            raise ValueError(
                "Reorg proposal decision is immutable after "
                f"{proposal.status.value}."
            )
        if not receipt.applied:
            raise RuntimeError(
                f"Reorg proposal decision failed ({receipt.outcome})."
            )
        if receipt.outcome == "applied":
            await self.store.record_reorg_event(
                ReorgEventRecord(
                    proposal_id=proposal.proposal_id,
                    project_id=proposal.project_id,
                    event_kind=(
                        ReorgEventKind.APPROVED
                        if approved
                        else ReorgEventKind.DENIED
                    ),
                    summary=notes or proposal.summary,
                    details={"status": proposal.status.value},
                )
            )
        return proposal

    @staticmethod
    def _persisted_apply_result(proposal: ReorgProposal) -> dict[str, Any]:
        saved = dict(proposal.metadata or {}).get("apply_result")
        if isinstance(saved, dict) and saved:
            return copy.deepcopy(saved)
        migration_summary = dict(
            proposal.migration_plan.metadata.get("migration_summary", {}) or {}
        )
        return {
            "proposal_id": proposal.proposal_id,
            "status": ReorgProposalStatus.APPLIED.value,
            "migration_summary": migration_summary,
            "change_result": {
                "old_org_version": proposal.old_org_version,
                "new_org_version": proposal.new_org_version,
                "role_mapping": dict(proposal.migration_plan.role_mapping or {}),
            },
            "snapshot_id": str(
                dict(proposal.metadata or {}).get("applied_snapshot_id", "") or ""
            ),
        }

    async def apply_reorg(self, proposal_id: str) -> dict[str, Any]:
        operation_token = uuid.uuid4().hex
        claim = await self.store.claim_reorg_application(
            proposal_id,
            operation_token=operation_token,
        )
        proposal = claim.proposal
        if claim.outcome == "applied" and proposal is not None:
            return self._persisted_apply_result(proposal)
        if claim.outcome in {"busy", "duplicate"}:
            raise RuntimeError(
                "Reorg application is already in progress or its prior controller "
                "stopped before recording the outcome; it will not be replayed."
            )
        if claim.outcome == "not_found" or proposal is None:
            raise ValueError(f"Unknown reorg proposal: {proposal_id}")
        if claim.outcome == "invalid_state":
            raise ValueError(
                "Reorg proposal must be approved before apply; "
                f"current status is {proposal.status.value}."
            )
        if not claim.acquired:
            raise RuntimeError(
                f"Reorg application claim failed ({claim.outcome})."
            )

        effect_started = False
        try:
            before_snapshot = await self.build_org_snapshot(proposal.project_id)
            await self.store.save_org_snapshot(before_snapshot)
            proposal.migration_plan.rollback_snapshot_id = before_snapshot.snapshot_id

            mutates_org = bool(
                proposal.scope == ReorgScope.ORG_MUTATION
                or proposal.changeset.role_changes
            )
            if mutates_org:
                effect_started = True
                change_result = self.org_engine.apply_changeset(
                    proposal.changeset,
                    persist=True,
                )
            else:
                change_result = {
                    "old_org_version": self.org_engine.current_org_version(),
                    "new_org_version": self.org_engine.current_org_version(),
                    "role_mapping": {},
                }

            effect_started = True
            migration_summary = await self._migrate_active_state(proposal, change_result)
            proposal.status = ReorgProposalStatus.APPLIED
            proposal.old_org_version = change_result["old_org_version"]
            proposal.new_org_version = change_result["new_org_version"]
            proposal.migration_plan.role_mapping = dict(change_result.get("role_mapping", {}))
            proposal.migration_plan.metadata["migration_summary"] = migration_summary
            proposal.updated_at = datetime.now()

            after_snapshot = await self.build_org_snapshot(proposal.project_id)
            await self.store.save_org_snapshot(after_snapshot)
            result = {
                "proposal_id": proposal.proposal_id,
                "status": proposal.status.value,
                "migration_summary": migration_summary,
                "change_result": change_result,
                "snapshot_id": after_snapshot.snapshot_id,
            }
            proposal.metadata = dict(proposal.metadata or {})
            proposal.metadata["applied_snapshot_id"] = after_snapshot.snapshot_id
            proposal.metadata["apply_result"] = copy.deepcopy(result)
            finished = await self.store.finish_reorg_application(
                proposal,
                operation_token=operation_token,
            )
            if not finished.applied:
                raise RuntimeError(
                    "Reorg application ownership was lost before its result "
                    f"could be persisted ({finished.outcome})."
                )
        except asyncio.CancelledError:
            await asyncio.shield(self.store.fail_reorg_application(
                proposal_id,
                operation_token=operation_token,
                effect_started=effect_started,
                error="application_cancelled",
            ))
            raise
        except Exception as exc:
            await self.store.fail_reorg_application(
                proposal_id,
                operation_token=operation_token,
                effect_started=effect_started,
                error=str(exc),
            )
            raise

        await self.store.record_reorg_event(
            ReorgEventRecord(
                proposal_id=proposal.proposal_id,
                project_id=proposal.project_id,
                event_kind=ReorgEventKind.APPLIED,
                summary=proposal.summary,
                details={
                    "change_result": change_result,
                    "migration_summary": migration_summary,
                    "snapshot_id": after_snapshot.snapshot_id,
                },
            )
        )
        return result

    async def suggest_task_adjustment(
        self,
        *,
        project_id: str,
        source_role_id: str,
        summary: str,
        changeset: ReorgChangeSet | dict[str, Any],
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(changeset, dict):
            changeset = ReorgChangeSet(**changeset)
        normalized_changeset = self._normalize_changeset(changeset)
        auto_authorized = bool(
            self._classify_risk(
                ReorgScope.TASK_ADJUSTMENT,
                normalized_changeset,
            )
            == ReorgRiskLevel.LOW
            and self._is_top_level_role(source_role_id)
        )
        proposal = await self._propose_reorg(
            project_id=project_id,
            summary=summary,
            rationale=summary,
            initiated_by=source_role_id,
            source_role_id=source_role_id,
            changeset=normalized_changeset,
            scope=ReorgScope.TASK_ADJUSTMENT,
            session_id=session_id,
            task_id=task_id,
            metadata={"auto_apply_candidate": auto_authorized},
            _system_authority=(
                _SYSTEM_REORG_DECISION_AUTHORITY
                if auto_authorized
                else None
            ),
        )
        if auto_authorized:
            proposal = await self._set_reorg_approval(
                proposal.proposal_id,
                approved=True,
                notes="Auto-approved low-risk task adjustment.",
                _system_authority=_SYSTEM_REORG_DECISION_AUTHORITY,
            )
            result = await self.apply_reorg(proposal.proposal_id)
            proposal = await self._require_proposal(proposal.proposal_id)
            await self.store.record_reorg_event(
                ReorgEventRecord(
                    proposal_id=proposal.proposal_id,
                    project_id=proposal.project_id,
                    event_kind=ReorgEventKind.AUTO_TASK_ADJUSTED,
                    summary=summary,
                    details=result,
                )
            )
            return {"proposal": proposal, "auto_applied": True, "result": result}
        # A notification callback may have consumed the freshly published
        # card before this coroutine resumes.  Return the durable proposal,
        # not the stale object constructed before publication.
        proposal = await self._require_proposal(proposal.proposal_id)
        return {"proposal": proposal, "auto_applied": False}

    async def _build_migration_plan(
        self,
        *,
        project_id: str,
        changeset: ReorgChangeSet,
        snapshot: OrgSnapshot,
        target_org_version: int,
    ) -> ReorgMigrationPlan:
        tasks = await self.store.get_tasks(project_id=project_id)
        checkpoints = await self.store.get_execution_checkpoints(
            project_id=project_id,
            statuses=["pending", "answered", "consuming"],
        )
        affected_tasks = [task.id for task in tasks if task.status in self.ACTIVE_TASK_STATUSES]
        role_mapping: dict[str, str] = {}
        for change in changeset.role_changes:
            if change.action == "replace" and change.replacement_role_id:
                role_mapping[change.role_id] = change.replacement_role_id
            elif change.action == "remove":
                role_mapping[change.role_id] = ""
        warnings: list[str] = []
        if any(task.status == TaskStatus.RUNNING for task in tasks):
            warnings.append("Running tasks are not force-migrated and will continue until their current iteration completes.")
        return ReorgMigrationPlan(
            affected_task_ids=affected_tasks,
            affected_checkpoint_ids=[checkpoint.checkpoint_id for checkpoint in checkpoints],
            affected_handoff_ids=[],
            role_mapping=role_mapping,
            invalidated_waits=[],
            migration_notes=[
                f"Snapshot org_version={snapshot.org_version}.",
                f"Target org_version={target_org_version}.",
            ],
            compatibility_warnings=warnings,
            metadata={
                "target_org_version": target_org_version,
            },
        )

    async def _migrate_active_state(self, proposal: ReorgProposal, change_result: dict[str, Any]) -> dict[str, Any]:
        tasks = await self.store.get_tasks(project_id=proposal.project_id)
        checkpoints = await self.store.get_execution_checkpoints(
            project_id=proposal.project_id,
            statuses=["pending", "answered", "consuming"],
        )
        migrated_task_ids: list[str] = []
        migrated_checkpoint_ids: list[str] = []
        role_mapping = dict(change_result.get("role_mapping", {}))
        target_org_version = change_result.get("new_org_version", self.org_engine.current_org_version())

        for task in tasks:
            if task.status == TaskStatus.RUNNING:
                task.metadata = dict(task.metadata)
                task.metadata["migration_status"] = "pending_running_completion"
                task.metadata["reorg_proposal_id"] = proposal.proposal_id
                await self.store.save_task(task)
                continue
            if task.status not in self.ACTIVE_TASK_STATUSES:
                continue
            task.metadata = dict(task.metadata)
            task.context_snapshot = dict(task.context_snapshot)
            current_role = task.assigned_to or str(task.metadata.get("work_item_role_id", ""))
            new_role = role_mapping.get(current_role, current_role)
            if current_role and new_role and new_role != current_role:
                task.assigned_to = new_role
                task.metadata["work_item_role_id"] = new_role
            elif current_role and new_role == "" and task.status in self.ACTIVE_TASK_STATUSES:
                task.status = TaskStatus.CANCELLED
            self._apply_task_adjustments(task, proposal.changeset)
            peer_wait = dict(task.metadata.get("peer_wait", {}))
            if peer_wait:
                waiting_on = list(peer_wait.get("waiting_on_agents", []))
                peer_wait["waiting_on_agents"] = [
                    role_mapping.get(agent_id, agent_id)
                    for agent_id in waiting_on
                    if role_mapping.get(agent_id, agent_id)
                ]
                task.metadata["peer_wait"] = peer_wait
            active_meeting = dict(task.context_snapshot.get("active_meeting", {}))
            if active_meeting:
                participants = list(active_meeting.get("participants", []))
                if participants:
                    active_meeting["participants"] = [
                        role_mapping.get(agent_id, agent_id)
                        for agent_id in participants
                        if role_mapping.get(agent_id, agent_id)
                    ]
                    task.context_snapshot["active_meeting"] = active_meeting
            task.metadata["org_version"] = target_org_version
            task.metadata["reorg_proposal_id"] = proposal.proposal_id
            task.metadata["migration_status"] = "migrated"
            task.metadata["superseded_by_reorg"] = proposal.proposal_id
            task.context_snapshot["migration_reason"] = proposal.summary
            task.context_snapshot["migration_role_mapping"] = role_mapping
            task.context_snapshot["migration_handoff"] = {
                "proposal_id": proposal.proposal_id,
                "reason": proposal.summary,
                "previous_role": current_role,
                "current_role": task.assigned_to,
            }
            await self.store.save_task(task)
            migrated_task_ids.append(task.id)

        for checkpoint in checkpoints:
            patch = {
                "org_version": target_org_version,
                "reorg_proposal_id": proposal.proposal_id,
            }
            if checkpoint.checkpoint_type in OWNER_INTERACTION_CHECKPOINT_TYPES:
                if self.interaction_coordinator is None:
                    raise RuntimeError(
                        "owner checkpoint migration requires InteractionCoordinator"
                    )
                _, applied = await self.interaction_coordinator.enrich_owner_checkpoint(
                    checkpoint.checkpoint_id,
                    checkpoint_type=checkpoint.checkpoint_type,
                    expected_statuses={checkpoint.status},
                    payload_patch=patch,
                )
            else:
                _, applied = await self.store.patch_execution_checkpoint_payload(
                    checkpoint.checkpoint_id,
                    project_id=checkpoint.project_id,
                    checkpoint_type=checkpoint.checkpoint_type,
                    expected_statuses={checkpoint.status},
                    payload_patch=patch,
                )
            if applied:
                migrated_checkpoint_ids.append(checkpoint.checkpoint_id)

        proposal.migration_plan.affected_task_ids = migrated_task_ids
        proposal.migration_plan.affected_checkpoint_ids = migrated_checkpoint_ids
        proposal.migration_plan.metadata["target_org_version"] = target_org_version
        await self._emit_progress(
            f"[Reorg] Applied proposal {proposal.proposal_id}: migrated {len(migrated_task_ids)} tasks and {len(migrated_checkpoint_ids)} checkpoints."
        )
        return {
            "migrated_task_ids": migrated_task_ids,
            "migrated_checkpoint_ids": migrated_checkpoint_ids,
            "target_org_version": target_org_version,
        }

    def _apply_task_adjustments(self, task: Task, changeset: ReorgChangeSet) -> None:
        if not changeset.task_adjustments:
            return
        for adjustment in changeset.task_adjustments:
            if adjustment.task_id and adjustment.task_id != task.id:
                continue
            if adjustment.action == "reassign" and adjustment.new_role_id:
                task.assigned_to = adjustment.new_role_id
                task.metadata["work_item_role_id"] = adjustment.new_role_id
            elif adjustment.action == "reprioritize" and adjustment.priority is not None:
                task.priority = adjustment.priority
            elif adjustment.action == "update_description" and adjustment.description_append.strip():
                addition = adjustment.description_append.strip()
                if addition not in task.description:
                    task.description = f"{task.description}\n\nAdjustment note:\n{addition}".strip()
            elif adjustment.action == "append_acceptance_criteria" and adjustment.acceptance_criteria:
                criteria = list(task.metadata.get("acceptance_criteria", []))
                for item in adjustment.acceptance_criteria:
                    if item not in criteria:
                        criteria.append(item)
                task.metadata["acceptance_criteria"] = criteria
            elif adjustment.action == "request_review":
                task.metadata["force_additional_review"] = True

    def _infer_scope(self, changeset: ReorgChangeSet) -> ReorgScope:
        if changeset.role_changes:
            return ReorgScope.ORG_MUTATION
        return ReorgScope.TASK_ADJUSTMENT

    def _classify_risk(self, scope: ReorgScope, changeset: ReorgChangeSet) -> ReorgRiskLevel:
        if scope == ReorgScope.ORG_MUTATION:
            return ReorgRiskLevel.HIGH
        for adjustment in changeset.task_adjustments:
            if adjustment.action not in {"reassign", "reprioritize", "update_description", "append_acceptance_criteria", "request_review"}:
                return ReorgRiskLevel.MEDIUM
        return ReorgRiskLevel.LOW

    def _is_top_level_role(self, role_id: str) -> bool:
        agent = self.org_engine.get_agent(role_id)
        if not agent:
            return role_id in {"owner", "coordinator"}
        return agent.reports_to == "owner"

    def _normalize_changeset(self, changeset: ReorgChangeSet) -> ReorgChangeSet:
        role_changes = [
            item if isinstance(item, ReorgRoleChange) else ReorgRoleChange(**item)
            for item in changeset.role_changes
        ]
        task_adjustments = [
            item if isinstance(item, ReorgTaskAdjustment) else ReorgTaskAdjustment(**item)
            for item in changeset.task_adjustments
        ]
        return ReorgChangeSet(
            role_changes=role_changes,
            task_adjustments=task_adjustments,
            metadata=dict(changeset.metadata),
        )

    async def _require_proposal(self, proposal_id: str) -> ReorgProposal:
        proposal = await self.store.get_reorg_proposal(proposal_id)
        if not proposal:
            raise ValueError(f"Unknown reorg proposal `{proposal_id}`.")
        return proposal
