import assert from 'node:assert/strict'
import type { ChatMessage } from '../types/chat'
import type { Session } from '../types/kanban'
import { mapBackendSession, mergeSessionDetailHasMore } from './collabSync'
import { stableMessageTimelineKey, stableResultDeliveryKey } from './messageTimelineIdentity'
import { canonicalizeSessionExecutionIdentity } from './sessionIdentity'
import { resolveCanonicalTurnId, terminalAssistantTurnId } from './turnIdentity'
import { deriveCompanyRuntimeDisplayStatus, getConversationHeaderSession, getConversationSessionView, getWorkItemChildSessions, getWorkItemRoleSessions, mergeConversationMessages, projectSessionConversation, resultSurfaceDedupeKey, selectCompanySummaryMessages } from './workItemSessions'

function makeSession(overrides: Partial<Session> & Pick<Session, 'taskId' | 'channelId' | 'title' | 'status' | 'columnId' | 'assigneeIds' | 'priority' | 'tags' | 'progressLog' | 'createdAt' | 'updatedAt' | 'messageCount'>): Session {
  return {
    projectId: 'test-project',
    ...overrides,
  }
}

const parent = makeSession({
  taskId: 'root-task',
  channelId: 'session:root-task',
  title: 'Root',
  status: 'running',
  columnId: 'in-progress',
  assigneeIds: [],
  priority: null,
  tags: [],
  progressLog: [],
  createdAt: 1,
  updatedAt: 10,
  messageCount: 1,
  mode: 'primary',
  originTaskId: 'root-task',
})

const child = makeSession({
  taskId: 'child-task',
  channelId: 'session:child-task',
  title: 'CEO Intake',
  status: 'done',
  columnId: 'done',
  assigneeIds: ['ceo'],
  priority: null,
  tags: [],
  progressLog: [],
  createdAt: 2,
  updatedAt: 11,
  messageCount: 2,
  mode: 'child',
  parentSessionId: 'parent-session-id-that-has-not-arrived-yet',
  originTaskId: 'root-task',
})

const matches = getWorkItemChildSessions(parent, [parent, child])
assert.equal(matches.length, 1)
assert.equal(matches[0]?.taskId, 'child-task')

const companyParent = makeSession({
  taskId: 'company-root',
  channelId: 'session:company-root',
  sessionId: 'company-root-session',
  title: 'Work-Item Runtime Root',
  status: 'running',
  columnId: 'in-progress',
  assigneeIds: [],
  priority: null,
  tags: [],
  progressLog: [],
  createdAt: 1,
  updatedAt: 10,
  messageCount: 1,
  mode: 'primary',
  execMode: 'company',
  originTaskId: 'company-root',
})

const companyChild = makeSession({
  taskId: 'company-child',
  channelId: 'session:company-child',
  title: 'CTO Delegation',
  status: 'running',
  columnId: 'in-progress',
  assigneeIds: ['cto'],
  priority: null,
  tags: [],
  progressLog: [],
  createdAt: 2,
  updatedAt: 25,
  messageCount: 4,
  mode: 'child',
  parentSessionId: 'company-root-session',
  originTaskId: 'company-root',
})

const companyProjection = projectSessionConversation(companyParent, [companyChild])
assert.deepEqual(
  companyProjection.timelineSessions.map((session) => session.taskId),
  ['company-root', 'company-child'],
)
assert.equal(companyProjection.displaySession?.taskId, 'company-root')
assert.equal(companyProjection.runtimeSession?.taskId, 'company-child')
assert.equal(companyProjection.projectedFromChild, false)

const idleChildWithNewerInboxTimestamp = makeSession({
  ...companyChild,
  taskId: 'company-idle-child',
  channelId: 'session:company-idle-child',
  title: 'Idle Child Inbox Update',
  updatedAt: 1_000_000,
  messageCount: 0,
  progressLog: [],
})
const stableCompanyProjection = projectSessionConversation(companyParent, [idleChildWithNewerInboxTimestamp])
assert.equal(stableCompanyProjection.runtimeSession?.taskId, 'company-root')

