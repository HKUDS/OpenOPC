import { useCallback, useMemo, useReducer } from 'react'
import type {
  ExternalTeamActivityCounts,
  ExternalTeamActivitySnapshotPayload,
  ExternalTeamActivitySummary,
  ExternalTeamEventKind,
  ExternalTeamEventV1,
  ExternalTeamInvocationSummary,
  ExternalTeamMember,
  ExternalTeamMessage,
  ExternalTeamTask,
} from '../types/externalTeamActivity'

const MAX_EVENTS_PER_INVOCATION = 1000
const ACTIVE_STATES = new Set(['active', 'busy', 'claimed', 'executing', 'in_progress', 'running', 'working'])
const COMPLETE_STATES = new Set(['complete', 'completed', 'done', 'finished', 'ready', 'shutdown', 'stopped'])
const FAILED_STATES = new Set(['cancelled', 'error', 'failed', 'failure'])

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : value == null ? '' : String(value).trim()
}

function numberValue(value: unknown, fallback = 0): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

function snake(raw: Record<string, unknown>, snakeKey: string, camelKey: string): unknown {
  return raw[snakeKey] ?? raw[camelKey]
}

function normalizeMember(raw: unknown): ExternalTeamMember | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const value = raw as Record<string, unknown>
  const memberId = text(snake(value, 'member_id', 'memberId'))
  if (!memberId) return undefined
  return {
    memberId,
    name: text(value.name) || memberId,
    role: text(value.role) || undefined,
    mode: text(value.mode) || undefined,
    status: text(value.status) || undefined,
    executionStatus: text(snake(value, 'execution_status', 'executionStatus')) || undefined,
    reason: text(value.reason) || undefined,
    restartCount: value.restart_count == null && value.restartCount == null
      ? undefined
      : numberValue(snake(value, 'restart_count', 'restartCount')),
    updatedAt: text(snake(value, 'updated_at', 'updatedAt')) || undefined,
  }
}

function normalizeTask(raw: unknown): ExternalTeamTask | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const value = raw as Record<string, unknown>
  const taskId = text(snake(value, 'task_id', 'taskId'))
  if (!taskId) return undefined
  const dependencies = snake(value, 'dependencies', 'dependencies')
  return {
    taskId,
    title: text(value.title) || undefined,
    content: text(value.content) || undefined,
    status: text(value.status) || undefined,
    assignee: text(value.assignee) || undefined,
    dependencies: Array.isArray(dependencies)
      ? dependencies.map(text).filter(Boolean)
      : undefined,
    updatedAt: text(snake(value, 'updated_at', 'updatedAt')) || undefined,
  }
}

function normalizeMessage(raw: unknown): ExternalTeamMessage | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const value = raw as Record<string, unknown>
  const content = text(value.content)
  const messageId = text(snake(value, 'message_id', 'messageId'))
  if (!content && !messageId) return undefined
  return {
    messageId: messageId || undefined,
    fromMember: text(snake(value, 'from_member', 'fromMember')) || undefined,
    toMember: text(snake(value, 'to_member', 'toMember')) || undefined,
    protocol: text(value.protocol) || undefined,
    content,
  }
}

function isLeaderMember(member: ExternalTeamMember): boolean {
  const memberId = text(member.memberId).toLowerCase().replaceAll('_', '-')
  const role = text(member.role).toLowerCase().replaceAll('_', '-')
  return memberId === 'team-leader' || role === 'leader' || role === 'team-leader'
}

