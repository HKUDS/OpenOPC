import type { ChatMessage } from '../types/chat'
import type { ProgressEntry, Session } from '../types/kanban'
import { getContextUsageMetrics } from './contextUsage'
import { isSessionWorking } from './sessionRuntime'
import { stableMessageTimelineKey, stableResultDeliveryKey } from './messageTimelineIdentity'

const CONTEXT_TOKENS_RE = /(\d[\d,]*)\s*\/\s*(\d[\d,]*)\s+tokens/i
const USED_PCT_RE = /(\d{1,3})%\s*used/i
const REMAINING_PCT_RE = /(\d{1,3})%\s*remaining/i

export function isMessageVisibleAtDetailLevel(
  message: ChatMessage,
  detailLevel: 'summary' | 'full',
): boolean {
  if (detailLevel === 'full') return true
  return String(message.metadata?.detail_visibility ?? 'summary').trim() !== 'full'
}

function compactWhitespace(value: string): string {
  return value.replace(/\s+/g, ' ').trim()
}

function normalizeResultContentFallback(content: string): string {
  let normalized = String(content || '')
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n')
    .split('\n')
    .map(line => line.trimEnd())
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()

  // Some result mirrors add one explicit Markdown narrative label. Only
  // remove that anchored wrapper; a colon later in Markdown (for example
  // "**Work Item 1: ..." or "- ID: ...") is message content, not a title.
  for (;;) {
    const markdownTitle = normalized.match(/^\*\*([^\r\n]{8,160}?)\*\*:(?:[ \t]+|\r?\n+)([\s\S]+)$/)
    if (!markdownTitle) break
    const body = markdownTitle[2].trim()
    if (body.length < 80 || body === normalized) break
    normalized = body
  }

  const paragraphs = normalized.split(/\n{2,}/).map(part => part.trim()).filter(Boolean)
  if (paragraphs.length > 1 && /^Verification:\s/i.test(paragraphs[paragraphs.length - 1])) {
    normalized = paragraphs.slice(0, -1).join('\n\n').trim()
  }
  return compactWhitespace(normalized).slice(0, 2000)
}

function resultSurfacePriority(message: ChatMessage): number {
  const meta = (message.metadata ?? {}) as Record<string, unknown>
  const transcriptKind = String(meta.transcript_kind ?? meta.kind ?? '').trim()
  switch (transcriptKind) {
    case 'child_task_result':
      return 80
    case 'child_task_result_retry':
      return 79
    case 'company_role_result':
      return 75
    case 'company_role_result_retry':
      return 74
    case 'child_result':
      return 70
    case 'runtime_v2_assistant':
      return 60
    case 'runtime_v2_company_assistant':
      return 20
    case 'top_level_reply':
      return 40
    default:
      break
  }
  if (String(meta.kind ?? '').trim() === 'worker_notification') {
    return 20
  }
  return 0
}

function normalizedResultContentLength(message: ChatMessage): number {
  return String(message.content ?? '')
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n')
    .split('\n')
    .map(line => line.trimEnd())
    .join('\n')
    .trim()
    .length
}

function resultAuthorityScore(message: ChatMessage): number {
  const metadata = (message.metadata ?? {}) as Record<string, unknown>
  let score = 0
  if (String(metadata.source ?? '').trim() === 'engine') score += 4
  if (String(metadata.source_result_message_id ?? '').trim()) score += 2
  if (metadata.authoritative_output === true || metadata.canonical_result === true) score += 1
  return score
}

function compareResultSurfacePreference(left: ChatMessage, right: ChatMessage): number {
  const numericComparisons: Array<[number, number]> = [
    [resultSurfacePriority(left), resultSurfacePriority(right)],
    [resultAuthorityScore(left), resultAuthorityScore(right)],
    [normalizedResultContentLength(left), normalizedResultContentLength(right)],
  ]
  for (const [leftValue, rightValue] of numericComparisons) {
    if (leftValue !== rightValue) return leftValue > rightValue ? 1 : -1
  }
  const idComparison = left.id.localeCompare(right.id)
  if (idComparison !== 0) return idComparison
  const contentComparison = left.content.localeCompare(right.content)
  if (contentComparison !== 0) return contentComparison
  // Result chronology is later rewritten to the earliest underlying delivery.
  // Timestamp is therefore safe only after immutable identity/content have
  // tied; it must never be able to flip the selected surface on replay.
  if (left.timestamp === right.timestamp) return 0
  return left.timestamp > right.timestamp ? 1 : -1
}

