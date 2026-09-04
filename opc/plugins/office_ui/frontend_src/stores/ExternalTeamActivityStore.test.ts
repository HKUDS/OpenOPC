import assert from 'node:assert/strict'

import {
  INITIAL_EXTERNAL_TEAM_ACTIVITY_STATE,
  normalizeExternalTeamEvent,
  normalizeExternalTeamSnapshot,
  reduceExternalTeamActivityStore,
} from './ExternalTeamActivityStore'
import type { ExternalTeamActivityStoreState } from './ExternalTeamActivityStore'

function event(overrides: Record<string, unknown> = {}) {
  return normalizeExternalTeamEvent({
    schema_version: 1,
    event_id: 'event-1',
    provider: 'jiuwenswarm',
    project_id: 'project-a',
    task_id: 'task-a',
    opc_session_id: 'session-a',
    external_invocation_id: 'invocation-a',
    sequence: 1,
    occurred_at: '2026-09-03T10:00:00Z',
    kind: 'runtime_ready',
    ...overrides,
  })!
}

function reduceFixture(events: ReturnType<typeof event>[]): ExternalTeamActivityStoreState {
  return events.reduce(
    (current, item) => reduceExternalTeamActivityStore(current, { type: 'event', event: item }),
    INITIAL_EXTERNAL_TEAM_ACTIVITY_STATE,
  )
}

// Fixture 1: the leader completes directly without inventing members or tasks.
const leaderOnlyState = reduceFixture([
  event({ event_id: 'direct-1', kind: 'runtime_ready' }),
  event({ event_id: 'direct-2', sequence: 2, kind: 'leader_output', output: 'Direct result' }),
  event({ event_id: 'direct-3', sequence: 3, kind: 'team_completed' }),
])
const leaderOnlyRecord = Object.values(leaderOnlyState.records)[0]
assert.equal(leaderOnlyRecord.summary.mode, 'completed')
assert.equal(leaderOnlyRecord.summary.counts.members, 0)
assert.equal(leaderOnlyRecord.summary.counts.tasks, 0)

// Fixture 2: three observed members run in parallel before leader synthesis.
const parallelState = reduceFixture([
  event({ event_id: 'parallel-1', kind: 'runtime_ready' }),
  ...['research', 'analysis', 'review'].map((name, index) => event({
    event_id: `parallel-member-${name}`,
    sequence: index + 2,
    kind: 'member_spawned',
    member: { member_id: name, name, status: 'working' },
  })),
  ...['research', 'analysis', 'review'].map((name, index) => event({
    event_id: `parallel-task-${name}`,
    sequence: index + 5,
    kind: 'task_claimed',
    task: { task_id: `task-${name}`, title: name, assignee: name },
  })),
  event({ event_id: 'parallel-output', sequence: 8, kind: 'leader_output', output: 'Synthesis' }),
])
const parallelRecord = Object.values(parallelState.records)[0]
assert.equal(parallelRecord.summary.mode, 'team_active')
assert.equal(parallelRecord.summary.leaderState, 'synthesizing')
assert.equal(parallelRecord.summary.counts.members, 3)
assert.equal(parallelRecord.summary.counts.tasksActive, 3)

// Fixture 3: a failed member is restarted and recovered under the same identity.
const retryState = reduceFixture([
  event({ event_id: 'retry-1', kind: 'member_spawned', member: { member_id: 'review', status: 'working' } }),
  event({ event_id: 'retry-2', sequence: 2, kind: 'member_status_changed', member: { member_id: 'review', status: 'failed' } }),
  event({ event_id: 'retry-3', sequence: 3, kind: 'member_restarted', member: { member_id: 'review', status: 'working', restart_count: 1 } }),
])
const retryRecord = Object.values(retryState.records)[0]
assert.equal(retryRecord.summary.counts.members, 1)
assert.equal(retryRecord.summary.counts.membersActive, 1)
assert.equal(retryRecord.summary.members[0].restartCount, 1)

const shutdownState = reduceFixture([
  event({ event_id: 'shutdown-1', kind: 'member_spawned', member: { member_id: 'worker', status: 'working' } }),
  event({ event_id: 'shutdown-2', sequence: 2, kind: 'member_shutdown', member: { member_id: 'worker' } }),
])
assert.equal(Object.values(shutdownState.records)[0].summary.members[0].status, 'shutdown')
assert.equal(Object.values(shutdownState.records)[0].summary.counts.membersCompleted, 1)

