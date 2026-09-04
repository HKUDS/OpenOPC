import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { VisualSocketClient } from './lib/wsClient'
import { GameBridge } from './game/GameBridge'
import { CollisionEditor } from './components/CollisionEditor'
import { registerTestRunner } from './game/test/eventTestRunner'
import type { AgentInfo, EmployeeDetailPayload, OrgCreateMemberInput, OrgSavedCreatePayload, OrgEmployee, OrgInfoPayload, OrgRole, ReorgProposalInfo, SavedOrgSummary, SocketStatus, TalentTemplate, VisualSnapshot } from './types/visual'
import { useBoardStore, type BoardStoreState } from './kanban/BoardStore'
import { WorkspacePage } from './workspace/WorkspacePage'
import { useChatStore, type ChatStoreState } from './chat/ChatStore'
import { useSessionStore, type SessionStoreState } from './stores/SessionStore'
import { useProjectStore, type ProjectStoreState } from './stores/ProjectStore'
import { ExecutionPanel } from './kanban/ExecutionPanel'
import { JiuwenTeamActivityPanel } from './kanban/JiuwenTeamActivityPanel'
import { ProjectSelector } from './components/ProjectSelector'
import { OrgTab } from './org/OrgTab'
import { notifyTaskAssigned } from './lib/taskChatBridge'
import { mapCollabSyncPayload, mapBackendMessage, mapBackendChannel, mapBackendSession, mapBackendBoard, mapBackendColumn, mapBackendTask, mergeSessionDetailHasMore } from './lib/collabSync'
import { normalizeOrgInfoPayload } from './lib/runtimeOrg'
import { companyRuntimeControlPatchForBoardStatus } from './lib/sessionRuntime'
import { getExecutionTurnId } from './lib/workItemRuntimeIds'
import { normalizeSessionCompanyProfile, normalizeSessionExecMode } from './lib/sessionIdentity'
import { extractSessionRecruitmentByRole, sessionChannelId } from './lib/sessionRecruitment'
import { resolveCanonicalTurnId, terminalAssistantTurnId } from './lib/turnIdentity'
import { compileProjectIdPolicy, type ProjectIdPolicy } from './lib/projectIdPolicy'
import { loadStoredTheme, saveStoredTheme, isThemeName, themeMessageKey, THEMES, type ThemeName } from './lib/theme'
import { unassignAgent } from './game/map/OfficeStore'
import type { AgentAnimStatus, EmployeeAssignment, KanbanPhase, KanbanTask, NativeApprovalLevel, RoleAggregatedStatus, RoleWorkItemSummary, Session, TaskPreferredAgent } from './types/kanban'
import { useI18n } from './i18n'
import { OfficePage } from './office/OfficePage'
import { EventTimelineStore } from './devtools/EventTimelineStore'
import { DevToolsOverlay } from './devtools/DevToolsOverlay'
import { useExternalTeamActivityStore, type ExternalTeamActivityRecord } from './stores/ExternalTeamActivityStore'
import { isJiuwenOpaqueTeam } from './lib/externalTeamActivity'

function readOutdoorOverrideUi(): 'auto' | 'day' | 'night' {
  try {
    const o = localStorage.getItem('opc_outdoor_override')
    if (o === 'day' || o === 'night') return o
    if (localStorage.getItem('opc_outdoor_day') === '1') return 'day'
    if (localStorage.getItem('opc_outdoor_night') === '1') return 'night'
  } catch { /* private mode */ }
  return 'auto'
}

const MAX_LOG_ITEMS = 80
const TASK_MODE_LOW_VALUE_RUNTIME_EVENTS = new Set([
  'message_start',
  'message_stop',
  'tool_call_delta',
  'status_snapshot',
  'context_usage',
  'cost_update',
  'task_ledger_updated',
  'prompt_prefix_state',
  'prompt_prefix_cache_fingerprint',
  'prefetch_started',
  'prefetch_completed',
  'prefetch_consumed',
  'durable_memory_extracted',
  'durable_memory_extraction_failed',
  'session_memory_updated',
  'session_memory_update_failed',
  'tool_batch_started',
  'tool_batch_completed',
  'permission_predicted',
  'turn_started',
  'turn_completed',
])
const SESSION_DETAIL_REFRESH_LOW_VALUE_RUNTIME_EVENTS = new Set([
  'member_inbox_updated',
])

type AppPage = 'office' | 'workspace' | 'org' | 'mapEditor'
type AppExecMode = 'task' | 'company' | 'org'

function defaultWsUrl(): string {
  const wsProto = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${wsProto}://${window.location.hostname}:${window.location.port || '8765'}/ws`
}

function statusClass(status: SocketStatus): string {
  if (status === 'connected') return 'ok'
  if (status === 'connecting') return 'warn'
  if (status === 'error') return 'error'
  return 'off'
}

function normalizeCompanyProfile(value?: string): 'corporate' | 'custom' {
  return normalizeSessionCompanyProfile(value)
}

function normalizeExecMode(value?: string): AppExecMode {
  return normalizeSessionExecMode(value)
}

function companyProfileForExecMode(mode: AppExecMode, profile?: string): 'corporate' | 'custom' | undefined {
  if (mode === 'task') return undefined
  if (mode === 'org') return 'custom'
  return 'corporate'
}

function orgIdForExecMode(mode: AppExecMode, orgId?: string | null): string | undefined {
  if (mode !== 'org') return undefined
  const normalized = String(orgId ?? '').trim()
  return normalized || undefined
}

function normalizeTaskPreferredAgent(value?: string): TaskPreferredAgent {
  const normalized = String(value ?? '').trim().toLowerCase().replace('-', '_')
  if (
    normalized === 'codex'
    || normalized === 'claude_code'
    || normalized === 'cursor'
    || normalized === 'opencode'
    || normalized === 'jiuwen'
    || normalized === 'jiuwenswarm'
  ) {
    return normalized
  }
  return 'native'
}

function mapAgentPayload(raw: Record<string, unknown>, previous?: AgentInfo): AgentInfo {
  const runtimeStatus = (
    typeof raw.runtime_status === 'string'
      ? raw.runtime_status
      : typeof raw.status === 'string'
        ? raw.status
        : previous?.runtime_status ?? previous?.status ?? 'idle'
  ) as AgentAnimStatus | string
  const appearance = raw.appearance && typeof raw.appearance === 'object'
    ? raw.appearance as AgentInfo['appearance']
    : (previous?.appearance ?? { palette: 0, hue_shift: 0, seat_zone: 'work_area' })
  const specialties = Array.isArray(raw.specialties)
    ? raw.specialties.filter((item): item is string => typeof item === 'string')
    : (previous?.specialties ?? [])
  const agentId = typeof raw.agent_id === 'string' ? raw.agent_id : (previous?.agent_id ?? '')

  return {
    agent_id: agentId,
    name: typeof raw.name === 'string' && raw.name
      ? raw.name
      : typeof raw.role_name === 'string' && raw.role_name
        ? raw.role_name
        : (previous?.name ?? agentId),
    description: typeof raw.description === 'string' ? raw.description : (previous?.description ?? ''),
    specialties,
    status: runtimeStatus,
    office_id: typeof raw.office_id === 'string' ? raw.office_id : previous?.office_id,
    appearance,
    employee_id: typeof raw.employee_id === 'string' ? raw.employee_id : previous?.employee_id,
    opc_role_id: typeof raw.opc_role_id === 'string' ? raw.opc_role_id : previous?.opc_role_id,
    runtime_status: runtimeStatus as AgentAnimStatus,
    current_tool: typeof raw.current_tool === 'string'
      ? raw.current_tool
      : raw.current_tool == null
        ? undefined
        : previous?.current_tool,
    current_task_id: typeof raw.current_task_id === 'string'
      ? raw.current_task_id
      : raw.current_task_id == null
        ? undefined
        : previous?.current_task_id,
  }
}

function mapAgentListPayload(rawAgents: unknown[], previous: AgentInfo[] = []): AgentInfo[] {
  const prevById = new Map(previous.map((agent) => [agent.agent_id, agent]))
  return rawAgents
    .filter((raw): raw is Record<string, unknown> => !!raw && typeof raw === 'object')
    .map((raw) => {
      const agentId = typeof raw.agent_id === 'string' ? raw.agent_id : ''
      return mapAgentPayload(raw, prevById.get(agentId))
    })
    .filter((agent) => !!agent.agent_id)
}

function mapEmployeeAssignmentPayload(raw: unknown): EmployeeAssignment | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const value = raw as Record<string, unknown>
  return {
    name: typeof value.name === 'string' ? value.name : undefined,
    employeeId: typeof value.employee_id === 'string'
      ? value.employee_id
      : typeof value.employeeId === 'string'
        ? value.employeeId
        : undefined,
    category: typeof value.category === 'string' ? value.category : undefined,
    experienceScore: typeof value.experience_score === 'number'
      ? value.experience_score
      : typeof value.experienceScore === 'number'
        ? value.experienceScore
        : undefined,
  }
}

function hasOwnPayloadField(raw: Record<string, unknown>, field: string): boolean {
  return Object.prototype.hasOwnProperty.call(raw, field)
}

function runtimeStatusClearsDisplayTool(status: unknown): boolean {
  const normalized = String(status ?? '').trim().toLowerCase()
  return normalized === 'idle'
    || normalized === 'done'
    || normalized === 'failed'
    || normalized === 'cancelled'
}

function workItemIdentityPatchFromPayload(raw: Record<string, unknown>): Partial<KanbanTask> {
  const patch: Partial<KanbanTask> = {}
  const executionMode = typeof raw.execution_mode === 'string' ? raw.execution_mode : ''
  const isTaskModeRuntime = executionMode === 'task_mode' || raw.work_item_projection_id === 'task_mode_execution'
  if (isTaskModeRuntime) {
    const employeeAssignment = mapEmployeeAssignmentPayload(raw.employee_assignment ?? raw.employeeAssignment)
    if (employeeAssignment) patch.employeeAssignment = employeeAssignment
    if (typeof raw.selected_execution_agent === 'string') patch.selectedExecutionAgent = normalizeTaskPreferredAgent(raw.selected_execution_agent)
    else if (typeof raw.selectedExecutionAgent === 'string') patch.selectedExecutionAgent = normalizeTaskPreferredAgent(raw.selectedExecutionAgent)
    return patch
  }
  if (typeof raw.work_item_projection_id === 'string') {
    patch.workItemProjectionId = raw.work_item_projection_id
  } else if (typeof raw.workItemProjectionId === 'string') {
    patch.workItemProjectionId = raw.workItemProjectionId
  }
  if (typeof raw.work_item_turn_type === 'string') patch.workItemTurnType = raw.work_item_turn_type
  else if (typeof raw.workItemTurnType === 'string') patch.workItemTurnType = raw.workItemTurnType

  if (typeof raw.work_item_role_id === 'string') patch.workItemRoleId = raw.work_item_role_id
  else if (typeof raw.workItemRoleId === 'string') patch.workItemRoleId = raw.workItemRoleId

  if (typeof raw.work_item_role_name === 'string') patch.workItemRoleName = raw.work_item_role_name
  else if (typeof raw.workItemRoleName === 'string') patch.workItemRoleName = raw.workItemRoleName

  const employeeAssignment = mapEmployeeAssignmentPayload(raw.employee_assignment ?? raw.employeeAssignment)
  if (employeeAssignment) patch.employeeAssignment = employeeAssignment
  if (typeof raw.selected_execution_agent === 'string') patch.selectedExecutionAgent = normalizeTaskPreferredAgent(raw.selected_execution_agent)
  else if (typeof raw.selectedExecutionAgent === 'string') patch.selectedExecutionAgent = normalizeTaskPreferredAgent(raw.selectedExecutionAgent)
  return patch
}

function sessionRuntimePatchFromPayload(raw: Record<string, unknown>): Partial<import('./types/kanban').Session> {
  const patch: Partial<import('./types/kanban').Session> = {}
  if (raw.latest_notification && typeof raw.latest_notification === 'object') {
    patch.latestNotification = raw.latest_notification as import('./types/kanban').WorkerNotification
  }
  if (typeof raw.runtime_session_id === 'string') patch.runtimeSessionId = raw.runtime_session_id
  if (typeof raw.resume_cursor === 'number') patch.resumeCursor = raw.resume_cursor
  if (Array.isArray(raw.active_subagents)) patch.activeSubagents = raw.active_subagents as Array<Record<string, unknown>>
  if (Array.isArray(raw.permission_requests)) patch.permissionRequests = raw.permission_requests as Array<Record<string, unknown>>
  if (typeof raw.worktree_path === 'string') patch.worktreePath = raw.worktree_path
  if (typeof raw.current_tool === 'string') patch.currentTool = raw.current_tool
  // displayTool is the sticky "last command" label; an empty string between
  // tools must NOT clear it (that causes the header tool-pill to flicker once
  // per tool call). Only write a real, non-empty label here.
  if (typeof raw.display_tool === 'string' && raw.display_tool.trim()) patch.displayTool = raw.display_tool
  if (typeof raw.tool_elapsed_ms === 'number') patch.toolElapsedMs = raw.tool_elapsed_ms
  if (typeof raw.last_tool_summary === 'string') patch.lastToolSummary = raw.last_tool_summary
  if (typeof raw.context_tokens === 'number') patch.contextTokens = raw.context_tokens
  // Ignore a non-positive window: an intra-turn 0 would wipe the last known
  // window and hide the context ring until the next tool call (flicker).
  if (typeof raw.context_window === 'number' && raw.context_window > 0) patch.contextWindow = raw.context_window
  if (typeof raw.context_remaining_pct === 'number') patch.contextRemainingPct = raw.context_remaining_pct
  if (typeof raw.input_tokens === 'number') patch.inputTokens = raw.input_tokens
  else if (typeof raw.input_tokens_total === 'number') patch.inputTokens = raw.input_tokens_total
  else if (typeof raw.tokens_in === 'number') patch.inputTokens = raw.tokens_in
  if (typeof raw.output_tokens === 'number') patch.outputTokens = raw.output_tokens
  else if (typeof raw.output_tokens_total === 'number') patch.outputTokens = raw.output_tokens_total
  else if (typeof raw.tokens_out === 'number') patch.outputTokens = raw.tokens_out
  if (typeof raw.total_tokens === 'number') patch.totalTokens = raw.total_tokens
  else if (typeof raw.tokens_total === 'number') patch.totalTokens = raw.tokens_total
  if (typeof raw.turn_cost_usd === 'number') patch.turnCostUsd = raw.turn_cost_usd
  if (typeof raw.session_cost_usd === 'number') patch.sessionCostUsd = raw.session_cost_usd
  if (typeof raw.pending_permission_count === 'number') patch.pendingPermissionCount = raw.pending_permission_count
  if (typeof raw.drain_mode === 'string') patch.drainMode = raw.drain_mode
  if (typeof raw.resident_status === 'string') patch.residentStatus = raw.resident_status
  if (typeof raw.actionable_inbox_count === 'number') patch.actionableInboxCount = raw.actionable_inbox_count
  if (typeof raw.protocol_backlog_count === 'number') patch.protocolBacklogCount = raw.protocol_backlog_count
  if (typeof raw.notification_backlog_count === 'number') patch.notificationBacklogCount = raw.notification_backlog_count
  return patch
}