export function resultSurfaceDedupeKey(message: ChatMessage): string {
  if (resultSurfacePriority(message) <= 0) return ''
  const deliveryKey = stableResultDeliveryKey(message)
  if (deliveryKey) return `result:${deliveryKey}`

  const content = normalizeResultContentFallback(message.content)
  return content ? `result:${content}` : ''
}

function parseProgressNumber(value: string | undefined): number | undefined {
  if (!value) return undefined
  const normalized = value.replace(/,/g, '').trim()
  if (!normalized) return undefined
  const parsed = Number(normalized)
  return Number.isFinite(parsed) ? Math.max(0, Math.round(parsed)) : undefined
}

function clampPct(value: number | undefined): number | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value)) return undefined
  return Math.max(0, Math.min(Math.round(value), 100))
}

export function deriveCompanyRuntimeDisplayStatus(session: Session | null | undefined): string | undefined {
  if (!session) return undefined
  const execMode = String(session.execMode ?? '').trim()
  const isCompanyLike = (
    execMode === 'company'
    || execMode === 'org'
    || execMode === 'custom'
    || !!session.isCompanyRuntime
    || !!session.roleWorkItems
    || !!session.executorRoleWorkItems
  )
  if (!isCompanyLike) return undefined
  const summaries = Object.values(session.executorRoleWorkItems ?? session.roleWorkItems ?? {})
  if (summaries.length === 0) return undefined
  const statuses = summaries.map(summary => summary.aggregatedStatus)
  if (statuses.some(status => status === 'active')) return 'running'
  if (statuses.some(status => status === 'waiting')) return 'pending'
  if (statuses.some(status => status === 'pending')) return 'pending'
  if (statuses.every(status => status === 'failed')) return 'failed'
  if (statuses.every(status => status === 'done')) return 'done'
  if (statuses.some(status => status === 'done')) return 'done'
  return undefined
}

function deriveContextFromProgressLog(progressLog: ProgressEntry[] | undefined): {
  contextTokens?: number
  contextWindow?: number
  contextRemainingPct?: number
} {
  const derived: {
    contextTokens?: number
    contextWindow?: number
    contextRemainingPct?: number
  } = {}

  for (const entry of progressLog ?? []) {
    const text = `${entry.summary ?? ''} ${entry.detail ?? ''}`.trim()
    if (!text) continue

    const tokenMatch = CONTEXT_TOKENS_RE.exec(text)
    if (tokenMatch) {
      const usedTokens = parseProgressNumber(tokenMatch[1])
      const windowTokens = parseProgressNumber(tokenMatch[2])
      if (typeof usedTokens === 'number') derived.contextTokens = usedTokens
      if (typeof windowTokens === 'number' && windowTokens > 0) derived.contextWindow = windowTokens
    }

    const remainingMatch = REMAINING_PCT_RE.exec(text)
    if (remainingMatch) {
      derived.contextRemainingPct = clampPct(parseProgressNumber(remainingMatch[1]))
      continue
    }

    const usedMatch = USED_PCT_RE.exec(text)
    if (usedMatch) {
      const usedPct = clampPct(parseProgressNumber(usedMatch[1]))
      if (typeof usedPct === 'number') derived.contextRemainingPct = 100 - usedPct
    }
  }

  return derived
}

function withDerivedSessionRuntime(session: Session): Session {
  const derivedContext = deriveContextFromProgressLog(session.progressLog)
  const contextTokens = session.contextTokens ?? derivedContext.contextTokens
  const contextWindow = session.contextWindow ?? derivedContext.contextWindow
  const contextRemainingPct = session.contextRemainingPct ?? derivedContext.contextRemainingPct

  if (
    contextTokens === session.contextTokens
    && contextWindow === session.contextWindow
    && contextRemainingPct === session.contextRemainingPct
  ) {
    return session
  }

  return {
    ...session,
    contextTokens,
    contextWindow,
    contextRemainingPct,
  }
}

