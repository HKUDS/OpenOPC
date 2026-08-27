import type {
  OrgInfoPayload,
  ProjectDossierInfo,
  ProjectRunInfo,
  RuntimeFrontierSummary,
  RuntimeSeatInfo,
  RuntimeTeamInfo,
  RuntimeWorkItemInfo,
  SeatDigestInfo,
  SessionLinkInfo,
} from '../types/visual'

type RecordLike = Record<string, unknown>

function isRecord(value: unknown): value is RecordLike {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

function asRecordArray<T extends RecordLike = RecordLike>(value: unknown): T[] {
  return Array.isArray(value) ? (value.filter(isRecord) as T[]) : []
}

function asStringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map((item) => String(item ?? '').trim()).filter(Boolean)
    : []
}

function asRuntimeFrontierSummary(value: unknown): RuntimeFrontierSummary {
  if (!isRecord(value)) return {}
  return {
    run_id: typeof value.run_id === 'string' ? value.run_id : undefined,
    status: typeof value.status === 'string' ? value.status : undefined,
    total_cells: typeof value.total_cells === 'number' ? value.total_cells : undefined,
    total_role_sessions: typeof value.total_role_sessions === 'number' ? value.total_role_sessions : undefined,
    total_work_items: typeof value.total_work_items === 'number' ? value.total_work_items : undefined,
    ready_count: typeof value.ready_count === 'number' ? value.ready_count : undefined,
    running_count: typeof value.running_count === 'number' ? value.running_count : undefined,
    blocked_count: typeof value.blocked_count === 'number' ? value.blocked_count : undefined,
    waiting_count: typeof value.waiting_count === 'number' ? value.waiting_count : undefined,
    done_count: typeof value.done_count === 'number' ? value.done_count : undefined,
    failed_count: typeof value.failed_count === 'number' ? value.failed_count : undefined,
  }
}

function mapRuntimeTeam(raw: RecordLike): RuntimeTeamInfo {
  return {
    cell_id: String(raw.cell_id ?? raw.team_id ?? raw.id ?? '').trim(),
    team_instance_id: typeof raw.team_instance_id === 'string' ? raw.team_instance_id : undefined,
    team_id: typeof raw.team_id === 'string' ? raw.team_id : undefined,
    manager_role_id: String(raw.manager_role_id ?? '').trim(),
    member_role_ids: asStringList(raw.member_role_ids),
    seat_ids: asStringList(raw.seat_ids),
    parent_team_id: typeof raw.parent_team_id === 'string' ? raw.parent_team_id : undefined,
    status: String(raw.status ?? 'idle'),
    is_final_decider_cell: typeof raw.is_final_decider_cell === 'boolean' ? raw.is_final_decider_cell : undefined,
  }
}

function mapRuntimeSeat(raw: RecordLike): RuntimeSeatInfo {
  return {
    role_session_id: String(raw.role_session_id ?? raw.id ?? '').trim(),
    role_id: String(raw.role_id ?? '').trim(),
    employee_id: String(raw.employee_id ?? '').trim(),
    team_id: typeof raw.team_id === 'string' ? raw.team_id : undefined,
    team_instance_id: typeof raw.team_instance_id === 'string' ? raw.team_instance_id : undefined,
    seat_id: typeof raw.seat_id === 'string' ? raw.seat_id : undefined,
    focused_work_item_id: typeof raw.focused_work_item_id === 'string' ? raw.focused_work_item_id : undefined,
    current_work_item_id: typeof raw.current_work_item_id === 'string' ? raw.current_work_item_id : undefined,
    background_work_item_ids: asStringList(raw.background_work_item_ids),
    manager_role_ids: asStringList(raw.manager_role_ids),
    manager_seat_id: typeof raw.manager_seat_id === 'string' ? raw.manager_seat_id : undefined,
    resident_status: typeof raw.resident_status === 'string' ? raw.resident_status : undefined,
    latest_notification: isRecord(raw.latest_notification) ? raw.latest_notification : undefined,
    manager_digest: isRecord(raw.manager_digest) ? raw.manager_digest : undefined,
    status: String(raw.status ?? 'cold'),
  }
}