export function normalizeExternalTeamEvent(raw: unknown): ExternalTeamEventV1 | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const value = raw as Record<string, unknown>
  const projectId = text(snake(value, 'project_id', 'projectId'))
  const taskId = text(snake(value, 'task_id', 'taskId'))
  const externalInvocationId = text(snake(value, 'external_invocation_id', 'externalInvocationId'))
  const eventId = text(snake(value, 'event_id', 'eventId'))
  const kind = text(value.kind) as ExternalTeamEventKind
  if (!projectId || !taskId || !externalInvocationId || !eventId || !kind) return undefined
  return {
    schemaVersion: 1,
    eventId,
    provider: 'jiuwenswarm',
    projectId,
    taskId,
    opcSessionId: text(snake(value, 'opc_session_id', 'opcSessionId')),
    executionTurnId: text(snake(value, 'execution_turn_id', 'executionTurnId')) || undefined,
    workItemId: text(snake(value, 'work_item_id', 'workItemId')) || undefined,
    workItemProjectionId: text(snake(value, 'work_item_projection_id', 'workItemProjectionId')) || undefined,
    externalInvocationId,
    providerSessionId: text(snake(value, 'provider_session_id', 'providerSessionId')) || undefined,
    teamId: text(snake(value, 'team_id', 'teamId')) || undefined,
    sequence: numberValue(value.sequence),
    occurredAt: text(snake(value, 'occurred_at', 'occurredAt')),
    kind,
    member: normalizeMember(value.member),
    task: normalizeTask(value.task),
    message: normalizeMessage(value.message),
    metrics: value.metrics && typeof value.metrics === 'object' ? value.metrics as Record<string, unknown> : undefined,
    output: text(value.output) || undefined,
    error: text(value.error) || undefined,
    summary: text(value.summary) || undefined,
    rawEventType: text(snake(value, 'raw_event_type', 'rawEventType')) || undefined,
  }
}

function emptyCounts(): ExternalTeamActivityCounts {
  return {
    members: 0,
    membersActive: 0,
    membersCompleted: 0,
    membersFailed: 0,
    tasks: 0,
    tasksActive: 0,
    tasksCompleted: 0,
    tasksFailed: 0,
  }
}

export function emptyExternalTeamSummary(): ExternalTeamActivitySummary {
  return {
    schemaVersion: 1,
    provider: 'jiuwenswarm',
    mode: 'starting',
    leaderState: 'starting',
    members: [],
    tasks: [],
    counts: emptyCounts(),
    telemetryIncomplete: false,
  }
}

export function normalizeExternalTeamSummary(raw: unknown): ExternalTeamActivitySummary | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const value = raw as Record<string, unknown>
  const rawCounts = value.counts && typeof value.counts === 'object'
    ? value.counts as Record<string, unknown>
    : {}
  const normalizedMembers = Array.isArray(value.members)
    ? value.members.map(normalizeMember).filter((item): item is ExternalTeamMember => !!item)
    : []
  const leaderMember = normalizedMembers.find(isLeaderMember)
  const members = normalizedMembers.filter(item => !isLeaderMember(item))
  const tasks = Array.isArray(value.tasks)
    ? value.tasks.map(normalizeTask).filter((item): item is ExternalTeamTask => !!item)
    : []
  const mode = text(value.mode)
  return {
    schemaVersion: 1,
    provider: 'jiuwenswarm',
    mode: ['starting', 'leader_only', 'team_active', 'completed', 'failed', 'unknown'].includes(mode)
      ? mode as ExternalTeamActivitySummary['mode']
      : 'unknown',
    leaderState: text(
      leaderMember?.executionStatus
      || leaderMember?.status
      || snake(value, 'leader_state', 'leaderState'),
    ) || 'unknown',
    teamId: text(snake(value, 'team_id', 'teamId')) || undefined,
    externalInvocationId: text(snake(value, 'external_invocation_id', 'externalInvocationId')) || undefined,
    providerSessionId: text(snake(value, 'provider_session_id', 'providerSessionId')) || undefined,
    members,
    tasks,
    counts: {
      members: members.length,
      membersActive: members.filter(item => ACTIVE_STATES.has(status(item.executionStatus || item.status))).length,
      membersCompleted: members.filter(item => COMPLETE_STATES.has(status(item.executionStatus || item.status))).length,
      membersFailed: members.filter(item => FAILED_STATES.has(status(item.executionStatus || item.status))).length,
      tasks: numberValue(rawCounts.tasks, tasks.length),
      tasksActive: numberValue(snake(rawCounts, 'tasks_active', 'tasksActive')),
      tasksCompleted: numberValue(snake(rawCounts, 'tasks_completed', 'tasksCompleted')),
      tasksFailed: numberValue(snake(rawCounts, 'tasks_failed', 'tasksFailed')),
    },
    lastEventAt: text(snake(value, 'last_event_at', 'lastEventAt')) || undefined,
    telemetryIncomplete: Boolean(snake(value, 'telemetry_incomplete', 'telemetryIncomplete')),
  }
}

