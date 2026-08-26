import type { TaskPreferredAgent } from './kanban'

export type ChannelType = 'session' | 'activity' | 'secretary'

export interface ChatChannel {
  id: string
  type: ChannelType
  name: string
  officeId?: string
  participants: string[]
  pinned: boolean
  createdAt: number
}

export interface ChatAttachment {
  name: string
  path: string
}

export interface AttachmentRefMeta {
  attachment_id: string
  filename: string
  mime_type: string
  size_bytes: number
  disk_path?: string
}

export interface OutgoingAttachmentPayload {
  filename: string
  data: string
  mime_type?: string
}

export type DetailVisibility = 'summary' | 'full'

export interface ExecutionDraftState {
  text: string
  updatedAt: number
  iteration?: number
}

export type ExecutionTimelineItem =
  | { kind: 'message'; id: string; timestamp: number; message: ChatMessage }
  | { kind: 'progress'; id: string; timestamp: number; entry: import('./kanban').ProgressEntry }
  | { kind: 'draft'; id: string; timestamp: number; draft: ExecutionDraftState }

// ── Checkpoint metadata types ─────────────────────────────────────────────

export interface RecruitmentCandidate {
  template_id: string
  template_name: string
  category: string
  domains: string[]
  proposed_name: string
  rationale: string
}

export interface RecruitmentExistingEmployee {
  employee_id: string
  employee_name: string
  role_id: string
  domains: string[]
  experience_score: number
  rationale: string
}

export interface RecruitmentProposalEntry {
  role_id: string
  status: 'existing_staff' | 'proposed_hire' | 'fallback_role_only' | 'direct_role_execution'
  rationale: string
  role_labels: string[]
  candidate?: RecruitmentCandidate
  existing_employee?: RecruitmentExistingEmployee
  existing_employee_ids?: string[]
  default_agent?: TaskPreferredAgent
  selected_agent?: TaskPreferredAgent
}

export interface RecruitmentRationaleEntry {
  role_id: string
  role_label?: string
  status?: string
  selection_label?: string
  rationale?: string
}

export interface RecruitmentCheckpointMeta {
  checkpoint_type: 'company_recruitment_confirmation'
  checkpoint_id: string
  company_profile: string
  proposals: RecruitmentProposalEntry[]
  summary: string
  recruitment_rationales?: RecruitmentRationaleEntry[]
  staffing_roles?: StaffingRoleEntry[]
  staffing_pool?: StaffingCheckpointMeta['staffing_pool']
  staffing_selections?: Record<string, StaffingSelectionValue>
  recruitment_role_agents?: Record<string, TaskPreferredAgent>
  recruitment_agent?: TaskPreferredAgent
}

export type StaffingSelectionKind = 'employee' | 'template' | 'fallback'

export interface StaffingSelectionValue {
  kind: StaffingSelectionKind
  id?: string
  employee_id?: string
  template_id?: string
  experience_mode?: 'with_experience' | 'template_only'
}

export interface StaffingRoleEntry {
  role_id: string
  role_label: string
  reports_to?: string
  role_responsibility?: string
  default_selection?: StaffingSelectionValue
  same_role_employee_ids?: string[]
  fallback_available?: boolean
  default_agent?: TaskPreferredAgent
  selected_agent?: TaskPreferredAgent
  default_source?: string
  staffing_locked?: boolean
  staffing_mode?: 'opaque_external_team' | string
  external_team_binding_id?: string
  covered_role_ids?: string[]
  capability_manifest?: Record<string, unknown>
}

export interface StaffingEmployeeOption {
  kind?: 'employee'
  employee_id: string
  employee_name: string
  template_id?: string
  role_id: string
  home_role_id?: string
  category?: string
  domains?: string[]
  tags?: string[]
  description?: string
  preferred_external_agent?: string | null
  experience_score?: number
}

export interface StaffingTemplateOption {
  kind?: 'template'
  template_id: string
  template_name: string
  category?: string
  domains?: string[]
  tags?: string[]
  description?: string
  preferred_external_agent?: string | null
  source_repo?: string
  source_path?: string
}

export interface StaffingCheckpointMeta {
  checkpoint_type: 'company_staffing_selection'
  checkpoint_id: string
  company_profile: string
  summary?: string
  staffing_strategy?: string
  recommended_action?: 'manual_approve' | 'auto_recruit' | string
  staffing_defaults?: {
    source?: string
    scope_key?: string
    updated_at?: string
  }
  staffing_roles: StaffingRoleEntry[]
  staffing_pool: {
    employees?: StaffingEmployeeOption[]
    templates?: StaffingTemplateOption[]
  }
  staffing_action?: string
  staffing_selections?: Record<string, StaffingSelectionValue>
  recruitment_role_agents?: Record<string, TaskPreferredAgent>
  recruitment_agent?: TaskPreferredAgent
}

export interface ReorgRoleChange {
  action: 'add' | 'remove' | 'replace' | 'update'
  role_id: string
  replacement_role_id?: string
  reason: string
}

