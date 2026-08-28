import { useCallback, useMemo, useReducer, useState } from 'react'
import type { ProgressEntry, Session, WorkItemProgressEntry } from '../types/kanban'
import {
  appendProgressEntry,
  appendWorkItemProgressEntry,
  normalizeProgressLog,
  normalizeWorkItemLog,
} from '../lib/progressLog'
import { canonicalizeSessionExecutionIdentity } from '../lib/sessionIdentity'

const MAX_PROGRESS_ENTRIES = 100
const TERMINAL_SESSION_STATUSES = new Set(['done', 'failed', 'cancelled'])

export interface SessionStoreInitOptions {
  /**
   * Lightweight project indexes are intentionally partial: they carry sidebar
   * rows, but not the complete runtime projection used by Execution Progress.
   * When applying one to the current project, keep existing sessions that are
   * missing from the index until the following full collab_sync arrives.
   */
  preserveExistingWhenIncomingPartial?: boolean
  preserveActiveWhenMissing?: boolean
}

export function mergePartialSessionSnapshot(
  existingSessions: Session[],
  incomingSessions: Session[],
  projectId: string,
): Session[] {
  const normalizedProjectId = projectId || 'default'
  const incomingByTaskId = new Map(
    incomingSessions
      .filter(session => session.projectId === normalizedProjectId)
      .map(session => [session.taskId, session] as const),
  )
  const seen = new Set<string>()
  const merged: Session[] = []

  for (const existing of existingSessions) {
    if (existing.projectId !== normalizedProjectId) continue
    const incoming = incomingByTaskId.get(existing.taskId)
    if (incoming) {
      merged.push(incoming)
      seen.add(existing.taskId)
    } else {
      merged.push(existing)
    }
  }

  for (const incoming of incomingSessions) {
    if (incoming.projectId !== normalizedProjectId || seen.has(incoming.taskId)) continue
    merged.push(incoming)
    seen.add(incoming.taskId)
  }

  return merged
}

export function preserveSessionNativePermission(
  existing: Session,
  incoming: Partial<Session>,
): Pick<Session, 'nativeApprovalLevel' | 'nativePermissionScopeId'> {
  return {
    nativeApprovalLevel: incoming.nativeApprovalLevel ?? existing.nativeApprovalLevel,
    nativePermissionScopeId: incoming.nativePermissionScopeId ?? existing.nativePermissionScopeId,
  }
}

type SessionAction =
  | { type: 'SET'; sessions: Session[]; reset?: boolean }
  | { type: 'ADD'; session: Session }
  | { type: 'UPDATE'; taskId: string; partial: Partial<Session> }
  | { type: 'DELETE'; taskId: string }
  | { type: 'APPEND_PROGRESS'; taskId: string; entry: ProgressEntry }
  | { type: 'APPEND_DRAFT'; taskId: string; text: string; timestamp: number; iteration?: number; turnId?: string }
  | { type: 'CLEAR_DRAFT'; taskId: string }
  | { type: 'CLEAR_PROGRESS'; taskId: string }
  | { type: 'APPEND_WORK_ITEM_PROGRESS'; taskId: string; entry: WorkItemProgressEntry }
  | { type: 'SET_COMPANY_RUNTIME'; taskId: string; isCompanyRuntime: boolean }