function kanbanRuntimePatchFromPayload(raw: Record<string, unknown>): Partial<KanbanTask> {
  const patch: Partial<KanbanTask> = {}
  if (raw.latest_notification && typeof raw.latest_notification === 'object') {
    patch.latestNotification = raw.latest_notification as import('./types/kanban').WorkerNotification
  }
  if (typeof raw.current_tool === 'string') patch.currentTool = raw.current_tool
  // Sticky display label — see sessionRuntimePatchFromPayload above.
  if (typeof raw.display_tool === 'string' && raw.display_tool.trim()) patch.displayTool = raw.display_tool
  if (typeof raw.tool_elapsed_ms === 'number') patch.toolElapsedMs = raw.tool_elapsed_ms
  if (typeof raw.last_tool_summary === 'string') patch.lastToolSummary = raw.last_tool_summary
  if (typeof raw.context_tokens === 'number') patch.contextTokens = raw.context_tokens
  if (typeof raw.context_window === 'number' && raw.context_window > 0) patch.contextWindow = raw.context_window
  if (typeof raw.context_remaining_pct === 'number') patch.contextRemainingPct = raw.context_remaining_pct
  if (typeof raw.input_tokens === 'number') patch.inputTokens = raw.input_tokens
  else if (typeof raw.input_tokens_total === 'number') patch.inputTokens = raw.input_tokens_total
  else if (typeof raw.tokens_in === 'number') patch.inputTokens = raw.tokens_in
  if (typeof raw.output_tokens === 'number') patch.outputTokens = raw.output_tokens
  else if (typeof raw.output_tokens_total === 'number') patch.outputTokens = raw.output_tokens_total
  else if (typeof raw.tokens_out === 'number') patch.outputTokens = raw.tokens_out
  if (typeof raw.total_tokens === 'number') patch.totalTokens = raw.total_tokens
  else if (typeof raw.tokens_total === 'number') patch.totalTokens = raw.tokens_total
  if (typeof raw.turn_cost_usd === 'number') patch.turnCostUsd = raw.turn_cost_usd
  if (typeof raw.session_cost_usd === 'number') patch.sessionCostUsd = raw.session_cost_usd
  if (typeof raw.pending_permission_count === 'number') patch.pendingPermissionCount = raw.pending_permission_count
  if (typeof raw.drain_mode === 'string') patch.drainMode = raw.drain_mode
  if (typeof raw.resident_status === 'string') patch.residentStatus = raw.resident_status
  if (typeof raw.actionable_inbox_count === 'number') patch.actionableInboxCount = raw.actionable_inbox_count
  if (typeof raw.protocol_backlog_count === 'number') patch.protocolBacklogCount = raw.protocol_backlog_count
  if (typeof raw.notification_backlog_count === 'number') patch.notificationBacklogCount = raw.notification_backlog_count
  return patch
}

function shouldRefreshLiveSession(taskId: string, sessionStore: SessionStoreState | null): boolean {
  if (!sessionStore || !taskId) return false
  if (sessionStore.activeSessionId === taskId) return true
  const active = sessionStore.activeSession
  if (!active) return false
  if (active.taskId === taskId || active.parentSessionId === taskId || active.sessionId === taskId) {
    return true
  }

  const target = sessionStore.sessions.find((session) => session.taskId === taskId)
  if (!target) return false

  const activeKeys = new Set(
    [String(active.taskId ?? '').trim(), String(active.sessionId ?? '').trim()].filter(Boolean),
  )
  const targetParent = String(target.parentSessionId ?? '').trim()
  if (targetParent && activeKeys.has(targetParent)) {
    return true
  }

  const activeParent = String(active.parentSessionId ?? '').trim()
  if (!activeParent) return false
  return String(target.taskId ?? '').trim() === activeParent || String(target.sessionId ?? '').trim() === activeParent
}

function legacyPhaseFromSessionStatus(status: string): KanbanPhase {
  if (status === 'done' || status === 'delivered') return 'approved'
  if (status === 'failed') return 'failed'
  if (status === 'cancelled') return 'cancelled'
  if (status === 'pending') return 'queued'
  if (status === 'awaiting_manager_review' || status === 'awaiting_review') {
    return 'awaiting_manager_review'
  }
  if (status === 'awaiting_human') return 'awaiting_human'
  return 'running'
}

function legacyColumnForPhase(phase: KanbanPhase): string {
  if (phase === 'approved' || phase === 'failed' || phase === 'cancelled') return 'done'
  if (phase === 'awaiting_manager_review' || phase === 'awaiting_human') return 'in-review'
  if (phase === 'queued' || phase === 'ready' || phase === 'ready_for_rework' || phase === 'waiting_dependencies') return 'todo'
  return 'in-progress'
}

function legacyAggregatedStatus(status: string): RoleAggregatedStatus {
  if (status === 'done' || status === 'delivered') return 'done'
  if (status === 'failed' || status === 'cancelled') return 'failed'
  if (status === 'pending') return 'pending'
  if (status === 'awaiting_manager_review' || status === 'awaiting_review' || status === 'awaiting_human') return 'waiting'
  return 'active'
}

function legacyRuntimeStatus(status: string | undefined): AgentAnimStatus {
  if (status === 'reflecting' || status === 'tool_active' || status === 'idle') return status
  return 'idle'
}

