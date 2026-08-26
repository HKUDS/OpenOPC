// ── Priority ────────────────────────────────────────────────────────────────

export type TaskPriority = 'urgent' | 'high' | 'medium' | 'low'

export const PRIORITY_META: Record<TaskPriority, { label: string; color: string; symbol: string }> = {
  urgent: { label: 'Urgent', color: 'var(--red)', symbol: '\u25B2\u25B2' },
  high: { label: 'High', color: 'var(--yellow)', symbol: '\u25B2' },
  medium: { label: 'Medium', color: 'var(--accent)', symbol: '\u25A0' },
  low: { label: 'Low', color: 'var(--text-secondary)', symbol: '\u25BD' },
}

// ── Project ─────────────────────────────────────────────────────────────────

export interface Project {
  id: string
  name: string
}

// ── Board ───────────────────────────────────────────────────────────────────

export interface KanbanBoard {
  id: string
  name: string
  description?: string
  color: string
  officeId?: string
  prefix: string
  nextTaskNum: number
  createdAt: number
  updatedAt: number
}

export const BOARD_COLORS = ['#4f46e5', '#059669', '#d97706', '#dc2626', '#7c3aed', '#0891b2']

export interface KanbanFilterState {
  searchQuery: string
  priorities: TaskPriority[]
  assigneeIds: string[]
}

export type KanbanViewState =
  | { level: 'global' }
  | { level: 'office'; targetId: string; targetLabel?: string }
  | { level: 'agent'; targetId: string; targetLabel?: string }

// ── Column ──────────────────────────────────────────────────────────────────

export interface KanbanColumn {
  id: string
  boardId: string
  name: string
  color: string
  sortOrder: number
  isTerminal: boolean
  /** Role name shown below column title in Company Mode */
  roleLabel?: string
  /** Gate icon for review/approval work items */
  gateType?: 'review' | 'approval' | 'human_confirmation'
  /** Whether this column runs in parallel with others */
  isParallel?: boolean
}

export const DEFAULT_COLUMN_DEFS: Array<{ name: string; color: string; isTerminal: boolean }> = [
  { name: 'Todo', color: 'var(--text-secondary)', isTerminal: false },
  { name: 'In Progress', color: 'var(--yellow)', isTerminal: false },
  { name: 'Done', color: 'var(--green)', isTerminal: true },
]

// ── Employee Assignment (Company Mode) ──────────────────────────────────────

export interface EmployeeAssignment {
  name?: string
  employeeId?: string
  category?: string
  experienceScore?: number
  domains?: string[]
  preferredExternalAgent?: string
  promptContext?: string
  deltaContext?: string
  skillRefs?: string[]
}

// ── Work-Item Gate (Company Mode) ───────────────────────────────────────────

export interface WorkItemGate {
  type?: string                // 'review' | 'approval' | 'human_confirmation'
  reviewerRole?: string
  autoApprove?: boolean
  criteria?: string
}

// ── Agent Runtime State (mirrors backend AgentAnimState) ────────────────────

export type AgentAnimStatus = 'idle' | 'reflecting' | 'tool_active'

export const AGENT_STATUS_LABEL: Record<AgentAnimStatus, string> = {
  idle: 'Idle',
  reflecting: 'Thinking...',
  tool_active: 'Running tool',
}

// ── Task ────────────────────────────────────────────────────────────────────

/**
 * The 14 unified backend phases. Carried alongside the kanban column id so
 * the UI can render the precise sub-state ("waiting for peer" vs "running")
 * within the four columns.
 */
export type KanbanPhase =
  | 'queued' | 'ready' | 'ready_for_rework' | 'waiting_dependencies'
  | 'running' | 'waiting_for_peer' | 'waiting_for_children' | 'paused' | 'needs_attention'
  | 'awaiting_manager_review' | 'awaiting_human'
  | 'approved' | 'failed' | 'cancelled'