const companyHeaderView = getConversationHeaderSession(
  {
    ...companyParent,
    workItemRoleName: 'CEO',
    employeeAssignment: {
      name: 'Root Employee',
      category: 'leadership',
    },
  },
  {
    ...companyChild,
    workItemRoleName: 'CTO',
    contextTokens: 0,
    contextWindow: 128000,
    inputTokens: 11,
    outputTokens: 22,
    totalTokens: 33,
    turnCostUsd: 0.001,
    sessionCostUsd: 0.002,
    selectedExecutionAgent: 'codex',
    employeeAssignment: {
      name: 'Child Employee',
      category: 'engineering',
    },
  },
  [companyParent, companyChild],
)
assert.equal(companyHeaderView?.taskId, 'company-root')
assert.equal(companyHeaderView?.workItemRoleName, 'CEO')
assert.equal(companyHeaderView?.employeeAssignment?.name, 'Root Employee')

const resultMessage = (id: string, channelId: string, content: string, metadata: ChatMessage['metadata'], sender = 'chao'): ChatMessage => ({
  id,
  channelId,
  sender,
  senderName: sender === 'system' ? 'Company Member' : 'Chao',
  content,
  timestamp: 1000 + id.length,
  mentions: [],
  metadata,
})

const finalBody = 'Final delivery is ready with a long enough body to be considered the same user-visible result across transcript mirrors and worker notifications.'
const mergedDeliveryMessages = mergeConversationMessages([
  [
    resultMessage(
      'opc-top-level',
      'session:company-root',
      finalBody,
      { source: 'engine', transcript_kind: 'top_level_reply' },
      'assistant',
    ),
  ],
  [
    resultMessage(
      'parent-mirror',
      'session:company-root',
      `**Deliver final result to user: Chao Intake**: ${finalBody}`,
      { source: 'engine', transcript_kind: 'child_result' },
    ),
  ],
  [
    resultMessage(
      'child-direct',
      'session:company-child',
      finalBody,
      { source: 'engine', transcript_kind: 'child_task_result' },
    ),
    resultMessage(
      'worker-note',
      'session:company-child',
      finalBody,
      { source: 'runtime_event', kind: 'worker_notification', notification_kind: 'task_complete' },
      'system',
    ),
  ],
])

assert.equal(mergedDeliveryMessages.length, 1)
assert.equal(mergedDeliveryMessages[0]?.id, 'child-direct')

const earlierResult = {
  ...resultMessage(
    'earlier-parent-result',
    'session:company-root',
    finalBody,
    { source: 'engine', transcript_kind: 'child_result' },
  ),
  timestamp: 900,
}
const authoritativeResult = {
  ...resultMessage(
    'later-authoritative-result',
    'session:company-child',
    finalBody,
    { source: 'engine', transcript_kind: 'child_task_result' },
  ),
  timestamp: 1_100,
}
for (const groups of [
  [[earlierResult], [authoritativeResult]],
  [[authoritativeResult], [earlierResult]],
]) {
  const result = mergeConversationMessages(groups)
  assert.equal(result.length, 1)
  assert.equal(result[0]?.id, 'later-authoritative-result')
  assert.equal(result[0]?.timestamp, 900, 'result chronology must not depend on channel traversal order')
}

const pendingCheckpoint = {
  ...resultMessage(
    'pending-checkpoint-surface',
    'session:company-root',
    'Approval required.',
    { checkpoint_id: 'shared-checkpoint', checkpoint_type: 'tool_permission', status: 'pending' },
    'system',
  ),
  timestamp: 1_200,
}
const resolvedCheckpoint = {
  ...resultMessage(
    'resolved-checkpoint-surface',
    'session:company-child',
    'Approval required.',
    { checkpoint_id: 'shared-checkpoint', checkpoint_type: 'tool_permission', status: 'resolved' },
    'system',
  ),
  timestamp: 1_300,
}
const mergedCheckpoint = mergeConversationMessages([[pendingCheckpoint], [resolvedCheckpoint]])
assert.equal(mergedCheckpoint.length, 1)
assert.equal(mergedCheckpoint[0]?.id, 'pending-checkpoint-surface')
assert.equal(mergedCheckpoint[0]?.timestamp, 1_200)
assert.equal(mergedCheckpoint[0]?.metadata?.status, 'resolved')

