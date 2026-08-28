/**
 * collabSync — translates backend collaboration data format to frontend store types.
 *
 * Backend uses snake_case; frontend uses camelCase. This module bridges the two.
 */

import type { ChatChannel, ChatMessage, ChannelType } from '../types/chat'
import type { KanbanBoard, KanbanColumn, KanbanPhase, KanbanTask, TaskPriority, AgentAnimStatus, ProgressEntry, Session, SessionMode, Project, EmployeeAssignment, RoleAggregatedStatus, RoleWorkItemActivitySection, RoleWorkItemRow, RoleWorkItemSummary, WorkItemGate, WorkItemProgressEntry } from '../types/kanban'
import { normalizeProgressLog, normalizeWorkItemLog } from './progressLog'
import { canonicalizeSessionExecutionIdentity } from './sessionIdentity'

/* eslint-disable @typescript-eslint/no-explicit-any */

const UNIX_MS_THRESHOLD = 1_000_000_000_000
const WORK_ITEM_EVENT_RE = /^\[Company:([^\]]+)\]\s*(.*)$/
const COMPANY_RUNTIME_EVENT_RE = /^\[Company\]\s*(.*)$/

export function mergeSessionDetailHasMore(
  previous: boolean | undefined,
  incoming: boolean,
  isHistoryPage: boolean,
): boolean {
  // A latest-page refresh only describes that 200-row response. It must not
  // reopen an older-history cursor that the user already exhausted.
  if (!isHistoryPage && previous === false) return false
  return incoming
}

function normalizeAgentRuntimeStatus(rawStatus: unknown, rawAgentStatus: unknown): AgentAnimStatus | undefined {
  if (rawAgentStatus === 'idle' || rawAgentStatus === 'reflecting' || rawAgentStatus === 'tool_active') {
    return rawAgentStatus
  }
  return rawStatus === 'running' ? 'reflecting' : undefined
}

function normalizeEpochMs(raw: unknown): number {
  const value = typeof raw === 'string' ? Number(raw) : raw
  if (typeof value !== 'number' || !Number.isFinite(value)) return Date.now()
  return value < UNIX_MS_THRESHOLD ? value * 1000 : value
}

function mapBackendProgressLog(raw: any): ProgressEntry[] {
  if (!Array.isArray(raw)) return []
  return normalizeProgressLog(raw
    .filter((entry): entry is Record<string, unknown> => !!entry && typeof entry === 'object')
    .map((entry) => ({
      timestamp: normalizeEpochMs(entry.timestamp),
      type: (entry.type ?? 'status_change') as ProgressEntry['type'],
      summary: typeof entry.summary === 'string' ? entry.summary : '',
      detail: typeof entry.detail === 'string' ? entry.detail : undefined,
      turnId: typeof entry.turn_id === 'string'
        ? entry.turn_id
        : typeof entry.turnId === 'string'
          ? entry.turnId
          : undefined,
      itemId: typeof entry.item_id === 'string'
        ? entry.item_id
        : typeof entry.itemId === 'string'
          ? entry.itemId
          : undefined,
      streamId: typeof entry.stream_id === 'string'
        ? entry.stream_id
        : typeof entry.streamId === 'string'
          ? entry.streamId
          : undefined,
      toolCallId: typeof entry.tool_call_id === 'string'
        ? entry.tool_call_id
        : typeof entry.toolCallId === 'string'
          ? entry.toolCallId
          : undefined,
      permissionGroupKey: typeof entry.permission_group_key === 'string'
        ? entry.permission_group_key
        : typeof entry.permissionGroupKey === 'string'
          ? entry.permissionGroupKey
          : undefined,
      seq: typeof entry.seq === 'number' && Number.isFinite(entry.seq) ? entry.seq : undefined,
      executionMode: typeof entry.execution_mode === 'string'
        ? entry.execution_mode
        : typeof entry.executionMode === 'string'
          ? entry.executionMode
          : undefined,
    })))
}

function mapBackendWorkItemLog(raw: any): WorkItemProgressEntry[] {
  if (!Array.isArray(raw)) return []
  return normalizeWorkItemLog(raw
    .filter((entry): entry is Record<string, unknown> => !!entry && typeof entry === 'object')
    .map((entry) => {
      const runtimeTaskId = typeof entry.runtime_task_id === 'string'
        ? entry.runtime_task_id
        : typeof entry.runtimeTaskId === 'string'
          ? entry.runtimeTaskId
          : typeof entry.execution_turn_id === 'string'
            ? entry.execution_turn_id
            : typeof entry.executionTurnId === 'string'
              ? entry.executionTurnId
              : undefined
      const executionTurnId = typeof entry.execution_turn_id === 'string'
        ? entry.execution_turn_id
        : typeof entry.executionTurnId === 'string'
          ? entry.executionTurnId
          : runtimeTaskId
      return {
        timestamp: normalizeEpochMs(entry.timestamp),
        type: (entry.type ?? 'gate_result') as WorkItemProgressEntry['type'],
        workItemProjectionId: typeof entry.work_item_projection_id === 'string'
          ? entry.work_item_projection_id
          : typeof entry.workItemProjectionId === 'string'
            ? entry.workItemProjectionId
            : undefined,
        workItemTurnType: typeof entry.work_item_turn_type === 'string'
          ? entry.work_item_turn_type
          : typeof entry.workItemTurnType === 'string'
            ? entry.workItemTurnType
            : undefined,
        workItemProjectionTitle: typeof entry.work_item_projection_title === 'string'
          ? entry.work_item_projection_title
          : typeof entry.workItemProjectionTitle === 'string'
            ? entry.workItemProjectionTitle
            : undefined,
        runtimeTaskId,
        executionTurnId,
        roleName: typeof entry.role_name === 'string'
          ? entry.role_name
          : typeof entry.roleName === 'string'
            ? entry.roleName
            : undefined,
        detail: typeof entry.detail === 'string' ? entry.detail : undefined,
      }
    }))
}