export interface KanbanTask {
  id: string
  displayId: string
  boardId: string
  columnId: string
  title: string
  description?: string
  /** Single-source-of-truth phase from backend (backend-driven kanban-push). */
  phase?: KanbanPhase
  priority: TaskPriority | null
  assigneeIds: string[]
  tags: string[]
  sortOrder: number
  /**
   * Audit reference to the runtime Task's session, used for chat routing.
   * NOT the card identity in Company Mode — that is `id` (= work_item_id).
   */
  sessionId?: string
  /** DelegationWorkItem.work_item_id — present in Company Mode. Mirrors `id`. */
  workItemId?: string
  /** Runtime Task backing this card. In Company Mode this is the linked execution turn, not the card id. */
  runtimeTaskId?: string
  /** User-facing alias for the runtime Task backing an execution turn. */
  executionTurnId?: string
  createdAt: number
  updatedAt: number

  // Agent runtime info (populated from agent_runtime_update WS events)
  agentStatus?: AgentAnimStatus
  currentTool?: string
  displayTool?: string
  iterationCount?: number
  toolElapsedMs?: number
  lastToolSummary?: string
  contextTokens?: number
  contextWindow?: number
  contextRemainingPct?: number
  inputTokens?: number
  outputTokens?: number
  totalTokens?: number
  turnCostUsd?: number
  sessionCostUsd?: number
  pendingPermissionCount?: number
  drainMode?: string
  residentStatus?: string
  actionableInboxCount?: number
  protocolBacklogCount?: number
  notificationBacklogCount?: number
  latestNotification?: WorkerNotification

  // Work-item runtime identity (populated in Company Mode).
  workItemProjectionId?: string
  workItemTurnType?: string
  companyProfile?: string
  orgId?: string
  workItemRoleId?: string
  workItemRoleName?: string
  workItemGate?: WorkItemGate
  runtimeSessionId?: string
  resumeCursor?: number
  worktreePath?: string
  blockedReason?: string
  reviewVerdict?: string  // 'approve' | 'reject' — agent's actual verdict
  reviewSummary?: string  // agent's review summary text
  reviewOwnerRoleId?: string
  reviewOwnerSeatId?: string
  managerRoleId?: string
  managerSeatId?: string
  scopeKey?: string
  completionReport?: string
  reworkFeedback?: string
  planningContext?: string
  deliverables?: string[]
  acceptanceCriteria?: string[]
  delegationRationale?: string
  nonOverlapGuard?: string
  coordinationNotes?: string
  originalMessage?: string
  residentAssignment?: Record<string, unknown>
  memberSessionState?: Record<string, unknown>
  ownershipContract?: Record<string, unknown>

  // Employee assignment (Company Mode)
  employeeAssignment?: EmployeeAssignment
  selectedExecutionAgent?: TaskPreferredAgent
  executionUnitKind?: string
  externalTeamBindingId?: string
  externalTeamBoundaryRoleId?: string
  coveredRoleIds?: string[]
  externalCompanyExecutionFence?: string

  // Origin channel (where the task originated)
  originChannel?: string

  // Dependency graph
  dependencies?: string[]   // upstream task IDs that must complete first
  dependents?: string[]     // downstream task IDs waiting on this task

  // Activity log (most recent entries)
  progressLog?: ProgressEntry[]

  // Handoff context from upstream work item (Company Mode)
  handoffContext?: string
}

// ── Progress Entry (per-task activity log) ──────────────────────────────────

export type ProgressEntryType =
  | 'thinking' | 'assistant' | 'tool_call' | 'autonomy' | 'handoff' | 'gate_result' | 'status_change'
  | 'work_item_started' | 'gate_approved' | 'gate_rejected'
  | 'awaiting_manager_review' | 'awaiting_human' | 'awaiting_review' | 'awaiting_peer'
  | 'work_item_failed' | 'deadlock' | 'needs_input' | 'verification'

export interface ProgressEntry {
  timestamp: number
  type: ProgressEntryType
  summary: string   // e.g. "file_read" or "Gate: approved"
  detail?: string   // e.g. tool arguments preview
  turnId?: string
  itemId?: string
  streamId?: string
  toolCallId?: string
  permissionGroupKey?: string
  seq?: number
  executionMode?: string
}