const companySummaryMessages = selectCompanySummaryMessages([
  resultMessage(
    'parent-user',
    'session:company-root',
    'Please investigate the issue.',
    { source: 'ui' },
    'user',
  ),
  resultMessage(
    'child-transient',
    'session:company-child',
    'A child draft or internal assistant turn must stay out of the parent transcript.',
    { source: 'runtime_event', transcript_kind: 'runtime_v2_assistant' },
    'assistant',
  ),
  resultMessage(
    'canonical-role-result',
    'session:company-child',
    'The canonical role delivery remains visible in the company summary.',
    { source: 'engine', transcript_kind: 'company_role_result' },
    'assistant',
  ),
  resultMessage(
    'summary-company-final',
    'session:company-child',
    'A summary-visible company final remains when no canonical role mirror exists.',
    { source: 'engine', kind: 'runtime_v2_company_assistant', detail_visibility: 'summary' },
    'assistant',
  ),
  {
    ...resultMessage(
      'parent-full-only-terminal',
      'session:company-root',
      'A full-only parent surface must neither render nor suppress the committed child summary.',
      {
        source: 'engine',
        transcript_kind: 'runtime_v2_assistant',
        detail_visibility: 'full',
        canonical_turn_id: 'shared-terminal-turn',
      },
      'assistant',
    ),
    timestamp: 1400,
  },
  {
    ...resultMessage(
      'summary-terminal-a',
      'session:company-child',
      'First authoritative terminal for one shared canonical turn.',
      {
        source: 'engine',
        transcript_kind: 'runtime_v2_assistant',
        detail_visibility: 'summary',
        canonical_turn_id: 'shared-terminal-turn',
      },
      'assistant',
    ),
    timestamp: 1200,
  },
  {
    ...resultMessage(
      'summary-terminal-b',
      'session:company-sibling',
      'A second terminal surface with different content must not duplicate the turn.',
      {
        source: 'engine',
        transcript_kind: 'runtime_v2_assistant',
        detail_visibility: 'summary',
        canonical_turn_id: 'shared-terminal-turn',
      },
      'assistant',
    ),
    timestamp: 1300,
  },
  resultMessage(
    'child-checkpoint',
    'session:company-child',
    'Approval is required.',
    { checkpoint_id: 'checkpoint-child', checkpoint_type: 'company_work_item_gate' },
    'assistant',
  ),
  resultMessage(
    'child-checkpoint-response',
    'session:company-child',
    'Approved.',
    { response_to_checkpoint_id: 'checkpoint-child', ui_message_id: 'ui-checkpoint-response' },
    'user',
  ),
], 'session:company-root')
assert.deepEqual(
  companySummaryMessages.map(message => message.id).sort(),
  [
    'parent-user',
    'canonical-role-result',
    'child-checkpoint',
    'child-checkpoint-response',
  ].sort(),
)

const companyRuntimeTurn = resultMessage(
  'company-runtime-turn',
  'session:company-child',
  'A company runtime terminal surface.',
  {
    source: 'engine',
    transcript_kind: 'runtime_v2_company_assistant',
    canonical_turn_id: 'company-turn-0009',
    result_delivery_id: 'company-delivery-0009',
  },
  'assistant',
)
assert.equal(
  stableMessageTimelineKey(companyRuntimeTurn),
  'turn:assistant:company-turn-0009',
)

const conversationOnlyRuntimeTurn = resultMessage(
  'company-runtime-conversation-turn',
  'session:company-child',
  'A terminal surface with only its conversation identity.',
  {
    source: 'engine',
    transcript_kind: 'runtime_v2_company_assistant',
    conversation_turn_id: 'conversation-turn-only',
    turn_id: 'iteration-turn-must-not-win',
    execution_turn_id: 'execution-turn-must-not-win',
    detail_visibility: 'summary',
  },
  'assistant',
)
assert.equal(
  resolveCanonicalTurnId(conversationOnlyRuntimeTurn.metadata),
  'conversation-turn-only',
)
assert.equal(terminalAssistantTurnId(conversationOnlyRuntimeTurn), 'conversation-turn-only')
assert.equal(
  terminalAssistantTurnId({
    ...conversationOnlyRuntimeTurn,
    metadata: {
      ...conversationOnlyRuntimeTurn.metadata,
      detail_visibility: 'full',
    },
  }),
  '',
  'a company tool-call iteration must not suppress the active draft',
)
assert.equal(
  stableMessageTimelineKey(conversationOnlyRuntimeTurn),
  'turn:assistant:conversation-turn-only',
)
assert.equal(
  terminalAssistantTurnId({
    ...conversationOnlyRuntimeTurn,
    metadata: {
      ...conversationOnlyRuntimeTurn.metadata,
      transcript_kind: 'company_role_result',
    },
  }),
  'conversation-turn-only',
  'committed result kinds must participate in terminal-turn matching',
)
assert.equal(
  stableMessageTimelineKey({
    ...conversationOnlyRuntimeTurn,
    metadata: {
      ...conversationOnlyRuntimeTurn.metadata,
      transcript_kind: 'company_role_result',
      source_task_id: 'committed-source-task',
    },
  }),
  'result:source-task:committed-source-task',
  'a committed terminal may expose its turn for matching without taking the runtime draft key',
)