let state = reduceExternalTeamActivityStore(INITIAL_EXTERNAL_TEAM_ACTIVITY_STATE, {
  type: 'event',
  event: event(),
})
state = reduceExternalTeamActivityStore(state, {
  type: 'event',
  event: event({
    event_id: 'event-3',
    sequence: 3,
    occurred_at: '2026-09-03T10:00:03Z',
    kind: 'member_status_changed',
    member: { member_id: 'worker-a', status: 'completed' },
  }),
})
// Late delivery of an older event must not regress the member.
state = reduceExternalTeamActivityStore(state, {
  type: 'event',
  event: event({
    event_id: 'event-2',
    sequence: 2,
    occurred_at: '2026-09-03T10:00:02Z',
    kind: 'member_spawned',
    member: { member_id: 'worker-a', status: 'working' },
  }),
})
const recordA = Object.values(state.records)[0]
assert.equal(recordA.summary.members[0].status, 'completed')
assert.equal(recordA.summary.counts.membersCompleted, 1)
assert.equal(recordA.events.length, 3)

// Duplicate replay is idempotent.
const replayed = reduceExternalTeamActivityStore(state, { type: 'event', event: event() })
assert.equal(replayed, state)

// Invocation, task, and project are independent keys.
state = reduceExternalTeamActivityStore(state, {
  type: 'event',
  event: event({
    event_id: 'event-other',
    project_id: 'project-b',
    task_id: 'task-b',
    external_invocation_id: 'invocation-b',
  }),
})
assert.equal(Object.keys(state.records).length, 2)

// Reusing one task for a new invocation must retain two independent records.
state = reduceExternalTeamActivityStore(state, {
  type: 'event',
  event: event({
    event_id: 'event-new-invocation',
    external_invocation_id: 'invocation-a-2',
    kind: 'member_spawned',
    member: { member_id: 'worker-new', status: 'working' },
  }),
})
const oldInvocation = state.records['project-a\u0000task-a\u0000invocation-a']
const newInvocation = state.records['project-a\u0000task-a\u0000invocation-a-2']
assert.deepEqual(oldInvocation.summary.members.map(item => item.memberId), ['worker-a'])
assert.deepEqual(newInvocation.summary.members.map(item => item.memberId), ['worker-new'])

let unavailableState = reduceExternalTeamActivityStore(INITIAL_EXTERNAL_TEAM_ACTIVITY_STATE, {
  type: 'loading', projectId: 'project-a', taskId: 'task-a', invocationId: 'missing-invocation',
})
unavailableState = reduceExternalTeamActivityStore(unavailableState, {
  type: 'snapshot',
  payload: normalizeExternalTeamSnapshot({
    available: false,
    reason: 'no_team_telemetry',
    project_id: 'project-a',
    task_id: 'task-a',
    external_invocation_id: 'missing-invocation',
    events: [],
    has_more: false,
  })!,
})
assert.equal(unavailableState.records['project-a\u0000task-a\u0000missing-invocation'].loading, false)

// Fixture 4: after disconnect/replay, a delayed snapshot cannot overwrite a newer live event.
const snapshot = normalizeExternalTeamSnapshot({
  available: true,
  project_id: 'project-a',
  task_id: 'task-a',
  external_invocation_id: 'invocation-a',
  invocations: [
    {
      external_invocation_id: 'invocation-a', event_count: 12,
      member_count: 1, task_count: 1, message_count: 2, output_count: 1,
      is_preferred: true, is_latest: false,
    },
    {
      external_invocation_id: 'invocation-a-2', event_count: 2,
      member_count: 0, task_count: 0, message_count: 0, output_count: 0,
      is_preferred: false, is_latest: true,
    },
  ],
  summary: {
    schema_version: 1,
    provider: 'jiuwenswarm',
    mode: 'team_active',
    leader_state: 'working',
    members: [{ member_id: 'worker-a', status: 'working', updated_at: '2026-09-03T10:00:02Z' }],
    tasks: [],
    counts: { members: 1, members_active: 1 },
    last_event_at: '2026-09-03T10:00:02Z',
  },
  events: [],
  has_more: false,
})!
state = reduceExternalTeamActivityStore(state, { type: 'snapshot', payload: snapshot })
const scopedRecord = Object.values(state.records).find(item => item.projectId === 'project-a')!
assert.equal(scopedRecord.summary.members[0].status, 'completed')
assert.equal(scopedRecord.invocations.length, 2)
assert.equal(scopedRecord.invocations[0].isPreferred, true)

// Live events from another invocation must not switch an explicitly selected
// Team Activity phase.
const selectedKey = state.selectedByTask['project-a\u0000task-a']
state = reduceExternalTeamActivityStore(state, {
  type: 'event',
  event: event({
    event_id: 'event-late-recovery',
    external_invocation_id: 'invocation-a-2',
    sequence: 20,
  }),
})
assert.equal(state.selectedByTask['project-a\u0000task-a'], selectedKey)

// Leader-only is explicit and does not invent members or tasks.
assert.equal(recordA.events[0].kind, 'runtime_ready')
assert.equal(recordA.events.filter(item => item.kind === 'runtime_ready').length, 1)

console.log('ExternalTeamActivityStore.test.ts: OK')
