import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { AgentInfo, OrgInfoPayload, SavedOrgSummary } from '../types/visual'
import type { ChatMessage, CheckpointReplyMetadata, OutgoingAttachmentPayload } from '../types/chat'
import type { KanbanTask, Session, TaskPreferredAgent } from '../types/kanban'
import type { BoardStoreState } from '../kanban/BoardStore'
import type { ChatStoreState } from '../chat/ChatStore'
import type { SessionStoreState } from '../stores/SessionStore'
import { SessionSidebar } from '../chat/SessionSidebar'
import { analyzeCheckpointMessages, checkpointReplyMetadataForComposer } from '../chat/checkpointUtils'
import { KanbanBoardView } from '../kanban/KanbanBoardView'
import { AgentStatusBar } from '../kanban/AgentStatusBar'
import { BoardSelector } from '../kanban/BoardSelector'
import {
  getConversationPeerSessions,
  getWorkItemChildSessions,
  isMessageVisibleAtDetailLevel,
  mergeConversationMessages,
  projectSessionConversation,
  selectCompanySummaryMessages,
} from '../lib/workItemSessions'
import { getRuntimeOrgView } from '../lib/runtimeOrg'
import { getLinkedRuntimeTaskId } from '../lib/workItemRuntimeIds'
import { ContextPanel } from './ContextPanel'
import { useResizePanel } from './useResizePanel'
import { useI18n } from '../i18n'

type ActiveView =
  | { kind: 'session'; taskId: string }
  | { kind: 'task-detail'; taskId: string }
  | { kind: 'activity' }
  | { kind: 'secretary' }
  | { kind: 'child-detail' }

const SESSION_DETAIL_PAGE_SIZE = 200

