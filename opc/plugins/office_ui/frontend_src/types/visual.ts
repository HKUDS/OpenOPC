import type { AgentAnimStatus, TaskPreferredAgent } from './kanban'

export type RoleId = string & { readonly __brand: 'RoleId' }
export type TemplateId = string & { readonly __brand: 'TemplateId' }
export type EmployeeId = string & { readonly __brand: 'EmployeeId' }

export const asRoleId = (s: string): RoleId => s as RoleId
export const asTemplateId = (s: string): TemplateId => s as TemplateId
export const asEmployeeId = (s: string): EmployeeId => s as EmployeeId

export type HireTalentHandler = (template: TemplateId, role: RoleId) => void

export interface VisualEvent {
  event_id: string
  type: string
  agent_id: string
  data: Record<string, unknown>
  timestamp: number
}

export interface VisualSnapshot {
  project_id?: string
  agents: Record<string, unknown>
  channels: Record<string, unknown>
  skills: {
    recent: Array<{ skill_name: string; version: string; timestamp: number }>
    total: number
  }
  practice: {
    count: number
    last: Record<string, unknown> | null
  }
  milestones: Array<Record<string, unknown>>
  timeline: VisualEvent[]
  exec_mode?: 'task' | 'company' | 'org' | 'custom'
  company_profile?: 'corporate' | 'custom'
  task_preferred_agent?: TaskPreferredAgent
}

export interface SavedOrgSummary {
  name: string
  organization_id?: string
  organization_name?: string
  filename?: string
  saved_at: number
  roles_count: number
  employees_count: number
}

export interface OrgCreateMemberInput {
  name: string
  responsibility?: string
  prompt?: string
  reports_to_index?: number | null
}

export interface OrgSavedCreatePayload {
  ok: boolean
  name: string
  organization_id?: string
  organization_name?: string
  filename?: string
  roles_count?: number
  employees_count?: number
  error?: string
}