const project0009CtoResult = [
  'Both work items have been successfully dispatched to my senior engineer. Here\'s the status:',
  '',
  '## Dispatch Summary',
  '',
  '**Work Item 1: OpenOPC Source Code Architecture Deep-Dive Analysis**',
  '- ID: `1ed5f5f1-ac41-49a1-b1fa-23bbc9adab82`',
  '- Owner: senior_engineer',
  '- Scope: `openopc-source-analysis`',
  '- Output: `/data2/bjdwhzzh/project-hku/OpenOPC_workplace/0009/openopc-architecture-analysis.md`',
  '- Covers: Layered architecture, collaboration policy, seat executor pattern, and self-evolution mechanisms.',
  '',
  '**Work Item 2: External Multi-Agent Frameworks Architecture Research**',
  '- ID: `d0307208-6b95-44c1-9b51-6bf073bbdcef`',
  '- Owner: senior_engineer',
  '- Scope: `external-frameworks-research`',
  '- Output: `/data2/bjdwhzzh/project-hku/OpenOPC_workplace/0009/external-frameworks-analysis.md`',
  '',
  'Both are independent and can execute in parallel. The runtime will monitor their completion.',
].join('\n')

const project0009WorkItemSuffix = project0009CtoResult.slice(
  project0009CtoResult.indexOf('OpenOPC Source Code Architecture Deep-Dive Analysis'),
)
const project0009IdSuffix = project0009CtoResult.slice(
  project0009CtoResult.indexOf('`1ed5f5f1-ac41-49a1-b1fa-23bbc9adab82`'),
)

// Content fallback must not treat arbitrary Markdown colons as removable
// narrative wrappers. The old normalization repeatedly turned the full 0009
// result into these two shorter variants, which changed the rendered height.
const fallback0009Full = resultMessage(
  '0009-fallback-full',
  'session:company-root',
  project0009CtoResult,
  { source: 'engine', transcript_kind: 'child_result' },
  'assistant',
)
const fallback0009WorkItem = resultMessage(
  '0009-fallback-work-item',
  'session:company-child',
  project0009WorkItemSuffix,
  { source: 'engine', transcript_kind: 'runtime_v2_assistant' },
  'assistant',
)
const fallback0009Id = resultMessage(
  '0009-fallback-id',
  'session:company-child',
  project0009IdSuffix,
  { source: 'engine', transcript_kind: 'runtime_v2_assistant' },
  'assistant',
)
assert.notEqual(resultSurfaceDedupeKey(fallback0009Full), resultSurfaceDedupeKey(fallback0009WorkItem))
assert.notEqual(resultSurfaceDedupeKey(fallback0009WorkItem), resultSurfaceDedupeKey(fallback0009Id))

const committed0009Parent = {
  ...resultMessage(
    '0009-parent-result',
    'session:company-root',
    project0009CtoResult,
    {
      source: 'engine',
      transcript_kind: 'child_result',
      source_task_id: 'cto-task-0009',
    },
    'assistant',
  ),
  timestamp: 2_000,
}
const committed0009Child = {
  ...resultMessage(
    '0009-child-result',
    'session:company-child',
    project0009CtoResult,
    {
      source: 'engine',
      transcript_kind: 'child_task_result',
      task_id: 'cto-task-0009',
    },
    'assistant',
  ),
  timestamp: 2_010,
}
const raw0009WorkItem = {
  ...fallback0009WorkItem,
  metadata: {
    ...fallback0009WorkItem.metadata,
    detail_visibility: 'summary' as const,
    canonical_turn_id: 'cto-turn-0009',
  },
  timestamp: 2_020,
}
const raw0009Id = {
  ...fallback0009Id,
  metadata: {
    ...fallback0009Id.metadata,
    detail_visibility: 'summary' as const,
    canonical_turn_id: 'cto-turn-0009',
  },
  timestamp: 2_030,
}