function makeOptimisticUserMessageId(): string {
  const cryptoApi = globalThis.crypto
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
    return `ui-${cryptoApi.randomUUID()}`
  }
  return `ui-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
}

/* ── Org mode pre-run readiness check ─────────────────────────────────── */

type TFunction = ReturnType<typeof useI18n>['t']

function checkOrgModeReadiness(
  mode: string,
  orgInfoData: OrgInfoPayload | null | undefined,
  onNavigateToOrg?: () => void,
  t?: TFunction,
): boolean {
  const tr = t ?? ((key: string, params?: Record<string, string | number>) => {
    const fallbacks: Record<string, string> = {
      'workspace.confirmNoRoles': 'Your org has no roles defined.\n\nSet up at least one role before running a task.\n\nGo to Org tab now?',
      'workspace.confirmNoDecider': 'Your organization has multiple top-level roles but no final decider selected.\n\nChoose one final decider in the Org tab before running a task.\n\nGo to Org tab now?',
      'workspace.warningNoTeams': 'No runtime teams defined — the system will auto-generate from your roles',
      'workspace.warningVacantRoles': '{count} role(s) have no employees: {names}',
      'workspace.confirmWarnings': 'Before running this task:\n\n{warnings}\n\nRun anyway?',
    }
    const template = fallbacks[key] ?? key
    return template.replace(/\{(\w+)\}/g, (_, name) => String(params?.[name] ?? `{${name}}`))
  }) as TFunction
  if (mode !== 'org' && mode !== 'custom') return true
  if (!orgInfoData) return true

  const { roles, employees } = orgInfoData
  const runtimeView = getRuntimeOrgView(orgInfoData)
  const topLevelRoleIds = orgInfoData.top_level_role_ids ?? []
  const finalDeciderRoleId = orgInfoData.final_decider_role_id ?? ''

  // Block: no roles defined
  if (roles.length === 0) {
    const goToOrg = confirm(tr('workspace.confirmNoRoles'))
    if (goToOrg) onNavigateToOrg?.()
    return false
  }

  if (topLevelRoleIds.length > 1 && !finalDeciderRoleId) {
    const goToOrg = confirm(tr('workspace.confirmNoDecider'))
    if (goToOrg) onNavigateToOrg?.()
    return false
  }

  const warnings: string[] = []

  const roleIds = new Set(roles.map(r => r.role_id))
  const relevantTeams = runtimeView.runtimeTeams.filter(team => (
    team.member_role_ids.some(roleId => roleIds.has(roleId))
    || (team.manager_role_id && roleIds.has(team.manager_role_id))
  ))
  if (relevantTeams.length === 0) {
    warnings.push(tr('workspace.warningNoTeams'))
  }

  // Warn: roles without employees
  const employeeRoleIds = new Set(employees.map(e => e.role_id))
  const vacantRoles = roles.filter(r => !employeeRoleIds.has(r.role_id))
  if (vacantRoles.length > 0) {
    const names = vacantRoles.map(r => r.name).join(', ')
    warnings.push(tr('workspace.warningVacantRoles', { count: vacantRoles.length, names }))
  }

  if (warnings.length > 0) {
    return confirm(tr('workspace.confirmWarnings', { warnings: warnings.map(w => '\u2022 ' + w).join('\n') }))
  }

  return true
}

export function hasWorkspaceTeamInfo(orgInfoData: OrgInfoPayload | null | undefined): boolean {
  if (!orgInfoData) return false
  const runtimeView = getRuntimeOrgView(orgInfoData)
  return !!(
    runtimeView.projectRun?.run_id
    || runtimeView.runtimeTeams.length > 0
    || runtimeView.runtimeSeats.length > 0
  )
}

function sessionDetailLevel(
  session: Session | null | undefined,
  options?: { childDetail?: boolean },
): 'summary' | 'full' {
  const childDetail = !!options?.childDetail
  if (!session) return 'summary'
  if (childDetail) return 'full'
  return session.execMode === 'company' || session.execMode === 'org' || session.execMode === 'custom' ? 'summary' : 'full'
}

function isCompanyConversation(session: Session | null | undefined, relatedSessionCount = 0): boolean {
  if (!session) return false
  const mode = String(session.execMode ?? '').trim().toLowerCase()
  return relatedSessionCount > 0
    || !!session.isCompanyRuntime
    || !!session.roleWorkItems
    || !!session.executorRoleWorkItems
    || mode === 'company'
    || mode === 'org'
    || mode === 'custom'
}

function sessionBoardId(session: Session | null | undefined): string | null {
  const boardId = String(session?.originTaskId ?? session?.taskId ?? '').trim()
  return boardId || null
}

/**
 * Inline-editable board/session title rendered above the kanban.  Click the
 * title to edit; Enter commits, Esc cancels.  Syncs via onCommit (wired to
 * session_update_title) so the backend updates the Task and the left-side
 * session sidebar refreshes on the next collab_sync.
 */
function BoardTitleEditor({
  boardColor,
  title,
  onCommit,
}: {
  boardColor?: string
  title: string
  onCommit: (next: string) => void
}) {
  const { t } = useI18n()
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(title)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!editing) setDraft(title)
  }, [title, editing])

  const commit = useCallback(() => {
    const trimmed = draft.trim()
    if (trimmed && trimmed !== title) {
      onCommit(trimmed)
    } else {
      setDraft(title)
    }
    setEditing(false)
  }, [draft, title, onCommit])

  const startEditing = useCallback(() => {
    setDraft(title)
    setEditing(true)
    setTimeout(() => inputRef.current?.select(), 0)
  }, [title])

  return (
    <div className="board-selector">
      <div className="board-tabs">
        <span
          className="board-tab active board-tab-editable"
          style={{ '--board-color': boardColor } as React.CSSProperties}
        >
          <span className="board-tab-dot" style={{ background: boardColor }} />
          {editing ? (
            <input
              ref={inputRef}
              className="board-tab-title-input"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={commit}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commit()
                if (e.key === 'Escape') { setDraft(title); setEditing(false) }
              }}
            />
          ) : (
            <span
              className="board-tab-title"
              onClick={startEditing}
              title={t('workspace.clickEditTitle')}
            >
              {title}
            </span>
          )}
        </span>
      </div>
    </div>
  )
}

interface WorkspacePageProps {
  boardStore: BoardStoreState
  agents: AgentInfo[]
  officeMap?: Record<string, string>
  execMode: string
  companyProfile: string
  taskPreferredAgent: TaskPreferredAgent

  chatStore: ChatStoreState
  sessionStore: SessionStoreState
  projectId: string

  onRunTask: (taskId: string, title: string, desc: string, mode: string, profile?: string) => void
  onCreateTask: (title: string, boardId: string, columnId: string, taskId?: string) => void
  onMoveTask: (taskId: string, columnId: string) => void
  onCreateSession: () => void
  onSessionSend: (
    taskId: string,
    content: string,
    attachments?: OutgoingAttachmentPayload[],
    metadata?: CheckpointReplyMetadata,
  ) => void
  onSecretarySend?: (content: string) => void
  onDeleteSession: (taskId: string) => void
  onTitleChange: (taskId: string, title: string) => void
  onSessionConfigChange?: (taskId: string, execMode: string, companyProfile?: string, orgId?: string) => void
  onSessionTaskAgentChange?: (taskId: string, preferredAgent: TaskPreferredAgent) => void
  /**
   * Forwarded to ContextPanel → MessageComposer locked-mode chip popover.
   * Spawns a fresh chat in the requested mode under the active project so
   * users can "continue this conversation in a different mode" without
   * mutating the locked chat.
   */
  onContinueInNewChat?: (mode: 'task' | 'company' | 'org' | 'custom', companyProfile?: 'corporate' | 'custom', orgId?: string) => void
  onSessionStop?: (taskId: string) => void
  onSessionResume?: (taskId: string, runtimeSessionId?: string, checkpointId?: string) => void
  onSessionComplete?: (taskId: string) => void
  onLoadSessionDetail?: (
    taskId: string,
    opts?: { beforeCreatedAt?: number; beforeMessageId?: string; limit?: number; detailLevel?: 'summary' | 'full'; include?: string[] },
  ) => Promise<void> | void
  onOpenExecutionPanel?: (taskId: string) => void
  onCollabSync?: () => void
  orgInfoData?: OrgInfoPayload | null
  onNavigateToOrg?: () => void
  commsState?: import('../lib/wsClient').CommsStatePayload | null
  commsMessage?: import('../lib/wsClient').CommsMessagePayload | null
  onCommsRefresh?: (opts?: { task_id?: string; session_id?: string; project_id?: string }) => void
  onCommsReadMessage?: (path: string) => void
  savedOrgsList?: SavedOrgSummary[] | null
  activeSavedOrg?: string | null
  onSavedOrgsList?: () => void
  onSavedOrgLoad?: (name: string) => void
}

export function WorkspacePage({
  boardStore,
  agents,
  officeMap,
  execMode,
  companyProfile,
  taskPreferredAgent,
  chatStore,
  sessionStore,
  projectId,
  onRunTask,
  onCreateTask,
  onMoveTask,
  onCreateSession,
  onSessionSend,
  onSecretarySend,
  onDeleteSession,
  onTitleChange,
  onSessionConfigChange,
  onSessionTaskAgentChange,
  onContinueInNewChat,
  onSessionStop,
  onSessionResume,
  onSessionComplete,
  onLoadSessionDetail,
  onOpenExecutionPanel,
  onCollabSync,
  orgInfoData,
  onNavigateToOrg,
  commsState,
  commsMessage,
  onCommsRefresh,
  onCommsReadMessage,
  savedOrgsList,
  activeSavedOrg,
  onSavedOrgsList,
  onSavedOrgLoad,
}: WorkspacePageProps) {
  const { t } = useI18n()
  const { sessions, activeSessionId, activeSession } = sessionStore
  const { markRead } = chatStore

  // ── Panel state ──
  const [panelState, setPanelState] = useState<'collapsed' | 'open' | 'maximized'>('collapsed')
  const { width, isResizing, handleMouseDown } = useResizePanel({
    initialWidth: 380,
    minWidth: 300,
    maxWidth: 600,
    onCollapse: () => setPanelState('collapsed'),
  })
  const [panelTab, setPanelTab] = useState<'chat' | 'agents' | 'info' | 'comms' | 'team'>('chat')
  const [childDetailTaskId, setChildDetailTaskId] = useState<string | null>(null)
  const [activeView, setActiveView] = useState<ActiveView>({ kind: 'activity' })
  const [openSessionIds, setOpenSessionIds] = useState<string[]>([])
  const [multiSessionView, setMultiSessionView] = useState(false)
  const [sessionHistoryLoading, setSessionHistoryLoading] = useState<Record<string, boolean>>({})
  const onLoadSessionDetailRef = useRef(onLoadSessionDetail)
  const sessionsRef = useRef(sessions)
  const getChannelMessagesRef = useRef(chatStore.getChannelMessages)
  const autoHistoryRequestRef = useRef<{ scope: string | null; active: Set<string>; child: string | null }>({
    scope: null,
    active: new Set(),
    child: null,
  })
  const historyRequestInFlightRef = useRef<Set<string>>(new Set())
  const historyRequestGenerationRef = useRef(0)

  sessionsRef.current = sessions
  getChannelMessagesRef.current = chatStore.getChannelMessages

  const isCompanyMode = execMode === 'company' || execMode === 'org' || execMode === 'custom'

  // Per-project channel IDs
  const secretaryChannelId = `secretary:${projectId}`
  const activityChannelId = `activity:${projectId}`

  // Child detail session — must be declared early (before channelId which references it)
  const childDetailSession = useMemo(() => {
    if (!childDetailTaskId) return null
    return sessions.find(s => s.taskId === childDetailTaskId) ?? null
  }, [sessions, childDetailTaskId])
  const activeTask = useMemo<KanbanTask | null>(() => {
    if (activeView.kind !== 'task-detail') return null
    return boardStore.tasks.find(task => task.id === activeView.taskId) ?? null
  }, [activeView, boardStore.tasks])
  // Linked child session for the currently selected kanban task (company mode)
  const linkedTaskSession = useMemo(() => {
    const linkedRuntimeTaskId = getLinkedRuntimeTaskId(activeTask)
    if (!linkedRuntimeTaskId) return null
    return sessions.find(s =>
      s.taskId === linkedRuntimeTaskId
      || s.runtimeTaskId === linkedRuntimeTaskId
      || s.executionTurnId === linkedRuntimeTaskId
    ) ?? null
  }, [sessions, activeTask])
  const linkedTaskSessionMessages = useMemo(() => {
    if (!linkedTaskSession) return []
    return chatStore.getChannelMessages(linkedTaskSession.channelId)
  }, [chatStore.messages, chatStore.getChannelMessages, linkedTaskSession])
  const childSessions = useMemo(() => {
    return getWorkItemChildSessions(activeSession, sessions)
  }, [sessions, activeSession])
  const conversationPeers = useMemo(() => {
    return getConversationPeerSessions(activeSession, sessions)
  }, [sessions, activeSession])
  const activeConversation = useMemo(() => {
    return projectSessionConversation(activeSession, [...conversationPeers, ...childSessions])
  }, [activeSession, childSessions, conversationPeers])

  useEffect(() => {
    onLoadSessionDetailRef.current = onLoadSessionDetail
  }, [onLoadSessionDetail])

  const requestSessionHistory = useCallback((
    taskId: string,
    oldestMessage?: ChatMessage,
    detailLevel: 'summary' | 'full' = 'summary',
  ) => {
    const loadSessionDetail = onLoadSessionDetailRef.current
    if (!loadSessionDetail || !taskId) return
    const generation = historyRequestGenerationRef.current
    const targetSession = sessionsRef.current.find(session => session.taskId === taskId)
    const targetChannelId = targetSession?.channelId
    const cursorMessage = oldestMessage && targetChannelId && oldestMessage.channelId !== targetChannelId
      ? getChannelMessagesRef.current(targetChannelId).find(
        message => isMessageVisibleAtDetailLevel(message, detailLevel),
      )
      : oldestMessage
    const requestKey = [
      generation,
      taskId,
      detailLevel,
      cursorMessage?.timestamp ?? 'latest',
      cursorMessage?.id ?? '',
    ].join('|')
    if (historyRequestInFlightRef.current.has(requestKey)) return
    // Claim the cursor synchronously before invoking the transport. Loading
    // state is asynchronous and cannot serve as a single-flight guard.
    historyRequestInFlightRef.current.add(requestKey)
    setSessionHistoryLoading(prev => prev[taskId] ? prev : { ...prev, [taskId]: true })
    let request: Promise<void> | void
    try {
      request = loadSessionDetail(taskId, {
        limit: SESSION_DETAIL_PAGE_SIZE,
        beforeCreatedAt: cursorMessage?.timestamp,
        beforeMessageId: cursorMessage?.id,
        detailLevel,
      })
    } catch (error) {
      historyRequestInFlightRef.current.delete(requestKey)
      if (historyRequestGenerationRef.current === generation) {
        setSessionHistoryLoading(prev => prev[taskId] ? { ...prev, [taskId]: false } : prev)
        autoHistoryRequestRef.current.active.delete(`${taskId}:${detailLevel}`)
        if (autoHistoryRequestRef.current.child === taskId) autoHistoryRequestRef.current.child = null
      }
      return
    }
    return Promise.resolve(request).catch(() => {
      if (historyRequestGenerationRef.current === generation) {
        autoHistoryRequestRef.current.active.delete(`${taskId}:${detailLevel}`)
        if (autoHistoryRequestRef.current.child === taskId) autoHistoryRequestRef.current.child = null
      }
    }).finally(() => {
      historyRequestInFlightRef.current.delete(requestKey)
      if (historyRequestGenerationRef.current !== generation) return
      const taskPrefix = `${generation}|${taskId}|`
      const taskStillLoading = [...historyRequestInFlightRef.current.keys()]
        .some(key => key.startsWith(taskPrefix))
      if (!taskStillLoading) {
        setSessionHistoryLoading(prev => prev[taskId] ? { ...prev, [taskId]: false } : prev)
      }
    })
  }, [])

  const isSessionHistoryLoading = useCallback((taskId: string) => {
    return !!sessionHistoryLoading[taskId]
  }, [sessionHistoryLoading])

  // Auto-clear childDetailTaskId if session was deleted
  useLayoutEffect(() => {
    historyRequestGenerationRef.current += 1
    historyRequestInFlightRef.current.clear()
    autoHistoryRequestRef.current = { scope: null, active: new Set(), child: null }
    setSessionHistoryLoading({})
  }, [projectId])

  useEffect(() => () => {
    historyRequestGenerationRef.current += 1
    historyRequestInFlightRef.current.clear()
  }, [])

  useEffect(() => {
    if (childDetailTaskId && !childDetailSession) {
      setChildDetailTaskId(null)
    }
  }, [childDetailTaskId, childDetailSession])

  useEffect(() => {
    if (activeView.kind === 'task-detail' && !activeTask) {
      setActiveView({ kind: 'activity' })
    }
  }, [activeView, activeTask])

  // Lazy-load linked child session messages when a kanban task is clicked
  const autoLinkedSessionRef = useRef<string | null>(null)
  useEffect(() => {
    if (!linkedTaskSession) {
      autoLinkedSessionRef.current = null
      return
    }
    if (autoLinkedSessionRef.current === linkedTaskSession.taskId) return
    autoLinkedSessionRef.current = linkedTaskSession.taskId
    requestSessionHistory(linkedTaskSession.taskId, undefined, 'full')
  }, [linkedTaskSession, requestSessionHistory])

  useEffect(() => {
    if (!childDetailTaskId) {
      autoHistoryRequestRef.current.child = null
      return
    }
    if (autoHistoryRequestRef.current.child === childDetailTaskId) return
    autoHistoryRequestRef.current.child = childDetailTaskId
    requestSessionHistory(childDetailTaskId, undefined, 'full')
  }, [childDetailTaskId, requestSessionHistory])

  useEffect(() => {
    if (!activeSessionId) {
      autoHistoryRequestRef.current.scope = null
      autoHistoryRequestRef.current.active.clear()
      return
    }
    if (autoHistoryRequestRef.current.scope !== activeSessionId) {
      autoHistoryRequestRef.current.scope = activeSessionId
      autoHistoryRequestRef.current.active.clear()
    }
    const historyTargets = activeConversation.timelineSessions.length > 0
      ? activeConversation.timelineSessions
      : (sessions.find(session => session.taskId === activeSessionId)
          ? [sessions.find(session => session.taskId === activeSessionId)!]
          : [])
    if (historyTargets.length === 0) {
      autoHistoryRequestRef.current.active.clear()
      return
    }
    for (const session of historyTargets) {
      const detailLevel = isCompanyConversation(activeSession, childSessions.length)
        ? 'summary'
        : sessionDetailLevel(session)
      const requestKey = `${session.taskId}:${detailLevel}`
      if (autoHistoryRequestRef.current.active.has(requestKey)) continue
      autoHistoryRequestRef.current.active.add(requestKey)
      requestSessionHistory(
        session.taskId,
        undefined,
        detailLevel,
      )
    }
  }, [activeConversation.timelineSessions, activeSession, activeSessionId, childSessions.length, requestSessionHistory, sessions])

  // Sync activeView when activeSessionId changes externally
  const effectiveView: ActiveView = useMemo(() => {
    if (childDetailTaskId) return { kind: 'child-detail' as const }
    if (activeView.kind === 'task-detail') return activeView
    if (activeView.kind === 'secretary') return activeView
    if (activeSessionId) return { kind: 'session' as const, taskId: activeSessionId }
    return { kind: 'activity' as const }
  }, [activeView, activeSessionId, childDetailTaskId])

  // Channel ID for message filtering
  const channelId = useMemo(() => {
    if (effectiveView.kind === 'secretary') return secretaryChannelId
    if (effectiveView.kind === 'child-detail' && childDetailSession) return childDetailSession.channelId
    if (effectiveView.kind === 'session' && activeConversation.displaySession) return activeConversation.displaySession.channelId
    if (effectiveView.kind === 'session' && activeSession) return activeSession.channelId
    return activityChannelId
  }, [effectiveView, activeConversation.displaySession, activeSession, childDetailSession, secretaryChannelId, activityChannelId])

  const visibleChannelIds = useMemo(() => {
    if (effectiveView.kind === 'activity') return [activityChannelId]
    if (effectiveView.kind === 'secretary') return [secretaryChannelId]
    if (effectiveView.kind === 'child-detail' && childDetailSession) return [childDetailSession.channelId]
    if (effectiveView.kind === 'session') {
      const ids = activeConversation.timelineSessions
        .map((session) => session.channelId)
        .filter((value, index, values) => !!value && values.indexOf(value) === index)
      if (ids.length > 0) return ids
    }
    return channelId ? [channelId] : []
  }, [
    effectiveView.kind,
    activeConversation.timelineSessions,
    childDetailSession,
    activityChannelId,
    secretaryChannelId,
    channelId,
  ])

  // Active channel IDs (non-cancelled sessions)
  const activeChannelIds = useMemo(() => {
    const set = new Set<string>()
    for (const s of sessions) {
      if (s.status !== 'cancelled') set.add(s.channelId)
    }
    set.add(activityChannelId)
    set.add(secretaryChannelId)
    return set
  }, [sessions, activityChannelId, secretaryChannelId])

  // Messages for current view
  const activeMessages = useMemo(() => {
    if (effectiveView.kind === 'activity') {
      return chatStore.messages
        .filter(m => m.sender !== 'user' && activeChannelIds.has(m.channelId))
        .sort((a, b) => b.timestamp - a.timestamp)
        .slice(0, 50)
        .reverse()
    }
    if (effectiveView.kind === 'session' && visibleChannelIds.length > 1) {
      const messageGroups = visibleChannelIds.map(
        (visibleChannelId) => chatStore.getChannelMessages(visibleChannelId),
      )
      if (isCompanyConversation(activeSession, childSessions.length) && activeSession) {
        return selectCompanySummaryMessages(messageGroups.flat(), activeSession.channelId)
      }
      return mergeConversationMessages(messageGroups)
    }
    return chatStore.getChannelMessages(channelId)
  }, [
    chatStore.messages,
    chatStore.getChannelMessages,
    channelId,
    effectiveView.kind,
    activeChannelIds,
    activeSession,
    childSessions.length,
    visibleChannelIds,
  ])
  const childDetailMessages = useMemo(() => {
    if (!childDetailSession) return []
    return chatStore.getChannelMessages(childDetailSession.channelId)
  }, [chatStore.messages, chatStore.getChannelMessages, childDetailSession])
  const latestPendingCheckpointReply = useMemo(
    () => {
      if (effectiveView.kind !== 'session') return undefined
      return checkpointReplyMetadataForComposer(
        analyzeCheckpointMessages(activeMessages).latestPendingReplyMetadata,
      )
    },
    [activeMessages, effectiveView.kind],
  )

  // Hide child sessions from sidebar in Company mode
  const sidebarSessions = useMemo(() => {
    const filtered = isCompanyMode ? sessions.filter(s => s.mode !== 'child') : sessions
    return [...filtered].sort((a, b) => b.updatedAt - a.updatedAt)
  }, [sessions, isCompanyMode])

  const childTaskIds = useMemo(() => {
    if (!isCompanyMode) return null
    const set = new Set<string>()
    for (const s of sessions) {
      if (s.mode === 'child') set.add(s.taskId)
    }
    return set.size > 0 ? set : null
  }, [sessions, isCompanyMode])

  const filteredTasksByColumn = useMemo(() => {
    if (!childTaskIds) return boardStore.tasksByColumn
    const result: Record<string, import('../types/kanban').KanbanTask[]> = {}
    for (const [colId, tasks] of Object.entries(boardStore.tasksByColumn)) {
      result[colId] = tasks.filter(t => !childTaskIds.has(t.id))
    }
    return result
  }, [boardStore.tasksByColumn, childTaskIds])

  // Keep the Agents tab visible whenever company-style runtime is active for a session.
  const isCompanyRuntime = !!(activeSession && (activeSession.isCompanyRuntime || childSessions.length > 0))
  const canShowAgentsTab = !!(activeSession && (isCompanyMode || isCompanyRuntime))
  const canShowTeamTab = !!(activeSession && isCompanyMode && hasWorkspaceTeamInfo(orgInfoData))
  useEffect(() => {
    if (panelTab === 'agents' && !canShowAgentsTab) {
      setPanelTab('chat')
    }
    if (panelTab === 'comms' && !onCommsRefresh) {
      setPanelTab('chat')
    }
    if (panelTab === 'team' && !canShowTeamTab) {
      setPanelTab('chat')
    }
  }, [panelTab, canShowAgentsTab, canShowTeamTab, onCommsRefresh])

  // Unread counts per channel
  const unreadCounts = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const s of sessions) {
      counts[s.channelId] = chatStore.getUnreadCount(s.channelId)
    }
    counts[secretaryChannelId] = chatStore.getUnreadCount(secretaryChannelId)
    return counts
  }, [sessions, chatStore.getUnreadCount, secretaryChannelId])

  const openSessions = useMemo(
    () => openSessionIds
      .map(taskId => sessions.find(s => s.taskId === taskId))
      .filter((session): session is Session => !!session && session.mode !== 'child'),
    [openSessionIds, sessions],
  )

  const openSessionMessages = useMemo<Record<string, ChatMessage[]>>(() => {
    const result: Record<string, ChatMessage[]> = {}
    for (const session of openSessions) {
      const sessionChildren = getWorkItemChildSessions(session, sessions)
      const sessionPeers = getConversationPeerSessions(session, sessions)
      const projection = projectSessionConversation(session, [...sessionPeers, ...sessionChildren])
      const messageGroups = projection.timelineSessions.map((timelineSession) => (
        chatStore.getChannelMessages(timelineSession.channelId)
      ))
      result[session.taskId] = isCompanyConversation(session, sessionChildren.length)
        ? selectCompanySummaryMessages(messageGroups.flat(), session.channelId)
        : mergeConversationMessages(messageGroups)
    }
    return result
  }, [openSessions, sessions, chatStore.getChannelMessages])

  const openSessionChildren = useMemo<Record<string, Session[]>>(() => {
    const result: Record<string, Session[]> = {}
    for (const session of openSessions) {
      result[session.taskId] = getWorkItemChildSessions(session, sessions)
    }
    return result
  }, [openSessions, sessions])

  const ensureSessionOpen = useCallback((taskId: string) => {
    setOpenSessionIds(prev => prev.includes(taskId) ? prev : [...prev, taskId])
  }, [])

  // Auto-open panel when a session is selected externally (e.g. from Office page submitMessage)
  const prevActiveSessionIdRef = useRef<string | null>(null)
  useEffect(() => {
    if (activeSessionId && !prevActiveSessionIdRef.current && panelState === 'collapsed') {
      setPanelState('open')
      setPanelTab('chat')
    }
    prevActiveSessionIdRef.current = activeSessionId
  }, [activeSessionId, panelState])

  useEffect(() => {
    if (activeSessionId) ensureSessionOpen(activeSessionId)
  }, [activeSessionId, ensureSessionOpen])

  useEffect(() => {
    const validTaskIds = new Set(sessions.map(session => session.taskId))
    setOpenSessionIds(prev => prev.filter(taskId => validTaskIds.has(taskId)))
    setSessionHistoryLoading(prev => Object.fromEntries(
      Object.entries(prev).filter(([taskId]) => validTaskIds.has(taskId)),
    ))
  }, [sessions])

  useEffect(() => {
    if (multiSessionView && openSessions.length < 2) {
      setMultiSessionView(false)
    }
  }, [multiSessionView, openSessions.length])

  // Esc to collapse panel
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && panelState !== 'collapsed') {
        // Don't collapse if user is typing in an input/textarea
        const tag = (e.target as HTMLElement)?.tagName
        if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return
        setPanelState('collapsed')
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [panelState])

  const handleMarkRead = useCallback(() => {
    for (const visibleChannelId of visibleChannelIds) {
      markRead(visibleChannelId)
    }
  }, [visibleChannelIds, markRead])

  const handleMarkSessionRead = useCallback((taskId: string) => {
    const session = sessions.find(item => item.taskId === taskId)
    if (session) markRead(session.channelId)
  }, [sessions, markRead])

  const focusSession = useCallback((taskId: string) => {
    const session = sessions.find(item => item.taskId === taskId)
    if (!session) return
    ensureSessionOpen(taskId)
    sessionStore.setActiveSession(taskId)
    setActiveView({ kind: 'session', taskId })
    setPanelState('open')
    setPanelTab('chat')
    setChildDetailTaskId(null)
  }, [sessions, ensureSessionOpen, sessionStore])

  const handleCloseSessionView = useCallback((taskId: string) => {
    const remaining = openSessionIds.filter(id => id !== taskId)
    setOpenSessionIds(remaining)
    if (childDetailTaskId === taskId) {
      setChildDetailTaskId(null)
    }
    if (activeSessionId !== taskId) return
    const nextActive = remaining[remaining.length - 1] ?? null
    sessionStore.setActiveSession(nextActive)
    if (nextActive) {
      setActiveView({ kind: 'session', taskId: nextActive })
      setPanelTab('chat')
      return
    }
    setActiveView({ kind: 'activity' })
  }, [openSessionIds, childDetailTaskId, activeSessionId, sessionStore])

  // ── Session selection (sidebar click or board card click) ──
  const handleSelectSession = useCallback((taskId: string | null) => {
    if (!taskId) {
      setActiveView({ kind: 'activity' })
      sessionStore.setActiveSession(null)
      setChildDetailTaskId(null)
      return
    }

    const session = sessions.find(s => s.taskId === taskId)

    // Company mode child → open child detail
    if (isCompanyMode && session?.mode === 'child') {
      setChildDetailTaskId(taskId)
      setPanelState('open')
      return
    }

    focusSession(taskId)
  }, [sessions, sessionStore, isCompanyMode, focusSession])

  const handleSelectSecretary = useCallback(() => {
    setActiveView({ kind: 'secretary' })
    sessionStore.setActiveSession(null)
    setChildDetailTaskId(null)
    setPanelState('open')
  }, [sessionStore])

  // ── Board interactions ──
  const handleCardClick = useCallback((task: { id: string }) => {
    const boardTask = boardStore.tasks.find(t => t.id === task.id)
    if (isCompanyMode && boardTask) {
      setChildDetailTaskId(null)
      setActiveView({ kind: 'task-detail', taskId: boardTask.id })
      setPanelState('open')
      setPanelTab('info')
      return
    }
    const session = sessions.find(s => s.taskId === task.id)
    if (session) {
      focusSession(task.id)
    } else {
      if (boardTask && boardTask.phase !== 'cancelled') {
        sessionStore.createSession({
          projectId,
          taskId: boardTask.id,
          channelId: `session:${boardTask.id}`,
          sessionId: boardTask.sessionId,
          title: boardTask.title,
          status: 'pending',
          columnId: boardTask.columnId,
          execMode,
          companyProfile,
          assigneeIds: boardTask.assigneeIds,
          priority: boardTask.priority,
          tags: boardTask.tags,
          selectedExecutionAgent: boardTask.selectedExecutionAgent,
          createdAt: boardTask.createdAt,
          updatedAt: boardTask.updatedAt,
          messageCount: 0,
          progressLog: [],
        })
        ensureSessionOpen(task.id)
        sessionStore.setActiveSession(task.id)
        setActiveView({ kind: 'session', taskId: task.id })
        setPanelState('open')
        setPanelTab('chat')
        onCollabSync?.()
      }
    }
  }, [sessions, boardStore.tasks, sessionStore, focusSession, ensureSessionOpen, onCollabSync, isCompanyMode])

  const handleQuickCreate = useCallback((title: string) => {
    if (!boardStore.activeBoardId) return
    const todoCol = boardStore.activeBoardColumns.find(c => c.name === 'Todo')
    if (!todoCol) return
    const task = boardStore.createTask({
      boardId: boardStore.activeBoardId,
      columnId: todoCol.id,
      title,
    })
    onCreateTask(title, boardStore.activeBoardId, todoCol.id, task.id)
  }, [boardStore, onCreateTask])

  const handleBoardSelect = useCallback((boardId: string) => {
    boardStore.setActiveBoard(boardId)
    const primarySession = sessions.find(session => session.mode !== 'child' && sessionBoardId(session) === boardId)
    if (primarySession) {
      focusSession(primarySession.taskId)
      return
    }
    setChildDetailTaskId(null)
    setActiveView({ kind: 'activity' })
    sessionStore.setActiveSession(null)
  }, [boardStore, sessions, focusSession, sessionStore])

  const boardIdSet = useMemo(
    () => new Set(boardStore.boards.map(board => board.id)),
    [boardStore.boards],
  )
  useEffect(() => {
    if (isCompanyMode) {
      // Company mode: active board follows the selected session's board.
      const targetBoardId = activeTask?.boardId
        ?? sessionBoardId(activeSession)
        ?? null
      if (!targetBoardId || !boardIdSet.has(targetBoardId)) {
        // No session selected, or the session's board hasn't been pushed yet.
        if (boardStore.activeBoardId !== null) {
          boardStore.setActiveBoard(null)
        }
        return
      }
      if (boardStore.activeBoardId !== targetBoardId) {
        boardStore.setActiveBoard(targetBoardId)
      }
      return
    }
    // Non-company mode: there's 1 project-wide board. Select it once.
    if (boardStore.boards.length > 0 && !boardStore.activeBoardId) {
      boardStore.setActiveBoard(boardStore.boards[0].id)
    }
  }, [
    isCompanyMode,
    activeTask?.boardId,
    activeSession,
    boardIdSet,
    boardStore.activeBoardId,
    boardStore.boards,
    boardStore.setActiveBoard,
  ])

  const handleStartTask = useCallback((taskId: string) => {
    const task = boardStore.tasks.find(t => t.id === taskId)
    if (!task) return
    const session = sessions.find(item => item.taskId === taskId)
    const sessionExecMode = session?.execMode ?? execMode
    const sessionCompanyProfile = session?.companyProfile ?? companyProfile
    const runtimeProfile = sessionExecMode === 'org' || sessionExecMode === 'custom'
      ? 'custom'
      : sessionExecMode === 'company'
        ? sessionCompanyProfile
        : undefined

    // Pre-run readiness check for org mode
    if (!checkOrgModeReadiness(sessionExecMode, orgInfoData, onNavigateToOrg, t)) {
      return
    }

    onRunTask(
      taskId,
      task.title,
      task.description ?? '',
      sessionExecMode,
      runtimeProfile,
    )
    const inProgressCol = boardStore.activeBoardColumns.find(c => c.name === 'In Progress')
    if (inProgressCol) {
      boardStore.moveTask(taskId, inProgressCol.id, 0)
    }
  }, [boardStore, onRunTask, sessions, execMode, companyProfile, orgInfoData, onNavigateToOrg, t])

  const handleSessionConfigChange = useCallback((taskId: string, sessionMode: string, sessionCompanyProfile?: string, orgId?: string) => {
    onSessionConfigChange?.(taskId, sessionMode, sessionCompanyProfile, orgId)
  }, [onSessionConfigChange])

  // ── Locate on Board ──
  const handleLocateOnBoard = useCallback((taskId: string) => {
    if (panelState === 'maximized') setPanelState('open')
    setTimeout(() => {
      const el = document.querySelector(`[data-task-id="${taskId}"]`)
      el?.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'nearest' })
    }, 100)
  }, [panelState])

  // ── Session actions ──
  const handleStop = useCallback(() => {
    const targetSession = activeConversation.runtimeSession ?? activeConversation.displaySession ?? activeSession
    const targetTaskId = targetSession?.taskId ?? activeSessionId
    if (targetTaskId) onSessionStop?.(targetTaskId)
  }, [activeConversation.runtimeSession, activeConversation.displaySession, activeSession, activeSessionId, onSessionStop])

  const handleComplete = useCallback(() => {
    if (childDetailTaskId) return // child-detail: don't accidentally complete parent
    if (activeSessionId) onSessionComplete?.(activeSessionId)
  }, [activeSessionId, childDetailTaskId, onSessionComplete])

  const handleStopTask = useCallback((taskId: string) => {
    onSessionStop?.(taskId)
  }, [onSessionStop])

  const handleResume = useCallback(() => {
    const targetSession = activeConversation.runtimeSession ?? activeConversation.displaySession ?? activeSession
    const uiTaskId = activeSessionId ?? targetSession?.taskId
    const runtimeSessionId = targetSession?.resumeParentSessionId
      ?? targetSession?.parentSessionId
      ?? targetSession?.sessionId
    if (uiTaskId) {
      onSessionResume?.(uiTaskId, runtimeSessionId, targetSession?.pendingRuntimeCheckpointId)
    }
  }, [activeConversation.runtimeSession, activeConversation.displaySession, activeSession, activeSessionId, onSessionResume])

  const handleResumeTask = useCallback((taskId: string) => {
    const session = sessions.find(s => s.taskId === taskId)
    const runtimeSessionId = session?.resumeParentSessionId
      ?? session?.parentSessionId
      ?? session?.sessionId
    onSessionResume?.(taskId, runtimeSessionId, session?.pendingRuntimeCheckpointId)
  }, [sessions, onSessionResume])

  const handleCompleteTask = useCallback((taskId: string) => {
    onSessionComplete?.(taskId)
  }, [onSessionComplete])

  const handleToggleMultiSessionView = useCallback(() => {
    if (!multiSessionView && effectiveView.kind !== 'session') {
      const fallbackSessionId = activeSessionId ?? openSessions[openSessions.length - 1]?.taskId ?? null
      if (fallbackSessionId) {
        focusSession(fallbackSessionId)
      }
    }
    setMultiSessionView(prev => !prev)
  }, [multiSessionView, effectiveView.kind, activeSessionId, openSessions, focusSession])

  const dispatchSessionSend = useCallback((
    taskId: string,
    content: string,
    attachments?: OutgoingAttachmentPayload[],
    metadata?: CheckpointReplyMetadata,
  ) => {
    // Every send carries a client-generated ui_message_id so the backend can
    // deduplicate re-deliveries (WS pending-queue flush after a reconnect).
    const outgoing = metadata?.ui_message_id
      ? metadata
      : { ...(metadata ?? {}), ui_message_id: makeOptimisticUserMessageId() }
    onSessionSend(taskId, content, attachments, outgoing)
  }, [onSessionSend])

  // ── Composer send ──
  const handleComposerSend = useCallback(
    (content: string, attachments?: OutgoingAttachmentPayload[]) => {
      if (effectiveView.kind === 'secretary') {
        onSecretarySend?.(content)
        return
      }
      const targetTaskId = activeSessionId
      if (!targetTaskId) return
      const runtimeSession = activeConversation.runtimeSession ?? activeConversation.displaySession ?? activeSession
      const runtimeCheckpointId = String(runtimeSession?.pendingRuntimeCheckpointId ?? '').trim()
      let outgoingMetadata = latestPendingCheckpointReply
        ?? (runtimeCheckpointId ? { response_to_checkpoint_id: runtimeCheckpointId } : undefined)
      const checkpointReplyId = String(outgoingMetadata?.response_to_checkpoint_id ?? '').trim()
      if (!checkpointReplyId) {
        const uiMessageId = makeOptimisticUserMessageId()
        outgoingMetadata = { ...(latestPendingCheckpointReply ?? {}), ui_message_id: uiMessageId }
        const targetSession = activeConversation.displaySession ?? activeSession
        chatStore.sendMessage({
          channelId: targetSession?.channelId ?? `session:${targetTaskId}`,
          sender: 'user',
          senderName: 'You',
          content,
          metadata: { ui_message_id: uiMessageId },
        })
      }
      dispatchSessionSend(targetTaskId, content, attachments, outgoingMetadata)
    },
    [effectiveView.kind, activeSessionId, activeConversation.runtimeSession, activeConversation.displaySession, activeSession, latestPendingCheckpointReply, chatStore, dispatchSessionSend, onSecretarySend],
  )

  // ── MessageList send (checkpoint replies) ──
  const handleMessageSend = useCallback(
    (content: string, taskId?: string, metadata?: CheckpointReplyMetadata) => {
      const targetTaskId = taskId ?? activeSessionId
      if (!targetTaskId) return
      dispatchSessionSend(targetTaskId, content, undefined, metadata)
    },
    [activeSessionId, dispatchSessionSend],
  )

  const handleOpenWorkItemSession = useCallback((executionTurnId: string) => {
    const session = sessions.find(s => (
      s.taskId === executionTurnId
      || s.runtimeTaskId === executionTurnId
      || s.executionTurnId === executionTurnId
    ))
    if (session) {
      setChildDetailTaskId(session.taskId)
      setPanelState('open')
      setPanelTab('chat')
    }
  }, [sessions])

  const handleWorkItemClick = useCallback((executionTurnId: string) => {
    // Always forward to ExecutionPanel. The panel's lookup matches against
    // ``sessions[].roleWorkItems[role].workItems[].executionTurnId`` so it
    // works even when the runtime task is shared with the user's primary
    // chat (leader's intake / review turns) and therefore doesn't appear as
    // a standalone session in ``sessionStore.sessions``.
    if (onOpenExecutionPanel) {
      onOpenExecutionPanel(executionTurnId)
      return
    }
    handleOpenWorkItemSession(executionTurnId)
  }, [handleOpenWorkItemSession, onOpenExecutionPanel])

  const isSecretary = effectiveView.kind === 'secretary'
  const channelName = isSecretary ? t('workspace.channel.secretary') : activeSession ? activeSession.title : t('workspace.channel.activity')

  return (
    <div className={`workspace-page${panelState === 'maximized' ? ' panel-maximized' : ''}`}>
      {/* Comms panel — floating overlay so it stays visible
          regardless of which workspace column is currently active /
          maximized. Pinned to top-right under any global header. */}
      {/* CommsPanel is now rendered inside ContextPanel as a tab */}
      {/* Left column: Session Navigator */}
      <SessionSidebar
        sessions={sidebarSessions}
        activeSessionId={isSecretary ? null : activeSessionId}
        activeChannel={isSecretary ? secretaryChannelId : null}
        secretaryChannelId={secretaryChannelId}
        unreadCounts={unreadCounts}
        onSelect={handleSelectSession}
        onCreateSession={onCreateSession}
        onDeleteSession={onDeleteSession}
        onSelectSecretary={onSecretarySend ? handleSelectSecretary : undefined}
      />

      {/* Middle column: Kanban Board (hidden when panel maximized) */}
      {panelState !== 'maximized' && (
        <div className="workspace-board">
          {agents.length > 0 && <AgentStatusBar agents={agents} tasks={boardStore.tasks} />}
          {isCompanyMode ? (
            boardStore.activeBoard && activeSession && (
              <BoardTitleEditor
                key={activeSession.taskId}
                boardColor={boardStore.activeBoard.color}
                title={boardStore.activeBoard.name}
                onCommit={(next) => onTitleChange(activeSession.taskId, next)}
              />
            )
          ) : boardStore.boards.length > 1 && (
            <BoardSelector
              boards={boardStore.boards}
              activeBoardId={boardStore.activeBoardId}
              onSelect={handleBoardSelect}
            />
          )}
          {isCompanyMode && !boardStore.activeBoard ? (
            <div className="kanban-empty-state">
              <p>{t('workspace.selectRuntimeSession')}</p>
            </div>
          ) : (
            <>
              <KanbanBoardView
                columns={boardStore.activeBoardColumns}
                tasksByColumn={filteredTasksByColumn}
                agents={agents}
                officeMap={officeMap}
                store={boardStore}
                companyMode={isCompanyMode}
                selectedTaskId={effectiveView.kind === 'task-detail' ? effectiveView.taskId : activeSessionId}
                onCardClick={handleCardClick}
                onStartTask={handleStartTask}
                onQuickCreate={handleQuickCreate}
                onMoveTask={onMoveTask}
              />
              {isCompanyMode && boardStore.activeBoard && boardStore.tasks.filter(t => t.boardId === boardStore.activeBoardId).length === 0 && (
                <div className="kanban-empty-state kanban-empty-state-inline">
                  <p>{t('workspace.noWorkItems')}</p>
                </div>
              )}
            </>
          )}
        </div>
      )}

      {/* Right column: Context Panel */}
      <ContextPanel
        panelState={panelState}
        width={width}
        onResizeMouseDown={handleMouseDown}
        isResizing={isResizing}
        activeView={effectiveView}
        activeSession={activeSession}
        activeTask={activeTask}
        linkedTaskSession={linkedTaskSession}
        linkedTaskSessionMessages={linkedTaskSessionMessages}
        childDetailSession={childDetailSession}
        messages={activeMessages}
        childDetailMessages={childDetailMessages}
        allSessions={sessions}
        openSessions={openSessions}
        openSessionMessages={openSessionMessages}
        openSessionChildren={openSessionChildren}
        agents={agents}
        childSessions={childSessions}
        execMode={execMode}
        taskPreferredAgent={taskPreferredAgent}
        savedOrgsList={savedOrgsList ?? null}
        activeSavedOrg={activeSavedOrg ?? null}
        onSavedOrgsList={onSavedOrgsList}
        onSavedOrgLoad={onSavedOrgLoad}
        canShowAgentsTab={canShowAgentsTab}
        channelId={channelId}
        channelName={channelName}
        secretaryChannelId={secretaryChannelId}
        unreadCounts={unreadCounts}
        multiSessionView={multiSessionView}
        panelTab={panelTab}
        onPanelTabChange={setPanelTab}
        commsState={commsState ?? null}
        commsMessage={commsMessage ?? null}
        onCommsRefresh={onCommsRefresh ? () => onCommsRefresh({ session_id: activeSession?.sessionId || undefined, project_id: projectId || undefined }) : undefined}
        onCommsReadMessage={onCommsReadMessage}
        orgInfoData={orgInfoData ?? null}
        canShowTeamTab={canShowTeamTab}
        onTeamStopRun={activeSessionId ? () => onSessionStop?.(activeSessionId) : undefined}
        onTitleChange={onTitleChange}
        onSessionConfigChange={handleSessionConfigChange}
        onSessionTaskAgentChange={onSessionTaskAgentChange}
        onContinueInNewChat={onContinueInNewChat}
        onStop={handleStop}
        onComplete={handleComplete}
        onResume={handleResume}
        onResumeTask={handleResumeTask}
        onStopTask={handleStopTask}
        onCompleteTask={handleCompleteTask}
        onLocateOnBoard={handleLocateOnBoard}
        onBackToParent={() => setChildDetailTaskId(null)}
        onCloseTaskDetail={() => setActiveView({ kind: 'activity' })}
        onOpenChildDetail={handleOpenWorkItemSession}
        onOpenExecutionPanel={onOpenExecutionPanel}
        onSelectSessionTab={focusSession}
        onCloseSessionTab={handleCloseSessionView}
        onToggleMultiSessionView={handleToggleMultiSessionView}
        onCollapse={() => setPanelState('collapsed')}
        onExpand={() => setPanelState('open')}
        onMaximize={() => setPanelState(prev => prev === 'maximized' ? 'open' : 'maximized')}
        onComposerSend={handleComposerSend}
        onMessageSend={handleMessageSend}
        onSessionSend={dispatchSessionSend}
        onWorkItemClick={handleWorkItemClick}
        onWorkItemOpenSession={handleOpenWorkItemSession}
        onMarkRead={handleMarkRead}
        onSessionMarkRead={handleMarkSessionRead}
        onLoadSessionHistory={requestSessionHistory}
        isSessionHistoryLoading={isSessionHistoryLoading}
      />
    </div>
  )
}