// ── Work-Item Progress Entry (primary session timeline) ─────────────────────

export interface WorkItemProgressEntry {
  timestamp: number
  type: ProgressEntryType
  workItemProjectionId?: string
  workItemTurnType?: string
  workItemProjectionTitle?: string
  runtimeTaskId?: string
  executionTurnId?: string
  roleName?: string
  detail?: string
}

export interface WorkerNotification {
  workerId?: string
  workerType?: string
  notificationKind?: string
  summary?: string
  residentStatus?: string
  pendingMessagesCount?: number
  [key: string]: unknown
}

// ── Session (merged task + channel) ──────────────────────────────────────────

export type SessionMode = 'primary' | 'child'
export type TaskPreferredAgent = 'native' | 'codex' | 'claude_code' | 'cursor' | 'opencode' | 'jiuwen' | 'jiuwenswarm'

export interface Session {
  projectId: string
  taskId: string
  /** Runtime Task id. Mirrors taskId for session rows, but gives UI code a semantic name. */
  runtimeTaskId?: string
  /** User-facing alias for the runtime Task backing this execution turn. */
  executionTurnId?: string
  channelId: string
  sessionId?: string
  parentSessionId?: string
  mode?: SessionMode
  execMode?: string
  companyProfile?: string
  orgId?: string
  preferredAgent?: TaskPreferredAgent
  title: string
  status: string
  columnId: string
  assigneeIds: string[]
  priority: string | null
  tags: string[]
  agentStatus?: string
  currentTool?: string
  displayTool?: string
  progressLog: ProgressEntry[]
  createdAt: number
  updatedAt: number
  messageCount: number
  latestPreview?: string
  latestSender?: string
  latestMessageId?: string
  indexLoaded?: boolean
  detailLoaded?: boolean
  fullLoaded?: boolean
  hasMore?: boolean
  /** Pagination state for the committed company/task transcript. */
  summaryHasMore?: boolean
  /** Pagination state for the full child/runtime transcript. */
  fullHasMore?: boolean
  detailLoading?: boolean
  detailError?: string
  viewGeneration?: number
  // Company Mode work-item metadata.
  workItemProjectionId?: string
  workItemTurnType?: string
  workItemRoleId?: string
  workItemRoleName?: string
  workItemGate?: WorkItemGate
  employeeAssignment?: EmployeeAssignment
  selectedExecutionAgent?: TaskPreferredAgent
  executionUnitKind?: string
  externalTeamBindingId?: string
  externalTeamBoundaryRoleId?: string
  coveredRoleIds?: string[]
  externalCompanyExecutionFence?: string
  originChannel?: string
  originTaskId?: string
  runtimeControlState?: 'running' | 'suspending' | 'suspended' | 'resuming' | 'idle'
  canStop?: boolean
  canResume?: boolean
  resumeParentSessionId?: string
  pendingRuntimeCheckpointId?: string
  stopIntentId?: string
  // Handoff context from upstream work item (Company Mode)
  handoffContext?: string
  // Downstream handoff (what this work item passed to next)
  handoffTo?: string
  // Artifacts produced by this work item
  artifacts?: string[]
  // Work-item runtime state (Company Mode primary sessions)
  isCompanyRuntime?: boolean
  workItemLog?: WorkItemProgressEntry[]
  /**
   * Per-role DelegationWorkItem rollup grouped by current owner. Present on
   * primary company-mode sessions only.
   * Builder: ``snapshot_builder._build_role_work_items_for_session``.
   */
  roleWorkItems?: Record<string, RoleWorkItemSummary>
  /**
   * Display-only DelegationWorkItem rollup grouped by original executor role.
   * Execution Progress prefers this so worker chips stay visible during review.
   */
  executorRoleWorkItems?: Record<string, RoleWorkItemSummary>
  // Native Runtime V2 state
  runtimeSessionId?: string
  resumeCursor?: number
  activeSubagents?: Array<Record<string, unknown>>
  permissionRequests?: Array<Record<string, unknown>>
  worktreePath?: string
  lastRuntimeEventType?: string
  draftAssistantText?: string
  draftUpdatedAt?: number
  draftIteration?: number
  draftTurnId?: string
  toolElapsedMs?: number
  lastToolSummary?: string
  contextTokens?: number
  contextWindow?: number
  contextRemainingPct?: number
  inputTokens?: number
  outputTokens?: number
  totalTokens?: number
  turnCostUsd?: number
  sessionCostUsd?: number
  pendingPermissionCount?: number
  drainMode?: string
  residentStatus?: string
  actionableInboxCount?: number
  protocolBacklogCount?: number
  notificationBacklogCount?: number
  latestNotification?: WorkerNotification
}