for (const messages of [
  [committed0009Parent, committed0009Child, raw0009WorkItem, raw0009Id],
  [raw0009Id, raw0009WorkItem, committed0009Child, committed0009Parent],
]) {
  const summary = selectCompanySummaryMessages(messages, 'session:company-root')
  assert.equal(summary.length, 1)
  assert.equal(summary[0]?.id, '0009-child-result')
  assert.equal(summary[0]?.content, project0009CtoResult)
  assert.equal(stableMessageTimelineKey(summary[0]!), 'result:source-task:cto-task-0009')
}

const sameRoleText = 'The role completed its independent architecture analysis and committed the result.'
const multiRoleSummary = selectCompanySummaryMessages([
  resultMessage(
    '0009-cto-role-result',
    'session:company-cto',
    sameRoleText,
    {
      source: 'engine',
      transcript_kind: 'company_role_result',
      work_item_projection_id: '0009-cto-work-item',
    },
    'assistant',
  ),
  resultMessage(
    '0009-coo-role-result',
    'session:company-coo',
    sameRoleText,
    {
      source: 'engine',
      transcript_kind: 'company_role_result',
      work_item_projection_id: '0009-coo-work-item',
    },
    'assistant',
  ),
], 'session:company-root')
assert.deepEqual(
  multiRoleSummary.map(message => stableMessageTimelineKey(message)).sort(),
  [
    'result:work-item:0009-coo-work-item',
    'result:work-item:0009-cto-work-item',
  ],
)

const sharedConversationTurn = 'shared-company-conversation-turn'
const parallelRoleResults = [
  resultMessage(
    'parallel-cto-result',
    'session:company-cto',
    'CTO completed the architecture assessment.',
    {
      source: 'engine',
      transcript_kind: 'company_role_result',
      canonical_turn_id: sharedConversationTurn,
      work_item_projection_id: 'architecture-assessment',
    },
    'assistant',
  ),
  resultMessage(
    'parallel-coo-result',
    'session:company-coo',
    'COO completed the feature assessment.',
    {
      source: 'engine',
      transcript_kind: 'company_role_result',
      canonical_turn_id: sharedConversationTurn,
      work_item_projection_id: 'feature-assessment',
    },
    'assistant',
  ),
]
assert.deepEqual(
  parallelRoleResults.map(resultSurfaceDedupeKey),
  [
    'result:work-item:architecture-assessment',
    'result:work-item:feature-assessment',
  ],
  'parallel committed roles must use work-item identity before a shared conversation turn',
)
assert.equal(
  selectCompanySummaryMessages(parallelRoleResults, 'session:company-root').length,
  2,
)

const versionedSourceResult = resultMessage(
  'versioned-source-result',
  'session:company-child',
  'Versioned result.',
  {
    source: 'engine',
    transcript_kind: 'company_role_result_retry',
    source_task_id: 'source-task-versioned',
    retry_count: 2,
    delivery_revision: 4,
  } as ChatMessage['metadata'],
  'assistant',
)
assert.equal(
  stableResultDeliveryKey(versionedSourceResult),
  'source-task:source-task-versioned:attempt:2:revision:4',
)