function activityKey(projectId: string, taskId: string, invocationId: string): string {
  return `${projectId}\u0000${taskId}\u0000${invocationId}`
}

function taskKey(projectId: string, taskId: string): string {
  return `${projectId}\u0000${taskId}`
}

function status(value: string | undefined): string {
  return text(value).toLowerCase()
}

export function reduceExternalTeamSummary(
  current: ExternalTeamActivitySummary | undefined,
  event: ExternalTeamEventV1,
): ExternalTeamActivitySummary {
  const summary = current ? {
    ...current,
    counts: { ...current.counts },
    members: current.members.map(item => ({ ...item })),
    tasks: current.tasks.map(item => ({ ...item, dependencies: item.dependencies ? [...item.dependencies] : undefined })),
  } : emptyExternalTeamSummary()
  const members = new Map(summary.members.map(item => [item.memberId, item]))
  const tasks = new Map(summary.tasks.map(item => [item.taskId, item]))
  if (event.teamId) summary.teamId = event.teamId
  summary.externalInvocationId = event.externalInvocationId
  if (event.providerSessionId) summary.providerSessionId = event.providerSessionId
  if (event.occurredAt && (!summary.lastEventAt || event.occurredAt > summary.lastEventAt)) {
    summary.lastEventAt = event.occurredAt
  }
  if (event.member && isLeaderMember(event.member)) {
    if (!['completed', 'failed'].includes(summary.mode)) {
      summary.leaderState = event.member.executionStatus || event.member.status || summary.leaderState
    }
    members.delete(event.member.memberId)
  } else if (event.member) {
    const nextMember = {
      ...(members.get(event.member.memberId) ?? {}),
      ...event.member,
      updatedAt: event.occurredAt || event.member.updatedAt,
    }
    if (event.kind === 'member_shutdown' && !event.member.status) {
      nextMember.status = 'shutdown'
    }
    members.set(event.member.memberId, nextMember)
  }
  if (event.task) {
    const inferred = event.kind === 'task_claimed' ? 'claimed'
      : event.kind === 'task_completed' ? 'completed'
        : event.kind === 'task_cancelled' ? 'cancelled'
          : event.kind === 'task_unblocked' ? 'pending'
            : undefined
    tasks.set(event.task.taskId, {
      ...(tasks.get(event.task.taskId) ?? {}),
      ...event.task,
      status: event.task.status || inferred,
      updatedAt: event.occurredAt || event.task.updatedAt,
    })
  }
  if (event.kind === 'runtime_ready') {
    summary.mode = 'leader_only'
    summary.leaderState = 'working'
  } else if (event.kind === 'leader_output' && !['completed', 'failed'].includes(summary.mode)) {
    summary.leaderState = 'synthesizing'
  } else if (event.kind === 'team_completed') {
    summary.mode = 'completed'
    summary.leaderState = 'completed'
  } else if (event.kind === 'team_error') {
    summary.mode = 'failed'
    summary.leaderState = 'failed'
  }
  if (members.size > 0 && !['completed', 'failed'].includes(summary.mode)) summary.mode = 'team_active'
  summary.members = [...members.values()].sort((a, b) => a.memberId.localeCompare(b.memberId))
  summary.tasks = [...tasks.values()].sort((a, b) => a.taskId.localeCompare(b.taskId))
  const memberStates = summary.members.map(item => status(item.executionStatus || item.status))
  const taskStates = summary.tasks.map(item => status(item.status))
  summary.counts = {
    members: summary.members.length,
    membersActive: memberStates.filter(item => ACTIVE_STATES.has(item)).length,
    membersCompleted: memberStates.filter(item => COMPLETE_STATES.has(item)).length,
    membersFailed: memberStates.filter(item => FAILED_STATES.has(item)).length,
    tasks: summary.tasks.length,
    tasksActive: taskStates.filter(item => ACTIVE_STATES.has(item)).length,
    tasksCompleted: taskStates.filter(item => COMPLETE_STATES.has(item)).length,
    tasksFailed: taskStates.filter(item => FAILED_STATES.has(item)).length,
  }
  return summary
}