function uniqueSessionsByTaskId(sessions: Session[]): Session[] {
  const seen = new Set<string>()
  const unique: Session[] = []
  for (const session of sessions) {
    if (seen.has(session.taskId)) continue
    seen.add(session.taskId)
    unique.push(session)
  }
  return unique
}

function hasWorkItemRoleIdentity(session: Session | null | undefined): boolean {
  return !!(
    String(session?.workItemRoleId ?? '').trim()
    || String(session?.workItemRoleName ?? '').trim()
  )
}

export function getWorkItemChildSessions(activeSession: Session | null, sessions: Session[]): Session[] {
  if (!activeSession || activeSession.mode === 'child') return []

  const parentKeys = new Set<string>()
  if (activeSession.sessionId) parentKeys.add(activeSession.sessionId)
  parentKeys.add(activeSession.taskId)
  const activeOriginTaskId = String(activeSession.originTaskId ?? activeSession.taskId ?? '').trim()

  const executionTurnOrder = new Map<string, number>()
  for (const entry of activeSession.workItemLog ?? []) {
    const taskId = entry.executionTurnId || entry.runtimeTaskId
    if (taskId && !executionTurnOrder.has(taskId)) {
      executionTurnOrder.set(taskId, executionTurnOrder.size)
    }
  }

  const seen = new Set<string>()
  const matches = sessions.filter((session) => {
    if (session.taskId === activeSession.taskId) return false
    if (seen.has(session.taskId)) return false

    const linkedByParent = !!session.parentSessionId && parentKeys.has(session.parentSessionId)
    const linkedByWorkItem = executionTurnOrder.has(session.taskId)
    const linkedByOrigin = !!activeOriginTaskId && String(session.originTaskId ?? '').trim() === activeOriginTaskId
    if (!linkedByParent && !linkedByWorkItem && !linkedByOrigin) return false

    seen.add(session.taskId)
    return true
  })

  return matches.sort((a, b) => {
    const aOrder = executionTurnOrder.get(a.taskId)
    const bOrder = executionTurnOrder.get(b.taskId)
    if (aOrder != null && bOrder != null && aOrder !== bOrder) return aOrder - bOrder
    if (aOrder != null && bOrder == null) return -1
    if (aOrder == null && bOrder != null) return 1
    return b.updatedAt - a.updatedAt
  })
}

export function getWorkItemRoleSessions(activeSession: Session | null, sessions: Session[]): Session[] {
  const childSessions = getWorkItemChildSessions(activeSession, sessions)
  if (!activeSession || activeSession.mode === 'child') return childSessions
  if (!hasWorkItemRoleIdentity(activeSession)) return childSessions

  return uniqueSessionsByTaskId([activeSession, ...childSessions])
}

export function getConversationPeerSessions(activeSession: Session | null, sessions: Session[]): Session[] {
  if (!activeSession || activeSession.mode === 'child') return []
  const activeSessionId = String(activeSession.sessionId ?? '').trim()
  if (!activeSessionId) return []

  const seen = new Set<string>()
  return sessions.filter((session) => {
    if (session.taskId === activeSession.taskId) return false
    if (seen.has(session.taskId)) return false
    if (String(session.sessionId ?? '').trim() !== activeSessionId) return false
    seen.add(session.taskId)
    return true
  }).sort((a, b) => b.updatedAt - a.updatedAt)
}

function isSummaryConversationSession(session: Session | null | undefined): boolean {
  const execMode = String(session?.execMode ?? '').trim()
  return execMode === 'company' || execMode === 'org' || execMode === 'custom'
}

function projectedDisplayScore(session: Session): number {
  const workingBonus = isSessionWorking(session) ? 1_000_000_000 : 0
  const draftBonus = String(session.draftAssistantText ?? '').trim() ? 10_000_000_000 : 0
  const messageBonus = Math.max(0, session.messageCount ?? 0) * 10_000
  const progressBonus = (session.progressLog?.length ?? 0) * 100
  return draftBonus + workingBonus + messageBonus + progressBonus + session.updatedAt
}