function workItemProjectionTitle(projectionId: string): string {
  return projectionId
    .replace(/[_-]/g, ' ')
    .replace(/\b\w/g, (char) => char.toUpperCase())
}

function workItemEntryType(action: string): WorkItemProgressEntry['type'] {
  const actionLower = action.toLowerCase()
  if (actionLower.includes('starting') || actionLower.includes('started')) return 'work_item_started'
  if (actionLower.includes('gate passed') || actionLower.includes('approved') || actionLower.includes('completed')) return 'gate_approved'
  if (actionLower.includes('rejected') || actionLower.includes('reworking')) return 'gate_rejected'
  if (actionLower.includes('awaiting peer')) return 'awaiting_peer'
  if (actionLower.includes('awaiting manager review')) return 'awaiting_manager_review'
  if (
    actionLower.includes('awaiting user')
    || actionLower.includes('awaiting human review')
    || actionLower.includes('awaiting review')
    || actionLower.includes('awaiting confirmation')
    || actionLower.includes('awaiting feedback')
  ) {
    return 'awaiting_human'
  }
  if (actionLower.includes('failed')) return 'work_item_failed'
  if (actionLower.includes('deadlock')) return 'deadlock'
  return 'gate_result'
}

function workItemEntryFromMessage(message: ChatMessage): WorkItemProgressEntry | null {
  const content = message.content.trim()
  const meta = (message.metadata ?? {}) as Record<string, unknown>
  const roleName = message.sender !== 'system' && message.sender !== 'assistant'
    ? message.senderName
    : undefined
  const executionTurnId = typeof meta.forwarded_from === 'string'
    ? meta.forwarded_from
    : typeof meta.task_id === 'string'
      ? meta.task_id
      : typeof meta.taskId === 'string'
        ? meta.taskId
        : undefined

  const projectionMatch = WORK_ITEM_EVENT_RE.exec(content)
  if (projectionMatch) {
    const [, projectionId, actionRaw] = projectionMatch
    const action = actionRaw.trim()
    return {
      timestamp: message.timestamp,
      type: workItemEntryType(action),
      workItemProjectionId: projectionId,
      workItemProjectionTitle: workItemProjectionTitle(projectionId),
      roleName,
      detail: action || undefined,
      runtimeTaskId: executionTurnId,
      executionTurnId,
    }
  }

  const runtimeMatch = COMPANY_RUNTIME_EVENT_RE.exec(content)
  if (runtimeMatch) {
    const action = runtimeMatch[1].trim()
    return {
      timestamp: message.timestamp,
      type: workItemEntryType(action),
      workItemProjectionId: 'company_runtime',
      workItemProjectionTitle: 'Company Runtime',
      roleName,
      detail: action || undefined,
      runtimeTaskId: executionTurnId,
      executionTurnId,
    }
  }

  return null
}

function deriveWorkItemLog(messages: ChatMessage[]): WorkItemProgressEntry[] {
  return messages
    .map(workItemEntryFromMessage)
    .filter((entry): entry is WorkItemProgressEntry => entry != null)
    .sort((a, b) => a.timestamp - b.timestamp)
}

function hydrateCompanyRuntimeSessions(sessions: Session[], messages: ChatMessage[]): Session[] {
  if (sessions.length === 0 || messages.length === 0) return sessions

  const messagesByChannel = new Map<string, ChatMessage[]>()
  for (const message of messages) {
    const bucket = messagesByChannel.get(message.channelId)
    if (bucket) bucket.push(message)
    else messagesByChannel.set(message.channelId, [message])
  }

  return sessions.map((session) => {
    const derivedWorkItemLog = deriveWorkItemLog(messagesByChannel.get(session.channelId) ?? [])
    const workItemLog = session.workItemLog && session.workItemLog.length > 0
      ? session.workItemLog
      : derivedWorkItemLog
    const latestConcreteWorkItem = [...workItemLog]
      .reverse()
      .find((entry) => {
        const projectionId = entry.workItemProjectionId
        return projectionId && projectionId !== 'company_runtime'
      })
    const workItemProjectionId = session.mode === 'primary'
      ? (latestConcreteWorkItem?.workItemProjectionId ?? session.workItemProjectionId)
      : (session.workItemProjectionId ?? latestConcreteWorkItem?.workItemProjectionId)
    const isCompanyRuntime = session.isCompanyRuntime || (session.mode === 'primary' && workItemLog.length > 0)

    if (
      workItemLog.length === 0
      && workItemProjectionId === session.workItemProjectionId
      && isCompanyRuntime === session.isCompanyRuntime
    ) {
      return session
    }

    return {
      ...session,
      isCompanyRuntime,
      workItemProjectionId,
      ...(workItemLog.length > 0 ? { workItemLog } : {}),
    }
  })
}