function mergeExternalTeamSummaries(
  left: ExternalTeamActivitySummary | undefined,
  right: ExternalTeamActivitySummary | undefined,
): ExternalTeamActivitySummary {
  if (!left) return right ?? emptyExternalTeamSummary()
  if (!right) return left
  const latest = (right.lastEventAt || '') >= (left.lastEventAt || '') ? right : left
  const members = new Map(left.members.map(item => [item.memberId, item]))
  for (const item of right.members) {
    const previous = members.get(item.memberId)
    if (!previous || (item.updatedAt || '') >= (previous.updatedAt || '')) members.set(item.memberId, item)
  }
  const tasks = new Map(left.tasks.map(item => [item.taskId, item]))
  for (const item of right.tasks) {
    const previous = tasks.get(item.taskId)
    if (!previous || (item.updatedAt || '') >= (previous.updatedAt || '')) tasks.set(item.taskId, item)
  }
  const merged: ExternalTeamActivitySummary = {
    ...latest,
    teamId: latest.teamId || left.teamId || right.teamId,
    externalInvocationId: latest.externalInvocationId || left.externalInvocationId || right.externalInvocationId,
    providerSessionId: latest.providerSessionId || left.providerSessionId || right.providerSessionId,
    members: [...members.values()].sort((a, b) => a.memberId.localeCompare(b.memberId)),
    tasks: [...tasks.values()].sort((a, b) => a.taskId.localeCompare(b.taskId)),
    telemetryIncomplete: left.telemetryIncomplete || right.telemetryIncomplete,
  }
  return reduceExternalTeamSummary(merged, {
    schemaVersion: 1,
    eventId: '__counts__',
    provider: 'jiuwenswarm',
    projectId: '',
    taskId: '',
    opcSessionId: '',
    externalInvocationId: merged.externalInvocationId || '',
    sequence: 0,
    occurredAt: merged.lastEventAt || '',
    kind: 'provider_event',
  })
}

function projectEvents(events: ExternalTeamEventV1[]): ExternalTeamActivitySummary {
  let summary: ExternalTeamActivitySummary | undefined
  for (const event of events) summary = reduceExternalTeamSummary(summary, event)
  return summary ?? emptyExternalTeamSummary()
}

export interface ExternalTeamActivityRecord {
  key: string
  projectId: string
  taskId: string
  externalInvocationId: string
  available: boolean
  loading: boolean
  reason?: string
  invocations: ExternalTeamInvocationSummary[]
  summary: ExternalTeamActivitySummary
  /** Last authoritative server projection, kept separate for out-of-order replay. */
  snapshotSummary?: ExternalTeamActivitySummary
  events: ExternalTeamEventV1[]
  hasMore: boolean
  nextCursor?: { beforeCreatedAt?: string; beforeEventId?: string } | null
}

export interface ExternalTeamActivityStoreState {
  records: Record<string, ExternalTeamActivityRecord>
  selectedByTask: Record<string, string>
}

export type ExternalTeamActivityAction =
  | { type: 'reset_project'; projectId: string }
  | { type: 'loading'; projectId: string; taskId: string; invocationId?: string }
  | { type: 'event'; event: ExternalTeamEventV1 }
  | { type: 'snapshot'; payload: ExternalTeamActivitySnapshotPayload }

export const INITIAL_EXTERNAL_TEAM_ACTIVITY_STATE: ExternalTeamActivityStoreState = { records: {}, selectedByTask: {} }

function mergeEvents(existing: ExternalTeamEventV1[], incoming: ExternalTeamEventV1[]): ExternalTeamEventV1[] {
  const byId = new Map(existing.map(event => [event.eventId, event]))
  for (const event of incoming) byId.set(event.eventId, event)
  return [...byId.values()]
    .sort((a, b) => a.sequence - b.sequence || a.occurredAt.localeCompare(b.occurredAt) || a.eventId.localeCompare(b.eventId))
    .slice(-MAX_EVENTS_PER_INVOCATION)
}