function runtimeProjectionScore(session: Session): number {
  const contextUsage = getContextUsageMetrics(session)
  const workingBonus = isSessionWorking(session) ? 100_000_000_000 : 0
  const draftBonus = String(session.draftAssistantText ?? '').trim() ? 10_000_000_000 : 0
  const contextBonus = (
    typeof contextUsage.usedPct === 'number'
    || typeof contextUsage.usedTokens === 'number'
    || typeof contextUsage.windowTokens === 'number'
  ) ? 10_000_000_000 : 0
  const tokenBonus = (
    typeof session.inputTokens === 'number'
    || typeof session.outputTokens === 'number'
    || typeof session.totalTokens === 'number'
  ) ? 1_000_000_000 : 0
  const messageBonus = Math.max(0, session.messageCount ?? 0) * 10_000
  const progressBonus = (session.progressLog?.length ?? 0) * 100
  return workingBonus + draftBonus + contextBonus + tokenBonus + messageBonus + progressBonus
}

export interface SessionConversationProjection {
  timelineSessions: Session[]
  displaySession: Session | null
  runtimeSession: Session | null
  projectedFromChild: boolean
}

export function projectSessionConversation(
  activeSession: Session | null,
  relatedSessions: Session[],
): SessionConversationProjection {
  if (!activeSession) {
    return {
      timelineSessions: [],
      displaySession: null,
      runtimeSession: null,
      projectedFromChild: false,
    }
  }

  const normalizedActiveSession = withDerivedSessionRuntime(activeSession)
  if (activeSession.mode === 'child' || relatedSessions.length === 0) {
    return {
      timelineSessions: [normalizedActiveSession],
      displaySession: normalizedActiveSession,
      runtimeSession: normalizedActiveSession,
      projectedFromChild: false,
    }
  }

  const normalizedRelatedSessions = relatedSessions.map(withDerivedSessionRuntime)
  const visibleRelatedSessions = normalizedRelatedSessions.filter((session) => session.status !== 'cancelled')
  const projectedSessions = visibleRelatedSessions.length > 0 ? visibleRelatedSessions : normalizedRelatedSessions
  const timelineSessions = uniqueSessionsByTaskId([normalizedActiveSession, ...projectedSessions])
  const summaryConversation = isSummaryConversationSession(activeSession)
  const projectedDisplaySession = [...timelineSessions].sort(
    (a, b) => projectedDisplayScore(b) - projectedDisplayScore(a),
  )[0] ?? normalizedActiveSession
  const displaySession = summaryConversation
    ? normalizedActiveSession
    : projectedDisplaySession
  const runtimeSession = [...timelineSessions].sort(
    (a, b) => runtimeProjectionScore(b) - runtimeProjectionScore(a),
  )[0] ?? displaySession

  return {
    timelineSessions,
    displaySession,
    runtimeSession,
    projectedFromChild: !summaryConversation && displaySession.taskId !== activeSession.taskId,
  }
}