const fullEqualPriorityResult = {
  ...resultMessage(
    'full-equal-priority',
    'session:company-child-a',
    'The complete authoritative body includes every required architectural conclusion and its supporting rationale.',
    {
      source: 'engine',
      transcript_kind: 'company_role_result',
      result_delivery_id: 'deterministic-delivery',
    },
    'assistant',
  ),
  timestamp: 2_100,
}
const truncatedEqualPriorityResult = {
  ...resultMessage(
    'truncated-equal-priority',
    'session:company-child-b',
    'supporting rationale.',
    {
      source: 'engine',
      transcript_kind: 'company_role_result',
      result_delivery_id: 'deterministic-delivery',
    },
    'assistant',
  ),
  timestamp: 2_200,
}
for (const groups of [
  [[fullEqualPriorityResult], [truncatedEqualPriorityResult]],
  [[truncatedEqualPriorityResult], [fullEqualPriorityResult]],
]) {
  const merged = mergeConversationMessages(groups)
  assert.equal(merged.length, 1)
  assert.equal(merged[0]?.id, 'full-equal-priority')
  assert.equal(merged[0]?.content, fullEqualPriorityResult.content)
  assert.equal(merged[0]?.timestamp, 2_100)
  assert.equal(
    stableMessageTimelineKey(merged[0]!),
    'result:delivery:deterministic-delivery',
  )
  const replayed = mergeConversationMessages([
    merged,
    [truncatedEqualPriorityResult],
  ])
  assert.equal(replayed.length, 1)
  assert.equal(replayed[0]?.id, 'full-equal-priority')
  assert.equal(replayed[0]?.content, fullEqualPriorityResult.content)
}

const equalLengthStableWinner = {
  ...resultMessage(
    'z-stable-winner',
    'session:company-child-a',
    'BBBB',
    {
      source: 'engine',
      transcript_kind: 'company_role_result',
      result_delivery_id: 'equal-length-delivery',
    },
    'assistant',
  ),
  timestamp: 3_200,
}
const equalLengthLoser = {
  ...resultMessage(
    'a-stable-loser',
    'session:company-child-b',
    'AAAA',
    {
      source: 'engine',
      transcript_kind: 'company_role_result',
      result_delivery_id: 'equal-length-delivery',
    },
    'assistant',
  ),
  timestamp: 3_100,
}
for (const groups of [
  [[equalLengthStableWinner], [equalLengthLoser]],
  [[equalLengthLoser], [equalLengthStableWinner]],
]) {
  const firstMerge = mergeConversationMessages(groups)
  assert.equal(firstMerge[0]?.id, 'z-stable-winner')
  assert.equal(firstMerge[0]?.content, 'BBBB')
  assert.equal(firstMerge[0]?.timestamp, 3_100)
  const replayed = mergeConversationMessages([firstMerge, [equalLengthLoser]])
  assert.equal(replayed[0]?.id, 'z-stable-winner')
  assert.equal(replayed[0]?.content, 'BBBB')
}
assert.equal(companyHeaderView?.status, 'running')
assert.equal(companyHeaderView?.contextTokens, 0)
assert.equal(companyHeaderView?.contextWindow, 128000)
assert.equal(companyHeaderView?.inputTokens, 11)
assert.equal(companyHeaderView?.outputTokens, 22)
assert.equal(companyHeaderView?.totalTokens, 33)
assert.equal(companyHeaderView?.turnCostUsd, 0.001)
assert.equal(companyHeaderView?.sessionCostUsd, 0.002)
assert.equal(companyHeaderView?.selectedExecutionAgent, 'codex')

const customOrgRoot = makeSession({
  taskId: 'custom-root',
  channelId: 'session:custom-root',
  sessionId: 'custom-root-session',
  title: 'Custom Work-Item Runtime Root',
  status: 'running',
  columnId: 'in-progress',
  assigneeIds: ['chief_architect'],
  priority: null,
  tags: [],
  progressLog: [],
  createdAt: 1,
  updatedAt: 30,
  messageCount: 1,
  mode: 'primary',
  execMode: 'custom',
  isCompanyRuntime: true,
  originTaskId: 'custom-root',
  workItemRoleId: 'chief_architect',
  workItemRoleName: 'Chief Architect',
})

const customOrgChild = makeSession({
  taskId: 'custom-child',
  channelId: 'session:custom-child',
  title: 'Research Lead Turn',
  status: 'running',
  columnId: 'in-progress',
  assigneeIds: ['research_lead'],
  priority: null,
  tags: [],
  progressLog: [],
  createdAt: 2,
  updatedAt: 31,
  messageCount: 2,
  mode: 'child',
  parentSessionId: 'custom-root-session',
  originTaskId: 'custom-root',
  workItemRoleId: 'research_lead',
  workItemRoleName: 'Research Lead',
})