export type SocketEnvelope =
  | { type: 'snapshot'; payload: VisualSnapshot }
  | { type: 'event'; payload: VisualEvent }
  | { type: 'ack'; payload: Record<string, unknown> }
  | { type: 'pong' }
  | { type: 'channel_created'; payload: { channel_id: string; name: string; channel_type: string; participants: string[] } }
  | { type: 'board_task_created'; payload: { project_id: string; task_id: string; display_id: string; board_id: string; title: string; assignee_ids: string[] } }
  | { type: 'board_task_moved'; payload: { project_id: string; task_id: string; display_id: string; column_name: string } }
  | { type: 'board_task_status_changed'; payload: { project_id: string; task_id: string; column_id: string; status: string } }
  | { type: 'session_runtime_control'; payload: Record<string, unknown> }
  | { type: 'cross_office_collab'; payload: { agent_ids: string[]; task_id: string; action: string } }
  | { type: 'chat_new_message'; payload: Record<string, unknown> }
  | { type: 'chat_channel_created'; payload: Record<string, unknown> }
  | { type: 'kanban_updated'; payload: Record<string, unknown> }
  | { type: 'kanban_board_created'; payload: Record<string, unknown> }
  | { type: 'agent_runtime_update'; payload: AgentRuntimePayload }
  | { type: 'worker_notification'; payload: WorkerNotificationPayload }
  | { type: 'execution_mode_resolved'; payload: { project_id: string; mode: string; profile?: string } }
  | { type: 'collab_sync_push'; payload: Record<string, unknown> }
  | { type: 'project_index_push'; payload: Record<string, unknown> }
  | { type: 'kanban_view_data'; payload: KanbanViewDataPayload }
  | { type: 'session_created'; payload: { project_id: string; task_id: string; channel_id: string; session_id?: string; parent_session_id?: string; title: string; status: string; created_at: number; exec_mode?: string; company_profile?: string; org_id?: string; organization_id?: string; preferred_agent?: TaskPreferredAgent; selected_execution_agent?: TaskPreferredAgent } }
  | { type: 'session_updated'; payload: { project_id: string; task_id: string; exec_mode?: string; company_profile?: string; org_id?: string; organization_id?: string; preferred_agent?: TaskPreferredAgent; selected_execution_agent?: TaskPreferredAgent } }
  | { type: 'session_message'; payload: Record<string, unknown> }
  | { type: 'session_title_updated'; payload: { project_id: string; task_id: string; title: string } }
  | { type: 'session_deleted'; payload: { project_id: string; task_id: string } }
  | { type: 'child_session_created'; payload: { project_id: string; session_id: string; parent_session_id: string; task_id: string; title: string; agent_id?: string; selected_execution_agent?: TaskPreferredAgent } }
  | { type: 'session_progress'; payload: SessionProgressPayload }
  | { type: 'work_item_progress'; payload: WorkItemProgressPayload }
  | { type: 'org_info'; payload: OrgInfoPayload }
  | { type: 'project_run_updated'; payload: ProjectRunInfo }
  | { type: 'seat_digest_updated'; payload: { run_id?: string; seat_digests: SeatDigestInfo[] } }
  | { type: 'work_item_batch_updated'; payload: { run_id?: string; work_items: RuntimeWorkItemInfo[]; frontier?: RuntimeFrontierSummary } }
  | { type: 'project_recovery_updated'; payload: Record<string, unknown> }
  | { type: 'project_revision_created'; payload: { run_id?: string; revision_links: SessionLinkInfo[] } }
  | { type: 'talent_list'; payload: TalentListPayload }
  | { type: 'talent_scan_local'; payload: { templates: Array<{ template_id: string; name: string; description: string; category: string; domains: string[]; tags: string[] }> } }
  | { type: 'employee_detail'; payload: EmployeeDetailPayload }
  | { type: 'reorg_list'; payload: ReorgListPayload }
  | { type: 'market_list_installed'; payload: { packages: Array<Record<string, unknown>> } }
  | { type: 'market_browse'; payload: { presets: Array<Record<string, unknown>> } }
  | { type: 'market_preview'; payload: Record<string, unknown> }
  | { type: 'org_config_export'; payload: { yaml: string } }
  | { type: 'org_config_import'; payload: { ok: boolean; dry_run?: boolean; preview?: { roles_added: number; roles_removed: number; employees_changed: number }; error?: string; validation_errors?: string[] } }
  | { type: 'org_saved_list'; payload: { orgs: SavedOrgSummary[]; active_name?: string | null } }
  | { type: 'org_saved_save_as'; payload: { ok: boolean; name: string; error?: string } }
  | { type: 'org_saved_create'; payload: OrgSavedCreatePayload }
  | { type: 'org_saved_load'; payload: { ok: boolean; name: string; error?: string } }
  | { type: 'org_saved_delete'; payload: { ok: boolean; name: string; error?: string } }
  | { type: 'project_switched'; payload: { project_id: string; switch_seq?: string } }
  | { type: 'project_deleted'; payload: { project_id: string } }
  | { type: 'comms_state'; payload: Record<string, unknown> }
  | { type: 'comms_message'; payload: Record<string, unknown> }
  | { type: 'comms_state_dirty'; payload: { project_id: string; [key: string]: unknown } }
  | { type: 'runtime_status_sync'; payload: { project_id: string; sessions: Array<{ task_id: string; status: string; agent_status?: string; current_tool?: string | null }> } }

export type SocketStatus = 'connecting' | 'connected' | 'disconnected' | 'error'

export interface AgentInfo {
  agent_id: string
  name: string
  description: string
  specialties: string[]
  status: string
  office_id?: string
  appearance: { palette: number; hue_shift: number; seat_zone: string; desk_id?: string }
  employee_id?: string
  opc_role_id?: string
  /** Runtime state from EventAdapter (updated via agent_runtime_update) */
  runtime_status?: AgentAnimStatus
  current_tool?: string
  current_task_id?: string
}

// ── New WS payload types ────────────────────────────────────────────────────

export interface AgentRuntimePayload {
  agent_id: string
  status: AgentAnimStatus
  current_tool: string | null
  display_tool?: string | null
  task_id: string | null
  iteration: number | null
  tool_elapsed_ms?: number | null
  last_tool_summary?: string | null
  context_tokens?: number | null
  context_window?: number | null
  context_remaining_pct?: number | null
  input_tokens?: number | null
  output_tokens?: number | null
  total_tokens?: number | null
  turn_cost_usd?: number | null
  session_cost_usd?: number | null
  pending_permission_count?: number | null
  drain_mode?: string | null
}

export interface WorkerNotificationPayload {
  worker_id?: string
  worker_type?: string
  notification_kind?: string
  summary?: string
  task_id?: string
  session_id?: string
  work_item_projection_id?: string
  resident_status?: string
  pending_messages_count?: number
  actionable_inbox_count?: number
  protocol_backlog_count?: number
  notification_backlog_count?: number
  latest_notification?: Record<string, unknown> | null
  [key: string]: unknown
}