export function reduceExternalTeamActivityStore(
  state: ExternalTeamActivityStoreState,
  action: ExternalTeamActivityAction,
): ExternalTeamActivityStoreState {
  if (action.type === 'reset_project') {
    const records = Object.fromEntries(Object.entries(state.records).filter(([, record]) => record.projectId === action.projectId))
    const selectedByTask = Object.fromEntries(Object.entries(state.selectedByTask).filter(([key]) => key.startsWith(`${action.projectId}\u0000`)))
    return { records, selectedByTask }
  }
  if (action.type === 'loading') {
    const invocation = action.invocationId || '__pending__'
    const key = activityKey(action.projectId, action.taskId, invocation)
    const previous = state.records[key]
    const selected = state.records[state.selectedByTask[taskKey(action.projectId, action.taskId)]]
    return {
      ...state,
      records: {
        ...state.records,
        [key]: previous
          ? { ...previous, loading: true, reason: undefined }
          : {
              key,
              projectId: action.projectId,
              taskId: action.taskId,
              externalInvocationId: invocation,
              available: false,
              loading: true,
              summary: emptyExternalTeamSummary(),
              invocations: selected?.invocations ?? [],
              events: [],
              hasMore: false,
            },
      },
      selectedByTask: { ...state.selectedByTask, [taskKey(action.projectId, action.taskId)]: key },
    }
  }
  if (action.type === 'event') {
    const event = action.event
    const key = activityKey(event.projectId, event.taskId, event.externalInvocationId)
    const previous = state.records[key]
    if (previous?.events.some(item => item.eventId === event.eventId)) return state
    const events = mergeEvents(previous?.events ?? [], [event])
    const summary = mergeExternalTeamSummaries(previous?.snapshotSummary, projectEvents(events))
    return {
      records: {
        ...state.records,
        [key]: {
          key,
          projectId: event.projectId,
          taskId: event.taskId,
          externalInvocationId: event.externalInvocationId,
          available: true,
          loading: false,
          summary,
          invocations: previous?.invocations ?? [],
          snapshotSummary: previous?.snapshotSummary,
          events,
          hasMore: previous?.hasMore ?? false,
          nextCursor: previous?.nextCursor,
        },
      },
      selectedByTask: {
        ...state.selectedByTask,
        [taskKey(event.projectId, event.taskId)]:
          state.selectedByTask[taskKey(event.projectId, event.taskId)] ?? key,
      },
    }
  }
  const payload = action.payload
  const invocation = payload.externalInvocationId || '__pending__'
  const key = activityKey(payload.projectId, payload.taskId, invocation)
  const pendingKey = activityKey(payload.projectId, payload.taskId, '__pending__')
  const previous = state.records[key] ?? state.records[pendingKey]
  const events = mergeEvents(previous?.events ?? [], payload.events)
  const snapshotSummary = payload.summary ?? previous?.snapshotSummary
  const summary = mergeExternalTeamSummaries(snapshotSummary, projectEvents(events))
  const records = { ...state.records }
  if (pendingKey !== key) delete records[pendingKey]
  records[key] = {
    key,
    projectId: payload.projectId,
    taskId: payload.taskId,
    externalInvocationId: invocation,
    available: payload.available,
    loading: false,
    reason: payload.reason,
    invocations: payload.invocations.length > 0 ? payload.invocations : (previous?.invocations ?? []),
    summary,
    snapshotSummary,
    events,
    hasMore: payload.hasMore,
    nextCursor: payload.nextCursor,
  }
  return {
    records,
    selectedByTask: { ...state.selectedByTask, [taskKey(payload.projectId, payload.taskId)]: key },
  }
}