export function getConversationSessionView(
  activeSession: Session | null,
  runtimeSession: Session | null,
  timelineSessions: Session[],
): Session | null {
  if (!activeSession) return null
  const normalizedActiveSession = withDerivedSessionRuntime(activeSession)
  const runtimeSource = runtimeSession ?? normalizedActiveSession
  const companyDisplayStatus = deriveCompanyRuntimeDisplayStatus(runtimeSource)
    ?? deriveCompanyRuntimeDisplayStatus(normalizedActiveSession)
  const mergedMessageCount = getConversationMessageCount(
    timelineSessions.length > 0 ? timelineSessions : [normalizedActiveSession],
  )

  return {
    ...normalizedActiveSession,
    status: companyDisplayStatus ?? (runtimeSource.status || normalizedActiveSession.status),
    assigneeIds: runtimeSource.assigneeIds.length > 0
      ? runtimeSource.assigneeIds
      : normalizedActiveSession.assigneeIds,
    agentStatus: runtimeSource.agentStatus ?? normalizedActiveSession.agentStatus,
    currentTool: runtimeSource.currentTool ?? normalizedActiveSession.currentTool,
    displayTool: runtimeSource.displayTool ?? runtimeSource.currentTool ?? normalizedActiveSession.displayTool ?? normalizedActiveSession.currentTool,
    toolElapsedMs: runtimeSource.toolElapsedMs ?? normalizedActiveSession.toolElapsedMs,
    lastToolSummary: runtimeSource.lastToolSummary ?? normalizedActiveSession.lastToolSummary,
    contextTokens: runtimeSource.contextTokens ?? normalizedActiveSession.contextTokens,
    contextWindow: runtimeSource.contextWindow ?? normalizedActiveSession.contextWindow,
    contextRemainingPct: runtimeSource.contextRemainingPct ?? normalizedActiveSession.contextRemainingPct,
    inputTokens: runtimeSource.inputTokens ?? normalizedActiveSession.inputTokens,
    outputTokens: runtimeSource.outputTokens ?? normalizedActiveSession.outputTokens,
    totalTokens: runtimeSource.totalTokens ?? normalizedActiveSession.totalTokens,
    turnCostUsd: runtimeSource.turnCostUsd ?? normalizedActiveSession.turnCostUsd,
    sessionCostUsd: runtimeSource.sessionCostUsd ?? normalizedActiveSession.sessionCostUsd,
    pendingPermissionCount: runtimeSource.pendingPermissionCount ?? normalizedActiveSession.pendingPermissionCount,
    drainMode: runtimeSource.drainMode ?? normalizedActiveSession.drainMode,
    workItemProjectionId: runtimeSource.workItemProjectionId ?? normalizedActiveSession.workItemProjectionId,
    workItemTurnType: runtimeSource.workItemTurnType ?? normalizedActiveSession.workItemTurnType,
    companyProfile: normalizedActiveSession.companyProfile ?? runtimeSource.companyProfile,
    workItemRoleId: normalizedActiveSession.workItemRoleId ?? runtimeSource.workItemRoleId,
    workItemRoleName: normalizedActiveSession.workItemRoleName ?? runtimeSource.workItemRoleName,
    workItemGate: normalizedActiveSession.workItemGate ?? runtimeSource.workItemGate,
    employeeAssignment: normalizedActiveSession.employeeAssignment ?? runtimeSource.employeeAssignment,
    selectedExecutionAgent: runtimeSource.selectedExecutionAgent ?? normalizedActiveSession.selectedExecutionAgent,
    draftAssistantText: runtimeSource.draftAssistantText ?? normalizedActiveSession.draftAssistantText,
    draftUpdatedAt: runtimeSource.draftUpdatedAt ?? normalizedActiveSession.draftUpdatedAt,
    draftIteration: runtimeSource.draftIteration ?? normalizedActiveSession.draftIteration,
    draftTurnId: runtimeSource.draftTurnId ?? normalizedActiveSession.draftTurnId,
    runtimeControlState: runtimeSource.runtimeControlState ?? normalizedActiveSession.runtimeControlState,
    canStop: runtimeSource.canStop ?? normalizedActiveSession.canStop,
    canResume: runtimeSource.canResume ?? normalizedActiveSession.canResume,
    resumeParentSessionId: runtimeSource.resumeParentSessionId ?? normalizedActiveSession.resumeParentSessionId,
    pendingRuntimeCheckpointId: runtimeSource.pendingRuntimeCheckpointId ?? normalizedActiveSession.pendingRuntimeCheckpointId,
    stopIntentId: runtimeSource.stopIntentId ?? normalizedActiveSession.stopIntentId,
    updatedAt: Math.max(
      normalizedActiveSession.updatedAt,
      runtimeSource.updatedAt,
    ),
    messageCount: Math.max(
      normalizedActiveSession.messageCount ?? 0,
      runtimeSource.messageCount ?? 0,
      mergedMessageCount,
    ),
    isCompanyRuntime: !!(normalizedActiveSession.isCompanyRuntime || runtimeSource.isCompanyRuntime),
    workItemLog: (normalizedActiveSession.workItemLog?.length ?? 0) > 0
      ? normalizedActiveSession.workItemLog
      : runtimeSource.workItemLog,
    roleWorkItems: normalizedActiveSession.roleWorkItems ?? runtimeSource.roleWorkItems,
    executorRoleWorkItems: normalizedActiveSession.executorRoleWorkItems ?? runtimeSource.executorRoleWorkItems,
    activeSubagents: normalizedActiveSession.activeSubagents ?? runtimeSource.activeSubagents,
    permissionRequests: normalizedActiveSession.permissionRequests ?? runtimeSource.permissionRequests,
  }
}