export interface SessionProgressPayload {
  task_id: string
  entry: {
    type: string
    summary: string
    detail?: string
    timestamp: number
    turn_id?: string
    turnId?: string
    item_id?: string
    itemId?: string
    stream_id?: string
    streamId?: string
    tool_call_id?: string
    toolCallId?: string
    permission_group_key?: string
    permissionGroupKey?: string
    seq?: number
    execution_mode?: string
    executionMode?: string
  }
}

export interface WorkItemProgressPayload {
  task_id: string
  runtime_task_id?: string
  execution_turn_id?: string
  entry: {
    type: string
    summary?: string
    detail?: string
    work_item_projection_id?: string
    work_item_projection_title?: string
    work_item_turn_type?: string
    runtime_task_id?: string
    execution_turn_id?: string
    role_name?: string
    timestamp: number
  }
}

export interface KanbanViewDataPayload {
  boards: Record<string, unknown>[]
  columns: Record<string, unknown>[]
  tasks: Record<string, unknown>[]
  work_item_projections: Record<string, unknown>[]
}

// ── Org Info types ──────────────────────────────────────────────────────────

export interface OrgRole {
  role_id: string
  name: string
  responsibility: string
  status: string
  reports_to: string
  icon?: string | null
  can_spawn: string[]
  tools: string[]
  is_builtin?: boolean
  execution_strategy?: string
  preferred_external_agent?: string | null
  prompt_refs?: string[]
  runtime_policy?: { execution_strategy?: string }
  role_type?: string
  skill_refs?: string[]
  artifact_contract_ref?: string | null
}

export interface OrgEmployee {
  employee_id: string
  name: string
  role_id: string
  role_ids?: string[]
  category: string
  domains: string[]
  seniority: string
  status: string
  tags: string[]
  skill_refs: string[]
  experience_score: number
  learned_skill_refs: string[]
  linked_agent_id?: string
  is_default_employee?: boolean
}

export interface RuntimeTeamInfo {
  cell_id: string
  team_instance_id?: string
  team_id?: string
  manager_role_id: string
  member_role_ids: string[]
  seat_ids?: string[]
  parent_team_id?: string
  status: string
  is_final_decider_cell?: boolean
}

export interface RuntimeSeatInfo {
  role_session_id: string
  role_id: string
  employee_id: string
  team_id?: string
  team_instance_id?: string
  seat_id?: string
  focused_work_item_id?: string
  current_work_item_id?: string
  background_work_item_ids?: string[]
  manager_role_ids?: string[]
  manager_seat_id?: string
  resident_status?: string
  latest_notification?: Record<string, unknown>
  manager_digest?: Record<string, unknown>
  status: string
}

export interface RuntimeWorkItemInfo {
  work_item_id: string
  role_id: string
  cell_id: string
  team_id?: string
  team_instance_id?: string
  seat_id?: string
  title: string
  kind: string
  /** Single source of truth: the work-item state-machine value. */
  phase: string
  /** Pure-function projection of `phase` to the kanban column id. */
  kanban_column: string
  batch_id?: string
  batch_index?: number
  deliverable_summary?: string
  blocked_reason?: string
  handoff_status?: string
  parent_work_item_id?: string | null
  work_item_projection_id?: string
  metadata?: Record<string, unknown>
  adaptive?: Record<string, unknown>
}

export interface RuntimeFrontierSummary {
  run_id?: string
  status?: string
  total_cells?: number
  total_role_sessions?: number
  total_work_items?: number
  ready_count?: number
  running_count?: number
  blocked_count?: number
  waiting_count?: number
  done_count?: number
  failed_count?: number
}

export interface ProjectRunInfo {
  run_id?: string
  project_id?: string
  session_id?: string
  status?: string
  lifecycle_status?: string
  company_profile?: string
  execution_model?: string
  current_revision?: number
  latest_deliverable_summary?: string
  recovery_pointer?: Record<string, unknown>
}

export interface ProjectDossierInfo {
  project_id?: string
  run_id?: string
  latest_deliverable_summary?: string
  architecture_decisions?: Array<Record<string, unknown>>
  completed_work_items?: Array<Record<string, unknown>>
  open_issues?: string[]
  verification_summary?: string
  artifact_index?: Array<Record<string, unknown>>
  handoff_summaries?: Array<Record<string, unknown>>
  last_failure_summary?: string
  project_memory_excerpt?: string
  session_memory_excerpt?: string
}

export interface SeatDigestInfo {
  seat_id: string
  team_id?: string
  role_id?: string
  employee_id?: string
  role_session_id?: string
  resident_status?: string
  current_work_item?: Record<string, unknown>
  latest_notification?: Record<string, unknown>
  manager_digest?: Record<string, unknown>
}