export interface ReorgWorkItemProjectionChange {
  action: 'add' | 'remove' | 'replace' | 'update'
  work_item_projection_id: string
  replacement_work_item_projection_id?: string
  reason: string
}

export interface ReorgCheckpointMeta {
  checkpoint_type: 'company_reorg_pending'
  checkpoint_id: string
  proposal_id: string
  scope: string
  risk_level: string
  status: string
  title: string
  summary: string
  rationale: string
  role_changes: ReorgRoleChange[]
  work_item_projection_changes: ReorgWorkItemProjectionChange[]
  impact_summary: Record<string, any>
  user_confirmation_required: boolean
}

export interface InteractionOption {
  id: string
  label: string
  description?: string
}

export interface TaskUserInputOption {
  id: string
  label: string
  description?: string
}

export interface TaskUserInputQuestion {
  id: string
  header?: string
  question: string
  options?: TaskUserInputOption[]
  allow_freeform?: boolean
  required?: boolean
}

export interface TaskUserInputAnswer {
  question_id?: string
  question?: string
  selected_option_id?: string
  selected_label?: string
  freeform_text?: string
  answer_text?: string
}

export interface CheckpointReplyMetadata {
  response_to_checkpoint_id?: string
  response_to_checkpoint_type?: string
  ui_message_id?: string
  checkpoint_reply_kind?: 'approve' | 'deny' | 'feedback' | 'ignore'
  self_evolution_trigger?: boolean
  human_feedback_text?: string
  recruitment_role_agents?: Record<string, TaskPreferredAgent>
  recruitment_agent?: TaskPreferredAgent
  staffing_action?: 'manual_approve' | 'auto_recruit' | 'deny'
  staffing_selections?: Record<string, StaffingSelectionValue>
  user_input_answers?: Record<string, TaskUserInputAnswer>
  interaction_option_id?: string
  interaction_option_label?: string
  interaction_requester_task_id?: string
  interaction_requester_session_id?: string
}

/** Metadata accepted by the ordinary chat composer transport. */
export interface SessionSendMetadata {
  ui_message_id?: string
}

export interface InteractionDecision {
  text: string
  option_id?: string
  option_label?: string
  checkpoint_reply_kind?: CheckpointReplyMetadata['checkpoint_reply_kind']
  self_evolution_trigger?: boolean
  human_feedback_text?: string
  recruitment_role_agents?: Record<string, TaskPreferredAgent>
  recruitment_agent?: TaskPreferredAgent
  staffing_action?: CheckpointReplyMetadata['staffing_action']
  staffing_selections?: Record<string, StaffingSelectionValue>
  user_input_answers?: Record<string, TaskUserInputAnswer>
}

export interface InteractionReplyRequest {
  checkpointId: string
  checkpointType: string
  clientRequestId: string
  requesterTaskId?: string
  requesterSessionId?: string
  decision: InteractionDecision
}

export interface InteractionReplyReceipt {
  ok: boolean
  accepted: boolean
  deduplicated?: boolean
  status?: string
  reason?: string
  error?: string
  checkpoint_id: string
  checkpoint_type: string
  client_request_id: string
}

export interface PermissionCheckpointMeta {
  checkpoint_type: 'tool_permission' | 'action_permission'
  checkpoint_id: string
  prompt: string
  summary: string
  options: InteractionOption[]
  default_action?: string
  tool_call_id?: string
  tool_name?: string
  tool_risk_level?: string
  action_kind?: string
  action_name?: string
}

export interface CompanyWorkItemGateMeta {
  checkpoint_type: 'company_work_item_gate'
  checkpoint_id: string
  work_item_projection_id?: string
  work_item_turn_type?: string
  work_item_projection_title?: string
  company_profile?: string
  summary?: string
  prompt?: string
  options?: InteractionOption[]
  default_action?: string
  runtime_session_id?: string
  resume_cursor?: number
  active_subagents?: Array<Record<string, unknown>>
  permission_requests?: Array<Record<string, unknown>>
  worktree_path?: string
  tool_call_id?: string
  tool_name?: string
  tool_risk_level?: string
  action_kind?: string
  action_name?: string
  interaction_requester_task_id?: string
  interaction_requester_session_id?: string
}

export interface CompanyDeliveryFeedbackMeta {
  checkpoint_type: 'company_delivery_feedback'
  checkpoint_id: string
  work_item_projection_id?: string
  work_item_turn_type?: string
  work_item_projection_title?: string
  company_profile?: string
  feedback_scope?: string
  summary?: string
  prompt?: string
  options?: InteractionOption[]
  runtime_session_id?: string
  resume_cursor?: number
  active_subagents?: Array<Record<string, unknown>>
  permission_requests?: Array<Record<string, unknown>>
  worktree_path?: string
}

