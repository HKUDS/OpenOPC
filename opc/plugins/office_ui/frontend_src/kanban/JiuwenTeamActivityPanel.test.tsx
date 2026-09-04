import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { I18nProvider } from '../i18n'
import type { ExternalTeamActivityRecord } from '../stores/ExternalTeamActivityStore'
import { JiuwenTeamActivityPanel } from './JiuwenTeamActivityPanel'

const record: ExternalTeamActivityRecord = {
  key: 'project\u0000task\u0000invocation',
  projectId: 'project',
  taskId: 'task',
  externalInvocationId: 'invocation',
  available: true,
  loading: false,
  hasMore: false,
  invocations: [
    {
      externalInvocationId: 'invocation',
      eventCount: 12,
      memberCount: 1,
      taskCount: 1,
      messageCount: 2,
      outputCount: 1,
      isPreferred: true,
      isLatest: false,
    },
    {
      externalInvocationId: 'invocation-recovery',
      eventCount: 3,
      memberCount: 0,
      taskCount: 0,
      messageCount: 0,
      outputCount: 1,
      isPreferred: false,
      isLatest: true,
    },
  ],
  events: [{
    schemaVersion: 1,
    eventId: 'spawned',
    provider: 'jiuwenswarm',
    projectId: 'project',
    taskId: 'task',
    opcSessionId: 'session',
    externalInvocationId: 'invocation',
    sequence: 1,
    occurredAt: '2026-09-04T10:00:00Z',
    kind: 'member_spawned',
    member: { memberId: 'real-worker-a82f123456', name: 'Research worker', status: 'completed' },
  }],
  summary: {
    schemaVersion: 1,
    provider: 'jiuwenswarm',
    mode: 'team_active',
    leaderState: 'synthesizing',
    members: [{ memberId: 'real-worker-a82f123456', name: 'Research worker', status: 'completed' }],
    tasks: [{ taskId: 'internal-task', title: 'Research recent projects', status: 'completed', assignee: 'real-worker-a82f123456' }],
    counts: {
      members: 1, membersActive: 0, membersCompleted: 1, membersFailed: 0,
      tasks: 1, tasksActive: 0, tasksCompleted: 1, tasksFailed: 0,
    },
    telemetryIncomplete: false,
  },
}

const markup = renderToStaticMarkup(
  React.createElement(I18nProvider, null,
    React.createElement(JiuwenTeamActivityPanel, {
      title: 'CTO · JiuwenSwarm Team',
      coveredRoleIds: ['cto', 'senior_engineer', 'devops_engineer'],
      record,
      onClose: () => undefined,
    }),
  ),
)

assert.match(markup, /CTO · JiuwenSwarm Team/)
assert.match(markup, /Research worker/)
assert.match(markup, /Leader/)
assert.match(markup, /OpenOPC covered roles/)
assert.match(markup, /Overview/)
assert.match(markup, /Tasks/)
assert.match(markup, /Timeline/)
assert.match(markup, /Output/)
assert.match(markup, /Autonomous team formed/)
assert.match(markup, /Main collaboration/)
assert.match(markup, /Run 2/)
// Covered org roles are disclosed separately and never rendered as observed members.
assert.doesNotMatch(markup, /class="team-member-main"><strong>Senior Engineer/)

console.log('JiuwenTeamActivityPanel.test.tsx: OK')