export function normalizeExternalTeamSnapshot(raw: unknown): ExternalTeamActivitySnapshotPayload | undefined {
  if (!raw || typeof raw !== 'object') return undefined
  const value = raw as Record<string, unknown>
  const projectId = text(snake(value, 'project_id', 'projectId'))
  const taskId = text(snake(value, 'task_id', 'taskId'))
  if (!projectId || !taskId) return undefined
  const events = Array.isArray(value.events)
    ? value.events.map(normalizeExternalTeamEvent).filter((event): event is ExternalTeamEventV1 => !!event)
    : []
  const cursorRaw = snake(value, 'next_cursor', 'nextCursor')
  const cursor = cursorRaw && typeof cursorRaw === 'object' ? cursorRaw as Record<string, unknown> : undefined
  return {
    available: Boolean(value.available),
    reason: text(value.reason || value.error) || undefined,
    projectId,
    taskId,
    executionTurnId: text(snake(value, 'execution_turn_id', 'executionTurnId')) || undefined,
    provider: text(value.provider) || undefined,
    executionUnitKind: text(snake(value, 'execution_unit_kind', 'executionUnitKind')) || undefined,
    externalInvocationId: text(snake(value, 'external_invocation_id', 'externalInvocationId')) || undefined,
    providerSessionId: text(snake(value, 'provider_session_id', 'providerSessionId')) || undefined,
    invocations: Array.isArray(value.invocations)
      ? value.invocations.flatMap((rawInvocation): ExternalTeamInvocationSummary[] => {
          if (!rawInvocation || typeof rawInvocation !== 'object') return []
          const invocation = rawInvocation as Record<string, unknown>
          const invocationId = text(snake(invocation, 'external_invocation_id', 'externalInvocationId'))
          if (!invocationId) return []
          return [{
            externalInvocationId: invocationId,
            startedAt: text(snake(invocation, 'started_at', 'startedAt')) || undefined,
            lastEventAt: text(snake(invocation, 'last_event_at', 'lastEventAt')) || undefined,
            eventCount: numberValue(snake(invocation, 'event_count', 'eventCount')),
            memberCount: numberValue(snake(invocation, 'member_count', 'memberCount')),
            taskCount: numberValue(snake(invocation, 'task_count', 'taskCount')),
            messageCount: numberValue(snake(invocation, 'message_count', 'messageCount')),
            outputCount: numberValue(snake(invocation, 'output_count', 'outputCount')),
            isPreferred: Boolean(snake(invocation, 'is_preferred', 'isPreferred')),
            isLatest: Boolean(snake(invocation, 'is_latest', 'isLatest')),
          }]
        })
      : [],
    summary: normalizeExternalTeamSummary(value.summary),
    events,
    hasMore: Boolean(snake(value, 'has_more', 'hasMore')),
    nextCursor: cursor ? {
      beforeCreatedAt: text(snake(cursor, 'before_created_at', 'beforeCreatedAt')) || undefined,
      beforeEventId: text(snake(cursor, 'before_event_id', 'beforeEventId')) || undefined,
    } : null,
    viewGeneration: value.view_generation == null && value.viewGeneration == null
      ? undefined
      : numberValue(snake(value, 'view_generation', 'viewGeneration')),
  }
}

export function useExternalTeamActivityStore() {
  const [state, dispatch] = useReducer(reduceExternalTeamActivityStore, INITIAL_EXTERNAL_TEAM_ACTIVITY_STATE)
  const resetProject = useCallback((projectId: string) => dispatch({ type: 'reset_project', projectId }), [])
  const markLoading = useCallback((projectId: string, taskId: string, invocationId?: string) => {
    dispatch({ type: 'loading', projectId, taskId, invocationId })
  }, [])
  const applyEvent = useCallback((raw: unknown) => {
    const event = normalizeExternalTeamEvent(raw)
    if (event) dispatch({ type: 'event', event })
  }, [])
  const applySnapshot = useCallback((raw: unknown) => {
    const payload = normalizeExternalTeamSnapshot(raw)
    if (payload) dispatch({ type: 'snapshot', payload })
  }, [])
  const getForTask = useCallback((projectId: string, taskId: string, invocationId?: string): ExternalTeamActivityRecord | undefined => {
    const exactInvocation = text(invocationId)
    const key = exactInvocation
      ? activityKey(projectId, taskId, exactInvocation)
      : state.selectedByTask[taskKey(projectId, taskId)]
    return key ? state.records[key] : undefined
  }, [state.selectedByTask, state.records])
  return useMemo(() => ({
    records: state.records,
    selectedByTask: state.selectedByTask,
    resetProject,
    markLoading,
    applyEvent,
    applySnapshot,
    getForTask,
  }), [state.records, state.selectedByTask, resetProject, markLoading, applyEvent, applySnapshot, getForTask])
}