function mapRuntimeWorkItem(raw: RecordLike): RuntimeWorkItemInfo {
  const metadata = isRecord(raw.metadata) ? raw.metadata : undefined
  const adaptive = isRecord(raw.adaptive)
    ? raw.adaptive
    : metadata && isRecord(metadata.adaptive)
      ? metadata.adaptive
      : undefined
  return {
    work_item_id: String(raw.work_item_id ?? raw.id ?? '').trim(),
    role_id: String(raw.role_id ?? '').trim(),
    cell_id: String(raw.cell_id ?? '').trim(),
    team_id: typeof raw.team_id === 'string' ? raw.team_id : undefined,
    team_instance_id: typeof raw.team_instance_id === 'string' ? raw.team_instance_id : undefined,
    seat_id: typeof raw.seat_id === 'string' ? raw.seat_id : undefined,
    title: String(raw.title ?? ''),
    kind: String(raw.kind ?? 'execute'),
    phase: String(raw.phase ?? 'ready'),
    kanban_column: String(raw.kanban_column ?? 'todo'),
    batch_id: typeof raw.batch_id === 'string' ? raw.batch_id : undefined,
    batch_index: typeof raw.batch_index === 'number' ? raw.batch_index : undefined,
    deliverable_summary: typeof raw.deliverable_summary === 'string' ? raw.deliverable_summary : undefined,
    blocked_reason: typeof raw.blocked_reason === 'string' ? raw.blocked_reason : undefined,
    handoff_status: typeof raw.handoff_status === 'string' ? raw.handoff_status : undefined,
    parent_work_item_id: typeof raw.parent_work_item_id === 'string' ? raw.parent_work_item_id : undefined,
    work_item_projection_id: typeof raw.work_item_projection_id === 'string' ? raw.work_item_projection_id : undefined,
    metadata,
    adaptive,
  }
}

function asProjectRunInfo(value: unknown): ProjectRunInfo | undefined {
  if (!isRecord(value)) return undefined
  return {
    run_id: typeof value.run_id === 'string' ? value.run_id : undefined,
    project_id: typeof value.project_id === 'string' ? value.project_id : undefined,
    session_id: typeof value.session_id === 'string' ? value.session_id : undefined,
    status: typeof value.status === 'string' ? value.status : undefined,
    lifecycle_status: typeof value.lifecycle_status === 'string' ? value.lifecycle_status : undefined,
    company_profile: typeof value.company_profile === 'string' ? value.company_profile : undefined,
    execution_model: typeof value.execution_model === 'string' ? value.execution_model : undefined,
    current_revision: typeof value.current_revision === 'number' ? value.current_revision : undefined,
    latest_deliverable_summary: typeof value.latest_deliverable_summary === 'string' ? value.latest_deliverable_summary : undefined,
    recovery_pointer: isRecord(value.recovery_pointer) ? value.recovery_pointer : undefined,
  }
}

function asProjectDossierInfo(value: unknown): ProjectDossierInfo | undefined {
  if (!isRecord(value)) return undefined
  return {
    project_id: typeof value.project_id === 'string' ? value.project_id : undefined,
    run_id: typeof value.run_id === 'string' ? value.run_id : undefined,
    latest_deliverable_summary: typeof value.latest_deliverable_summary === 'string' ? value.latest_deliverable_summary : undefined,
    architecture_decisions: asRecordArray(value.architecture_decisions),
    completed_work_items: asRecordArray(value.completed_work_items),
    open_issues: asStringList(value.open_issues),
    verification_summary: typeof value.verification_summary === 'string' ? value.verification_summary : undefined,
    artifact_index: asRecordArray(value.artifact_index),
    handoff_summaries: asRecordArray(value.handoff_summaries),
    last_failure_summary: typeof value.last_failure_summary === 'string' ? value.last_failure_summary : undefined,
    project_memory_excerpt: typeof value.project_memory_excerpt === 'string' ? value.project_memory_excerpt : undefined,
    session_memory_excerpt: typeof value.session_memory_excerpt === 'string' ? value.session_memory_excerpt : undefined,
  }
}

function mapSeatDigest(raw: RecordLike): SeatDigestInfo {
  return {
    seat_id: String(raw.seat_id ?? raw.id ?? '').trim(),
    team_id: typeof raw.team_id === 'string' ? raw.team_id : undefined,
    role_id: typeof raw.role_id === 'string' ? raw.role_id : undefined,
    employee_id: typeof raw.employee_id === 'string' ? raw.employee_id : undefined,
    role_session_id: typeof raw.role_session_id === 'string' ? raw.role_session_id : undefined,
    resident_status: typeof raw.resident_status === 'string' ? raw.resident_status : undefined,
    current_work_item: isRecord(raw.current_work_item) ? raw.current_work_item : undefined,
    latest_notification: isRecord(raw.latest_notification) ? raw.latest_notification : undefined,
    manager_digest: isRecord(raw.manager_digest) ? raw.manager_digest : undefined,
  }
}