export interface TaskUserInputCheckpointMeta {
  checkpoint_type: 'task_user_input'
  checkpoint_id: string
  task_id: string
  work_item_projection_id?: string
  work_item_turn_type?: string
  work_item_projection_title?: string
  prompt: string
  summary: string
  questions: string[]
  input_questions?: TaskUserInputQuestion[]
  required_fields: string[]
  context_note?: string
  resume_hint?: string
  requesting_role_id?: string
  requesting_task_id?: string
  requesting_work_item_id?: string
  seat_id?: string
  runtime_session_id?: string
  resume_cursor?: number
  active_subagents?: Array<Record<string, unknown>>
  permission_requests?: Array<Record<string, unknown>>
  worktree_path?: string
}

export type CheckpointMeta =
  | StaffingCheckpointMeta
  | RecruitmentCheckpointMeta
  | ReorgCheckpointMeta
  | CompanyWorkItemGateMeta
  | CompanyDeliveryFeedbackMeta
  | PermissionCheckpointMeta
  | TaskUserInputCheckpointMeta

// ── General message metadata ──────────────────────────────────────────────

export interface ChatMessageMeta {
  type?: 'task_created' | 'task_assigned' | 'collab_request' | 'system'
  taskId?: string
  task_id?: string
  boardId?: string
  source?: string
  kind?: string
  ui_message_id?: string
  ui_created_at?: number
  /** UI-only identity retained across semantic result-surface replacement. */
  ui_timeline_id?: string
  canonical_turn_id?: string
  conversation_turn_id?: string
  turn_id?: string
  execution_turn_id?: string
  result_delivery_id?: string
  source_result_message_id?: string
  source_task_id?: string
  child_session_id?: string
  execution_mode?: string
  transcript_kind?: string
  detail_visibility?: DetailVisibility
  runtime_thinking?: string
  runtime_iteration?: number
  stop_reason?: string
  attachment_refs?: AttachmentRefMeta[]
  // Checkpoint interactive metadata (present on checkpoint response messages)
  checkpoint_type?: string
  checkpoint_id?: string
  checkpoint_status?: string
  checkpoint_response_message_id?: string
  checkpoint_responded_at?: number | string
  // Recruitment-specific fields
  company_profile?: string
  previous_checkpoint_id?: string
  recruitment_revision?: number
  recruiter_feedback?: string[]
  proposals?: RecruitmentProposalEntry[]
  recruitment_rationales?: RecruitmentRationaleEntry[]
  summary?: string
  recruitment_role_agents?: Record<string, TaskPreferredAgent>
  recruitment_agent?: TaskPreferredAgent
  // Manual staffing-specific fields
  staffing_roles?: StaffingRoleEntry[]
  staffing_pool?: StaffingCheckpointMeta['staffing_pool']
  staffing_strategy?: string
  recommended_action?: 'manual_approve' | 'auto_recruit' | string
  staffing_defaults?: StaffingCheckpointMeta['staffing_defaults']
  staffing_action?: string
  staffing_selections?: Record<string, StaffingSelectionValue>
  // Reorg-specific fields
  proposal_id?: string
  scope?: string
  risk_level?: string
  status?: string
  title?: string
  rationale?: string
  role_changes?: ReorgRoleChange[]
  work_item_projection_changes?: ReorgWorkItemProjectionChange[]
  impact_summary?: Record<string, any>
  user_confirmation_required?: boolean
  // Typed owner-interaction fields
  escalation_id?: string
  escalation_type?: string
  prompt?: string
  options?: InteractionOption[]
  default_action?: string
  // Generic task user input checkpoint fields
  questions?: string[]
  input_questions?: TaskUserInputQuestion[]
  required_fields?: string[]
  context_note?: string
  resume_hint?: string
  requesting_role_id?: string
  requesting_task_id?: string
  requesting_work_item_id?: string
  seat_id?: string
  work_item_projection_id?: string
  work_item_turn_type?: string
  work_item_projection_title?: string
  feedback_scope?: string
  runtime_session_id?: string
  resume_cursor?: number
  active_subagents?: Array<Record<string, unknown>>
  permission_requests?: Array<Record<string, unknown>>
  worktree_path?: string
  // Typed tool/external-action permission fields
  tool_call_id?: string
  tool_name?: string
  tool_risk_level?: string
  action_kind?: string
  action_name?: string
  interaction_requester_task_id?: string
  interaction_requester_session_id?: string
  // User reply linkage for checkpoint panels
  response_to_checkpoint_id?: string
  response_to_checkpoint_type?: string
  response_to_escalation_id?: string
  checkpoint_reply_kind?: 'approve' | 'deny' | 'feedback' | 'ignore'
  self_evolution_completed?: boolean
  notification_kind?: string
}

export interface ChatMessage {
  id: string
  channelId: string
  sender: string
  senderName: string
  content: string
  timestamp: number
  replyToId?: string
  mentions: string[]
  attachments?: ChatAttachment[]
  metadata?: ChatMessageMeta
  senderDeleted?: boolean
}