function hydrateCompanyRuntimeTasks(tasks: KanbanTask[], sessions: Session[]): KanbanTask[] {
  if (tasks.length === 0 || sessions.length === 0) return tasks

  const sessionsByTaskId = new Map(sessions.map((session) => [session.taskId, session]))
  return tasks.map((task) => {
    const session = sessionsByTaskId.get(task.id)
    if (!session) return task

    const workItemProjectionId = session.workItemProjectionId ?? task.workItemProjectionId
    const companyProfile = session.companyProfile ?? task.companyProfile
    const workItemRoleId = session.workItemRoleId ?? task.workItemRoleId
    const workItemRoleName = session.workItemRoleName ?? task.workItemRoleName

    if (
      workItemProjectionId === task.workItemProjectionId
      && companyProfile === task.companyProfile
      && workItemRoleId === task.workItemRoleId
      && workItemRoleName === task.workItemRoleName
    ) {
      return task
    }

    return {
      ...task,
      workItemProjectionId,
      companyProfile,
      workItemRoleId,
      workItemRoleName,
    }
  })
}

export function mapBackendChannel(raw: any): ChatChannel {
  return {
    id: raw.channel_id ?? raw.id ?? '',
    type: (raw.channel_type ?? raw.type ?? 'session') as ChannelType,
    name: raw.name ?? '',
    officeId: raw.office_id ?? raw.officeId,
    participants: raw.participants ?? [],
    pinned: !!raw.pinned,
    createdAt: typeof raw.created_at === 'number' ? raw.created_at * 1000 : (raw.createdAt ?? Date.now()),
  }
}

export function mapBackendMessage(raw: any): ChatMessage {
  const rawSenderName = raw.sender_name ?? raw.senderName ?? ''
  const senderName = String(rawSenderName).trim().toLowerCase() === 'task generalist'
    ? 'OPC'
    : rawSenderName
  return {
    id: raw.message_id ?? raw.id ?? '',
    channelId: raw.channel_id ?? raw.channelId ?? '',
    sender: raw.sender ?? '',
    senderName,
    content: raw.content ?? '',
    timestamp: typeof raw.created_at === 'number' ? raw.created_at * 1000 : (raw.timestamp ?? Date.now()),
    replyToId: raw.reply_to_id ?? raw.replyToId,
    mentions: raw.mentions ?? [],
    metadata: raw.metadata,
    senderDeleted: !!(raw.sender_deleted ?? raw.senderDeleted),
  }
}

export function mapBackendBoard(raw: any): KanbanBoard {
  return {
    id: raw.board_id ?? raw.id ?? '',
    name: raw.name ?? '',
    description: raw.description,
    color: raw.color ?? '#3b82f6',
    officeId: raw.office_id ?? raw.officeId,
    prefix: raw.prefix ?? 'T',
    nextTaskNum: raw.next_task_num ?? raw.nextTaskNum ?? 1,
    createdAt: typeof raw.created_at === 'number' ? raw.created_at * 1000 : (raw.createdAt ?? Date.now()),
    updatedAt: typeof raw.updated_at === 'number' ? raw.updated_at * 1000 : (raw.updatedAt ?? Date.now()),
  }
}

export function mapBackendColumn(raw: any): KanbanColumn {
  return {
    id: raw.column_id ?? raw.id ?? '',
    boardId: raw.board_id ?? raw.boardId ?? '',
    name: raw.name ?? '',
    color: raw.color ?? '#888',
    sortOrder: raw.sort_order ?? raw.sortOrder ?? 0,
    isTerminal: !!(raw.is_terminal ?? raw.isTerminal),
    // Phase 2: work-item column metadata
    roleLabel: raw.role_label ?? raw.roleLabel,
    gateType: raw.gate_type ?? raw.gateType,
    isParallel: raw.is_parallel ?? raw.isParallel,
  }
}