export function getConversationHeaderSession(
  activeSession: Session | null,
  runtimeSession: Session | null,
  timelineSessions: Session[],
): Session | null {
  if (!activeSession) return null
  const normalizedActiveSession = withDerivedSessionRuntime(activeSession)
  const runtimeSource = runtimeSession ?? normalizedActiveSession
  const companyDisplayStatus = deriveCompanyRuntimeDisplayStatus(runtimeSource)
    ?? deriveCompanyRuntimeDisplayStatus(normalizedActiveSession)
  const mergedMessageCount = getConversationMessageCount(
    timelineSessions.length > 0 ? timelineSessions : [normalizedActiveSession],
  )

  return {
    ...normalizedActiveSession,
    status: companyDisplayStatus ?? (runtimeSource.status || normalizedActiveSession.status),
    agentStatus: runtimeSource.agentStatus ?? normalizedActiveSession.agentStatus,
    currentTool: runtimeSource.currentTool ?? normalizedActiveSession.currentTool,
    displayTool: runtimeSource.displayTool ?? runtimeSource.currentTool ?? normalizedActiveSession.displayTool ?? normalizedActiveSession.currentTool,
    toolElapsedMs: runtimeSource.toolElapsedMs ?? normalizedActiveSession.toolElapsedMs,
    lastToolSummary: runtimeSource.lastToolSummary ?? normalizedActiveSession.lastToolSummary,
    contextTokens: runtimeSource.contextTokens ?? normalizedActiveSession.contextTokens,
    contextWindow: runtimeSource.contextWindow ?? normalizedActiveSession.contextWindow,
    contextRemainingPct: runtimeSource.contextRemainingPct ?? normalizedActiveSession.contextRemainingPct,
    inputTokens: runtimeSource.inputTokens ?? normalizedActiveSession.inputTokens,
    outputTokens: runtimeSource.outputTokens ?? normalizedActiveSession.outputTokens,
    totalTokens: runtimeSource.totalTokens ?? normalizedActiveSession.totalTokens,
    turnCostUsd: runtimeSource.turnCostUsd ?? normalizedActiveSession.turnCostUsd,
    sessionCostUsd: runtimeSource.sessionCostUsd ?? normalizedActiveSession.sessionCostUsd,
    pendingPermissionCount: runtimeSource.pendingPermissionCount ?? normalizedActiveSession.pendingPermissionCount,
    drainMode: runtimeSource.drainMode ?? normalizedActiveSession.drainMode,
    selectedExecutionAgent: runtimeSource.selectedExecutionAgent ?? normalizedActiveSession.selectedExecutionAgent,
    runtimeControlState: runtimeSource.runtimeControlState ?? normalizedActiveSession.runtimeControlState,
    canStop: runtimeSource.canStop ?? normalizedActiveSession.canStop,
    canResume: runtimeSource.canResume ?? normalizedActiveSession.canResume,
    resumeParentSessionId: runtimeSource.resumeParentSessionId ?? normalizedActiveSession.resumeParentSessionId,
    pendingRuntimeCheckpointId: runtimeSource.pendingRuntimeCheckpointId ?? normalizedActiveSession.pendingRuntimeCheckpointId,
    stopIntentId: runtimeSource.stopIntentId ?? normalizedActiveSession.stopIntentId,
    updatedAt: Math.max(normalizedActiveSession.updatedAt, runtimeSource.updatedAt),
    messageCount: Math.max(
      normalizedActiveSession.messageCount ?? 0,
      runtimeSource.messageCount ?? 0,
      mergedMessageCount,
    ),
  }
}