export interface SessionLinkInfo {
  link_id?: string
  session_id?: string
  linked_session_id?: string | null
  link_type?: string
  metadata?: Record<string, unknown>
  created_at?: string
}

export interface ChannelStatusInfo {
  name: string
  enabled: boolean
  running: boolean
  configured: boolean
  available: boolean
  ready: boolean
  last_error: string | null
  delivery_mode: string
}

export interface ConnectorInfo {
  connector_id: string
  name: string
  connector_type: string
  description: string
  actions: string[]
}

export interface InstalledPackageInfo {
  package_id: string
  name: string
  version: string
  installed_at: string
  source_path: string
  role_ids: string[]
  template_ids: string[]
  work_item_template_ids: string[]
}

export interface SandboxReport {
  passed: boolean
  warnings: string[]
  errors: string[]
}

export interface ArchitecturePreset {
  id: string
  name: string
  description: string
  category: string
  tags: string[]
  team_size: string
  emoji: string
  color: string
  roles_count: number
  work_item_templates_count: number
  gates_count: number
  collaboration_pattern: string
  dag_summary: string
}

export interface ArchitecturePresetDetail {
  id: string
  name: string
  description: string
  category: string
  tags: string[]
  team_size: string
  emoji: string
  color: string
  roles: Array<{ id: string; name: string; responsibility: string; reports_to: string; can_spawn?: string[] }>
  work_item_templates: Array<{
    id: string; title: string; role_id: string; dependencies: string[]
    parallel_group: string | null
    gate?: { type: string; reviewer_role: string } | null
  }>
}

export interface RuntimePolicy {
  communication?: { default_mode?: string; blocking_default?: boolean; allow_broadcast?: boolean }
  handoff?: { require_structured_handoff?: boolean; require_ack?: boolean }
  review?: { strict_gate_inference?: boolean; allow_human_override?: boolean }
  parallel?: { auto_dispatch?: boolean; max_workers?: number }
}

export interface OrgInfoPayload {
  roles: OrgRole[]
  employees: OrgEmployee[]
  company_profile: string
  organization_id?: string
  organization_name?: string
  organization_config_file?: string
  final_decider_role_id?: string | null
  top_level_role_ids?: string[]
  runtime_teams?: RuntimeTeamInfo[]
  runtime_seats?: RuntimeSeatInfo[]
  runtime_topology_preview?: Record<string, unknown>
  work_item_runtime_preview?: Record<string, unknown>
  work_items?: RuntimeWorkItemInfo[]
  frontier?: RuntimeFrontierSummary
  project_run?: ProjectRunInfo
  project_dossier?: ProjectDossierInfo
  seat_digests?: SeatDigestInfo[]
  revision_links?: SessionLinkInfo[]
  project_recovery?: Record<string, unknown>
  channels: ChannelStatusInfo[]
  connectors: ConnectorInfo[]
  org_version: number
  runtime_topology_version: number
  installed_packages?: InstalledPackageInfo[]
  runtime_policy?: RuntimePolicy
}

// ── Phase 4: Talent Market ────────────────────────────────────────────────

export interface TalentTemplate {
  template_id: string
  name: string
  description: string
  category: string
  domains: string[]
  tags: string[]
  preferred_external_agent: string | null
  source_repo: string
  emoji?: string
  color?: string
  vibe?: string
  is_builtin?: boolean
}

export interface TalentListPayload {
  templates: TalentTemplate[]
  talent_dir?: string
}

// ── Phase 4: Employee Detail ──────────────────────────────────────────────

export interface EmployeeDetailPayload {
  employee_id: string
  name: string
  role_id: string
  category: string
  domains: string[]
  seniority: string
  tags: string[]
  skill_refs: string[]
  experience_score: number
  learned_skill_refs: string[]
  delta_context: string
  profile: Record<string, unknown>
}

// ── Phase 4: Reorg Proposals ──────────────────────────────────────────────

export interface ReorgChangesetSummary {
  role_changes: { action: string; role_id: string; reason: string }[]
  work_item_projection_changes: { action: string; work_item_projection_id: string; reason: string }[]
  task_adjustments_count: number
}

export interface ReorgProposalInfo {
  proposal_id: string
  title: string
  summary: string
  rationale: string
  scope: string
  risk_level: string
  status: string
  initiated_by: string
  changeset: ReorgChangesetSummary
  impact_summary: Record<string, unknown>
  created_at: number
  updated_at: number
}

export interface ReorgListPayload {
  proposals: ReorgProposalInfo[]
}