export function mapBackendTask(raw: any): KanbanTask {
  const agentStatus = normalizeAgentRuntimeStatus(raw.status, raw.agent_status ?? raw.agentStatus)
  // Backend now emits the work-item phase plus a `kanban_column` projection.
  // The legacy `status: <column>` alias on the kanban-card payload was removed
  // when DelegationWorkItem.status was retired, so we accept either shape and
  // fall back to the explicit `column_id` for the few payloads that still use
  // the older field name.
  const columnId = raw.kanban_column ?? raw.column_id ?? raw.columnId ?? ''
  const cardId = raw.work_item_id ?? raw.workItemId ?? raw.task_id ?? raw.id ?? ''
  const runtimeTaskId = raw.runtime_task_id ?? raw.runtimeTaskId
    ?? raw.execution_turn_id ?? raw.executionTurnId
    ?? ((raw.work_item_id ?? raw.workItemId) ? undefined : (raw.task_id ?? raw.id))
  const executionTurnId = raw.execution_turn_id ?? raw.executionTurnId ?? runtimeTaskId
  const executionMode = raw.execution_mode ?? raw.executionMode
  const rawProjectionId = raw.work_item_projection_id ?? raw.workItemProjectionId
  const isTaskModeRuntime = executionMode === 'task_mode' || rawProjectionId === 'task_mode_execution'
  return {
    id: cardId,
    displayId: raw.display_id ?? raw.displayId ?? '',
    boardId: raw.board_id ?? raw.boardId ?? '',
    columnId,
    title: raw.title ?? '',
    description: raw.description,
    priority: (raw.priority ?? null) as TaskPriority | null,
    assigneeIds: raw.assignee_ids ?? raw.assigneeIds ?? [],
    tags: raw.tags ?? [],
    sortOrder: raw.sort_order ?? raw.sortOrder ?? 0,
    sessionId: raw.session_id ?? raw.sessionId,
    workItemId: raw.work_item_id ?? raw.workItemId,
    runtimeTaskId,
    executionTurnId,
    createdAt: typeof raw.created_at === 'number' ? raw.created_at * 1000 : (raw.createdAt ?? Date.now()),
    updatedAt: typeof raw.updated_at === 'number' ? raw.updated_at * 1000 : (raw.updatedAt ?? Date.now()),
    // Phase 2: agent runtime state
    agentStatus,
    currentTool: raw.current_tool ?? raw.currentTool,
    displayTool: raw.display_tool ?? raw.displayTool ?? raw.current_tool ?? raw.currentTool,
    toolElapsedMs: raw.tool_elapsed_ms ?? raw.toolElapsedMs,
    lastToolSummary: raw.last_tool_summary ?? raw.lastToolSummary,
    contextTokens: raw.context_tokens ?? raw.contextTokens,
    contextWindow: raw.context_window ?? raw.contextWindow,
    contextRemainingPct: raw.context_remaining_pct ?? raw.contextRemainingPct,
    inputTokens: raw.input_tokens ?? raw.inputTokens,
    outputTokens: raw.output_tokens ?? raw.outputTokens,
    totalTokens: raw.total_tokens ?? raw.totalTokens,
    turnCostUsd: raw.turn_cost_usd ?? raw.turnCostUsd,
    sessionCostUsd: raw.session_cost_usd ?? raw.sessionCostUsd,
    pendingPermissionCount: raw.pending_permission_count ?? raw.pendingPermissionCount,
    drainMode: raw.drain_mode ?? raw.drainMode,
    residentStatus: raw.resident_status ?? raw.residentStatus,
    actionableInboxCount: raw.actionable_inbox_count ?? raw.actionableInboxCount,
    protocolBacklogCount: raw.protocol_backlog_count ?? raw.protocolBacklogCount,
    notificationBacklogCount: raw.notification_backlog_count ?? raw.notificationBacklogCount,
    latestNotification: raw.latest_notification ?? raw.latestNotification,
    // Phase 2: work-item runtime & dependencies
    workItemProjectionId: isTaskModeRuntime ? undefined : rawProjectionId,
    workItemTurnType: isTaskModeRuntime ? undefined : (raw.work_item_turn_type ?? raw.workItemTurnType),
    companyProfile: isTaskModeRuntime ? undefined : (raw.company_profile ?? raw.companyProfile),
    orgId: isTaskModeRuntime ? undefined : (raw.org_id ?? raw.organization_id ?? raw.orgId ?? raw.organizationId),
    workItemRoleId: isTaskModeRuntime ? undefined : (raw.work_item_role_id ?? raw.workItemRoleId),
    workItemRoleName: isTaskModeRuntime ? undefined : (raw.work_item_role_name ?? raw.workItemRoleName),
    workItemGate: isTaskModeRuntime ? undefined : _mapWorkItemGate(raw.work_item_gate ?? raw.workItemGate),
    employeeAssignment: _mapEmployeeAssignment(raw.employee_assignment ?? raw.employeeAssignment),
    selectedExecutionAgent: raw.selected_execution_agent ?? raw.selectedExecutionAgent,
    executionUnitKind: raw.execution_unit_kind ?? raw.executionUnitKind,
    externalTeamBindingId: raw.external_team_binding_id ?? raw.externalTeamBindingId,
    externalTeamBoundaryRoleId: raw.external_team_boundary_role_id ?? raw.externalTeamBoundaryRoleId,
    coveredRoleIds: raw.covered_role_ids ?? raw.coveredRoleIds,
    externalCompanyExecutionFence: raw.external_company_execution_fence ?? raw.externalCompanyExecutionFence,
    originChannel: raw.origin_channel ?? raw.originChannel,
    dependencies: raw.dependencies,
    progressLog: mapBackendProgressLog(raw.progress_log ?? raw.progressLog),
    handoffContext: raw.handoff_context ?? raw.handoffContext,
    phase: raw.phase,
    runtimeSessionId: raw.runtime_session_id ?? raw.runtimeSessionId,
    resumeCursor: raw.resume_cursor ?? raw.resumeCursor,
    worktreePath: raw.worktree_path ?? raw.worktreePath,
    blockedReason: raw.blocked_reason ?? raw.blockedReason,
    reviewVerdict: raw.review_verdict ?? raw.reviewVerdict,
    reviewSummary: raw.review_summary ?? raw.reviewSummary,
    reviewOwnerRoleId: raw.review_owner_role_id ?? raw.reviewOwnerRoleId,
    reviewOwnerSeatId: raw.review_owner_seat_id ?? raw.reviewOwnerSeatId,
    managerRoleId: raw.manager_role_id ?? raw.managerRoleId,
    managerSeatId: raw.manager_seat_id ?? raw.managerSeatId,
    scopeKey: raw.scope_key ?? raw.scopeKey,
    completionReport: raw.completion_report ?? raw.completionReport,
    reworkFeedback: raw.rework_feedback ?? raw.reworkFeedback,
    planningContext: raw.planning_context ?? raw.planningContext,
    deliverables: raw.deliverables,
    acceptanceCriteria: raw.acceptance_criteria ?? raw.acceptanceCriteria,
    delegationRationale: raw.delegation_rationale ?? raw.delegationRationale,
    nonOverlapGuard: raw.non_overlap_guard ?? raw.nonOverlapGuard,
    coordinationNotes: raw.coordination_notes ?? raw.coordinationNotes,
    originalMessage: raw.original_message ?? raw.originalMessage,
    residentAssignment: raw.resident_assignment ?? raw.residentAssignment,
    memberSessionState: raw.member_session_state ?? raw.memberSessionState,
    ownershipContract: raw.ownership_contract ?? raw.ownershipContract,
  }
}