function mapSessionLink(raw: RecordLike): SessionLinkInfo {
  return {
    link_id: typeof raw.link_id === 'string' ? raw.link_id : undefined,
    session_id: typeof raw.session_id === 'string' ? raw.session_id : undefined,
    linked_session_id: typeof raw.linked_session_id === 'string' ? raw.linked_session_id : undefined,
    link_type: typeof raw.link_type === 'string' ? raw.link_type : undefined,
    metadata: isRecord(raw.metadata) ? raw.metadata : undefined,
    created_at: typeof raw.created_at === 'string' ? raw.created_at : undefined,
  }
}

export function normalizeOrgInfoPayload(raw: OrgInfoPayload | RecordLike | null | undefined): OrgInfoPayload {
  const source = isRecord(raw) ? raw : {}
  const runtimeTeams = asRecordArray(source.runtime_teams).map(mapRuntimeTeam)
  const runtimeSeats = asRecordArray(source.runtime_seats).map(mapRuntimeSeat)
  const workItems = asRecordArray(source.work_items).map(mapRuntimeWorkItem)

  return {
    roles: Array.isArray(source.roles) ? source.roles as OrgInfoPayload['roles'] : [],
    employees: Array.isArray(source.employees) ? source.employees as OrgInfoPayload['employees'] : [],
    company_profile: typeof source.company_profile === 'string' ? source.company_profile : '',
    organization_id: typeof source.organization_id === 'string' ? source.organization_id : undefined,
    organization_name: typeof source.organization_name === 'string' ? source.organization_name : undefined,
    organization_config_file: typeof source.organization_config_file === 'string' ? source.organization_config_file : undefined,
    final_decider_role_id: typeof source.final_decider_role_id === 'string' ? source.final_decider_role_id : null,
    top_level_role_ids: asStringList(source.top_level_role_ids),
    runtime_teams: runtimeTeams,
    runtime_seats: runtimeSeats,
    work_items: workItems,
    frontier: asRuntimeFrontierSummary(source.frontier),
    project_run: asProjectRunInfo(source.project_run),
    project_dossier: asProjectDossierInfo(source.project_dossier),
    seat_digests: asRecordArray(source.seat_digests).map(mapSeatDigest),
    revision_links: asRecordArray(source.revision_links).map(mapSessionLink),
    project_recovery: isRecord(source.project_recovery) ? source.project_recovery : undefined,
    channels: Array.isArray(source.channels) ? source.channels as OrgInfoPayload['channels'] : [],
    connectors: Array.isArray(source.connectors) ? source.connectors as OrgInfoPayload['connectors'] : [],
    org_version: typeof source.org_version === 'number' ? source.org_version : 0,
    runtime_topology_version: typeof source.runtime_topology_version === 'number' ? source.runtime_topology_version : 0,
    installed_packages: Array.isArray(source.installed_packages) ? source.installed_packages as NonNullable<OrgInfoPayload['installed_packages']> : [],
    runtime_policy: isRecord(source.runtime_policy) ? source.runtime_policy as NonNullable<OrgInfoPayload['runtime_policy']> : undefined,
    runtime_topology_preview: isRecord(source.runtime_topology_preview) ? source.runtime_topology_preview : undefined,
    work_item_runtime_preview: isRecord(source.work_item_runtime_preview) ? source.work_item_runtime_preview : undefined,
    external_team_bindings: asRecordArray(source.external_team_bindings),
    external_execution_units: asRecordArray(source.external_execution_units) as unknown as NonNullable<OrgInfoPayload['external_execution_units']>,
    external_agent_profiles: asRecordArray(source.external_agent_profiles),
  }
}

export interface RuntimeOrgView {
  runtimeTeams: RuntimeTeamInfo[]
  runtimeSeats: RuntimeSeatInfo[]
  workItems: RuntimeWorkItemInfo[]
  frontier: RuntimeFrontierSummary
  projectRun?: ProjectRunInfo
  projectDossier?: ProjectDossierInfo
  seatDigests: SeatDigestInfo[]
  revisionLinks: SessionLinkInfo[]
  projectRecovery?: Record<string, unknown>
}

export function getRuntimeOrgView(payload: OrgInfoPayload | null | undefined): RuntimeOrgView {
  const normalized = normalizeOrgInfoPayload(payload)
  return {
    runtimeTeams: normalized.runtime_teams ?? [],
    runtimeSeats: normalized.runtime_seats ?? [],
    workItems: normalized.work_items ?? [],
    frontier: normalized.frontier ?? {},
    projectRun: normalized.project_run,
    projectDossier: normalized.project_dossier,
    seatDigests: normalized.seat_digests ?? [],
    revisionLinks: normalized.revision_links ?? [],
    projectRecovery: normalized.project_recovery,
  }
}
