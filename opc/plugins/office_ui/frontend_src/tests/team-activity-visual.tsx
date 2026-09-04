import React from 'react'
import { createRoot } from 'react-dom/client'

import '../index.css'
import { I18nProvider } from '../i18n'
import { JiuwenTeamActivityPanel } from '../kanban/JiuwenTeamActivityPanel'
import type { ExternalTeamActivityRecord } from '../stores/ExternalTeamActivityStore'

const members = [
  { memberId: 'research-a82f19c4', name: 'Research worker', role: 'researcher', mode: 'build_mode', status: 'completed', updatedAt: '2026-09-03T10:27:00Z' },
  { memberId: 'analysis-c31d8b72', name: 'Analysis worker', role: 'analyst', mode: 'build_mode', status: 'running', updatedAt: '2026-09-03T10:28:00Z' },
  { memberId: 'review-f9097e11', name: 'Review worker', role: 'reviewer', mode: 'plan_mode', status: 'failed', reason: 'Source validation timed out', restartCount: 1, updatedAt: '2026-09-03T10:28:30Z' },
]
const tasks = [
  { taskId: 'internal-research', title: 'Research recent agent projects', status: 'completed', assignee: 'research-a82f19c4', updatedAt: '2026-09-03T10:27:00Z' },
  { taskId: 'internal-analysis', title: 'Compare technical directions', status: 'running', assignee: 'analysis-c31d8b72', dependencies: ['internal-research'], updatedAt: '2026-09-03T10:28:00Z' },
  { taskId: 'internal-review', title: 'Validate source reliability', status: 'failed', assignee: 'review-f9097e11', updatedAt: '2026-09-03T10:28:30Z' },
  { taskId: 'internal-trends', title: 'Synthesize future trends', status: 'pending', updatedAt: '2026-09-03T10:29:00Z' },
]

const base = {
  schemaVersion: 1 as const,
  provider: 'jiuwenswarm' as const,
  projectId: 'team-visual-project',
  taskId: 'cto-turn',
  opcSessionId: 'company-session',
  executionTurnId: 'cto-turn',
  workItemId: 'work-item-cto',
  workItemProjectionId: 'cto-projection',
  externalInvocationId: 'invocation-42',
  providerSessionId: 'provider-session-42',
  teamId: 'cto-research-team',
}

const events: ExternalTeamActivityRecord['events'] = [
  { ...base, eventId: 'e1', sequence: 1, occurredAt: '2026-09-03T10:21:00Z', kind: 'runtime_ready' },
  { ...base, eventId: 'delegation', sequence: 2, occurredAt: '2026-09-03T10:21:30Z', kind: 'message_broadcast', message: { messageId: 'msg-delegation', fromMember: 'team-leader', content: 'Research worker: collect recent projects. Analysis worker: compare technical directions. Review worker: validate every source before synthesis.' } },
  ...members.map((member, index) => ({ ...base, eventId: `em${index}`, sequence: 3 + index, occurredAt: `2026-09-03T10:22:0${index}Z`, kind: 'member_spawned' as const, member })),
  ...tasks.map((task, index) => ({ ...base, eventId: `et${index}`, sequence: 5 + index, occurredAt: `2026-09-03T10:23:0${index}Z`, kind: 'task_created' as const, task })),
  { ...base, eventId: 'msg1', sequence: 9, occurredAt: '2026-09-03T10:24:00Z', kind: 'message_p2p', message: { messageId: 'msg-1', fromMember: 'research-a82f19c4', toMember: 'analysis-c31d8b72', content: 'The project dataset is ready. Prioritize architecture changes from the last quarter.' } },
  { ...base, eventId: 'restart1', sequence: 10, occurredAt: '2026-09-03T10:28:35Z', kind: 'member_restarted', member: members[2], error: 'Source validation timed out; retry scheduled.' },
  { ...base, eventId: 'out1', sequence: 11, occurredAt: '2026-09-03T10:27:00Z', kind: 'member_output', member: members[0], output: 'Collected the recent projects and primary-source release notes.' },
  { ...base, eventId: 'out2', sequence: 12, occurredAt: '2026-09-03T10:29:00Z', kind: 'leader_output', output: '## Synthesis\n\nThe team combined project research, technical comparison, and source review into one delivery.' },
]

const record: ExternalTeamActivityRecord = {
  key: 'team-visual-project\u0000cto-turn\u0000invocation-42',
  projectId: 'team-visual-project',
  taskId: 'cto-turn',
  externalInvocationId: 'invocation-42',
  available: true,
  loading: false,
  hasMore: false,
  invocations: [
    {
      externalInvocationId: 'invocation-42', eventCount: 18,
      memberCount: 3, taskCount: 4, messageCount: 2, outputCount: 2,
      isPreferred: true, isLatest: false,
    },
    {
      externalInvocationId: 'invocation-recovery', eventCount: 4,
      memberCount: 0, taskCount: 0, messageCount: 0, outputCount: 0,
      isPreferred: false, isLatest: true,
    },
  ],
  events,
  summary: {
    schemaVersion: 1,
    provider: 'jiuwenswarm',
    mode: 'team_active',
    leaderState: 'synthesizing',
    teamId: 'cto-research-team',
    externalInvocationId: 'invocation-42',
    providerSessionId: 'provider-session-42',
    members,
    tasks,
    counts: {
      members: 3,
      membersActive: 1,
      membersCompleted: 1,
      membersFailed: 1,
      tasks: 4,
      tasksActive: 1,
      tasksCompleted: 1,
      tasksFailed: 1,
    },
    lastEventAt: '2026-09-03T10:29:00Z',
    telemetryIncomplete: false,
  },
}

function Fixture() {
  return (
    <div className="app-shell" data-theme="openopc" style={{ background: 'var(--bg)', minHeight: '100vh' }}>
      <div style={{ padding: 32, opacity: .48 }}>
        <h2 style={{ margin: 0 }}>Execution Progress</h2>
        <p>CTO · JiuwenSwarm Team</p>
      </div>
      <JiuwenTeamActivityPanel
        title="CTO · JiuwenSwarm Team"
        coveredRoleIds={['cto', 'senior_engineer', 'devops_engineer', 'env_engineer']}
        record={record}
        onClose={() => undefined}
      />
    </div>
  )
}

createRoot(document.getElementById('root')!).render(<I18nProvider><Fixture /></I18nProvider>)
requestAnimationFrame(() => { window.__teamActivityFixtureReady = true })

declare global {
  interface Window { __teamActivityFixtureReady?: boolean }
}