function _mapEmployeeAssignment(raw: any): EmployeeAssignment | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  return {
    name: raw.name,
    employeeId: raw.employee_id ?? raw.employeeId,
    category: raw.category,
    experienceScore: raw.experience_score ?? raw.experienceScore,
    domains: raw.domains,
    preferredExternalAgent: raw.preferred_external_agent ?? raw.preferredExternalAgent,
    promptContext: raw.prompt_context ?? raw.promptContext,
    deltaContext: raw.delta_context ?? raw.deltaContext,
    skillRefs: raw.skill_refs ?? raw.skillRefs,
  }
}

function _mapWorkItemGate(raw: any): WorkItemGate | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  return {
    type: raw.type ?? raw.gate_type ?? raw.gateType,
    reviewerRole: raw.reviewer_role ?? raw.reviewerRole,
    autoApprove: raw.auto_approve ?? raw.autoApprove,
    criteria: raw.criteria,
  }
}

// Backend phase string is the source of truth — the frontend lives off
// that vocabulary too (see ``types/kanban.ts:KanbanPhase``). Validate at
// the deserialization boundary so a typo upstream surfaces here, not deep
// inside the rendering code.
const KNOWN_PHASES: ReadonlySet<KanbanPhase> = new Set<KanbanPhase>([
  'queued', 'ready', 'ready_for_rework', 'waiting_dependencies',
  'running', 'waiting_for_peer', 'waiting_for_children', 'paused', 'needs_attention',
  'awaiting_manager_review', 'awaiting_human',
  'approved', 'failed', 'cancelled',
])

const KNOWN_AGGREGATED_STATUS: ReadonlySet<RoleAggregatedStatus> = new Set<RoleAggregatedStatus>([
  'active', 'waiting', 'pending', 'done', 'failed',
])

function coercePhase(value: unknown): KanbanPhase {
  if (typeof value === 'string' && KNOWN_PHASES.has(value as KanbanPhase)) {
    return value as KanbanPhase
  }
  // Falling back to ``queued`` matches the column id ``todo``, which is
  // the safest "haven't done anything yet" placeholder.
  return 'queued'
}

function coerceAggregatedStatus(value: unknown): RoleAggregatedStatus {
  if (typeof value === 'string' && KNOWN_AGGREGATED_STATUS.has(value as RoleAggregatedStatus)) {
    return value as RoleAggregatedStatus
  }
  return 'pending'
}

function coerceRuntimeStatus(value: unknown): AgentAnimStatus {
  if (value === 'reflecting' || value === 'tool_active' || value === 'idle') return value
  return 'idle'
}