function roleSummaryFromLegacySession(session: Session): RoleWorkItemSummary {
  const executionTurnId = getExecutionTurnId(session) || session.taskId
  const roleId = session.workItemRoleId || session.assigneeIds[0] || session.taskId
  const roleName = session.workItemRoleName || roleId.replace(/[_-]/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
  const phase = legacyPhaseFromSessionStatus(session.status)
  return {
    roleKey: roleId,
    roleId,
    roleName,
    runtimeStatus: legacyRuntimeStatus(session.agentStatus),
    aggregatedStatus: legacyAggregatedStatus(session.status),
    workItems: [
      {
        workItemId: session.workItemProjectionId || executionTurnId,
        workItemProjectionId: session.workItemProjectionId,
        phase,
        kanbanColumn: legacyColumnForPhase(phase),
        title: session.title || roleName,
        kind: session.workItemTurnType,
        executorRoleId: roleId,
        executorRoleName: roleName,
        createdAt: session.createdAt,
        updatedAt: session.updatedAt,
        executionTurnId,
        progressLog: session.progressLog,
        activitySections: session.progressLog.length > 0
          ? [{
              kind: 'activity',
              title: 'Runtime activity',
              roleName,
              runtimeTaskId: executionTurnId,
              entries: session.progressLog,
            }]
          : [],
      },
    ],
  }
}

/** Thin wrapper so the execution-panel lookup is a normal component, not a JSX IIFE. */
function MaybeExecutionPanel({ taskId, projectId, sessions, agents, getExternalTeamActivity, onLoadOlder, onSelectInvocation, runtimeOnly, onOpenRuntimeActivity, onClose }: {
  taskId: string | null
  projectId: string
  sessions: Session[]
  agents: AgentInfo[]
  getExternalTeamActivity: (projectId: string, taskId: string, invocationId?: string) => ExternalTeamActivityRecord | undefined
  onLoadOlder: (taskId: string, record: ExternalTeamActivityRecord) => void
  onSelectInvocation: (taskId: string, invocationId: string) => void
  runtimeOnly: boolean
  onOpenRuntimeActivity: () => void
  onClose: () => void
}) {
  if (!taskId) return null

  for (const session of sessions) {
    const payload = session.roleWorkItems
    if (!payload) continue
    for (const role of Object.values(payload)) {
      const row = role.workItems.find(workItem => workItem.executionTurnId === taskId)
      if (!row) continue
      if (isJiuwenOpaqueTeam(row) && !runtimeOnly) {
        const record = getExternalTeamActivity(projectId, taskId)
        return (
          <JiuwenTeamActivityPanel
            key={`${projectId}:${taskId}`}
            title={`${row.executorRoleName || row.executorRoleId || role.roleName || role.roleId} · JiuwenSwarm Team`}
            coveredRoleIds={row.coveredRoleIds}
            record={record}
            fallbackSummary={row.externalTeamSummary}
            onLoadOlder={record?.hasMore ? () => onLoadOlder(taskId, record) : undefined}
            onSelectInvocation={invocationId => onSelectInvocation(taskId, invocationId)}
            onOpenRuntimeActivity={onOpenRuntimeActivity}
            onClose={onClose}
          />
        )
      }
      return (
        <ExecutionPanel
          role={role}
          focusedWorkItemId={row.workItemId}
          focusedExecutionTurnId={row.executionTurnId}
          agents={agents}
          onClose={onClose}
        />
      )
    }
  }

  const focused = sessions.find(x => x.taskId === taskId || getExecutionTurnId(x) === taskId)
  if (!focused) return null
  if (isJiuwenOpaqueTeam(focused) && !runtimeOnly) {
    const runtimeTaskId = getExecutionTurnId(focused) || focused.taskId
    const record = getExternalTeamActivity(projectId, runtimeTaskId)
    return (
      <JiuwenTeamActivityPanel
        key={`${projectId}:${runtimeTaskId}`}
        title={`${focused.workItemRoleName || focused.title || 'Task'} · JiuwenSwarm Team`}
        coveredRoleIds={focused.coveredRoleIds}
        record={record}
        fallbackSummary={focused.externalTeamSummary}
        onLoadOlder={record?.hasMore ? () => onLoadOlder(runtimeTaskId, record) : undefined}
        onSelectInvocation={invocationId => onSelectInvocation(runtimeTaskId, invocationId)}
        onOpenRuntimeActivity={onOpenRuntimeActivity}
        onClose={onClose}
      />
    )
  }
  const role = roleSummaryFromLegacySession(focused)
  return (
    <ExecutionPanel
      role={role}
      focusedExecutionTurnId={taskId}
      agents={agents}
      onClose={onClose}
    />
  )
}

export default function App() {
  const { locale, setLocale, t } = useI18n()
  const bridgeRef = useRef(new GameBridge())
  useMemo(() => registerTestRunner(bridgeRef.current), [])
  const clientRef = useRef<VisualSocketClient | null>(null)
  const [eventTimeline] = useState(() => new EventTimelineStore(MAX_LOG_ITEMS))

  const initialUrl = defaultWsUrl()

  const [wsUrl, setWsUrl] = useState(initialUrl)
  const [wsUrlInput, setWsUrlInput] = useState(initialUrl)
  const [status, setStatus] = useState<SocketStatus>('disconnected')
  const [statusDetail, setStatusDetail] = useState('')
  const [snapshot, setSnapshot] = useState<VisualSnapshot | null>(null)
  const [projectIdPolicy, setProjectIdPolicy] = useState<ProjectIdPolicy | null>(null)
  const [theme, setTheme] = useState<ThemeName>(loadStoredTheme)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try { return localStorage.getItem('opc_office_sidebar_collapsed') === '1' } catch { return false }
  })
  const toggleSidebar = useCallback(() => setSidebarCollapsed(v => {
    const next = !v
    try { localStorage.setItem('opc_office_sidebar_collapsed', next ? '1' : '0') } catch { /* private mode */ }
    return next
  }), [])
  const [activePage, setActivePage] = useState<AppPage>('workspace')
  const [swarmAgents, setSwarmAgents] = useState<AgentInfo[]>([])
  const [showDevTools, setShowDevTools] = useState(false)
  const [lastTaskDoneAgent, setLastTaskDoneAgent] = useState<string | null>(null)
  const [globalExecMode, setGlobalExecMode] = useState<AppExecMode>('task')
  const [globalCompanyProfile, setGlobalCompanyProfile] = useState<'corporate' | 'custom'>('corporate')
  const [globalTaskPreferredAgent, setGlobalTaskPreferredAgent] = useState<TaskPreferredAgent>('native')
  const [globalNativeApprovalDefault, setGlobalNativeApprovalDefault] = useState<NativeApprovalLevel>('auto')
  const [orgInfoData, setOrgInfoData] = useState<OrgInfoPayload | null>(null)
  const [commsState, setCommsState] = useState<import('./lib/wsClient').CommsStatePayload | null>(null)
  const [commsMessage, setCommsMessage] = useState<import('./lib/wsClient').CommsMessagePayload | null>(null)
  const [talentTemplates, setTalentTemplates] = useState<TalentTemplate[]>([])
  const [defaultTalentDir, setDefaultTalentDir] = useState<string>('')
  const [employeeDetail, setEmployeeDetail] = useState<EmployeeDetailPayload | null>(null)
  const [reorgProposals, setReorgProposals] = useState<ReorgProposalInfo[]>([])
  const [marketPresets, setMarketPresets] = useState<any[]>([])
  const [marketPreviewData, setMarketPreviewData] = useState<any>(null)
  const [configExportYaml, setConfigExportYaml] = useState<string | null>(null)
  const [configImportPreview, setConfigImportPreview] = useState<{ roles_added: number; roles_removed: number; employees_changed: number } | null>(null)
  const [configImportError, setConfigImportError] = useState<string | null>(null)
  const [savedOrgsList, setSavedOrgsList] = useState<SavedOrgSummary[] | null>(null)
  const [activeSavedOrg, setActiveSavedOrg] = useState<string | null>(null)
  const [savedOrgVersionAtLoad, setSavedOrgVersionAtLoad] = useState<number | null>(null)
  const [orgCreatePending, setOrgCreatePending] = useState(false)
  const [orgCreateResult, setOrgCreateResult] = useState<(OrgSavedCreatePayload & { nonce: number }) | null>(null)
  const [orgToast, setOrgToast] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null)
  const timersRef = useRef<Set<ReturnType<typeof setTimeout>>>(new Set())
  const replayedEventIds = useRef<Set<string>>(new Set())
  const swarmAgentsRef = useRef<AgentInfo[]>([])
  const globalExecModeRef = useRef<AppExecMode>('task')
  const globalNativeApprovalDefaultRef = useRef<NativeApprovalLevel>('auto')
  const kanbanCreateRef = useRef<((data: { title: string; description?: string; priority: null; assignee_id?: string }) => void) | null>(null)
  const chatStoreRef = useRef<ChatStoreState | null>(null)
  const boardStoreRef = useRef<BoardStoreState | null>(null)
  const sessionStoreRef = useRef<SessionStoreState | null>(null)
  const projectStoreRef = useRef<ProjectStoreState | null>(null)
  const activeProjectIdRef = useRef<string>('default')
  const pendingProjectSwitchRef = useRef<string | null>(null)
  const currentSwitchSeqRef = useRef<string>('')
  const projectViewGenerationRef = useRef<number>(0)
  const userSelectedProjectRef = useRef<boolean>(false)
  const projectsHydratedRef = useRef<boolean>(false)
  const lastProjectIndexRefreshRef = useRef<number>(0)
  const pendingSessionCreateRef = useRef(false)
  const pendingSessionCreateProjectIdRef = useRef<string | null>(null)
  const pendingSessionCreateTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pendingSessionDetailRefreshRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())
  const [hiringTemplateId, setHiringTemplateId] = useState<string | null>(null)
  const [toastMessage, setToastMessage] = useState<string | null>(null)
  const [toastType, setToastType] = useState<'success' | 'error'>('success')
  const [deletingAgentId, setDeletingAgentId] = useState<string | null>(null)
  const [executionPanelTaskId, setExecutionPanelTaskId] = useState<string | null>(null)
  const [executionPanelRuntimeOnly, setExecutionPanelRuntimeOnly] = useState(false)
  const [outdoorOverride, setOutdoorOverride] = useState<'auto' | 'day' | 'night'>(() => readOutdoorOverrideUi())

  const normalizeProjectId = useCallback((value: unknown): string => {
    const projectId = typeof value === 'string' ? value.trim() : ''
    return projectId || 'default'
  }, [])

  const getActiveProjectId = useCallback(() => normalizeProjectId(activeProjectIdRef.current), [normalizeProjectId])

  const newSwitchSeq = useCallback(() => {
    return `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
  }, [])

  const projectIdFromPayload = useCallback((payload: Record<string, unknown> | null | undefined): string => {
    if (!payload || typeof payload !== 'object') return ''
    for (const key of ['project_id', 'projectId', 'active_project_id', 'activeProjectId']) {
      const value = payload[key]
      if (typeof value === 'string' && value.trim()) return value.trim()
    }
    const data = payload.data
    if (data && typeof data === 'object') {
      for (const key of ['project_id', 'projectId']) {
        const value = (data as Record<string, unknown>)[key]
        if (typeof value === 'string' && value.trim()) return value.trim()
      }
    }
    return ''
  }, [])

  const payloadMatchesActiveProject = useCallback((payload: Record<string, unknown> | null | undefined, allowMissing = false): boolean => {
    const projectId = projectIdFromPayload(payload)
    if (!projectId) return allowMissing
    return projectId === getActiveProjectId()
  }, [getActiveProjectId, projectIdFromPayload])

  const payloadSwitchSeq = useCallback((payload: Record<string, unknown> | null | undefined): string => {
    if (!payload || typeof payload !== 'object') return ''
    const value = payload.switch_seq ?? payload.switchSeq
    return typeof value === 'string' ? value.trim() : ''
  }, [])

  const payloadViewGeneration = useCallback((payload: Record<string, unknown> | null | undefined): number | null => {
    if (!payload || typeof payload !== 'object') return null
    const value = payload.view_generation ?? payload.viewGeneration
    if (typeof value === 'number') return value
    if (typeof value === 'string' && value.trim()) {
      const parsed = Number(value)
      return Number.isFinite(parsed) ? parsed : null
    }
    return null
  }, [])

  const payloadMatchesCurrentSwitch = useCallback((payload: Record<string, unknown> | null | undefined): boolean => {
    const projectId = projectIdFromPayload(payload)
    if (!projectId) return false
    const pendingProjectId = pendingProjectSwitchRef.current
    if (projectId !== getActiveProjectId() && projectId !== pendingProjectId) return false
    const generation = payloadViewGeneration(payload)
    if (generation !== null && generation !== projectViewGenerationRef.current) return false
    const seq = payloadSwitchSeq(payload)
    return !seq || !currentSwitchSeqRef.current || seq === currentSwitchSeqRef.current
  }, [getActiveProjectId, payloadSwitchSeq, payloadViewGeneration, projectIdFromPayload])

  const shouldSuppressTaskNotFound = useCallback((payload: Record<string, unknown> | null | undefined): boolean => {
    if (!payload || payload.error !== 'task_not_found') return false
    if (!payloadMatchesActiveProject(payload, false)) return true
    const action = typeof payload.action === 'string' ? payload.action : ''
    if (action !== 'session_detail') return false
    const generation = payloadViewGeneration(payload)
    if (generation !== null && generation !== projectViewGenerationRef.current) return true
    const taskId = typeof payload.task_id === 'string' ? payload.task_id : ''
    if (!taskId) return true
    const sessions = sessionStoreRef.current?.sessions ?? []
    return !sessions.some(session => session.taskId === taskId)
  }, [payloadMatchesActiveProject, payloadViewGeneration])

  const clearPendingSessionDetailRefreshes = useCallback(() => {
    for (const tid of pendingSessionDetailRefreshRef.current.values()) clearTimeout(tid)
    pendingSessionDetailRefreshRef.current.clear()
  }, [])

  const scheduleSessionDetailRefresh = useCallback((taskId: string, detailLevel: 'summary' | 'full' = 'full', force = false, projectId?: string) => {
    if (!taskId) return
    const scopedProjectId = projectId || getActiveProjectId()
    const generation = projectViewGenerationRef.current
    // Check relevance using latest ref; skip non-live sessions unless forced.
    // Re-check inside the timer so that a session that *becomes* active during
    // the debounce window still gets its detail loaded.
    if (!force && !shouldRefreshLiveSession(taskId, sessionStoreRef.current)) return
    const timerKey = `${scopedProjectId}:${generation}:${taskId}`
    const existing = pendingSessionDetailRefreshRef.current.get(timerKey)
    if (existing) {
      clearTimeout(existing)
      pendingSessionDetailRefreshRef.current.delete(timerKey)
    }
    const tid = setTimeout(() => {
      pendingSessionDetailRefreshRef.current.delete(timerKey)
      if (scopedProjectId !== getActiveProjectId()) return
      if (generation !== projectViewGenerationRef.current) return
      // Re-check liveness at fire time (ref may have been updated by now)
      if (!force && !shouldRefreshLiveSession(taskId, sessionStoreRef.current)) return
      const client = clientRef.current
      sessionStoreRef.current?.updateSession(taskId, {
        detailLoading: true,
        detailError: undefined,
        viewGeneration: generation,
      })
      if (!client) {
        sessionStoreRef.current?.updateSession(taskId, {
          detailLoading: false,
          detailError: 'connection_unavailable',
        })
        return
      }
      void client.sessionDetail(scopedProjectId, taskId, {
        limit: 200,
        detailLevel,
        include: detailLevel === 'full'
          ? ['messages', 'session_state', 'progress', 'work_items', 'runtime_context']
          : ['messages', 'session_state'],
        viewGeneration: generation,
      }).then((payload) => {
        // Transport-local failures resolve the request Promise without
        // producing a websocket ACK.
        if (payload.ok !== false) return
        if (scopedProjectId !== getActiveProjectId()) return
        if (generation !== projectViewGenerationRef.current) return
        sessionStoreRef.current?.updateSession(taskId, {
          detailLoading: false,
          detailError: String(payload.error ?? 'request_failed'),
        })
      })
    }, 180)
    pendingSessionDetailRefreshRef.current.set(timerKey, tid)
  }, [getActiveProjectId])

  const showToast = useCallback((msg: string, type: 'success' | 'error' = 'success') => {
    setToastMessage(msg); setToastType(type)
    setTimeout(() => setToastMessage(null), 3000)
  }, [])

  const clearPendingSessionCreate = useCallback(() => {
    if (pendingSessionCreateTimerRef.current) {
      clearTimeout(pendingSessionCreateTimerRef.current)
      pendingSessionCreateTimerRef.current = null
    }
    pendingSessionCreateRef.current = false
    pendingSessionCreateProjectIdRef.current = null
  }, [])

  const beginPendingSessionCreate = useCallback((projectId: string) => {
    clearPendingSessionCreate()
    pendingSessionCreateRef.current = true
    pendingSessionCreateProjectIdRef.current = projectId
    pendingSessionCreateTimerRef.current = setTimeout(() => {
      if (!pendingSessionCreateRef.current || pendingSessionCreateProjectIdRef.current !== projectId) return
      clearPendingSessionCreate()
      setStatusDetail('create_session_timeout')
      showToast('Session creation timed out. Please try again.', 'error')
    }, 30_000)
  }, [clearPendingSessionCreate, showToast])

  // ── Store hooks: MUST be declared before WebSocket effect so refs are available ──
  const boardStore = useBoardStore()
  const chatStore = useChatStore()
  const sessionStore = useSessionStore()
  const projectStore = useProjectStore()
  const externalTeamActivityStore = useExternalTeamActivityStore()
  const {
    resetProject: resetExternalTeamProject,
    markLoading: markExternalTeamLoading,
    applyEvent: applyExternalTeamEvent,
    applySnapshot: applyExternalTeamSnapshot,
  } = externalTeamActivityStore

  // Per-session recruitment (role_id -> recruited names) for the org canvas.
  // The org_info payload is project-global, so the canvas otherwise can't show
  // the *selected* session's hires; this derives them from that session's chat
  // recruitment checkpoint (display-only — no backend change). Null when the
  // selected session has no recruitment loaded -> canvas falls back to global.
  const activeSessionId = sessionStore.activeSessionId
  const sessionRecruitmentByRole = useMemo(() => {
    if (!activeSessionId) return null
    return extractSessionRecruitmentByRole(chatStore.getChannelMessages(sessionChannelId(activeSessionId)))
  }, [activeSessionId, chatStore])

  const resetProjectScopedView = useCallback((projectId: string) => {
    const nextProjectId = normalizeProjectId(projectId)
    activeProjectIdRef.current = nextProjectId
    clearPendingSessionDetailRefreshes()
    chatStore.initFromBackend(nextProjectId, [], [])
    boardStore.initFromBackend(nextProjectId, [], [], [])
    sessionStore.initFromBackend(nextProjectId, [])
    resetExternalTeamProject(nextProjectId)
    clearPendingSessionCreate()
    setExecutionPanelTaskId(null)
    setExecutionPanelRuntimeOnly(false)
    setCommsState(null)
    setCommsMessage(null)
    setReorgProposals([])
  }, [boardStore, chatStore, clearPendingSessionCreate, clearPendingSessionDetailRefreshes, normalizeProjectId, resetExternalTeamProject, sessionStore])

  const beginProjectSwitch = useCallback((projectId: string): string => {
    const nextProjectId = normalizeProjectId(projectId)
    const switchSeq = newSwitchSeq()
    userSelectedProjectRef.current = true
    currentSwitchSeqRef.current = switchSeq
    projectViewGenerationRef.current += 1
    pendingProjectSwitchRef.current = nextProjectId
    clearPendingSessionDetailRefreshes()
    setStatusDetail(`Switching to ${nextProjectId}...`)
    return switchSeq
  }, [clearPendingSessionDetailRefreshes, newSwitchSeq, normalizeProjectId])

  // Sync store refs via useLayoutEffect — runs synchronously after commit,
  // BEFORE any async callbacks (WebSocket messages) can fire.
  // This eliminates the race condition where handlers read null refs.
  const storesReadyRef = useRef(false)
  useLayoutEffect(() => {
    chatStoreRef.current = chatStore
    boardStoreRef.current = boardStore
    sessionStoreRef.current = sessionStore
    projectStoreRef.current = projectStore
    kanbanCreateRef.current = (data) => {
      if (!boardStore.activeBoardId || boardStore.activeBoardColumns.length === 0) return
      const todoCol = boardStore.activeBoardColumns.find(c => c.name === 'Todo')
      const colId = todoCol?.id ?? boardStore.activeBoardColumns[0].id
      boardStore.createTask({
        boardId: boardStore.activeBoardId,
        columnId: colId,
        title: data.title,
        priority: data.priority,
        assigneeIds: data.assignee_id ? [data.assignee_id] : [],
      })
    }
    storesReadyRef.current = true
  })

  useEffect(() => {
    swarmAgentsRef.current = swarmAgents
  }, [swarmAgents])

  useEffect(() => {
    globalExecModeRef.current = globalExecMode
  }, [globalExecMode])

  // ── Runtime-delta coalescing ────────────────────────────────────────
  // assistant_delta / thinking_delta arrive at token frequency; writing each
  // one straight into the stores re-renders the whole app once per token.
  // Buffer them per task and flush at most every 80ms. Any non-delta event
  // for the same task flushes first, so store-write ordering (e.g. clearDraft
  // on turn boundaries) is preserved exactly.
  const pendingDeltaFlushRef = useRef<Map<string, {
    draftText: string
    draftIteration?: number
    draftTurnId?: string
    sessionPatch: Partial<import('./types/kanban').Session>
    kanbanPatch: Partial<KanbanTask>
  }>>(new Map())
  const deltaFlushTimerRef = useRef<number | null>(null)

  const flushPendingDeltas = useCallback((onlyTaskId?: string) => {
    const pending = pendingDeltaFlushRef.current
    if (pending.size === 0) return
    const ids = onlyTaskId ? [onlyTaskId] : Array.from(pending.keys())
    for (const taskId of ids) {
      const entry = pending.get(taskId)
      if (!entry) continue
      pending.delete(taskId)
      const ss = sessionStoreRef.current
      if (entry.draftText) {
        ss?.appendDraft(taskId, entry.draftText, entry.draftIteration, entry.draftTurnId)
      }
      ss?.updateSession(taskId, entry.sessionPatch)
      if (Object.keys(entry.kanbanPatch).length > 0) {
        boardStoreRef.current?.updateTask(taskId, entry.kanbanPatch)
      }
    }
    if (pending.size === 0 && deltaFlushTimerRef.current !== null) {
      window.clearTimeout(deltaFlushTimerRef.current)
      deltaFlushTimerRef.current = null
    }
  }, [])

  const scheduleDeltaFlush = useCallback(() => {
    if (deltaFlushTimerRef.current !== null) return
    deltaFlushTimerRef.current = window.setTimeout(() => {
      deltaFlushTimerRef.current = null
      flushPendingDeltas()
    }, 80)
  }, [flushPendingDeltas])

  useEffect(() => {
    const client = new VisualSocketClient(wsUrl, {
      onSnapshot: (data) => {
        if (!payloadMatchesCurrentSwitch(data as unknown as Record<string, unknown>)) return
        setSnapshot(data)
        const timeline = data.timeline.slice(-MAX_LOG_ITEMS)
        eventTimeline.replace(timeline)

        const ids = new Set<string>()
        for (const evt of timeline) ids.add(evt.event_id)
        replayedEventIds.current = ids

        // Push snapshot to Phaser
        bridgeRef.current.pushSnapshot(data)

        const agentEntries = Object.entries(data.agents ?? {})
        if (agentEntries.length > 0) {
          const infos = mapAgentListPayload(
            agentEntries.map(([id, info]) => ({
              ...((info && typeof info === 'object') ? info as Record<string, unknown> : {}),
              agent_id: id,
            })),
            swarmAgentsRef.current,
          )
          swarmAgentsRef.current = infos
          setSwarmAgents(infos)
        }
        // Restore exec_mode / company_profile from snapshot (survives reconnection)
        if (data.exec_mode) setGlobalExecMode(normalizeExecMode(data.exec_mode))
        if (data.company_profile) setGlobalCompanyProfile(normalizeCompanyProfile(data.company_profile))
        if (data.task_preferred_agent) setGlobalTaskPreferredAgent(normalizeTaskPreferredAgent(data.task_preferred_agent))
        if (data.native_approval_default) {
          globalNativeApprovalDefaultRef.current = data.native_approval_default
          setGlobalNativeApprovalDefault(data.native_approval_default)
        }

      },
      onEvent: (evt) => {
        try {
          if (!payloadMatchesActiveProject(evt as unknown as Record<string, unknown>, false)) return
          if (replayedEventIds.current.has(evt.event_id)) {
            replayedEventIds.current.delete(evt.event_id)
            return
          }
          eventTimeline.append(evt)

          // Push event to Phaser
          bridgeRef.current.pushEvent(evt)

          // task_routed: create kanban card only if task_id is provided (#6 — no title-matching)
          if (evt.type === 'task_routed' && evt.agent_id) {
            if (globalExecModeRef.current === 'company' || globalExecModeRef.current === 'org') {
              return
            }
            const taskId = evt.data?.task_id as string | undefined
            if (taskId && !boardStoreRef.current?.tasks.find(t => t.id === taskId)) {
              const preview = typeof evt.data?.content_preview === 'string'
                ? evt.data.content_preview.slice(0, 80)
                : 'Task'
              kanbanCreateRef.current?.({
                title: preview,
                priority: null,
                assignee_id: evt.agent_id,
              })
            }
          }

          if (evt.type === 'task_done' && evt.agent_id) {
            setLastTaskDoneAgent(evt.agent_id)
          }

          if ([
            'turn_started',
            'assistant_delta',
            'thinking_delta',
            'status_snapshot',
            'tool_started',
            'tool_progress',
            'tool_completed',
            'permission_requested',
            'permission_resolved',
            'cost_update',
            'context_usage',
            'context_warning',
            'subagent_started',
            'subagent_updated',
            'subagent_completed',
            'member_inbox_updated',
            'compaction_applied',
            'checkpoint_saved',
            'turn_completed',
            'turn_failed',
          ].includes(evt.type)) {
            const data = evt.data as Record<string, unknown>
            const taskId = typeof data.task_id === 'string' ? data.task_id : ''
            if (taskId) {
              const isDeltaEvent = evt.type === 'assistant_delta' || evt.type === 'thinking_delta'
              const bufferedDraftTurnId = pendingDeltaFlushRef.current.get(taskId)?.draftTurnId
              // Store-write ordering guarantee: everything buffered for this
              // task lands before a non-delta event. A matching terminal
              // message replaces the draft later; flushing here must not make
              // a completion boundary remove the visible turn prematurely.
              if (!isDeltaEvent) flushPendingDeltas(taskId)
              const ss = sessionStoreRef.current
              const bs = boardStoreRef.current
              const existingSession = ss?.sessions.find(session => session.taskId === taskId)
              const projectionId = typeof data.work_item_projection_id === 'string'
                ? data.work_item_projection_id
                : ''
              const executionMode = typeof data.execution_mode === 'string' ? data.execution_mode : ''
              const isTaskModeRuntime = executionMode === 'task_mode' || projectionId === 'task_mode_execution'
              // Drafts represent one logical assistant turn. The shared
              // resolver deliberately prefers conversation identity over the
              // iteration-scoped runtime turn id.
              const turnId = resolveCanonicalTurnId(data) || undefined
              const marksCompanyRuntime =
                !!projectionId && projectionId !== 'task_mode_execution' && !isTaskModeRuntime
              if (marksCompanyRuntime && !isDeltaEvent && existingSession?.isCompanyRuntime !== true) {
                ss?.setCompanyRuntime(taskId, true)
              }

              const runtimePartial: Partial<import('./types/kanban').Session> = {
                lastRuntimeEventType: evt.type,
                ...(evt.type === 'member_inbox_updated' ? {} : { updatedAt: Date.now() }),
                ...sessionRuntimePatchFromPayload(data),
              }
              const toolName = typeof data.tool_name === 'string' ? data.tool_name : undefined
              const kanbanPatch: Partial<KanbanTask> = {
                ...kanbanRuntimePatchFromPayload(data),
                // currentTool is active-only (clears between tools); displayTool
                // is sticky and only updates on a real, non-empty tool name.
                ...(toolName !== undefined ? { currentTool: toolName || undefined } : {}),
                ...(toolName ? { displayTool: toolName } : {}),
              }
              if (isDeltaEvent) {
                const pending = pendingDeltaFlushRef.current
                let entry = pending.get(taskId)
                const deltaText = evt.type === 'assistant_delta' && typeof data.text === 'string'
                  ? data.text
                  : ''
                // A turn boundary inside the buffer would corrupt the draft
                // reset logic (APPEND_DRAFT resets on turnId change) — flush
                // the previous turn's chunk before starting a new one.
                if (
                  entry && deltaText && entry.draftText &&
                  entry.draftTurnId !== undefined && turnId !== undefined &&
                  entry.draftTurnId !== turnId
                ) {
                  flushPendingDeltas(taskId)
                  entry = undefined
                }
                if (!entry) {
                  entry = { draftText: '', sessionPatch: {}, kanbanPatch: {} }
                  pending.set(taskId, entry)
                }
                if (deltaText) {
                  entry.draftText += deltaText
                  entry.draftIteration = typeof data.iteration === 'number'
                    ? data.iteration
                    : entry.draftIteration ?? existingSession?.draftIteration
                  if (turnId !== undefined) entry.draftTurnId = turnId
                }
                entry.sessionPatch = {
                  ...entry.sessionPatch,
                  ...runtimePartial,
                  ...(marksCompanyRuntime ? { isCompanyRuntime: true } : {}),
                }
                entry.kanbanPatch = { ...entry.kanbanPatch, ...kanbanPatch }
                scheduleDeltaFlush()
              } else {
                const activeDraftTurnId = String(
                  bufferedDraftTurnId ?? existingSession?.draftTurnId ?? '',
                ).trim()
                const startsNewCanonicalTurn = evt.type === 'turn_started'
                  && !!turnId
                  && !!activeDraftTurnId
                  && turnId !== activeDraftTurnId
                if (evt.type === 'turn_failed' || startsNewCanonicalTurn) {
                  ss?.clearDraft(taskId)
                }
                ss?.updateSession(taskId, runtimePartial)
                bs?.updateTask(taskId, kanbanPatch)
              }
              const skipDetailRefresh = (
                isTaskModeRuntime && TASK_MODE_LOW_VALUE_RUNTIME_EVENTS.has(evt.type)
              ) || SESSION_DETAIL_REFRESH_LOW_VALUE_RUNTIME_EVENTS.has(evt.type)
              if (evt.type !== 'assistant_delta' && !skipDetailRefresh) {
                scheduleSessionDetailRefresh(taskId)
              }
            }
          }

          if (evt.type === 'agent_removed' && evt.agent_id) {
            const agentId = evt.agent_id
            unassignAgent(agentId)
            const cs = chatStoreRef.current
            if (cs) {
              cs.markSenderDeleted(agentId)
              cs.removeParticipant(agentId)
            }
          }

        } catch (e) { console.error('[onEvent] Error:', e, evt) }
      },
      onAck: (payload) => {
        try {
          if (payload.ok === false) {
            if (payload.action === 'create_session') {
              clearPendingSessionCreate()
            }
            if (shouldSuppressTaskNotFound(payload)) return
            if (payload.action === 'session_detail' && typeof payload.task_id === 'string') {
              sessionStoreRef.current?.updateSession(payload.task_id, {
                detailLoading: false,
                detailError: String(payload.error ?? 'request_failed'),
              })
            }
            setStatusDetail(String(payload.error ?? 'request_failed'))
            setDeletingAgentId(null)
            showToast(String(payload.error ?? 'Request failed'), 'error')
          }
          // Employee deployed to office
          if (payload.ok && payload.action === 'employee_imported') {
            showToast('Employee deployed to office!')
          }
          // Talent import success
          if (payload.ok && payload.action === 'talent_imported') {
            showToast(`Imported ${payload.count ?? 0} templates!`)
          }
          // Talent hire success
          if (payload.ok && payload.action === 'talent_hired') {
            setHiringTemplateId(null)
            showToast(`${payload.name ?? 'Agent'} hired and added to office!`)
          }
          if (payload.ok && payload.action === 'architecture_reset') {
            setActiveSavedOrg(null)
            setSavedOrgVersionAtLoad(null)
          }
          if (payload.ok && payload.action === 'session_detail') {
            if (!payloadMatchesActiveProject(payload, false)) return
            const detailGeneration = payloadViewGeneration(payload)
            if (detailGeneration !== null && detailGeneration !== projectViewGenerationRef.current) return
            const detailTaskId = typeof payload.task_id === 'string' ? payload.task_id : ''
            const detailMessages = Array.isArray(payload.messages)
              ? payload.messages.map(mapBackendMessage)
              : []
            const totalMessageCount = typeof payload.message_count === 'number'
              ? payload.message_count
              : detailMessages.length
            const detailLevel = payload.detail_level === 'full' ? 'full' : 'summary'
            const cs = chatStoreRef.current
            if (cs && detailMessages.length > 0) {
              cs.mergeMessagesFromBackend(detailMessages)
            }
            const ss = sessionStoreRef.current
            if (ss && detailTaskId) {
              const existingSession = ss.sessions.find(session => session.taskId === detailTaskId)
              const previousHasMore = detailLevel === 'full'
                ? existingSession?.fullHasMore
                : existingSession?.summaryHasMore
              const detailHasMore = mergeSessionDetailHasMore(
                previousHasMore,
                payload.has_more === true,
                payload.client_history_page === true,
              )
              const draftTurnId = String(existingSession?.draftTurnId ?? '').trim()
              const detailHasFinalForDraft = !!draftTurnId
                && !!cs
                && detailMessages.some(message => terminalAssistantTurnId(message) === draftTurnId)
              if (detailHasFinalForDraft) {
                ss.clearDraft(detailTaskId)
              }
              const rawSessionState = payload.session_state
              const sessionPatch = rawSessionState && typeof rawSessionState === 'object'
                ? mapBackendSession(rawSessionState)
                : null
              ss.updateSession(detailTaskId, {
                ...(sessionPatch ?? {}),
                ...(typeof payload.handoff_context === 'string' ? { handoffContext: payload.handoff_context } : {}),
                ...(typeof payload.handoff_to === 'string' ? { handoffTo: payload.handoff_to } : {}),
                messageCount: totalMessageCount,
                detailLoaded: true,
                ...(detailLevel === 'full' ? { fullLoaded: !detailHasMore } : {}),
                hasMore: detailHasMore,
                ...(detailLevel === 'full'
                  ? { fullHasMore: detailHasMore }
                  : { summaryHasMore: detailHasMore }),
                detailLoading: false,
                detailError: undefined,
                viewGeneration: detailGeneration ?? projectViewGenerationRef.current,
              })
            }
          }
          if (Array.isArray(payload.agents)) {
            const nextAgents = mapAgentListPayload(payload.agents as unknown[], swarmAgentsRef.current)
            swarmAgentsRef.current = nextAgents
            setSwarmAgents(nextAgents)
            for (const agent of nextAgents) {
              bridgeRef.current.ensureAgent(agent.agent_id, agent.name, agent.office_id, agent.appearance?.palette, agent.appearance?.desk_id)
            }
            bridgeRef.current.emit('officeChanged')
          }
          if (payload.ok && (payload.agent_id || payload.deleted)) {
            if (payload.deleted) {
              setDeletingAgentId(null)
              showToast('Agent removed')
            }
            if (payload.agent_id && !payload.deleted) {
              showToast('Agent created!')
            }
            clientRef.current?.listAgents()
          }
          // Handle project list response
          if (payload.ok && Array.isArray(payload.projects)) {
            setProjectIdPolicy(compileProjectIdPolicy(payload.project_id_policy))
            const ps = projectStoreRef.current
            if (ps) {
              const previousActiveId = getActiveProjectId()
              const initialHydration = !projectsHydratedRef.current
              const shouldUseBackendActive = !projectsHydratedRef.current && !userSelectedProjectRef.current
              const backendActiveId = typeof payload.active_project_id === 'string'
                ? payload.active_project_id.trim()
                : ''
              const createdProjectId = payload.action === 'create_project' && typeof payload.project_id === 'string'
                ? payload.project_id.trim()
                : ''
              const activeId = shouldUseBackendActive
                ? normalizeProjectId(createdProjectId || backendActiveId || activeProjectIdRef.current)
                : (createdProjectId ? normalizeProjectId(createdProjectId) : getActiveProjectId())
              if (createdProjectId) userSelectedProjectRef.current = true
              projectsHydratedRef.current = true
              activeProjectIdRef.current = activeId
              ps.initFromBackend(
                payload.projects as { id: string; name: string }[],
                activeId,
              )
              if (activeId !== previousActiveId || initialHydration) {
                const switchSeq = newSwitchSeq()
                currentSwitchSeqRef.current = switchSeq
                pendingProjectSwitchRef.current = activeId
                projectViewGenerationRef.current += 1
                setStatusDetail(`Switching to ${activeId}...`)
                clientRef.current?.switchProject(activeId, switchSeq)
              }
            }
          }
          // Handle collab_sync response
          if (payload.ok && Array.isArray(payload.channels)) {
            if (!payloadMatchesCurrentSwitch(payload)) return
            const syncData = mapCollabSyncPayload(payload)
            const syncProjectId = projectIdFromPayload(payload)
            if (!syncProjectId) return
            const applyingProjectSwitch = pendingProjectSwitchRef.current === syncProjectId || getActiveProjectId() !== syncProjectId
            activeProjectIdRef.current = syncProjectId
            pendingProjectSwitchRef.current = null
            projectStoreRef.current?.setActiveProject(syncProjectId)
            if (applyingProjectSwitch) {
              clearPendingSessionDetailRefreshes()
              setExecutionPanelTaskId(null)
              setExecutionPanelRuntimeOnly(false)
              setCommsState(null)
              setCommsMessage(null)
            }
            setStatusDetail('')
            const cs = chatStoreRef.current
            if (cs) {
              cs.initFromBackend(syncProjectId, syncData.channels, syncData.messages)
            }
            const bs = boardStoreRef.current
            if (bs) {
              bs.initFromBackend(syncProjectId, syncData.boards, syncData.columns, syncData.tasks)
            }
            const ss = sessionStoreRef.current
            if (ss) {
              ss.initFromBackend(syncProjectId, syncData.sessions)
              // After sync, refresh active session detail so content is loaded
              const activeId = ss.activeSessionId
              if (activeId) {
                scheduleSessionDetailRefresh(activeId, 'full', true)
              }
            }
          }
        } catch (e) { console.error('[onAck] Error:', e) }
      },
      onStatus: (next, detail) => {
        setStatus(next)
        setStatusDetail(detail ?? '')
        if (next !== 'connected') setProjectIdPolicy(null)
        if (next === 'connected') {
          const projectId = getActiveProjectId()
          client.listProjects()
          // Re-fetch org data so OrgTab isn't stale after reconnect
          client.orgInfo()
          client.orgSavedList()
          client.collabSync(projectId, undefined, projectViewGenerationRef.current)
        }
      },
      onCollabMessage: (type, payload) => {
        try {
          if (type === 'project_index_push') {
            if (!payloadMatchesCurrentSwitch(payload)) return
          } else if (!payloadMatchesActiveProject(payload, false)) return
          const cs = chatStoreRef.current
          if (type === 'chat_new_message') {
            if (cs) cs.addMessageFromBackend(mapBackendMessage(payload))
          } else if (type === 'chat_channel_created') {
            if (cs) cs.addChannelFromBackend(mapBackendChannel(payload))
          } else if (type === 'session_runtime_control') {
            const ss = sessionStoreRef.current
            if (ss) {
              const taskIds = Array.isArray(payload.task_ids)
                ? payload.task_ids.map(String).filter(Boolean)
                : []
              const patch: Partial<import('./types/kanban').Session> = {
                runtimeControlState: String(payload.runtime_control_state ?? payload.runtimeControlState ?? 'idle') as any,
                canStop: Boolean(payload.can_stop ?? payload.canStop),
                canResume: Boolean(payload.can_resume ?? payload.canResume),
                resumeParentSessionId: String(payload.resume_parent_session_id ?? payload.resumeParentSessionId ?? ''),
                pendingRuntimeCheckpointId: String(payload.pending_runtime_checkpoint_id ?? payload.pendingRuntimeCheckpointId ?? ''),
                stopIntentId: String(payload.stop_intent_id ?? payload.stopIntentId ?? ''),
              }
              for (const taskId of taskIds) {
                ss.updateSession(taskId, patch)
              }
            }
          } else if (type === 'board_task_status_changed') {
            const taskId = String(payload.task_id ?? '')
            const columnId = String(payload.column_id ?? '')
            const statusStr = String(payload.status ?? '')
            const isTerminal = ['done', 'failed', 'cancelled'].includes(statusStr)
            if (taskId && columnId) {
              const bs = boardStoreRef.current
              if (bs) {
                const task = bs.tasks.find(t => t.id === taskId)
                if (task) {
                  const partial: Partial<import('./types/kanban').KanbanTask> = {}
                  if (task.columnId !== columnId) {
                    bs.moveTask(taskId, columnId, 0)
                  }
                  if (statusStr === 'running') {
                    if (!task.agentStatus || task.agentStatus === 'idle') {
                      partial.agentStatus = 'reflecting'
                    }
                  } else if (task.agentStatus || task.currentTool || task.displayTool) {
                    partial.agentStatus = undefined
                    partial.currentTool = undefined
                    partial.displayTool = undefined
                  }
                  if (Object.keys(partial).length > 0) {
                    bs.updateTask(taskId, partial)
                  }
                }
              }
              const ss = sessionStoreRef.current
              if (ss) {
                const session = ss.sessions.find(s => s.taskId === taskId)
                const sessionPatch: Partial<import('./types/kanban').Session> = {
                  columnId,
                  status: statusStr || columnId,
                }
                if (statusStr === 'running') {
                  if (!session?.agentStatus || session.agentStatus === 'idle') {
                    sessionPatch.agentStatus = 'reflecting'
                  }
                } else if (session?.agentStatus || session?.currentTool || session?.displayTool || isTerminal) {
                  sessionPatch.agentStatus = undefined
                  sessionPatch.currentTool = undefined
                  sessionPatch.displayTool = undefined
                }
                Object.assign(
                  sessionPatch,
                  companyRuntimeControlPatchForBoardStatus(session, statusStr),
                )
                ss.updateSession(taskId, {
                  ...sessionPatch,
                })
              }
            }
          } else if (type === 'execution_mode_resolved') {
            const mode = String(payload.mode ?? '')
            const profile = String(payload.profile ?? '')
            // Update UI mode selector to reflect what engine actually decided (#13)
            if (mode) {
              const normalizedMode = normalizeExecMode(mode)
              setGlobalExecMode(normalizedMode)
              setGlobalCompanyProfile(
                normalizedMode === 'org'
                  ? 'custom'
                  : normalizedMode === 'company'
                    ? normalizeCompanyProfile(profile)
                    : 'corporate',
              )
            } else if (profile) {
              setGlobalCompanyProfile(normalizeCompanyProfile(profile))
            }
          } else if (type === 'project_run_updated' || type === 'seat_digest_updated' || type === 'work_item_batch_updated') {
            clientRef.current?.collabSync(getActiveProjectId(), undefined, projectViewGenerationRef.current)
          } else if (type === 'collab_sync_push' || type === 'project_index_push') {
            const syncScope = String((payload as Record<string, unknown>).sync_scope ?? (payload as Record<string, unknown>).syncScope ?? '').toLowerCase()
            const isProjectIndexPush = type === 'project_index_push' || syncScope === 'index'
            if (!payloadMatchesCurrentSwitch(payload)) return
            const syncData = mapCollabSyncPayload(payload)
            const syncProjectId = projectIdFromPayload(payload)
            if (!syncProjectId) return
            const applyingProjectSwitch = pendingProjectSwitchRef.current === syncProjectId || getActiveProjectId() !== syncProjectId
            activeProjectIdRef.current = syncProjectId
            pendingProjectSwitchRef.current = null
            projectStoreRef.current?.setActiveProject(syncProjectId)
            if (applyingProjectSwitch) {
              clearPendingSessionDetailRefreshes()
              setExecutionPanelTaskId(null)
              setExecutionPanelRuntimeOnly(false)
              setCommsState(null)
              setCommsMessage(null)
            }
            setStatusDetail('')
            if (isProjectIndexPush) {
              const ss2 = sessionStoreRef.current
              if (ss2) {
                ss2.initFromBackend(syncProjectId, syncData.sessions, {
                  preserveExistingWhenIncomingPartial: true,
                  preserveActiveWhenMissing: true,
                })
              }
              clientRef.current?.collabSync(syncProjectId, undefined, projectViewGenerationRef.current)
              return
            }
            const cs2 = chatStoreRef.current
            if (cs2) {
              cs2.initFromBackend(syncProjectId, syncData.channels, syncData.messages)
            }
            const bs2 = boardStoreRef.current
            if (bs2) {
              bs2.initFromBackend(syncProjectId, syncData.boards, syncData.columns, syncData.tasks)
            }
            const ss2 = sessionStoreRef.current
            if (ss2) {
              ss2.initFromBackend(syncProjectId, syncData.sessions)
              // Re-sync active session detail after full state push
              const activeId = ss2.activeSessionId
              if (activeId) {
                scheduleSessionDetailRefresh(activeId, 'full', true)
              }
            }
          }
        } catch (e) { console.error('[onCollabMessage] Error:', e) }
      },
      onAgentRuntimeUpdate: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        if (payload.agent_id) {
          setSwarmAgents((prev) => {
            const next = prev.map((agent) => (
              agent.agent_id === payload.agent_id
                ? {
                    ...agent,
                    status: payload.status,
                    runtime_status: payload.status,
                    current_tool: payload.current_tool ?? undefined,
                    current_task_id: payload.task_id ?? undefined,
                  }
                : agent
            ))
            swarmAgentsRef.current = next
            return next
          })
        }
        const bs = boardStoreRef.current
        if (!bs) return
        // Only update the specific task the agent is working on (#2)
        if (payload.task_id) {
          const rawPayload = payload as unknown as Record<string, unknown>
          const boardRuntimePatch = kanbanRuntimePatchFromPayload(rawPayload)
          const sessionRuntimePatch = sessionRuntimePatchFromPayload(rawPayload)
          if (hasOwnPayloadField(rawPayload, 'current_tool')) {
            const currentTool = typeof rawPayload.current_tool === 'string' && rawPayload.current_tool.trim()
              ? rawPayload.current_tool
              : undefined
            boardRuntimePatch.currentTool = currentTool
            sessionRuntimePatch.currentTool = currentTool
            if (currentTool) {
              boardRuntimePatch.displayTool = currentTool
              sessionRuntimePatch.displayTool = currentTool
            }
          }
          if (hasOwnPayloadField(rawPayload, 'display_tool')) {
            const displayTool = typeof rawPayload.display_tool === 'string' && rawPayload.display_tool.trim()
              ? rawPayload.display_tool
              : undefined
            if (displayTool) {
              boardRuntimePatch.displayTool = displayTool
              sessionRuntimePatch.displayTool = displayTool
            }
            // An empty display_tool arriving between tools keeps the sticky last
            // command; only the terminal-status clear below resets it. This is
            // what stops the header tool-pill from flickering once per tool call.
          }
          if (runtimeStatusClearsDisplayTool(payload.status)) {
            boardRuntimePatch.displayTool = undefined
            sessionRuntimePatch.displayTool = undefined
          }
          if (hasOwnPayloadField(rawPayload, 'tool_elapsed_ms')) {
            const toolElapsedMs = typeof rawPayload.tool_elapsed_ms === 'number' ? rawPayload.tool_elapsed_ms : undefined
            boardRuntimePatch.toolElapsedMs = toolElapsedMs
            sessionRuntimePatch.toolElapsedMs = toolElapsedMs
          }
          if (hasOwnPayloadField(rawPayload, 'last_tool_summary')) {
            const lastToolSummary = typeof rawPayload.last_tool_summary === 'string' ? rawPayload.last_tool_summary : undefined
            boardRuntimePatch.lastToolSummary = lastToolSummary
            sessionRuntimePatch.lastToolSummary = lastToolSummary
          }
          if (typeof payload.iteration === 'number') boardRuntimePatch.iterationCount = payload.iteration
          bs.updateTask(payload.task_id, {
            agentStatus: payload.status,
            ...boardRuntimePatch,
          })
          // Also update session sidebar status
          const ss = sessionStoreRef.current
          if (ss) {
            ss.updateSession(payload.task_id, {
              agentStatus: payload.status,
              ...sessionRuntimePatch,
              updatedAt: Date.now(),
            })
          }
          scheduleSessionDetailRefresh(payload.task_id)
        }
      },
      onRuntimeStatusSync: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const ss = sessionStoreRef.current
        const bs = boardStoreRef.current
        if (!ss) return
        // Periodic reconciliation against the backend's authoritative status.
        // Diff before dispatching: a tick where nothing drifted must not
        // trigger a single store update (and therefore no re-render).
        for (const entry of payload.sessions ?? []) {
          const taskId = String(entry.task_id ?? '').trim()
          if (!taskId) continue
          const session = ss.sessions.find((s) => s.taskId === taskId)
          if (!session) continue
          const status = String(entry.status ?? '').trim()
          const patch: Partial<import('./types/kanban').Session> = {}
          if (status && status !== session.status) patch.status = status
          const rawAgentStatus = typeof entry.agent_status === 'string' ? entry.agent_status.trim() : ''
          if (rawAgentStatus === 'idle' || rawAgentStatus === 'reflecting' || rawAgentStatus === 'tool_active') {
            if (rawAgentStatus !== session.agentStatus) patch.agentStatus = rawAgentStatus
            const tool = typeof entry.current_tool === 'string' && entry.current_tool.trim()
              ? entry.current_tool
              : undefined
            if (tool !== session.currentTool) patch.currentTool = tool
            if (runtimeStatusClearsDisplayTool(rawAgentStatus) && session.displayTool !== undefined) {
              patch.displayTool = undefined
            }
          } else {
            // No live tracker for this task: only clear stale indicators when
            // the backend says the task is no longer running (mirrors the
            // mergeLiveRuntimeField semantics used by collab_sync).
            const controlActive = session.runtimeControlState === 'running'
              || session.runtimeControlState === 'suspending'
              || session.runtimeControlState === 'resuming'
            if (status && status !== 'running' && !controlActive) {
              if (session.agentStatus !== undefined) patch.agentStatus = undefined
              if (session.currentTool !== undefined) patch.currentTool = undefined
              if (session.displayTool !== undefined) patch.displayTool = undefined
            }
          }
          if (Object.keys(patch).length === 0) continue
          ss.updateSession(taskId, patch)
          if (bs && ('agentStatus' in patch || 'currentTool' in patch || 'displayTool' in patch)) {
            const boardPatch: Partial<KanbanTask> = {}
            if ('agentStatus' in patch) boardPatch.agentStatus = patch.agentStatus as KanbanTask['agentStatus']
            if ('currentTool' in patch) boardPatch.currentTool = patch.currentTool
            if ('displayTool' in patch) boardPatch.displayTool = patch.displayTool
            bs.updateTask(taskId, boardPatch)
          }
        }
      },
      onWorkerNotification: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const data = payload as Record<string, unknown>
        const taskId = typeof payload.task_id === 'string' ? payload.task_id : ''
        if (!taskId) return
        const notification = data as import('./types/kanban').WorkerNotification
        sessionStoreRef.current?.updateSession(taskId, {
          ...sessionRuntimePatchFromPayload(data),
          latestNotification: notification,
          updatedAt: Date.now(),
        })
        boardStoreRef.current?.updateTask(taskId, {
          ...kanbanRuntimePatchFromPayload(data),
          latestNotification: notification,
        })
        scheduleSessionDetailRefresh(taskId)
      },
      onKanbanViewData: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const bs = boardStoreRef.current
        if (!bs) return
        bs.initFromBackend(
          projectIdFromPayload(payload as unknown as Record<string, unknown>) || getActiveProjectId(),
          (payload.boards ?? []).map(mapBackendBoard),
          (payload.columns ?? []).map(mapBackendColumn),
          (payload.tasks ?? []).map(mapBackendTask),
        )
      },
      onSessionProgress: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const ss = sessionStoreRef.current
        if (!ss || !payload.task_id || !payload.entry) return
        ss.appendProgress(payload.task_id, {
          type: payload.entry.type as any,
          summary: payload.entry.summary,
          detail: payload.entry.detail,
          timestamp: payload.entry.timestamp * 1000,
          turnId: typeof payload.entry.turn_id === 'string'
            ? payload.entry.turn_id
            : typeof payload.entry.turnId === 'string'
              ? payload.entry.turnId
              : undefined,
          itemId: typeof payload.entry.item_id === 'string'
            ? payload.entry.item_id
            : typeof payload.entry.itemId === 'string'
              ? payload.entry.itemId
              : undefined,
          streamId: typeof payload.entry.stream_id === 'string'
            ? payload.entry.stream_id
            : typeof payload.entry.streamId === 'string'
              ? payload.entry.streamId
              : undefined,
          toolCallId: typeof payload.entry.tool_call_id === 'string'
            ? payload.entry.tool_call_id
            : typeof payload.entry.toolCallId === 'string'
              ? payload.entry.toolCallId
              : undefined,
          permissionGroupKey: typeof payload.entry.permission_group_key === 'string'
            ? payload.entry.permission_group_key
            : typeof payload.entry.permissionGroupKey === 'string'
              ? payload.entry.permissionGroupKey
              : undefined,
          seq: typeof payload.entry.seq === 'number' ? payload.entry.seq : undefined,
          executionMode: typeof payload.entry.execution_mode === 'string'
            ? payload.entry.execution_mode
            : typeof payload.entry.executionMode === 'string'
              ? payload.entry.executionMode
              : undefined,
        })
        if (payload.entry.type === 'tool_call') {
          const toolLabel = String(payload.entry.summary ?? '').trim()
          ss.updateSession(payload.task_id, {
            ...(toolLabel ? { currentTool: toolLabel, displayTool: toolLabel } : {}),
            updatedAt: payload.entry.timestamp * 1000,
          })
        }
        if (payload.entry.type !== 'thinking' && payload.entry.type !== 'verification') {
          scheduleSessionDetailRefresh(payload.task_id)
        }
      },
      onBoardEvent: (payload) => {
        if (!payloadMatchesActiveProject(payload, false)) return
        const bs = boardStoreRef.current
        if (!bs) return
        const taskId = String(payload.task_id ?? '')
        if (!taskId) return
        const ss = sessionStoreRef.current
        if (ss) {
          const session = ss.sessions.find(s => s.taskId === taskId)
          if (session?.mode === 'child') return
        }
        const assigneeIds = Array.isArray(payload.assignee_ids) ? payload.assignee_ids.map(String) : []
        const workItemIdentity = workItemIdentityPatchFromPayload(payload)
        const existing = bs.tasks.find(t => t.id === taskId)
        if (existing) {
          const partial: Partial<import('./types/kanban').KanbanTask> = {}
          if (payload.title) partial.title = String(payload.title)
          if (payload.display_id) partial.displayId = String(payload.display_id)
          if (assigneeIds.length > 0) partial.assigneeIds = assigneeIds
          Object.assign(partial, workItemIdentity)
          if (Object.keys(partial).length > 0) {
            bs.updateTask(taskId, partial)
          }
          return
        }
        const boardId = String(payload.board_id ?? bs.activeBoardId ?? 'default')
        // Find the real Todo column ID for this board
        const boardCols = bs.columns.filter(c => c.boardId === boardId)
        const todoCol = boardCols.find(c => c.name === 'Todo')
        const columnId = todoCol?.id ?? boardCols[0]?.id ?? ''
        if (!columnId) return // no columns exist yet — skip
        bs.createTask({
          boardId,
          columnId,
          title: String(payload.title ?? 'Untitled'),
          taskId,
          displayId: payload.display_id ? String(payload.display_id) : undefined,
          assigneeIds,
        })
        if (Object.keys(workItemIdentity).length > 0) {
          bs.updateTask(taskId, workItemIdentity)
        }
      },
      onSessionCreated: (payload) => {
        try {
          if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
          const ss = sessionStoreRef.current
          if (!ss) return
          const taskId = String(payload.task_id ?? '')
          if (!taskId) return
          const eventProjectId = projectIdFromPayload(payload as unknown as Record<string, unknown>)
          if (!eventProjectId) return
          const existing = taskId ? ss.sessions.find(s => s.taskId === taskId) : undefined
          const workItemIdentity = workItemIdentityPatchFromPayload(payload)
          const normalizedSessionExecMode = normalizeExecMode(payload.exec_mode ?? existing?.execMode)
          const payloadCompanyProfile = companyProfileForExecMode(
            normalizedSessionExecMode,
            payload.company_profile ?? existing?.companyProfile,
          )
          const payloadOrgId = orgIdForExecMode(
            normalizedSessionExecMode,
            payload.org_id ?? payload.organization_id ?? existing?.orgId,
          )
          if (normalizedSessionExecMode === 'org' && payloadOrgId) setActiveSavedOrg(payloadOrgId)
          ss.createSession({
            projectId: eventProjectId,
            taskId,
            channelId: payload.channel_id,
            sessionId: payload.session_id,
            parentSessionId: payload.parent_session_id,
            originTaskId: payload.origin_task_id ?? existing?.originTaskId ?? taskId,
            mode: payload.parent_session_id ? 'child' : 'primary',
            execMode: normalizedSessionExecMode,
            companyProfile: payloadCompanyProfile,
            orgId: payloadOrgId,
            preferredAgent: normalizeTaskPreferredAgent(payload.preferred_agent ?? existing?.preferredAgent),
            nativeApprovalLevel: payload.native_approval_level ?? existing?.nativeApprovalLevel ?? globalNativeApprovalDefaultRef.current,
            nativePermissionScopeId: payload.native_permission_scope_id ?? existing?.nativePermissionScopeId,
            title: payload.title,
            status: payload.status,
            columnId: existing?.columnId ?? 'todo',
            assigneeIds: Array.isArray(payload.assignee_ids)
              ? payload.assignee_ids.map(String)
              : (existing?.assigneeIds ?? []),
            priority: existing?.priority ?? null,
            tags: existing?.tags ?? [],
            progressLog: existing?.progressLog ?? [],
            createdAt: typeof payload.created_at === 'number'
              ? payload.created_at * 1000
              : (existing?.createdAt ?? Date.now()),
            updatedAt: Date.now(),
            messageCount: 0,
            ...workItemIdentity,
          })
          // Ensure a kanban card exists for this session (1 session = 1 card)
          const bs = boardStoreRef.current
          const execMode = normalizedSessionExecMode
          if (bs && execMode === 'task' && !payload.parent_session_id && taskId && !bs.tasks.find(t => t.id === taskId) && bs.activeBoardId) {
            const boardCols = bs.columns.filter(c => c.boardId === bs.activeBoardId)
            const todoCol = boardCols.find(c => c.name === 'Todo')
            if (todoCol) {
              bs.createTask({
                boardId: bs.activeBoardId,
                columnId: todoCol.id,
                title: payload.title,
                taskId,
                assigneeIds: existing?.assigneeIds ?? [],
              })
            }
          }
          // Only auto-select if the user explicitly created this session.
          if (pendingSessionCreateRef.current) {
            if (pendingSessionCreateProjectIdRef.current !== eventProjectId) return
            clearPendingSessionCreate()
            ss.setActiveSession(payload.task_id)
            // Force a detail refresh so content loads immediately for the new active session
            scheduleSessionDetailRefresh(payload.task_id, 'full', true)
          }
        } catch (e) { console.error('[onSessionCreated] Error:', e) }
      },
      onSessionUpdated: (payload) => {
        try {
          if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
          const ss = sessionStoreRef.current
          if (!ss || !payload.task_id) return
          const nextExecMode = normalizeExecMode(payload.exec_mode ?? ss.sessions.find(s => s.taskId === payload.task_id)?.execMode)
          ss.updateSession(payload.task_id, {
            ...(payload.exec_mode ? { execMode: nextExecMode } : {}),
            ...(payload.exec_mode || payload.company_profile ? { companyProfile: companyProfileForExecMode(nextExecMode, payload.company_profile) } : {}),
            ...('org_id' in payload || 'organization_id' in payload ? { orgId: orgIdForExecMode(nextExecMode, payload.org_id ?? payload.organization_id) } : {}),
            ...(payload.preferred_agent ? { preferredAgent: normalizeTaskPreferredAgent(payload.preferred_agent) } : {}),
            ...(payload.selected_execution_agent ? { selectedExecutionAgent: normalizeTaskPreferredAgent(payload.selected_execution_agent) } : {}),
            ...(payload.native_approval_level ? { nativeApprovalLevel: payload.native_approval_level } : {}),
            ...(payload.native_permission_scope_id ? { nativePermissionScopeId: payload.native_permission_scope_id } : {}),
          })
          if ((payload.exec_mode === 'org' || payload.exec_mode === 'custom') && (payload.org_id || payload.organization_id)) {
            setActiveSavedOrg(String(payload.org_id ?? payload.organization_id))
          }
        } catch (e) { console.error('[onSessionUpdated] Error:', e) }
      },
      onNativePermissionDefaultUpdated: (payload) => {
        if (payload.native_approval_default) {
          globalNativeApprovalDefaultRef.current = payload.native_approval_default
          setGlobalNativeApprovalDefault(payload.native_approval_default)
        }
      },
      onSessionMessage: (payload) => {
        try {
          if (!payloadMatchesActiveProject(payload, false)) return
          const cs = chatStoreRef.current
          if (!cs) return
          const mapped = mapBackendMessage(payload)
          console.debug('[onSessionMessage]', mapped.sender, mapped.channelId, mapped.content?.slice(0, 60))
          cs.addMessageFromBackend(mapped)
          const taskId = mapped.channelId.startsWith('session:') ? mapped.channelId.slice('session:'.length) : ''
          const terminalTurnId = terminalAssistantTurnId(mapped)
          const activeDraftTurnId = String(
            sessionStoreRef.current?.sessions.find(session => session.taskId === taskId)?.draftTurnId ?? '',
          ).trim()
          if (taskId && mapped.sender !== 'user') {
            if (terminalTurnId && terminalTurnId === activeDraftTurnId) {
              sessionStoreRef.current?.clearDraft(taskId)
            }
            // Force refresh — session messages are critical content that must sync
            scheduleSessionDetailRefresh(taskId, 'full', true)
          }
        } catch (e) { console.error('[onSessionMessage] Error:', e) }
      },
      onSessionTitleUpdated: (payload) => {
        try {
          if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
          const ss = sessionStoreRef.current
          if (ss) ss.updateSession(payload.task_id, { title: payload.title })
          // Also update the matching kanban board's name so the title shown
          // on top of the kanban stays in sync with the sidebar.  In company
          // mode the board id equals the primary session's task_id (or
          // origin_task_id for child sessions).
          const bs = boardStoreRef.current
          if (bs) {
            const session = ss?.sessions.find(s => s.taskId === payload.task_id)
            const boardId = session?.originTaskId ?? payload.task_id
            const board = bs.boards.find(b => b.id === boardId)
            if (board && board.name !== payload.title) {
              bs.updateBoardName(boardId, payload.title)
            }
          }
        } catch (e) { console.error('[onSessionTitleUpdated] Error:', e) }
      },
      onSessionDeleted: (payload) => {
        try {
          if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
          const ss = sessionStoreRef.current
          if (!ss) return
          if (ss.activeSessionId === payload.task_id) {
            ss.setActiveSession(null)
          }
          ss.deleteSession(payload.task_id)
          // Remove kanban card entirely — delete means delete
          const bs = boardStoreRef.current
          if (bs) {
            bs.deleteTask(payload.task_id)
          }
          // Clean up chat messages + channel for this session
          const cs = chatStoreRef.current
          if (cs) {
            cs.removeSessionData(payload.task_id)
          }
        } catch (e) { console.error('[onSessionDeleted] Error:', e) }
      },
      onProjectSwitched: (payload) => {
        if (!payloadMatchesCurrentSwitch(payload as unknown as Record<string, unknown>)) return
        const projectId = typeof payload.project_id === 'string' ? payload.project_id.trim() : ''
        if (!projectId) return
        pendingProjectSwitchRef.current = projectId
        setStatusDetail(`Switching to ${projectId}...`)
        // project_index_push seeds the project view; collab_sync hydrates full runtime state.
      },
      onProjectDeleted: (payload) => {
        const ps = projectStoreRef.current
        if (ps) {
          ps.removeProject(payload.project_id)
        }
        // If backend switched to default, collab_sync_push + project_switched will follow
      },
      onOrgInfo: (payload) => {
        const normalized = normalizeOrgInfoPayload(payload)
        setOrgInfoData(normalized)
        // Capture versionAtLoad: the first org_version we see after a
        // saved-org Load/SaveAs becomes the dirty-detection baseline.
        setSavedOrgVersionAtLoad(prev =>
          prev === null ? (normalized?.org_version ?? 0) : prev,
        )
        if (payload?.project_run?.execution_model === 'multi_team_org' || (Array.isArray(payload?.work_items) && payload.work_items.length > 0)) {
          clientRef.current?.collabSync(getActiveProjectId(), undefined, projectViewGenerationRef.current)
        }
      },
      onCommsState: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        setCommsState(payload)
      },
      onCommsMessage: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, true)) return
        setCommsMessage(payload)
      },
      onTalentList: (payload) => {
        setTalentTemplates(payload.templates ?? [])
        if (payload.talent_dir) setDefaultTalentDir(payload.talent_dir)
      },
      onEmployeeDetail: (payload) => {
        setEmployeeDetail(payload)
      },
      onReorgList: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        setReorgProposals(payload.proposals ?? [])
      },
      onOrgConfigExport: (payload) => {
        setConfigExportYaml(payload.yaml ?? '')
      },
      onOrgConfigImport: (payload) => {
        // Fires only on manual YAML import via ConfigImportExportPanel.
        // Saved-org Load uses the dedicated onOrgSavedLoad handler, which
        // calls _apply_org_config directly on the backend — no spurious
        // "Dry run OK" banner side-effect.
        if (payload.ok) {
          setConfigImportPreview(payload.preview ?? null)
          setConfigImportError(null)
          if (!payload.dry_run) {
            setActiveSavedOrg(null)
            setSavedOrgVersionAtLoad(null)
          }
        } else {
          setConfigImportError(payload.error ?? 'Import failed')
          setConfigImportPreview(null)
        }
      },
      onOrgSavedList: (payload) => {
        setSavedOrgsList(payload.orgs ?? [])
        if ('active_name' in payload) {
          setActiveSavedOrg(payload.active_name ?? null)
        }
      },
      onOrgSavedSaveAs: (payload) => {
        if (payload.ok) {
          clientRef.current?.orgSavedList()
          setActiveSavedOrg(payload.name)
          // Overwriting (or freshly naming) the active org resets the
          // dirty baseline — the next onOrgInfo captures versionAtLoad.
          setSavedOrgVersionAtLoad(null)
          setOrgToast({ kind: 'ok', text: `Saved "${payload.name}"` })
        } else {
          setOrgToast({ kind: 'error', text: `Save failed: ${payload.error ?? 'unknown'}` })
        }
      },
      onOrgSavedCreate: (payload) => {
        setOrgCreatePending(false)
        setOrgCreateResult({ ...payload, nonce: Date.now() })
        if (payload.ok) {
          const orgId = payload.organization_id || payload.name
          setActiveSavedOrg(orgId)
          setGlobalExecMode('org')
          setGlobalCompanyProfile('custom')
          clientRef.current?.orgSavedList()
          clientRef.current?.orgInfo()
          setSavedOrgVersionAtLoad(null)
          setOrgToast({ kind: 'ok', text: `Created "${payload.organization_name || orgId}"` })
        } else {
          setOrgToast({ kind: 'error', text: `Create failed: ${payload.error ?? 'unknown'}` })
        }
      },
      onOrgSavedLoad: (payload) => {
        if (payload.ok) {
          setActiveSavedOrg(payload.name)
          setGlobalExecMode('org')
          setGlobalCompanyProfile('custom')
          clientRef.current?.orgSavedList()
          // versionAtLoad is captured by the next onOrgInfo — see that handler.
          setSavedOrgVersionAtLoad(null)
          setOrgToast({ kind: 'ok', text: `Loaded "${payload.name}"` })
        } else {
          setOrgToast({ kind: 'error', text: `Load failed: ${payload.error ?? 'unknown'}` })
        }
      },
      onOrgSavedDelete: (payload) => {
        if (payload.ok) {
          clientRef.current?.orgSavedList()
          // Clear the active indicator when the deleted name matches.
          // (Closure over state is stale, so use functional update.)
          setActiveSavedOrg(prev => {
            if (prev === payload.name) {
              setSavedOrgVersionAtLoad(null)
              return null
            }
            return prev
          })
          setOrgToast({ kind: 'ok', text: `Deleted "${payload.name}"` })
        } else {
          setOrgToast({ kind: 'error', text: `Delete failed: ${payload.error ?? 'unknown'}` })
        }
      },
      onMarketBrowse: (payload) => {
        setMarketPresets((payload as any).presets ?? [])
      },
      onMarketPreview: (payload) => {
        setMarketPreviewData(payload as any)
      },
      onChildSessionCreated: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const ss = sessionStoreRef.current
        if (!ss) return
        const workItemIdentity = workItemIdentityPatchFromPayload(payload as Record<string, unknown>)
        const childProjectId = projectIdFromPayload(payload as unknown as Record<string, unknown>)
        if (!childProjectId) return
        // child_session_created adds a child session to the sidebar
        // The backend also broadcasts session_created which handles kanban card creation
        ss.createSession({
          projectId: childProjectId,
          taskId: payload.task_id,
          channelId: (payload as any).channel_id ?? `session:${payload.task_id}`,
          sessionId: payload.session_id,
          parentSessionId: payload.parent_session_id,
          originTaskId: payload.origin_task_id ?? payload.task_id,
          mode: 'child',
          orgId: String((payload as any).org_id ?? (payload as any).organization_id ?? '').trim() || undefined,
          title: payload.title,
          status: 'pending',
          columnId: 'todo',
          assigneeIds: payload.agent_id ? [payload.agent_id] : [],
          priority: null,
          tags: [],
          createdAt: Date.now(),
          updatedAt: Date.now(),
          messageCount: 0,
          progressLog: [],
          ...workItemIdentity,
        })
        // Mark parent session as company runtime (Company mode child sessions)
        if (payload.parent_session_id) {
          const parent = ss.sessions.find(s =>
            s.sessionId === payload.parent_session_id || s.taskId === payload.parent_session_id,
          )
          if (parent && !parent.isCompanyRuntime) {
            ss.setCompanyRuntime(parent.taskId, true)
          }
        }
      },
      onWorkItemProgress: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const ss = sessionStoreRef.current
        if (!ss || !payload.task_id || !payload.entry) return
        const entryProjectionId = typeof payload.entry.work_item_projection_id === 'string'
          ? payload.entry.work_item_projection_id
          : ''
        const projectionId = entryProjectionId && entryProjectionId !== 'company_runtime'
          ? entryProjectionId
          : undefined
        const runtimeTaskId = typeof payload.entry.runtime_task_id === 'string' && payload.entry.runtime_task_id
          ? payload.entry.runtime_task_id
          : typeof payload.runtime_task_id === 'string' && payload.runtime_task_id
            ? payload.runtime_task_id
            : typeof payload.entry.execution_turn_id === 'string' && payload.entry.execution_turn_id
              ? payload.entry.execution_turn_id
              : typeof payload.execution_turn_id === 'string' && payload.execution_turn_id
                ? payload.execution_turn_id
                : undefined
        const executionTurnId = typeof payload.entry.execution_turn_id === 'string' && payload.entry.execution_turn_id
          ? payload.entry.execution_turn_id
          : typeof payload.execution_turn_id === 'string' && payload.execution_turn_id
            ? payload.execution_turn_id
            : runtimeTaskId
        ss.updateSession(payload.task_id, {
          isCompanyRuntime: true,
          ...(projectionId ? { workItemProjectionId: projectionId } : {}),
        })
        const bs = boardStoreRef.current
        if (bs && projectionId) {
          bs.updateTask(payload.task_id, { workItemProjectionId: projectionId })
        }
        ss.appendWorkItemProgress(payload.task_id, {
          timestamp: payload.entry.timestamp * 1000,
          type: payload.entry.type as any,
          workItemProjectionId: projectionId,
          workItemTurnType: typeof payload.entry.work_item_turn_type === 'string' ? payload.entry.work_item_turn_type : undefined,
          workItemProjectionTitle: typeof payload.entry.work_item_projection_title === 'string' ? payload.entry.work_item_projection_title : undefined,
          runtimeTaskId,
          executionTurnId,
          roleName: payload.entry.role_name,
          detail: payload.entry.detail,
        })
      },
      onExternalTeamActivityEvent: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        applyExternalTeamEvent(payload)
      },
      onExternalTeamActivitySnapshot: (payload) => {
        if (!payloadMatchesActiveProject(payload as unknown as Record<string, unknown>, false)) return
        const generation = payloadViewGeneration(payload as unknown as Record<string, unknown>)
        if (generation !== null && generation !== projectViewGenerationRef.current) return
        applyExternalTeamSnapshot(payload)
      },
    })
    clientRef.current = client
    client.connect()
    return () => {
      client.disconnect()
      for (const tid of timersRef.current) clearTimeout(tid)
      timersRef.current.clear()
      for (const tid of pendingSessionDetailRefreshRef.current.values()) clearTimeout(tid)
      pendingSessionDetailRefreshRef.current.clear()
      if (deltaFlushTimerRef.current !== null) {
        window.clearTimeout(deltaFlushTimerRef.current)
        deltaFlushTimerRef.current = null
      }
      pendingDeltaFlushRef.current.clear()
    }
  }, [wsUrl])

  useEffect(() => {
    const refreshProjectState = () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      const now = Date.now()
      if (now - lastProjectIndexRefreshRef.current < 1_500) return
      lastProjectIndexRefreshRef.current = now
      clientRef.current?.collabSync(getActiveProjectId(), undefined, projectViewGenerationRef.current)
    }
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') refreshProjectState()
    }
    window.addEventListener('focus', refreshProjectState)
    document.addEventListener('visibilitychange', handleVisibilityChange)
    return () => {
      window.removeEventListener('focus', refreshProjectState)
      document.removeEventListener('visibilitychange', handleVisibilityChange)
    }
  }, [getActiveProjectId])

  // Auto-clear the org-toast after 3s.
  useEffect(() => {
    if (!orgToast) return
    const t = setTimeout(() => setOrgToast(null), 3000)
    return () => clearTimeout(t)
  }, [orgToast])

  // Stable client-call refs for OrgTab → TeamView → OrgVersionSwitcher.
  // useCallback so downstream effects don't churn on every App re-render.
  const handleSavedOrgsList = useCallback(() => {
    clientRef.current?.orgSavedList()
  }, [])
  const handleSavedOrgSaveAs = useCallback((name: string, overwrite: boolean) => {
    clientRef.current?.orgSavedSaveAs(name, overwrite)
  }, [])
  const handleSavedOrgCreate = useCallback((organizationName: string, members: OrgCreateMemberInput[]) => {
    setOrgCreatePending(true)
    setOrgCreateResult(null)
    clientRef.current?.orgSavedCreate(organizationName, members)
  }, [])
  const handleSavedOrgLoad = useCallback((name: string) => {
    clientRef.current?.orgSavedLoad(name)
  }, [])
  const handleSavedOrgDelete = useCallback((name: string) => {
    clientRef.current?.orgSavedDelete(name)
  }, [])
  const handleSelectCorporateOrg = useCallback(() => {
    setGlobalExecMode('company')
    setGlobalCompanyProfile('corporate')
    setSavedOrgVersionAtLoad(null)
    clientRef.current?.setExecutionMode('company', 'corporate', globalTaskPreferredAgent)
    clientRef.current?.orgInfo()
  }, [globalTaskPreferredAgent])

  const handleOpenExecutionPanel = useCallback((taskId: string) => {
    if (!taskId) return
    setExecutionPanelRuntimeOnly(false)
    setExecutionPanelTaskId(taskId)
    const projectId = getActiveProjectId()
    const isTeamTarget = sessionStore.sessions.some(session => (
      ((session.taskId === taskId || getExecutionTurnId(session) === taskId) && isJiuwenOpaqueTeam(session))
      || Object.values(session.roleWorkItems ?? {}).some(role => role.workItems.some(item => item.executionTurnId === taskId && isJiuwenOpaqueTeam(item)))
    ))
    if (!isTeamTarget) return
    markExternalTeamLoading(projectId, taskId)
    clientRef.current?.externalTeamActivityGet(projectId, taskId, {
      limit: 100,
      viewGeneration: projectViewGenerationRef.current,
    })
  }, [getActiveProjectId, markExternalTeamLoading, sessionStore.sessions])

  const handleSelectTeamInvocation = useCallback((taskId: string, invocationId: string) => {
    const projectId = getActiveProjectId()
    markExternalTeamLoading(projectId, taskId, invocationId)
    clientRef.current?.externalTeamActivityGet(projectId, taskId, {
      externalInvocationId: invocationId,
      limit: 100,
      viewGeneration: projectViewGenerationRef.current,
    })
  }, [getActiveProjectId, markExternalTeamLoading])

  const handleLoadOlderTeamActivity = useCallback((taskId: string, record: ExternalTeamActivityRecord) => {
    const cursor = record.nextCursor
    if (!cursor) return
    clientRef.current?.externalTeamActivityGet(record.projectId, taskId, {
      externalInvocationId: record.externalInvocationId,
      beforeCreatedAt: cursor.beforeCreatedAt,
      beforeEventId: cursor.beforeEventId,
      limit: 100,
      viewGeneration: projectViewGenerationRef.current,
    })
  }, [])

  useEffect(() => {
    if (!lastTaskDoneAgent) return
    const agentId = lastTaskDoneAgent
    // Card movement is driven by board_task_status_changed events (Phase 1).
    // This effect only handles the celebration animation.
    bridgeRef.current.setAgentBubble(agentId, 'Done!')
    const tid = setTimeout(() => { timersRef.current.delete(tid); bridgeRef.current.setAgentBubble(agentId, null) }, 3000)
    timersRef.current.add(tid)
    setLastTaskDoneAgent(null)
  }, [lastTaskDoneAgent]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleTaskAssigned = useCallback(
    (taskId: string, agentIds: string[], taskTitle: string) => {
      const task = boardStore.tasks.find(t => t.id === taskId)
      for (const agentId of agentIds) {
        bridgeRef.current.sendToSeat(agentId)
        bridgeRef.current.setAgentActive(agentId, true)
        bridgeRef.current.setAgentBubble(agentId, `Task: ${taskTitle.slice(0, 22)}`)
        clientRef.current?.assignTaskToAgent(getActiveProjectId(), taskId, agentId, taskTitle)
      }
      if (task) {
        const names = agentIds.map(id => swarmAgents.find(a => a.agent_id === id)?.name ?? id)
        notifyTaskAssigned(chatStore, task, names)
      }
      bridgeRef.current.emit('officeChanged')
    },
    [boardStore.tasks, chatStore, swarmAgents, getActiveProjectId]
  )


  const metrics = useMemo(() => {
    const totalAgents = snapshot ? Object.keys(snapshot.agents).length : 0
    const totalSkills = snapshot?.skills.total ?? 0
    return { totalAgents, totalSkills }
  }, [snapshot])

  const officeMap = useMemo(() => {
    const result: Record<string, string> = {}
    for (const agent of swarmAgents) {
      if (agent.office_id) result[agent.agent_id] = agent.office_id
    }
    return result
  }, [swarmAgents])

  // Board initialization is driven entirely by collab_sync from backend (#7)
  // No local ensureDefaultBoards — avoids column ID mismatch with backend IDs

  const applyWsUrl = () => {
    const next = wsUrlInput.trim()
    if (!next || next === wsUrl) return
    setWsUrl(next)
  }

  const handleMoveOfficeAgent = useCallback((agentId: string, officeId: string) => {
    setSwarmAgents(previous => {
      const next = previous.map(agent => (
        agent.agent_id === agentId ? { ...agent, office_id: officeId } : agent
      ))
      swarmAgentsRef.current = next
      return next
    })
    clientRef.current?.moveAgent(agentId, officeId)
  }, [])

  const handleDeleteOfficeAgent = useCallback((agentId: string) => {
    setDeletingAgentId(agentId)
    clientRef.current?.deleteAgent(agentId)
  }, [])

  const handleSessionModeChange = useCallback((taskId: string, mode: string, profile?: string, orgId?: string) => {
    const existingSession = sessionStore.sessions.find(session => session.taskId === taskId)
    const normalizedMode = normalizeExecMode(mode)
    const currentProfile = normalizeCompanyProfile(existingSession?.companyProfile ?? globalCompanyProfile)
    const nextProfile = normalizedMode === 'org'
      ? 'custom'
      : normalizedMode === 'company'
        ? 'corporate'
        : 'corporate'
    const currentOrgId = String(existingSession?.orgId ?? activeSavedOrg ?? '').trim()
    const nextOrgId = orgIdForExecMode(normalizedMode, orgId ?? existingSession?.orgId ?? activeSavedOrg)
    const currentSessionMode = normalizeExecMode(existingSession?.execMode)
    const currentGlobalProfile = normalizeCompanyProfile(globalCompanyProfile)

    if (
      currentSessionMode === normalizedMode
      && currentProfile === nextProfile
      && currentOrgId === String(nextOrgId ?? '').trim()
      && globalExecMode === normalizedMode
      && currentGlobalProfile === nextProfile
    ) {
      return
    }

    sessionStore.updateSession(taskId, {
      execMode: normalizedMode,
      companyProfile: nextProfile,
      orgId: nextOrgId,
      preferredAgent: existingSession?.preferredAgent ?? globalTaskPreferredAgent,
    })
    setGlobalExecMode(normalizedMode)
    setGlobalCompanyProfile(nextProfile)
    if (normalizedMode === 'org' && nextOrgId) setActiveSavedOrg(nextOrgId)

    const nextPreferredAgent = existingSession?.preferredAgent ?? globalTaskPreferredAgent
    const runtimeProfile = normalizedMode === 'task' ? undefined : nextProfile
    clientRef.current?.sessionUpdateConfig(getActiveProjectId(), taskId, normalizedMode, runtimeProfile, nextPreferredAgent, nextOrgId)
    clientRef.current?.setExecutionMode(normalizedMode, runtimeProfile, nextPreferredAgent, nextOrgId)
  }, [sessionStore, globalExecMode, globalCompanyProfile, globalTaskPreferredAgent, activeSavedOrg, getActiveProjectId])

  const handleSessionTaskAgentChange = useCallback((taskId: string, preferredAgent: TaskPreferredAgent) => {
    const existingSession = sessionStore.sessions.find(session => session.taskId === taskId)
    const normalizedPreferredAgent = normalizeTaskPreferredAgent(preferredAgent)
    const normalizedMode = normalizeExecMode(existingSession?.execMode)
    const nextProfile = normalizedMode === 'org'
      ? 'custom'
      : normalizedMode === 'company'
        ? 'corporate'
        : 'corporate'

    sessionStore.updateSession(taskId, {
      preferredAgent: normalizedPreferredAgent,
      selectedExecutionAgent: normalizedPreferredAgent,
    })
    setGlobalTaskPreferredAgent(normalizedPreferredAgent)

    const runtimeProfile = normalizedMode === 'task' ? undefined : nextProfile
    const orgId = orgIdForExecMode(normalizedMode, existingSession?.orgId ?? activeSavedOrg)
    clientRef.current?.sessionUpdateConfig(getActiveProjectId(), taskId, normalizedMode, runtimeProfile, normalizedPreferredAgent, orgId)
    clientRef.current?.setExecutionMode(normalizedMode, runtimeProfile, normalizedPreferredAgent, orgId)
  }, [sessionStore, globalCompanyProfile, activeSavedOrg, getActiveProjectId])

  // Triggered from a locked-mode chip's "Continue in a new chat" popover.
  // We do NOT mutate the existing chat or its task — instead we spin up a
  // fresh session in the requested mode under the same project. The existing
  // pendingSessionCreateRef flow then auto-focuses the new chat as soon as
  // the server emits `session_created`.
  const handleContinueInNewChat = useCallback((
    mode: 'task' | 'company' | 'org' | 'custom',
    profile?: 'corporate' | 'custom',
    orgId?: string,
  ) => {
    if (pendingProjectSwitchRef.current || pendingSessionCreateRef.current) return
    const projectId = getActiveProjectId()
    beginPendingSessionCreate(projectId)

    const normalizedMode = normalizeExecMode(mode)
    // Resolve a sensible profile per mode so the backend gets the right
    // company configuration even if the chip didn't carry one.
    const resolvedProfile: 'corporate' | 'custom' | undefined =
      normalizedMode === 'org'
        ? 'custom'
        : normalizedMode === 'company'
          ? 'corporate'
          : undefined

    clientRef.current?.createSession(
      projectId,
      undefined,
      normalizedMode,
      resolvedProfile,
      globalTaskPreferredAgent,
      orgIdForExecMode(normalizedMode, orgId ?? activeSavedOrg),
      globalNativeApprovalDefault,
    )
    setActivePage('workspace')
  }, [getActiveProjectId, beginPendingSessionCreate, globalTaskPreferredAgent, activeSavedOrg, globalNativeApprovalDefault])

  const handleSessionNativeApprovalLevelChange = useCallback((
    taskId: string,
    level: NativeApprovalLevel,
  ) => {
    // Server confirmation is authoritative.  The session_updated event is
    // the only path that changes visible state, which also makes reconnects
    // and rejected updates deterministic.
    clientRef.current?.sessionUpdateNativePermission(getActiveProjectId(), taskId, level)
  }, [getActiveProjectId])

  const handleSetNativeApprovalDefault = useCallback((level: NativeApprovalLevel) => {
    clientRef.current?.setNativePermissionDefault(level)
  }, [])

  const markRuntimeControlForTask = useCallback((
    taskId: string,
    patch: Partial<import('./types/kanban').Session>,
  ) => {
    const session = sessionStore.sessions.find(s => s.taskId === taskId)
    const parentSessionId = session?.resumeParentSessionId ?? session?.parentSessionId ?? session?.sessionId
    for (const candidate of sessionStore.sessions) {
      if (
        candidate.taskId === taskId
        || (!!parentSessionId && (candidate.parentSessionId === parentSessionId || candidate.sessionId === parentSessionId))
      ) {
        sessionStore.updateSession(candidate.taskId, {
          ...patch,
          resumeParentSessionId: parentSessionId,
        })
      }
    }
  }, [sessionStore])

  const handleSessionStop = useCallback((taskId: string) => {
    const session = sessionStore.sessions.find(s => s.taskId === taskId)
    const isCompanyRuntime = session?.execMode === 'company'
      || session?.execMode === 'org'
      || session?.execMode === 'custom'
      || !!session?.isCompanyRuntime
      || !!session?.parentSessionId
      || !!session?.companyProfile
    if (isCompanyRuntime) {
      markRuntimeControlForTask(taskId, {
        runtimeControlState: 'suspending',
        canStop: false,
        canResume: false,
      })
    }
    clientRef.current?.sessionStop(getActiveProjectId(), taskId)
  }, [sessionStore.sessions, markRuntimeControlForTask, getActiveProjectId])

  const handleSessionResume = useCallback((taskId: string, runtimeSessionId?: string, checkpointId?: string) => {
    const session = sessionStore.sessions.find(s => s.taskId === taskId)
    const isCompanyRuntime = session?.execMode === 'company'
      || session?.execMode === 'org'
      || session?.execMode === 'custom'
      || !!session?.isCompanyRuntime
      || !!session?.parentSessionId
      || !!session?.companyProfile
    if (isCompanyRuntime) {
      markRuntimeControlForTask(taskId, {
        runtimeControlState: 'resuming',
        canStop: false,
        canResume: false,
      })
    }
    clientRef.current?.sessionResume(
      getActiveProjectId(),
      taskId,
      runtimeSessionId ?? session?.resumeParentSessionId ?? session?.parentSessionId ?? session?.sessionId,
      checkpointId ?? session?.pendingRuntimeCheckpointId,
    )
  }, [sessionStore.sessions, markRuntimeControlForTask, getActiveProjectId])

  const handleGlobalModeChange = useCallback((mode: 'task' | 'company' | 'org' | 'custom', profile?: string, orgId?: string) => {
    const normalizedMode = normalizeExecMode(mode)
    const nextProfile = normalizedMode === 'org'
      ? 'custom'
      : normalizedMode === 'company'
        ? 'corporate'
        : 'corporate'
    const nextOrgId = orgIdForExecMode(normalizedMode, orgId ?? activeSavedOrg)
    setGlobalExecMode(normalizedMode)
    setGlobalCompanyProfile(nextProfile)
    if (nextOrgId) setActiveSavedOrg(nextOrgId)
    clientRef.current?.setExecutionMode(
      normalizedMode,
      normalizedMode === 'task' ? undefined : nextProfile,
      globalTaskPreferredAgent,
      nextOrgId,
    )
  }, [globalCompanyProfile, globalTaskPreferredAgent, activeSavedOrg])

  // Whether the current mode allows agent creation/editing
  const isOrgMode = globalExecMode === 'org'
  const globalModeLabel = globalExecMode === 'task'
    ? 'task'
    : globalExecMode === 'org'
      ? `company/${activeSavedOrg ?? 'org'}`
      : `company/${globalCompanyProfile}`


  return (
    <div className="app-shell" data-theme={theme}>
      {orgToast && (
        <div className={`org-toast org-toast--${orgToast.kind}`} role="status" aria-live="polite">
          {orgToast.text}
        </div>
      )}
      {/* Topbar */}
      <header className="topbar">
        <div className="topbar-left">
          <span className="logo-text">Open<span className="logo-accent">OPC</span></span>
          <div className={`conn-dot ${statusClass(status)}`} title={`${status}${statusDetail ? ` — ${statusDetail}` : ''}\n${wsUrl}`} />
          <ProjectSelector
            projects={projectStore.projects}
            activeId={projectStore.activeProjectId}
            projectIdPolicy={status === 'connected' ? projectIdPolicy : null}
            onSelect={(id) => {
              const switchSeq = beginProjectSwitch(id)
              clientRef.current?.switchProject(id, switchSeq)
            }}
            onCreate={(id) => {
              clientRef.current?.createProject(id)
            }}
            onDelete={(id) => clientRef.current?.deleteProject(id)}
          />
        </div>
        <div className="topbar-center">
          <div className="page-nav">
            <button className={`page-nav-btn${activePage === 'workspace' ? ' active' : ''}`} onClick={() => setActivePage('workspace')}>
              {t('app.page.workspace')}
              {(() => {
                const total = chatStore.channels.reduce((sum, ch) => sum + chatStore.getUnreadCount(ch.id), 0)
                return total > 0 ? <span className="nav-unread-badge">{total > 99 ? '99+' : total}</span> : null
              })()}
            </button>
            <button className={`page-nav-btn${activePage === 'office' ? ' active' : ''}`} onClick={() => setActivePage('office')}>{t('app.page.office')}</button>
            <button className={`page-nav-btn${activePage === 'org' ? ' active' : ''}`} onClick={() => setActivePage('org')}>{t('app.page.org')}</button>
          </div>
          <div className="stat-chips">
            <span className="stat-chip"><b>{metrics.totalAgents}</b> {t('app.metric.agents')}</span>
            <span className="stat-chip"><b>{metrics.totalSkills}</b> {t('app.metric.skills')}</span>
            <span className="stat-chip"><b>{boardStore.getOpenTaskCount()}</b> {t('app.metric.tasks')}</span>
          </div>
        </div>
        <div className="topbar-right">
          <div className="language-toggle" role="group" aria-label={t('language.label')} title={t('language.label')}>
            <button
              type="button"
              data-locale="en"
              aria-pressed={locale === 'en'}
              className={`language-toggle-btn${locale === 'en' ? ' active' : ''}`}
              onClick={() => setLocale('en')}
            >
              {t('language.english')}
            </button>
            <button
              type="button"
              data-locale="zh-CN"
              aria-pressed={locale === 'zh-CN'}
              className={`language-toggle-btn${locale === 'zh-CN' ? ' active' : ''}`}
              onClick={() => setLocale('zh-CN')}
            >
              {t('language.chinese')}
            </button>
          </div>
          <select
            className="theme-select"
            value={outdoorOverride}
            title={t('outdoor.title')}
            aria-label={t('outdoor.title')}
            onChange={(e) => {
              const v = e.target.value as 'auto' | 'day' | 'night'
              setOutdoorOverride(v)
              try {
                if (v === 'auto') {
                  localStorage.removeItem('opc_outdoor_override')
                  localStorage.removeItem('opc_outdoor_day')
                  localStorage.removeItem('opc_outdoor_night')
                } else {
                  localStorage.setItem('opc_outdoor_override', v)
                  localStorage.removeItem('opc_outdoor_day')
                  localStorage.removeItem('opc_outdoor_night')
                }
              } catch { /* private mode */ }
              bridgeRef.current.syncOutdoorLighting()
            }}
          >
            <option value="auto">{t('outdoor.auto')}</option>
            <option value="day">{t('outdoor.day')}</option>
            <option value="night">{t('outdoor.night')}</option>
          </select>
          <select
            className="theme-select"
            value={theme}
            title={t('theme.label')}
            aria-label={t('theme.label')}
            onChange={(e) => {
              const next = e.currentTarget.value
              if (!isThemeName(next)) return
              setTheme(next)
              saveStoredTheme(next)
            }}
          >
            {THEMES.map(({ name }) => (
              <option key={name} value={name}>{t(themeMessageKey(name))}</option>
            ))}
          </select>
          <button className={`icon-btn ${showDevTools ? 'active' : ''}`} onClick={() => setShowDevTools((v) => !v)} title={t('dev.tools')} aria-label={t('dev.tools')}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M5.5 2L2 5.5 5.5 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/><path d="M10.5 7L14 10.5 10.5 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
          </button>
        </div>
      </header>

      {/* Workspace Page (unified Chat + Kanban) */}
      {activePage === 'workspace' && (
        <WorkspacePage
          boardStore={boardStore}
          chatStore={chatStore}
          sessionStore={sessionStore}
          agents={swarmAgents}
          officeMap={officeMap}
          execMode={globalExecMode}
          companyProfile={globalCompanyProfile}
          taskPreferredAgent={globalTaskPreferredAgent}
          nativeApprovalDefault={globalNativeApprovalDefault}
          projectId={projectStore.activeProjectId}
          orgInfoData={orgInfoData}
          onNavigateToOrg={() => setActivePage('org')}
          savedOrgsList={savedOrgsList}
          activeSavedOrg={activeSavedOrg}
          onSavedOrgsList={handleSavedOrgsList}
          onSavedOrgLoad={handleSavedOrgLoad}
          commsState={commsState}
          commsMessage={commsMessage}
          onCommsRefresh={(opts) => {
            const { project_id: _ignoredProjectId, ...scopedOpts } = opts ?? {}
            clientRef.current?.commsState(getActiveProjectId(), scopedOpts)
          }}
          onCommsReadMessage={(path) => clientRef.current?.commsReadMessage(
            getActiveProjectId(),
            path,
            commsState,
          )}
          onRunTask={(taskId, title, desc, mode, profile) => {
            clientRef.current?.send({ type: 'run_task', project_id: getActiveProjectId(), task_id: taskId, title, description: desc, mode, profile })
          }}
          onCreateTask={(title, boardId, columnId, taskId) => {
            clientRef.current?.send({ type: 'kanban_create_task', project_id: getActiveProjectId(), title, board_id: boardId, column_id: columnId, task_id: taskId })
          }}
          onMoveTask={(taskId, columnId) => {
            clientRef.current?.send({ type: 'kanban_move_task', project_id: getActiveProjectId(), task_id: taskId, column_id: columnId })
          }}
          onCreateSession={() => {
            if (pendingProjectSwitchRef.current || pendingSessionCreateRef.current) return
            const projectId = getActiveProjectId()
            beginPendingSessionCreate(projectId)
            clientRef.current?.createSession(
              projectId,
              undefined,
              globalExecMode,
              companyProfileForExecMode(globalExecMode, globalCompanyProfile),
              globalTaskPreferredAgent,
              orgIdForExecMode(globalExecMode, activeSavedOrg),
              globalNativeApprovalDefault,
            )
          }}
          onSessionSend={(taskId, content, attachments, metadata) => clientRef.current?.sessionSend(getActiveProjectId(), taskId, content, attachments, metadata)}
          onInteractionReply={(request) => {
            const client = clientRef.current
            if (!client) {
              return Promise.resolve({
                ok: false,
                accepted: false,
                error: 'socket_unavailable',
                checkpoint_id: request.checkpointId,
                checkpoint_type: request.checkpointType,
                client_request_id: request.clientRequestId,
              })
            }
            return client.interactionReply(getActiveProjectId(), request)
          }}
          onSecretarySend={(content) => clientRef.current?.secretarySend(getActiveProjectId(), content)}
          onDeleteSession={(taskId) => clientRef.current?.deleteSession(getActiveProjectId(), taskId)}
          onTitleChange={(taskId, title) => clientRef.current?.sessionUpdateTitle(getActiveProjectId(), taskId, title)}
          onSessionConfigChange={handleSessionModeChange}
          onSessionTaskAgentChange={handleSessionTaskAgentChange}
          onSessionNativeApprovalLevelChange={handleSessionNativeApprovalLevelChange}
          onSetNativeApprovalDefault={handleSetNativeApprovalDefault}
          onContinueInNewChat={handleContinueInNewChat}
          onSessionStop={handleSessionStop}
          onSessionResume={handleSessionResume}
          onSessionComplete={(taskId) => clientRef.current?.sessionComplete(getActiveProjectId(), taskId)}
          onLoadSessionDetail={(taskId, opts) => {
            const client = clientRef.current
            if (!client) return
            return client.sessionDetail(
              getActiveProjectId(),
              taskId,
              {
                ...opts,
                include: opts?.detailLevel === 'full'
                  ? ['messages', 'session_state', 'progress', 'work_items', 'runtime_context']
                  : ['messages', 'session_state'],
                viewGeneration: projectViewGenerationRef.current,
              },
            ).then((payload) => {
              if (payload.ok === false) {
                throw new Error(String(payload.error ?? 'session_detail failed'))
              }
            })
          }}
          onOpenExecutionPanel={handleOpenExecutionPanel}
          getExternalTeamActivity={(taskId, invocationId) => externalTeamActivityStore.getForTask(projectStore.activeProjectId, taskId, invocationId)}
          onCollabSync={() => clientRef.current?.collabSync(getActiveProjectId(), undefined, projectViewGenerationRef.current)}
        />
      )}

      {/* Org Page */}
      {activePage === 'org' && (
        <div className="org-page">
          <OrgTab
            data={orgInfoData}
            sessionRecruitmentByRole={sessionRecruitmentByRole}
            talents={talentTemplates}
            employeeDetail={employeeDetail}
            reorgProposals={reorgProposals}
            isCustomMode={isOrgMode}
            onRequestData={() => clientRef.current?.orgInfo()}
            onRequestTalents={() => clientRef.current?.talentList()}
            onRequestEmployeeDetail={(id) => clientRef.current?.employeeDetail(id)}
            onHireTalent={(tid, rid) => {
              setHiringTemplateId(tid)
              clientRef.current?.talentHire(tid, rid, undefined, orgInfoData?.organization_id || activeSavedOrg || undefined)
            }}
            hiringTemplateId={hiringTemplateId}
            onImportEmployee={(empId) => clientRef.current?.importEmployeeAsAgent(empId)}
            onRequestReorgList={() => {
              const projectId = getActiveProjectId()
              clientRef.current?.reorgList(projectId)
            }}
            onReorgDecide={(proposal, approved, notes) => {
              const client = clientRef.current
              const activeProjectId = getActiveProjectId()
              const checkpointType = proposal.checkpoint_type || 'company_reorg_pending'
              const clientRequestId = [
                'org-reorg',
                proposal.proposal_id,
                Date.now().toString(36),
                Math.random().toString(36).slice(2),
              ].join('-')
              if (
                !client
                || !proposal.checkpoint_id
                || proposal.checkpoint_status !== 'pending'
                || proposal.project_id !== activeProjectId
              ) {
                return Promise.resolve({
                  ok: false,
                  accepted: false,
                  error: 'reorg_checkpoint_unavailable',
                  checkpoint_id: proposal.checkpoint_id,
                  checkpoint_type: checkpointType,
                  client_request_id: clientRequestId,
                })
              }
              return client.interactionReply(activeProjectId, {
                checkpointId: proposal.checkpoint_id,
                checkpointType,
                clientRequestId,
                requesterTaskId: proposal.requester_task_id || undefined,
                requesterSessionId: proposal.requester_session_id || undefined,
                decision: {
                  text: notes?.trim() || (approved ? 'approve' : 'deny'),
                  option_id: approved ? 'approve' : 'deny',
                },
              }).then((receipt) => {
                if (receipt.accepted) client.reorgList(activeProjectId)
                return receipt
              })
            }}
            onMarketExport={(data) => clientRef.current?.marketExport(data)}
            onMarketInstall={(path, strategy) => clientRef.current?.marketInstall(path, strategy)}
            onMarketUninstall={(pkgId) => clientRef.current?.marketUninstall(pkgId)}
            marketPresets={marketPresets}
            marketPreviewData={marketPreviewData}
            onMarketBrowse={() => clientRef.current?.marketBrowse()}
            onMarketPreview={(id) => clientRef.current?.marketPreview(id)}
            onMarketApplyPreset={(id, strategy) => clientRef.current?.marketApplyPreset(id, strategy)}
            onMarketClearPreview={() => setMarketPreviewData(null)}
            onAddRole={(rid, name, resp, rt, icon) => clientRef.current?.addRole(rid, name, resp, rt, icon)}
            onBulkAddRoles={(roles) => clientRef.current?.bulkAddRoles(roles)}
            onUpdateRole={(rid, updates) => clientRef.current?.updateRole(rid, updates)}
            onDeleteRole={(rid) => clientRef.current?.deleteRole(rid)}
            onBindExternalTeam={(data) => clientRef.current?.bindExternalTeam(data)}
            onUnbindExternalTeam={(data) => clientRef.current?.unbindExternalTeam(data)}
            onUpdateOrgStrategy={(data) => clientRef.current?.updateOrgStrategy(data)}
            onUpdateRuntimePolicy={(policy) => clientRef.current?.updateRuntimePolicy(policy)}
            onResetArchitecture={() => clientRef.current?.resetArchitecture()}
            onConfigExport={() => clientRef.current?.orgConfigExport()}
            onConfigImport={(yaml, dryRun) => clientRef.current?.orgConfigImport(yaml, dryRun)}
            configExportYaml={configExportYaml}
            configImportPreview={configImportPreview}
            configImportError={configImportError}
            onSavedOrgsList={handleSavedOrgsList}
            onSavedOrgSaveAs={handleSavedOrgSaveAs}
            onSavedOrgCreate={handleSavedOrgCreate}
            onSavedOrgLoad={handleSavedOrgLoad}
            onSavedOrgDelete={handleSavedOrgDelete}
            savedOrgsList={savedOrgsList}
            activeSavedOrg={activeSavedOrg}
            activeSavedOrgVersionAtLoad={savedOrgVersionAtLoad}
            orgCreatePending={orgCreatePending}
            orgCreateResult={orgCreateResult}
            onSelectCorporate={handleSelectCorporateOrg}
          />
        </div>
      )}

      {activePage === 'mapEditor' && (
        <div className="editor-page">
          <CollisionEditor bridge={bridgeRef.current} />
        </div>
      )}

      <OfficePage
        bridge={bridgeRef.current}
        active={activePage === 'office'}
        sidebarCollapsed={sidebarCollapsed}
        onToggleSidebar={toggleSidebar}
        agents={swarmAgents}
        execMode={globalExecMode}
        companyProfile={globalCompanyProfile}
        modeLabel={globalModeLabel}
        isOrgMode={isOrgMode}
        deletingAgentId={deletingAgentId}
        onMoveAgent={handleMoveOfficeAgent}
        onDeleteAgent={handleDeleteOfficeAgent}
      />
      <DevToolsOverlay
        open={showDevTools}
        store={eventTimeline}
        snapshot={snapshot}
        wsUrlInput={wsUrlInput}
        onWsUrlInputChange={setWsUrlInput}
        onApplyWsUrl={applyWsUrl}
        onClose={() => setShowDevTools(false)}
      />
      {toastMessage && <div className={toastType === 'error' ? 'toast-error' : 'toast-success'}>{toastMessage}</div>}
      {/* ── Global Execution Panel (accessible from any page) ── */}
      <MaybeExecutionPanel
        taskId={executionPanelTaskId}
        projectId={projectStore.activeProjectId}
        sessions={sessionStore.sessions}
        agents={swarmAgents}
        getExternalTeamActivity={externalTeamActivityStore.getForTask}
        onLoadOlder={handleLoadOlderTeamActivity}
        onSelectInvocation={handleSelectTeamInvocation}
        runtimeOnly={executionPanelRuntimeOnly}
        onOpenRuntimeActivity={() => setExecutionPanelRuntimeOnly(true)}
        onClose={() => {
          setExecutionPanelTaskId(null)
          setExecutionPanelRuntimeOnly(false)
        }}
      />
    </div>
  )
}