export function mergeConversationMessages(messageGroups: ChatMessage[][]): ChatMessage[] {
  const seen = new Set<string>()
  const resultKeyIndex = new Map<string, number>()
  const checkpointIndex = new Map<string, number>()
  const merged: ChatMessage[] = []
  for (const group of messageGroups) {
    for (const message of group) {
      const metadata = (message.metadata ?? {}) as Record<string, unknown>
      const checkpointId = String(metadata.checkpoint_id ?? '').trim()
      if (checkpointId) {
        const existingIndex = checkpointIndex.get(checkpointId)
        if (existingIndex !== undefined) {
          const existing = merged[existingIndex]
          const latest = message.timestamp >= existing.timestamp ? message : existing
          const earliest = message.timestamp < existing.timestamp ? message : existing
          merged[existingIndex] = {
            ...existing,
            ...latest,
            id: existing.id,
            channelId: existing.channelId,
            timestamp: earliest.timestamp,
            metadata: { ...(existing.metadata ?? {}), ...(latest.metadata ?? {}) },
          }
          continue
        }
      }
      const uiMessageId = typeof metadata.ui_message_id === 'string'
        ? metadata.ui_message_id.trim()
        : ''
      const resultKey = resultSurfaceDedupeKey(message)
      if (resultKey) {
        const existingIndex = resultKeyIndex.get(resultKey)
        if (existingIndex !== undefined) {
          const existing = merged[existingIndex]
          const candidateWins = compareResultSurfacePreference(message, existing) > 0
          const preferred = candidateWins ? message : existing
          const secondary = candidateWins ? existing : message
          merged[existingIndex] = {
            ...secondary,
            ...preferred,
            // A result surface keeps the chronology of the first underlying
            // delivery, independent of which related channel happened to be
            // traversed first for this render.
            timestamp: Math.min(existing.timestamp, message.timestamp),
            metadata: {
              ...(secondary.metadata ?? {}),
              ...(preferred.metadata ?? {}),
              ui_timeline_id: resultKey || stableMessageTimelineKey(existing),
            },
          }
          continue
        }
        resultKeyIndex.set(resultKey, merged.length)
      }
      const checkpointKey = checkpointId ? `checkpoint:${checkpointId}` : ''
      const dedupeKey = resultKey || checkpointKey || uiMessageId || `${message.sender}:${message.replyToId ?? ''}:${message.timestamp}:${message.content.trim()}`
      if (seen.has(dedupeKey)) continue
      seen.add(dedupeKey)
      if (checkpointId) checkpointIndex.set(checkpointId, merged.length)
      merged.push(message)
    }
  }
  return merged.sort((a, b) => (
    a.timestamp === b.timestamp
      ? a.id.localeCompare(b.id)
      : a.timestamp - b.timestamp
  ))
}

/**
 * Build the durable transcript shown by a company/org parent session.
 *
 * A parent conversation may observe every related runtime channel so that
 * canonical role deliveries can be surfaced in one place.  Those channels
 * also contain transient child turns, however, and rendering all of them in
 * the parent transcript makes the visible timeline change whenever the
 * runtime projection selects a different child.  Keep the parent's committed
 * messages and only admit the canonical cross-channel result surfaces.
 */
export function selectCompanySummaryMessages(
  messages: ChatMessage[],
  parentChannelId: string,
): ChatMessage[] {
  const durableMessages: ChatMessage[] = []
  for (const message of messages) {
    if (message.channelId === parentChannelId) {
      if (isMessageVisibleAtDetailLevel(message, 'summary')) {
        durableMessages.push(message)
      }
      continue
    }
    const metadata = (message.metadata ?? {}) as Record<string, unknown>
    const kind = String(metadata.transcript_kind ?? metadata.kind ?? '').trim()
    const checkpointId = String(metadata.checkpoint_id ?? '').trim()
    const checkpointType = String(metadata.checkpoint_type ?? '').trim()
    const checkpointResponseId = String(metadata.response_to_checkpoint_id ?? '').trim()
    if ((checkpointId && checkpointType) || checkpointResponseId) {
      durableMessages.push(message)
      continue
    }
    if ([
      'company_role_result',
      'company_role_result_retry',
      'child_task_result',
      'child_task_result_retry',
      'child_result',
      'top_level_reply',
    ].includes(kind)) {
      durableMessages.push(message)
    }
  }
  return mergeConversationMessages([durableMessages])
}

export function mergeConversationProgressLog(timelineSessions: Session[]): ProgressEntry[] {
  const seen = new Set<string>()
  const merged: ProgressEntry[] = []
  for (const session of timelineSessions) {
    for (const entry of session.progressLog ?? []) {
      const dedupeKey = `${entry.timestamp}:${entry.type}:${entry.summary}:${entry.detail ?? ''}`
      if (seen.has(dedupeKey)) continue
      seen.add(dedupeKey)
      merged.push(entry)
    }
  }
  return merged.sort((a, b) => a.timestamp - b.timestamp)
}

export function getConversationMessageCount(timelineSessions: Session[]): number {
  return timelineSessions.reduce(
    (total, session) => total + Math.max(0, session.messageCount ?? 0),
    0,
  )
}