function sessionReducer(state: Session[], action: SessionAction): Session[] {
  const normalizeSession = (session: Session): Session => canonicalizeSessionExecutionIdentity({
    ...session,
    progressLog: normalizeProgressLog(session.progressLog ?? [], MAX_PROGRESS_ENTRIES),
    workItemLog: normalizeWorkItemLog(session.workItemLog ?? [], MAX_PROGRESS_ENTRIES),
  })

  const isCompanyLikeSession = (session: Partial<Session>): boolean => {
    const mode = String(session.execMode ?? '').trim().toLowerCase()
    return mode === 'company'
      || mode === 'org'
      || mode === 'custom'
      || !!session.isCompanyRuntime
      || !!session.parentSessionId
      || !!session.workItemProjectionId
      || !!session.roleWorkItems
      || !!session.executorRoleWorkItems
      || !!session.orgId
  }

  const isExplicitTaskSession = (session: Partial<Session>): boolean => {
    const mode = String(session.execMode ?? '').trim().toLowerCase()
    const profile = String(session.companyProfile ?? '').trim().toLowerCase()
    const orgId = String(session.orgId ?? '').trim()
    return mode === 'task'
      && (!profile || profile === 'corporate')
      && !orgId
      && !session.isCompanyRuntime
      && !session.parentSessionId
      && !session.workItemProjectionId
      && !session.workItemRoleId
      && !session.roleWorkItems
      && !session.executorRoleWorkItems
  }

  const mergeExecMode = (incoming: Session, existing: Session): string | undefined => {
    const next = incoming.execMode
    const previous = existing.execMode
    if (!next) return previous
    if (
      next === 'task'
      && previous
      && previous !== 'task'
      && !isExplicitTaskSession(incoming)
      && (isCompanyLikeSession(incoming) || isCompanyLikeSession(existing))
    ) {
      return previous
    }
    return next
  }

  const mergeTaskAgent = (
    incoming: Session,
    existing: Session,
    field: 'preferredAgent' | 'selectedExecutionAgent',
  ): Session[typeof field] => {
    const next = incoming[field]
    const previous = existing[field]
    if (!next) return previous
    if (next === 'native' && previous && previous !== 'native') return previous
    return next
  }

  const hasOwn = (value: object, field: keyof Session): boolean =>
    Object.prototype.hasOwnProperty.call(value, field)

  const mergeOptionalField = <K extends keyof Session>(
    incoming: Partial<Session>,
    existing: Session,
    field: K,
  ): Session[K] | undefined => (
    hasOwn(incoming, field) ? incoming[field] : existing[field]
  )

  const mergeUpdatedAt = (
    incoming: Partial<Session>,
    existing: Session,
  ): number => {
    const incomingUpdatedAt = typeof incoming.updatedAt === 'number' && Number.isFinite(incoming.updatedAt)
      ? incoming.updatedAt
      : undefined
    const existingUpdatedAt = typeof existing.updatedAt === 'number' && Number.isFinite(existing.updatedAt)
      ? existing.updatedAt
      : undefined
    if (incomingUpdatedAt === undefined) return existingUpdatedAt ?? Date.now()
    if (existingUpdatedAt === undefined) return incomingUpdatedAt
    return Math.max(existingUpdatedAt, incomingUpdatedAt)
  }

  const guardRuntimeControlRegression = (
    existing: Session,
    incoming: Partial<Session>,
  ): Partial<Session> => {
    if (existing.runtimeControlState === 'suspending' && incoming.runtimeControlState === 'running') {
      return {
        ...incoming,
        runtimeControlState: 'suspending',
        canStop: false,
        canResume: existing.canResume ?? false,
        pendingRuntimeCheckpointId: existing.pendingRuntimeCheckpointId ?? incoming.pendingRuntimeCheckpointId,
        stopIntentId: existing.stopIntentId ?? incoming.stopIntentId,
      }
    }
    return incoming
  }

  const mergeLiveRuntimeField = <T,>(
    incoming: Session,
    existing: Session,
    field: 'agentStatus' | 'currentTool' | 'displayTool' | 'toolElapsedMs' | 'lastToolSummary',
  ): T | undefined => {
    const incomingValue = incoming[field] as T | undefined
    if (incomingValue !== undefined && incomingValue !== null) return incomingValue
    const backendSaysIdle = TERMINAL_SESSION_STATUSES.has(incoming.status)
      || (
        incoming.status !== 'running'
        && incoming.runtimeControlState !== 'running'
        && incoming.runtimeControlState !== 'suspending'
        && incoming.runtimeControlState !== 'resuming'
      )
    if (backendSaysIdle) return undefined
    return existing[field] as T | undefined
  }

  switch (action.type) {
    case 'SET': {
      // Merge incoming sessions with existing runtime state so that
      // collab_sync reinit doesn't wipe live draft text, progress logs,
      // agent status, token metrics, etc.
      if (action.reset) {
        return action.sessions.map(normalizeSession)
      }
      const existingByTaskId = new Map(state.map(s => [s.taskId, s]))
      return action.sessions.map(incoming => {
        const existing = existingByTaskId.get(incoming.taskId)
        if (!existing) return normalizeSession(incoming)
        const guardedRuntimeControl = guardRuntimeControlRegression(existing, incoming)
        // Preserve runtime-only fields that the backend snapshot doesn't carry
        return normalizeSession({
          ...incoming,
          updatedAt: mergeUpdatedAt(incoming, existing),
          execMode: mergeExecMode(incoming, existing),
          preferredAgent: mergeTaskAgent(incoming, existing, 'preferredAgent'),
          ...preserveSessionNativePermission(existing, incoming),
          draftAssistantText: existing.draftAssistantText ?? incoming.draftAssistantText,
          draftUpdatedAt: existing.draftUpdatedAt ?? incoming.draftUpdatedAt,
          draftIteration: existing.draftIteration ?? incoming.draftIteration,
          draftTurnId: existing.draftTurnId ?? incoming.draftTurnId,
          agentStatus: mergeLiveRuntimeField<string>(incoming, existing, 'agentStatus'),
          currentTool: mergeLiveRuntimeField<string>(incoming, existing, 'currentTool'),
          displayTool: mergeLiveRuntimeField<string>(incoming, existing, 'displayTool'),
          toolElapsedMs: mergeLiveRuntimeField<number>(incoming, existing, 'toolElapsedMs'),
          lastToolSummary: mergeLiveRuntimeField<string>(incoming, existing, 'lastToolSummary'),
          contextTokens: existing.contextTokens ?? incoming.contextTokens,
          contextWindow: existing.contextWindow ?? incoming.contextWindow,
          contextRemainingPct: existing.contextRemainingPct ?? incoming.contextRemainingPct,
          inputTokens: existing.inputTokens ?? incoming.inputTokens,
          outputTokens: existing.outputTokens ?? incoming.outputTokens,
          totalTokens: existing.totalTokens ?? incoming.totalTokens,
          turnCostUsd: existing.turnCostUsd ?? incoming.turnCostUsd,
          sessionCostUsd: existing.sessionCostUsd ?? incoming.sessionCostUsd,
          pendingPermissionCount: existing.pendingPermissionCount ?? incoming.pendingPermissionCount,
          drainMode: existing.drainMode ?? incoming.drainMode,
          workItemProjectionId: incoming.workItemProjectionId ?? existing.workItemProjectionId,
          workItemTurnType: incoming.workItemTurnType ?? existing.workItemTurnType,
          companyProfile: mergeOptionalField(incoming, existing, 'companyProfile'),
          orgId: mergeOptionalField(incoming, existing, 'orgId'),
          workItemRoleId: incoming.workItemRoleId ?? existing.workItemRoleId,
          workItemRoleName: incoming.workItemRoleName ?? existing.workItemRoleName,
          workItemGate: incoming.workItemGate ?? existing.workItemGate,
          roleWorkItems: incoming.roleWorkItems ?? existing.roleWorkItems,
          executorRoleWorkItems: incoming.executorRoleWorkItems ?? existing.executorRoleWorkItems,
          employeeAssignment: incoming.employeeAssignment ?? existing.employeeAssignment,
          selectedExecutionAgent: mergeTaskAgent(incoming, existing, 'selectedExecutionAgent'),
          runtimeControlState: guardedRuntimeControl.runtimeControlState ?? existing.runtimeControlState,
          canStop: guardedRuntimeControl.canStop ?? existing.canStop,
          canResume: guardedRuntimeControl.canResume ?? existing.canResume,
          resumeParentSessionId: incoming.resumeParentSessionId ?? existing.resumeParentSessionId,
          pendingRuntimeCheckpointId: guardedRuntimeControl.pendingRuntimeCheckpointId ?? existing.pendingRuntimeCheckpointId,
          stopIntentId: guardedRuntimeControl.stopIntentId ?? existing.stopIntentId,
          residentStatus: existing.residentStatus ?? incoming.residentStatus,
          actionableInboxCount: existing.actionableInboxCount ?? incoming.actionableInboxCount,
          protocolBacklogCount: existing.protocolBacklogCount ?? incoming.protocolBacklogCount,
          notificationBacklogCount: existing.notificationBacklogCount ?? incoming.notificationBacklogCount,
          latestNotification: existing.latestNotification ?? incoming.latestNotification,
          latestPreview: incoming.latestPreview ?? existing.latestPreview,
          latestSender: incoming.latestSender ?? existing.latestSender,
          latestMessageId: incoming.latestMessageId ?? existing.latestMessageId,
          indexLoaded: incoming.indexLoaded ?? existing.indexLoaded,
          detailLoaded: existing.detailLoaded ?? incoming.detailLoaded,
          fullLoaded: existing.fullLoaded ?? incoming.fullLoaded,
          hasMore: incoming.hasMore ?? existing.hasMore,
          summaryHasMore: incoming.summaryHasMore ?? existing.summaryHasMore,
          fullHasMore: incoming.fullHasMore ?? existing.fullHasMore,
          detailLoading: incoming.detailLoading ?? existing.detailLoading,
          detailError: incoming.detailError ?? existing.detailError,
          viewGeneration: incoming.viewGeneration ?? existing.viewGeneration,
          progressLog: (existing.progressLog && existing.progressLog.length > 0)
            ? existing.progressLog
            : incoming.progressLog,
          workItemLog: (existing.workItemLog && existing.workItemLog.length > 0)
            ? existing.workItemLog
            : incoming.workItemLog,
          lastRuntimeEventType: existing.lastRuntimeEventType ?? incoming.lastRuntimeEventType,
          activeSubagents: existing.activeSubagents ?? incoming.activeSubagents,
          permissionRequests: existing.permissionRequests ?? incoming.permissionRequests,
          // Prefer the incoming snapshot, but fall back to existing when the
          // backend transiently omits the field mid-turn (otherwise the
          // "Received From" / "Passed To" panels flicker on/off while the
          // role is actively working).
          handoffContext: incoming.handoffContext ?? existing.handoffContext,
          handoffTo: incoming.handoffTo ?? existing.handoffTo,
        })
      })
    }
    case 'ADD':
      const nextSession = normalizeSession(action.session)
      if (state.some(s => s.taskId === action.session.taskId)) {
        return state.map(s =>
          s.taskId === action.session.taskId ? normalizeSession({
            ...s,
            ...nextSession,
            progressLog: nextSession.progressLog.length > 0 ? nextSession.progressLog : s.progressLog,
            workItemLog: (nextSession.workItemLog ?? []).length > 0 ? nextSession.workItemLog : s.workItemLog,
            messageCount: Math.max(s.messageCount ?? 0, nextSession.messageCount ?? 0),
            updatedAt: Math.max(s.updatedAt ?? 0, nextSession.updatedAt ?? 0),
            execMode: mergeExecMode(nextSession, s),
            preferredAgent: mergeTaskAgent(nextSession, s, 'preferredAgent'),
            ...preserveSessionNativePermission(s, nextSession),
            workItemProjectionId: nextSession.workItemProjectionId ?? s.workItemProjectionId,
            workItemTurnType: nextSession.workItemTurnType ?? s.workItemTurnType,
            companyProfile: mergeOptionalField(nextSession, s, 'companyProfile'),
            orgId: mergeOptionalField(nextSession, s, 'orgId'),
            workItemRoleId: nextSession.workItemRoleId ?? s.workItemRoleId,
            workItemRoleName: nextSession.workItemRoleName ?? s.workItemRoleName,
            workItemGate: nextSession.workItemGate ?? s.workItemGate,
            roleWorkItems: nextSession.roleWorkItems ?? s.roleWorkItems,
            executorRoleWorkItems: nextSession.executorRoleWorkItems ?? s.executorRoleWorkItems,
            employeeAssignment: nextSession.employeeAssignment ?? s.employeeAssignment,
            selectedExecutionAgent: mergeTaskAgent(nextSession, s, 'selectedExecutionAgent'),
            handoffContext: nextSession.handoffContext ?? s.handoffContext,
            handoffTo: nextSession.handoffTo ?? s.handoffTo,
            latestPreview: nextSession.latestPreview ?? s.latestPreview,
            latestSender: nextSession.latestSender ?? s.latestSender,
            latestMessageId: nextSession.latestMessageId ?? s.latestMessageId,
            indexLoaded: nextSession.indexLoaded ?? s.indexLoaded,
            detailLoaded: nextSession.detailLoaded ?? s.detailLoaded,
            fullLoaded: nextSession.fullLoaded ?? s.fullLoaded,
            hasMore: nextSession.hasMore ?? s.hasMore,
            summaryHasMore: nextSession.summaryHasMore ?? s.summaryHasMore,
            fullHasMore: nextSession.fullHasMore ?? s.fullHasMore,
            detailLoading: nextSession.detailLoading ?? s.detailLoading,
            detailError: nextSession.detailError ?? s.detailError,
            viewGeneration: nextSession.viewGeneration ?? s.viewGeneration,
          }) : s,
        )
      }
      return [nextSession, ...state]
    case 'UPDATE':
      return state.map(s =>
        s.taskId === action.taskId
          ? (() => {
            const guarded = guardRuntimeControlRegression(s, action.partial)
            return normalizeSession({
              ...s,
              ...guarded,
              updatedAt: mergeUpdatedAt(guarded, s),
              runtimeControlState: guarded.runtimeControlState ?? s.runtimeControlState,
              canStop: guarded.canStop ?? s.canStop,
              canResume: guarded.canResume ?? s.canResume,
              resumeParentSessionId: guarded.resumeParentSessionId ?? s.resumeParentSessionId,
              pendingRuntimeCheckpointId: guarded.pendingRuntimeCheckpointId ?? s.pendingRuntimeCheckpointId,
              stopIntentId: guarded.stopIntentId ?? s.stopIntentId,
              roleWorkItems: guarded.roleWorkItems ?? s.roleWorkItems,
              executorRoleWorkItems: guarded.executorRoleWorkItems ?? s.executorRoleWorkItems,
              execMode: mergeOptionalField(guarded, s, 'execMode'),
              companyProfile: mergeOptionalField(guarded, s, 'companyProfile'),
              orgId: mergeOptionalField(guarded, s, 'orgId'),
              isCompanyRuntime: guarded.isCompanyRuntime ?? s.isCompanyRuntime,
              workItemProjectionId: guarded.workItemProjectionId ?? s.workItemProjectionId,
              workItemTurnType: guarded.workItemTurnType ?? s.workItemTurnType,
              workItemRoleId: guarded.workItemRoleId ?? s.workItemRoleId,
              workItemRoleName: guarded.workItemRoleName ?? s.workItemRoleName,
              workItemGate: guarded.workItemGate ?? s.workItemGate,
              employeeAssignment: guarded.employeeAssignment ?? s.employeeAssignment,
              selectedExecutionAgent: guarded.selectedExecutionAgent ?? s.selectedExecutionAgent,
              ...preserveSessionNativePermission(s, guarded),
              activeSubagents: guarded.activeSubagents ?? s.activeSubagents,
              permissionRequests: guarded.permissionRequests ?? s.permissionRequests,
              // Live-only runtime counters: status_snapshot/cost_update carry real
              // values, but session_detail refreshes read them from persisted task
              // metadata (runtime_v2), which never stores them — so those refreshes
              // arrive as undefined/null. Without this guard the plain `...guarded`
              // spread would clobber the live values on every post-event refresh,
              // making the context-usage ring flip between the real % and empty.
              contextTokens: guarded.contextTokens ?? s.contextTokens,
              contextWindow: guarded.contextWindow ?? s.contextWindow,
              contextRemainingPct: guarded.contextRemainingPct ?? s.contextRemainingPct,
              inputTokens: guarded.inputTokens ?? s.inputTokens,
              outputTokens: guarded.outputTokens ?? s.outputTokens,
              totalTokens: guarded.totalTokens ?? s.totalTokens,
              turnCostUsd: guarded.turnCostUsd ?? s.turnCostUsd,
              sessionCostUsd: guarded.sessionCostUsd ?? s.sessionCostUsd,
              ...(action.partial.progressLog ? { progressLog: action.partial.progressLog } : {}),
              ...(action.partial.workItemLog ? { workItemLog: action.partial.workItemLog } : {}),
            })
          })()
          : s,
      )
    case 'DELETE':
      return state.filter(s => s.taskId !== action.taskId)
    case 'APPEND_PROGRESS':
      return state.map(s => {
        if (s.taskId !== action.taskId) return s
        return {
          ...s,
          progressLog: appendProgressEntry(s.progressLog, action.entry, MAX_PROGRESS_ENTRIES),
          updatedAt: Math.max(s.updatedAt, action.entry.timestamp),
        }
      })
    case 'APPEND_DRAFT':
      return state.map(s => {
        if (s.taskId !== action.taskId) return s
        const nextTurnId = action.turnId ?? s.draftTurnId
        const resetText = !!action.turnId && !!s.draftTurnId && action.turnId !== s.draftTurnId
        return {
          ...s,
          draftAssistantText: `${resetText ? '' : s.draftAssistantText ?? ''}${action.text}`,
          draftUpdatedAt: action.timestamp,
          draftIteration: action.iteration ?? s.draftIteration,
          draftTurnId: nextTurnId,
          updatedAt: Math.max(s.updatedAt, action.timestamp),
        }
      })
    case 'CLEAR_DRAFT':
      return state.map(s =>
        s.taskId === action.taskId
          ? {
              ...s,
              draftAssistantText: undefined,
              draftUpdatedAt: undefined,
              draftIteration: undefined,
              draftTurnId: undefined,
            }
          : s,
      )
    case 'CLEAR_PROGRESS':
      return state.map(s =>
        s.taskId === action.taskId ? { ...s, progressLog: [] } : s,
      )
    case 'APPEND_WORK_ITEM_PROGRESS':
      return state.map(s => {
        if (s.taskId !== action.taskId) return s
        return {
          ...s,
          workItemLog: appendWorkItemProgressEntry(s.workItemLog ?? [], action.entry, MAX_PROGRESS_ENTRIES),
        }
      })
    case 'SET_COMPANY_RUNTIME':
      return state.map(s =>
        s.taskId === action.taskId ? { ...s, isCompanyRuntime: action.isCompanyRuntime } : s,
      )
    default:
      return state
  }
}