function mapBackendRoleWorkItemActivitySection(raw: any): RoleWorkItemActivitySection {
  const kind = typeof raw.kind === 'string' && raw.kind.trim() ? raw.kind : 'activity'
  const title = typeof raw.title === 'string' && raw.title.trim()
    ? raw.title
    : kind.replace(/_/g, ' ')
  return {
    kind,
    title,
    roleName: typeof raw.role_name === 'string'
      ? raw.role_name
      : (typeof raw.roleName === 'string' ? raw.roleName : undefined),
    runtimeTaskId: typeof raw.runtime_task_id === 'string'
      ? raw.runtime_task_id
      : (typeof raw.runtimeTaskId === 'string' ? raw.runtimeTaskId : undefined),
    entries: mapBackendProgressLog(raw.entries),
  }
}

function mapBackendRoleWorkItemRow(raw: any): RoleWorkItemRow {
  const rawActivitySections: unknown[] = Array.isArray(raw.activity_sections)
    ? raw.activity_sections
    : Array.isArray(raw.activitySections)
      ? raw.activitySections
      : []
  return {
    workItemId: typeof raw.work_item_id === 'string' ? raw.work_item_id : (raw.workItemId ?? ''),
    workItemProjectionId: typeof raw.work_item_projection_id === 'string'
      ? raw.work_item_projection_id
      : (raw.workItemProjectionId ?? undefined),
    phase: coercePhase(raw.phase),
    kanbanColumn: typeof raw.kanban_column === 'string'
      ? raw.kanban_column
      : (raw.kanbanColumn ?? 'todo'),
    title: typeof raw.title === 'string' ? raw.title : '',
    kind: typeof raw.kind === 'string' ? raw.kind : (raw.kind ?? undefined),
    isReviewTarget: !!(raw.is_review_target ?? raw.isReviewTarget),
    executorRoleId: raw.executor_role_id ?? raw.executorRoleId ?? undefined,
    executorRoleName: raw.executor_role_name ?? raw.executorRoleName ?? undefined,
    reviewerRoleId: raw.reviewer_role_id ?? raw.reviewerRoleId ?? undefined,
    selectedExecutionAgent: raw.selected_execution_agent ?? raw.selectedExecutionAgent ?? undefined,
    executionUnitKind: raw.execution_unit_kind ?? raw.executionUnitKind ?? undefined,
    externalTeamBindingId: raw.external_team_binding_id ?? raw.externalTeamBindingId ?? undefined,
    coveredRoleIds: raw.covered_role_ids ?? raw.coveredRoleIds ?? undefined,
    createdAt: normalizeEpochMs(raw.created_at ?? raw.createdAt),
    updatedAt: normalizeEpochMs(raw.updated_at ?? raw.updatedAt),
    executionTurnId: raw.execution_turn_id ?? raw.executionTurnId ?? undefined,
    activitySections: rawActivitySections
      .filter((section): section is Record<string, unknown> => !!section && typeof section === 'object')
      .map(mapBackendRoleWorkItemActivitySection),
    progressLog: mapBackendProgressLog(raw.progress_log ?? raw.progressLog),
  }
}

function mapBackendRoleWorkItems(raw: any): Record<string, RoleWorkItemSummary> | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const out: Record<string, RoleWorkItemSummary> = {}
  for (const [key, value] of Object.entries(raw)) {
    if (!value || typeof value !== 'object') continue
    const summary = value as Record<string, unknown>
    const workItemsRaw = Array.isArray(summary.work_items)
      ? summary.work_items
      : Array.isArray(summary.workItems)
        ? summary.workItems
        : []
    const workItems = (workItemsRaw as any[])
      .map(mapBackendRoleWorkItemRow)
      // Defensive ASC sort by createdAt — backend already orders, but a
      // belt-and-braces sort here means UI never sees a flapping row order
      // if backend ordering ever drifts.
      .sort((a, b) => a.createdAt - b.createdAt)
    const roleKey = typeof summary.role_key === 'string'
      ? summary.role_key
      : (typeof summary.roleKey === 'string' ? summary.roleKey : key)
    out[key] = {
      roleKey,
      roleId: typeof summary.role_id === 'string'
        ? summary.role_id
        : (typeof summary.roleId === 'string' ? summary.roleId : key),
      roleName: typeof summary.role_name === 'string'
        ? summary.role_name
        : (typeof summary.roleName === 'string' ? summary.roleName : key),
      roleSessionId: typeof summary.role_session_id === 'string'
        ? summary.role_session_id
        : (typeof summary.roleSessionId === 'string' ? summary.roleSessionId : undefined),
      teamInstanceId: typeof summary.team_instance_id === 'string'
        ? summary.team_instance_id
        : (typeof summary.teamInstanceId === 'string' ? summary.teamInstanceId : undefined),
      runtimeStatus: coerceRuntimeStatus(summary.runtime_status ?? summary.runtimeStatus),
      aggregatedStatus: coerceAggregatedStatus(summary.aggregated_status ?? summary.aggregatedStatus),
      workItems,
    }
  }
  return Object.keys(out).length > 0 ? out : undefined
}