assert.deepEqual(
  getWorkItemChildSessions(customOrgRoot, [customOrgRoot, customOrgChild]).map((session) => session.taskId),
  ['custom-child'],
)
assert.deepEqual(
  getWorkItemRoleSessions(customOrgRoot, [customOrgRoot, customOrgChild]).map((session) => session.workItemRoleName),
  ['Chief Architect', 'Research Lead'],
)

const mergedCustomView = getConversationSessionView(
  {
    ...customOrgRoot,
    status: 'failed',
    roleWorkItems: {
      chief_architect: {
        roleKey: 'chief_architect',
        roleId: 'chief_architect',
        roleName: 'Chief Architect',
        runtimeStatus: 'idle',
        aggregatedStatus: 'active',
        workItems: [],
      },
    },
  },
  {
    ...customOrgChild,
    runtimeControlState: 'running',
    canStop: true,
  },
  [customOrgRoot, customOrgChild],
)
assert.equal(mergedCustomView?.runtimeControlState, 'running')
assert.equal(mergedCustomView?.canStop, true)
assert.equal(mergedCustomView?.roleWorkItems?.chief_architect.roleName, 'Chief Architect')
assert.equal(mergedCustomView?.status, 'running')
assert.equal(deriveCompanyRuntimeDisplayStatus(mergedCustomView), 'running')

const failedCustomView = getConversationSessionView(
  {
    ...customOrgRoot,
    status: 'failed',
    roleWorkItems: {
      chief_architect: {
        roleKey: 'chief_architect',
        roleId: 'chief_architect',
        roleName: 'Chief Architect',
        runtimeStatus: 'idle',
        aggregatedStatus: 'failed',
        workItems: [],
      },
    },
  },
  null,
  [],
)
assert.equal(failedCustomView?.status, 'failed')

const runtimeRollupOverridesStaleActiveView = getConversationSessionView(
  {
    ...customOrgRoot,
    status: 'failed',
    roleWorkItems: {
      chief_architect: {
        roleKey: 'chief_architect',
        roleId: 'chief_architect',
        roleName: 'Chief Architect',
        runtimeStatus: 'idle',
        aggregatedStatus: 'failed',
        workItems: [],
      },
    },
  },
  {
    ...customOrgRoot,
    status: 'failed',
    roleWorkItems: {
      chief_architect: {
        roleKey: 'chief_architect',
        roleId: 'chief_architect',
        roleName: 'Chief Architect',
        runtimeStatus: 'tool_active',
        aggregatedStatus: 'active',
        workItems: [],
      },
    },
  },
  [],
)
assert.equal(runtimeRollupOverridesStaleActiveView?.status, 'running')

const companyIdentity = canonicalizeSessionExecutionIdentity({
  taskId: 'company-stale-org',
  execMode: 'company',
  companyProfile: 'custom',
  orgId: 'quantum_harbor',
})
assert.equal(companyIdentity.execMode, 'company')
assert.equal(companyIdentity.companyProfile, 'corporate')
assert.equal(companyIdentity.orgId, undefined)

const customIdentity = canonicalizeSessionExecutionIdentity({
  taskId: 'custom-org',
  execMode: 'org',
  companyProfile: 'corporate',
  orgId: 'quantum_harbor',
})
assert.equal(customIdentity.execMode, 'org')
assert.equal(customIdentity.companyProfile, 'custom')
assert.equal(customIdentity.orgId, 'quantum_harbor')

const mappedCompanySession = mapBackendSession({
  task_id: 'mapped-company',
  channel_id: 'session:mapped-company',
  title: 'Mapped Company',
  status: 'running',
  column_id: 'in-progress',
  assignee_ids: [],
  tags: [],
  created_at: 1,
  updated_at: 2,
  exec_mode: 'company',
  company_profile: 'custom',
  org_id: 'quantum_harbor',
})
assert.equal(mappedCompanySession.execMode, 'company')
assert.equal(mappedCompanySession.companyProfile, 'corporate')
assert.equal(mappedCompanySession.orgId, undefined)

assert.equal(
  mergeSessionDetailHasMore(false, true, false),
  false,
  'a cursorless live refresh must not reopen an exhausted history boundary',
)
assert.equal(
  mergeSessionDetailHasMore(false, true, true),
  true,
  'a real cursor page may advance the scoped history boundary',
)

console.log('workItemSessions origin-task linking checks passed')