export interface SessionStoreState {
  scopeProjectId: string
  sessions: Session[]
  activeSessionId: string | null
  activeSession: Session | null
  createSession: (session: Session) => void
  deleteSession: (taskId: string) => void
  setActiveSession: (taskId: string | null) => void
  updateSession: (taskId: string, partial: Partial<Session>) => void
  appendProgress: (taskId: string, entry: ProgressEntry) => void
  appendDraft: (taskId: string, text: string, iteration?: number, turnId?: string) => void
  clearDraft: (taskId: string) => void
  clearProgress: (taskId: string) => void
  appendWorkItemProgress: (taskId: string, entry: WorkItemProgressEntry) => void
  setCompanyRuntime: (taskId: string, isCompanyRuntime: boolean) => void
  initFromBackend: (projectId: string, sessions: Session[], options?: SessionStoreInitOptions) => void
}

export function useSessionStore(): SessionStoreState {
  const [sessions, dispatch] = useReducer(sessionReducer, [])
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null)
  const [scopeProjectId, setScopeProjectId] = useState<string>('default')

  const activeSession = useMemo(
    () => sessions.find(s => s.taskId === activeSessionId) ?? null,
    [sessions, activeSessionId],
  )

  const createSession = useCallback((session: Session) => {
    const sessionProjectId = session.projectId
    if (sessionProjectId !== scopeProjectId) return
    dispatch({ type: 'ADD', session })
  }, [scopeProjectId])

  const deleteSession = useCallback((taskId: string) => {
    dispatch({ type: 'DELETE', taskId })
  }, [])

  const setActiveSession = useCallback((taskId: string | null) => {
    setActiveSessionId(taskId)
  }, [])

  const updateSession = useCallback(
    (taskId: string, partial: Partial<Session>) => {
      dispatch({ type: 'UPDATE', taskId, partial })
    },
    [],
  )

  const appendProgress = useCallback(
    (taskId: string, entry: ProgressEntry) => {
      dispatch({ type: 'APPEND_PROGRESS', taskId, entry })
    },
    [],
  )

  const appendDraft = useCallback(
    (taskId: string, text: string, iteration?: number, turnId?: string) => {
      dispatch({ type: 'APPEND_DRAFT', taskId, text, iteration, turnId, timestamp: Date.now() })
    },
    [],
  )

  const clearDraft = useCallback(
    (taskId: string) => {
      dispatch({ type: 'CLEAR_DRAFT', taskId })
    },
    [],
  )

  const clearProgress = useCallback(
    (taskId: string) => {
      dispatch({ type: 'CLEAR_PROGRESS', taskId })
    },
    [],
  )

  const appendWorkItemProgress = useCallback(
    (taskId: string, entry: WorkItemProgressEntry) => {
      dispatch({ type: 'APPEND_WORK_ITEM_PROGRESS', taskId, entry })
    },
    [],
  )

  const setCompanyRuntime = useCallback(
    (taskId: string, isCompanyRuntime: boolean) => {
      dispatch({ type: 'SET_COMPANY_RUNTIME', taskId, isCompanyRuntime })
    },
    [],
  )

  const initFromBackend = useCallback((
    projectId: string,
    backendSessions: Session[],
    options?: SessionStoreInitOptions,
  ) => {
    const nextProjectId = projectId || 'default'
    const projectChanged = nextProjectId !== scopeProjectId
    const scopedSessions = options?.preserveExistingWhenIncomingPartial && !projectChanged
      ? mergePartialSessionSnapshot(sessions, backendSessions, nextProjectId)
      : backendSessions.filter(session => session.projectId === nextProjectId)
    setScopeProjectId(nextProjectId)
    dispatch({ type: 'SET', sessions: scopedSessions, reset: projectChanged })
    // Preserve activeSessionId if session still exists, else clear (#9)
    setActiveSessionId(prev =>
      !projectChanged
        && prev
        && (
          scopedSessions.some(s => s.taskId === prev)
          || !!options?.preserveActiveWhenMissing
        )
        ? prev
        : null,
    )
  }, [scopeProjectId, sessions])

  return useMemo(() => ({
    scopeProjectId,
    sessions,
    activeSessionId,
    activeSession,
    createSession,
    deleteSession,
    setActiveSession,
    updateSession,
    appendProgress,
    appendDraft,
    clearDraft,
    clearProgress,
    appendWorkItemProgress,
    setCompanyRuntime,
    initFromBackend,
  }), [scopeProjectId, sessions, activeSessionId, activeSession,
       createSession, deleteSession, setActiveSession,
       updateSession, appendProgress, appendDraft, clearDraft, clearProgress,
       appendWorkItemProgress, setCompanyRuntime, initFromBackend])
}