export type WorkItemCard = KanbanTask
export type RuntimeSession = Session
export type ExecutionTurn = Session

// ── Per-role DelegationWorkItem rollup (drives Execution Progress panel) ──
//
// One ``RoleWorkItemSummary`` per role within a primary company-mode
// session. Each ``RoleWorkItemRow`` is a single DelegationWorkItem — NOT a
// runtime Task / turn — so the same work item walking through execute →
// review → rework keeps a single row whose phase changes drive its colour.
//
// Backend builder: ``snapshot_builder._build_role_work_items_for_session``.
// The mapping is locked by ``test_snapshot_builder_company_kanban.RoleWorkItemsRollupTests``
// and ``frontend_src/lib/roleWorkItems.test.ts``.

export type RoleAggregatedStatus =
  | 'active'    // tracker is reflecting/tool_active OR a phase is in_progress → orange
  | 'waiting'   // queued / awaiting review / awaiting human → yellow
  | 'pending'   // no work items yet → gray
  | 'done'      // all approved → green
  | 'failed'    // any failed/cancelled (and others terminal) → red

export interface RoleWorkItemActivitySection {
  kind: string
  title: string
  roleName?: string
  runtimeTaskId?: string
  entries: ProgressEntry[]
}

export interface RoleWorkItemRow {
  workItemId: string
  workItemProjectionId?: string
  /** 14-state phase value (matches backend ``Phase``). */
  phase: KanbanPhase
  /** Frontend column id (``todo`` / ``in-progress`` / ``in-review`` / ``done``). */
  kanbanColumn: string
  title: string
  kind?: string
  /** True when the row appears under the reviewer (not executor) because
   *  the work item is in an ``in_review`` phase. */
  isReviewTarget?: boolean
  executorRoleId?: string
  executorRoleName?: string
  reviewerRoleId?: string
  selectedExecutionAgent?: TaskPreferredAgent
  executionUnitKind?: string
  externalTeamBindingId?: string
  coveredRoleIds?: string[]
  /** Epoch milliseconds; sourced from ``WorkItem.created_at``. */
  createdAt: number
  /** Epoch milliseconds; sourced from ``WorkItem.updated_at`` (last phase
   *  transition), NOT from chat channel message activity. */
  updatedAt: number
  /** Linked runtime Task id, if any. ``undefined`` for queued/never-dispatched
   *  work items, in which case the row is not yet click-through-able. */
  executionTurnId?: string
  /** Activity log already filtered by ``workItemProjectionId`` server-side. */
  progressLog: ProgressEntry[]
  /** Detailed runtime activity grouped by visible work item + hidden
   *  report/review helper work items that belong to this row. */
  activitySections?: RoleWorkItemActivitySection[]
}

export interface RoleWorkItemSummary {
  /** Stable key for React lists; equals the role_id within a single run. */
  roleKey: string
  roleId: string
  roleName: string
  roleSessionId?: string
  teamInstanceId?: string
  /** Live agent runtime state from the per-role tracker. */
  runtimeStatus: AgentAnimStatus
  /** Single-source aggregated status; UI maps this directly to colour. */
  aggregatedStatus: RoleAggregatedStatus
  /** Sorted ASC by ``createdAt`` so the UI reads "first happened first". */
  workItems: RoleWorkItemRow[]
}