export function mapBackendSession(raw: any): Session {
  const agentStatus = normalizeAgentRuntimeStatus(raw.status, raw.agent_status ?? raw.agentStatus)
  const taskId = raw.task_id ?? raw.taskId ?? ''
  const runtimeTaskId = raw.runtime_task_id ?? raw.runtimeTaskId ?? raw.execution_turn_id ?? raw.executionTurnId ?? taskId
  const executionTurnId = raw.execution_turn_id ?? raw.executionTurnId ?? runtimeTaskId
  const executionMode = raw.execution_mode ?? raw.executionMode
  const rawExecMode = String(raw.exec_mode ?? raw.execMode ?? '').trim().toLowerCase()
  const rawProjectionId = raw.work_item_projection_id ?? raw.workItemProjectionId
  const isTaskModeRuntime = (
    rawExecMode === 'task'
    || rawExecMode === 'project'
    || rawExecMode === 'single'
    || executionMode === 'task_mode'
    || rawProjectionId === 'task_mode_execution'
  )
  const parentSessionId = isTaskModeRuntime
    ? undefined
    : (raw.parent_session_id ?? raw.parentSessionId)
  const mapped: Session = {
    projectId: raw.project_id ?? raw.projectId ?? '',
    taskId,
    runtimeTaskId,
    executionTurnId,
    channelId: raw.channel_id ?? raw.channelId ?? '',
    sessionId: raw.session_id ?? raw.sessionId,
    parentSessionId,
    mode: (isTaskModeRuntime ? 'primary' : (raw.mode ?? (parentSessionId ? 'child' : 'primary'))) as SessionMode,
    title: raw.title ?? 'Untitled',
    status: raw.status ?? 'pending',
    columnId: raw.column_id ?? raw.columnId ?? 'todo',
    assigneeIds: raw.assignee_ids ?? raw.assigneeIds ?? [],
    priority: raw.priority ?? null,
    tags: raw.tags ?? [],
    agentStatus,
    currentTool: raw.current_tool ?? raw.currentTool,
    displayTool: raw.display_tool ?? raw.displayTool ?? raw.current_tool ?? raw.currentTool,
    toolElapsedMs: raw.tool_elapsed_ms ?? raw.toolElapsedMs,
    lastToolSummary: raw.last_tool_summary ?? raw.lastToolSummary,
    contextTokens: raw.context_tokens ?? raw.contextTokens,
    contextWindow: raw.context_window ?? raw.contextWindow,
    contextRemainingPct: raw.context_remaining_pct ?? raw.contextRemainingPct,
    inputTokens: raw.input_tokens ?? raw.inputTokens,
    outputTokens: raw.output_tokens ?? raw.outputTokens,
    totalTokens: raw.total_tokens ?? raw.totalTokens,
    turnCostUsd: raw.turn_cost_usd ?? raw.turnCostUsd,
    sessionCostUsd: raw.session_cost_usd ?? raw.sessionCostUsd,
    pendingPermissionCount: raw.pending_permission_count ?? raw.pendingPermissionCount,
    drainMode: raw.drain_mode ?? raw.drainMode,
    progressLog: mapBackendProgressLog(raw.progress_log ?? raw.progressLog),
    createdAt: typeof raw.created_at === 'number' ? raw.created_at * 1000 : (raw.createdAt ?? Date.now()),
    updatedAt: typeof raw.updated_at === 'number' ? raw.updated_at * 1000 : (raw.updatedAt ?? Date.now()),
    messageCount: raw.message_count ?? raw.messageCount ?? 0,
    latestPreview: raw.latest_preview ?? raw.latestPreview,
    latestSender: raw.latest_sender ?? raw.latestSender,
    latestMessageId: raw.latest_message_id ?? raw.latestMessageId,
    indexLoaded: raw.index_loaded ?? raw.indexLoaded,
    detailLoaded: raw.detail_loaded ?? raw.detailLoaded,
    fullLoaded: raw.full_loaded ?? raw.fullLoaded,
    hasMore: raw.has_more ?? raw.hasMore,
    summaryHasMore: raw.summary_has_more ?? raw.summaryHasMore,
    fullHasMore: raw.full_has_more ?? raw.fullHasMore,
    detailLoading: raw.detail_loading ?? raw.detailLoading,
    detailError: raw.detail_error ?? raw.detailError,
    viewGeneration: raw.view_generation ?? raw.viewGeneration,
    execMode: raw.exec_mode ?? raw.execMode,
    companyProfile: isTaskModeRuntime ? undefined : (raw.company_profile ?? raw.companyProfile),
    orgId: isTaskModeRuntime ? undefined : (raw.org_id ?? raw.organization_id ?? raw.orgId ?? raw.organizationId),
    preferredAgent: raw.preferred_agent ?? raw.preferredAgent,
    nativeApprovalLevel: raw.native_approval_level ?? raw.nativeApprovalLevel,
    nativePermissionScopeId: raw.native_permission_scope_id ?? raw.nativePermissionScopeId,
    // Company Mode metadata
    workItemProjectionId: isTaskModeRuntime ? undefined : rawProjectionId,
    workItemTurnType: isTaskModeRuntime ? undefined : (raw.work_item_turn_type ?? raw.workItemTurnType),
    workItemRoleId: isTaskModeRuntime ? undefined : (raw.work_item_role_id ?? raw.workItemRoleId),
    workItemRoleName: isTaskModeRuntime ? undefined : (raw.work_item_role_name ?? raw.workItemRoleName),
    workItemGate: isTaskModeRuntime ? undefined : _mapWorkItemGate(raw.work_item_gate ?? raw.workItemGate),
    employeeAssignment: _mapEmployeeAssignment(raw.employee_assignment ?? raw.employeeAssignment),
    selectedExecutionAgent: raw.selected_execution_agent ?? raw.selectedExecutionAgent,
    executionUnitKind: raw.execution_unit_kind ?? raw.executionUnitKind,
    externalTeamBindingId: raw.external_team_binding_id ?? raw.externalTeamBindingId,
    externalTeamBoundaryRoleId: raw.external_team_boundary_role_id ?? raw.externalTeamBoundaryRoleId,
    coveredRoleIds: raw.covered_role_ids ?? raw.coveredRoleIds,
    externalCompanyExecutionFence: raw.external_company_execution_fence ?? raw.externalCompanyExecutionFence,
    originChannel: raw.origin_channel ?? raw.originChannel,
    originTaskId: raw.origin_task_id ?? raw.originTaskId ?? raw.task_id ?? raw.taskId,
    runtimeControlState: raw.runtime_control_state ?? raw.runtimeControlState,
    canStop: raw.can_stop ?? raw.canStop,
    canResume: raw.can_resume ?? raw.canResume,
    resumeParentSessionId: raw.resume_parent_session_id ?? raw.resumeParentSessionId,
    pendingRuntimeCheckpointId: raw.pending_runtime_checkpoint_id ?? raw.pendingRuntimeCheckpointId,
    stopIntentId: raw.stop_intent_id ?? raw.stopIntentId,
    handoffContext: raw.handoff_context ?? raw.handoffContext,
    handoffTo: raw.handoff_to ?? raw.handoffTo,
    artifacts: raw.artifacts,
    isCompanyRuntime: isTaskModeRuntime ? false : (raw.is_company_runtime ?? raw.isCompanyRuntime),
    workItemLog: isTaskModeRuntime ? [] : mapBackendWorkItemLog(raw.work_item_log ?? raw.workItemLog),
    roleWorkItems: isTaskModeRuntime ? undefined : mapBackendRoleWorkItems(raw.role_work_items ?? raw.roleWorkItems),
    executorRoleWorkItems: isTaskModeRuntime
      ? undefined
      : mapBackendRoleWorkItems(raw.executor_role_work_items ?? raw.executorRoleWorkItems),
    draftTurnId: raw.draft_turn_id ?? raw.draftTurnId,
    runtimeSessionId: raw.runtime_session_id ?? raw.runtimeSessionId,
    resumeCursor: raw.resume_cursor ?? raw.resumeCursor,
    activeSubagents: raw.active_subagents ?? raw.activeSubagents,
    permissionRequests: raw.permission_requests ?? raw.permissionRequests,
    worktreePath: raw.worktree_path ?? raw.worktreePath,
    residentStatus: raw.resident_status ?? raw.residentStatus,
    actionableInboxCount: raw.actionable_inbox_count ?? raw.actionableInboxCount,
    protocolBacklogCount: raw.protocol_backlog_count ?? raw.protocolBacklogCount,
    notificationBacklogCount: raw.notification_backlog_count ?? raw.notificationBacklogCount,
    latestNotification: raw.latest_notification ?? raw.latestNotification,
  }
  return canonicalizeSessionExecutionIdentity(mapped)
}

export interface CollabSyncData {
  channels: ChatChannel[]
  messages: ChatMessage[]
  boards: KanbanBoard[]
  columns: KanbanColumn[]
  tasks: KanbanTask[]
  sessions: Session[]
}

export function mapCollabSyncPayload(payload: any): CollabSyncData {
  const channels = (payload.channels ?? []).map(mapBackendChannel)
  const messages = (payload.messages ?? []).map(mapBackendMessage)
  const boards = (payload.boards ?? []).map(mapBackendBoard)
  const columns = (payload.columns ?? []).map(mapBackendColumn)
  const sessions = hydrateCompanyRuntimeSessions(
    (payload.sessions ?? []).map(mapBackendSession),
    messages,
  )
  const tasks = hydrateCompanyRuntimeTasks(
    (payload.tasks ?? []).map(mapBackendTask),
    sessions,
  )

  return {
    channels,
    messages,
    boards,
    columns,
    tasks,
    sessions,
  }
}
