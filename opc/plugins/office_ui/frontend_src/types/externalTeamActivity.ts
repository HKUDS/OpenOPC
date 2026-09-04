export type ExternalTeamEventKind =
  | 'runtime_ready'
  | 'team_completed'
  | 'team_error'
  | 'member_spawned'
  | 'member_status_changed'
  | 'member_execution_changed'
  | 'member_restarted'
  | 'member_shutdown'
  | 'task_created'
  | 'task_claimed'
  | 'task_completed'
  | 'task_cancelled'
  | 'task_unblocked'
  | 'message_p2p'
  | 'message_broadcast'
  | 'member_output'
  | 'leader_output'
  | 'provider_event'

export interface ExternalTeamMember {
  memberId: string
  name: string
  role?: string
  mode?: string
  status?: string
  executionStatus?: string
  reason?: string
  restartCount?: number
  updatedAt?: string
}

export interface ExternalTeamTask {
  taskId: string
  title?: string
  content?: string
  status?: string
  assignee?: string
  dependencies?: string[]
  updatedAt?: string
}

export interface ExternalTeamMessage {
  messageId?: string
  fromMember?: string
  toMember?: string
  protocol?: string
  content: string
}

export interface ExternalTeamEventV1 {
  schemaVersion: 1
  eventId: string
  provider: 'jiuwenswarm'
  projectId: string
  taskId: string
  opcSessionId: string
  executionTurnId?: string
  workItemId?: string
  workItemProjectionId?: string
  externalInvocationId: string
  providerSessionId?: string
  teamId?: string
  sequence: number
  occurredAt: string
  kind: ExternalTeamEventKind
  member?: ExternalTeamMember
  task?: ExternalTeamTask
  message?: ExternalTeamMessage
  metrics?: Record<string, unknown>
  output?: string
  error?: string
  summary?: string
  rawEventType?: string
}

export interface ExternalTeamActivityCounts {
  members: number
  membersActive: number
  membersCompleted: number
  membersFailed: number
  tasks: number
  tasksActive: number
  tasksCompleted: number
  tasksFailed: number
}

export type ExternalTeamActivityMode =
  | 'starting'
  | 'leader_only'
  | 'team_active'
  | 'completed'
  | 'failed'
  | 'unknown'

export interface ExternalTeamActivitySummary {
  schemaVersion: 1
  provider: 'jiuwenswarm'
  mode: ExternalTeamActivityMode
  leaderState: string
  teamId?: string
  externalInvocationId?: string
  providerSessionId?: string
  members: ExternalTeamMember[]
  tasks: ExternalTeamTask[]
  counts: ExternalTeamActivityCounts
  lastEventAt?: string
  telemetryIncomplete: boolean
}

export interface ExternalTeamInvocationSummary {
  externalInvocationId: string
  startedAt?: string
  lastEventAt?: string
  eventCount: number
  memberCount: number
  taskCount: number
  messageCount: number
  outputCount: number
  isPreferred: boolean
  isLatest: boolean
}

export interface ExternalTeamActivitySnapshotPayload {
  available: boolean
  reason?: string
  projectId: string
  taskId: string
  executionTurnId?: string
  provider?: string
  executionUnitKind?: string
  externalInvocationId?: string
  providerSessionId?: string
  invocations: ExternalTeamInvocationSummary[]
  summary?: ExternalTeamActivitySummary
  events: ExternalTeamEventV1[]
  hasMore: boolean
  nextCursor?: { beforeCreatedAt?: string; beforeEventId?: string } | null
  viewGeneration?: number
}
