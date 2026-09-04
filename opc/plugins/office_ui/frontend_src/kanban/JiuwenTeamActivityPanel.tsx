import React, { useCallback, useEffect, useMemo, useState } from 'react'
import type { ExternalTeamActivityRecord } from '../stores/ExternalTeamActivityStore'
import type { ExternalTeamEventV1, ExternalTeamInvocationSummary, ExternalTeamMember, ExternalTeamTask } from '../types/externalTeamActivity'
import { MarkdownBody } from '../chat/MessageList'
import { IconClose, IconTimeline, IconWorkItem } from '../chat/SvgIcons'
import { externalTeamSummaryText } from '../lib/externalTeamActivity'
import { useI18n } from '../i18n'

type TeamTab = 'overview' | 'tasks' | 'timeline' | 'output'
type TimelineFilter = 'highlights' | 'all' | 'members' | 'tasks' | 'messages' | 'errors'
type TimelineCategory = Exclude<TimelineFilter, 'highlights' | 'all'> | 'general'
type Translator = ReturnType<typeof useI18n>['t']

interface JiuwenTeamActivityPanelProps {
  title: string
  coveredRoleIds?: string[]
  record?: ExternalTeamActivityRecord
  fallbackSummary?: import('../types/externalTeamActivity').ExternalTeamActivitySummary
  onClose: () => void
  onLoadOlder?: () => void
  onSelectInvocation?: (invocationId: string) => void
  onOpenRuntimeActivity?: () => void
}

function humanize(value?: string): string {
  return String(value ?? '').trim().replace(/[_.-]+/g, ' ').replace(/\b\w/g, char => char.toUpperCase())
}

function shortId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 8)}…` : value
}

function stateClass(value?: string): string {
  const normalized = String(value ?? '').toLowerCase()
  if (['completed', 'complete', 'done', 'finished', 'shutdown', 'stopped'].includes(normalized)) return 'done'
  if (['failed', 'failure', 'error'].includes(normalized)) return 'failed'
  if (['active', 'busy', 'claimed', 'executing', 'in_progress', 'running', 'working'].includes(normalized)) return 'running'
  return 'pending'
}

function displayState(value?: string): string {
  return humanize(value) || 'Unknown'
}

function taskForMember(tasks: ExternalTeamTask[], member: ExternalTeamMember): ExternalTeamTask | undefined {
  return tasks.find(task => task.assignee === member.memberId || task.assignee === member.name)
}

function eventCategory(event: ExternalTeamEventV1): TimelineCategory {
  if (event.kind === 'team_error' || event.error || event.kind === 'member_restarted') return 'errors'
  if (event.kind.startsWith('member_')) return 'members'
  if (event.kind.startsWith('task_')) return 'tasks'
  if (event.kind.startsWith('message_')) return 'messages'
  return 'general'
}

function eventMatchesFilter(event: ExternalTeamEventV1, filter: TimelineFilter): boolean {
  if (filter === 'highlights') {
    return [
      'runtime_ready', 'team_completed', 'team_error', 'leader_output',
      'member_spawned', 'member_restarted', 'member_shutdown',
      'task_created', 'task_claimed', 'task_completed', 'task_cancelled',
      'task_unblocked', 'message_p2p', 'message_broadcast',
    ].includes(event.kind)
  }
  if (filter === 'all') return true
  if (filter !== 'errors') return eventCategory(event) === filter
  const memberState = String(event.member?.executionStatus || event.member?.status || '').toLowerCase()
  const taskState = String(event.task?.status || '').toLowerCase()
  return event.kind === 'team_error'
    || event.kind === 'member_restarted'
    || Boolean(event.error)
    || ['error', 'failed', 'failure'].includes(memberState)
    || ['cancelled', 'error', 'failed', 'failure'].includes(taskState)
}

function eventTitle(event: ExternalTeamEventV1, t: Translator): string {
  const member = event.member?.name || event.member?.memberId || t('teamActivity.teamMember')
  const task = event.task?.title || event.task?.taskId || t('teamActivity.task')
  if (event.kind === 'runtime_ready') return t('teamActivity.eventLeaderStarted')
  if (event.kind === 'team_completed') return t('teamActivity.eventTeamCompleted')
  if (event.kind === 'team_error') return t('teamActivity.eventTeamError')
  if (event.kind === 'leader_output') return t('teamActivity.leaderOutput')
  if (event.kind === 'member_output') return t('teamActivity.eventMemberOutput', { member })
  if (event.kind === 'member_spawned') return t('teamActivity.eventMemberJoined', { member })
  if (event.kind === 'member_restarted') return t('teamActivity.eventMemberRestarted', { member })
  if (event.kind === 'member_shutdown') return t('teamActivity.eventMemberStopped', { member })
  if (event.kind === 'task_created') return t('teamActivity.eventTaskCreated', { task })
  if (event.kind === 'task_claimed') {
    return t('teamActivity.eventTaskAssigned', { task, member: event.task?.assignee || member })
  }
  if (event.kind === 'task_completed') return t('teamActivity.eventTaskCompleted', { task })
  if (event.kind === 'task_cancelled') return t('teamActivity.eventTaskCancelled', { task })
  if (event.kind === 'task_unblocked') return t('teamActivity.eventTaskUnblocked', { task })
  if (event.kind.startsWith('member_')) {
    return `${member} · ${humanize(event.kind.replace('member_', ''))}`
  }
  if (event.kind.startsWith('task_')) {
    return `${task} · ${humanize(event.kind.replace('task_', ''))}`
  }
  if (event.kind === 'message_p2p') return `${event.message?.fromMember || 'Member'} → ${event.message?.toMember || 'Member'}`
  if (event.kind === 'message_broadcast') return `${event.message?.fromMember || 'Member'} broadcast`
  return humanize(event.rawEventType || event.kind)
}

function eventBody(event: ExternalTeamEventV1): string {
  return event.message?.content || event.output || event.error || event.summary
    || event.member?.reason || event.task?.content || ''
}

function eventDetailLabel(event: ExternalTeamEventV1, t: Translator): string {
  if (event.kind.startsWith('message_')) return t('teamActivity.viewOriginalMessage')
  if (event.kind === 'member_output' || event.kind === 'leader_output') return t('teamActivity.viewOriginalOutput')
  if (event.error || event.kind === 'team_error') return t('teamActivity.viewErrorDetails')
  if (event.kind.startsWith('task_')) return t('teamActivity.viewOriginalTaskDetails')
  return t('teamActivity.viewOriginalDetails')
}

function formatTime(value?: string): string {
  if (!value) return '—'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function CopyId({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  const copy = useCallback(() => {
    void navigator.clipboard?.writeText(value).then(() => {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1200)
    })
  }, [value])
  return (
    <button type="button" className="team-copy-id" onClick={copy} title={value}>
      {shortId(value)}{copied ? ' ✓' : ''}
    </button>
  )
}

function InvocationSelector({ invocations, selectedInvocationId, loading, onSelect }: {
  invocations: ExternalTeamInvocationSummary[]
  selectedInvocationId: string
  loading: boolean
  onSelect?: (invocationId: string) => void
}) {
  const { t } = useI18n()
  if (invocations.length <= 1) return null
  return (
    <section className="team-run-selector" aria-label={t('teamActivity.runPhases')}>
      <header>
        <strong>{t('teamActivity.runPhases')}</strong>
        <span>{t('teamActivity.runPhasesHint')}</span>
      </header>
      <div>
        {invocations.map((invocation, index) => {
          const selected = invocation.externalInvocationId === selectedInvocationId
          return (
            <button
              key={invocation.externalInvocationId}
              type="button"
              className={selected ? 'active' : ''}
              disabled={loading && selected}
              onClick={() => !selected && onSelect?.(invocation.externalInvocationId)}
              title={invocation.externalInvocationId}
            >
              <span>
                {t('teamActivity.runPhase', { number: index + 1 })}
                {invocation.isPreferred && <b>{t('teamActivity.primaryCollaboration')}</b>}
                {invocation.isLatest && <b>{t('teamActivity.latest')}</b>}
              </span>
              <small>{t('teamActivity.runFacts', {
                members: invocation.memberCount,
                tasks: invocation.taskCount,
                messages: invocation.messageCount,
              })}</small>
            </button>
          )
        })}
      </div>
    </section>
  )
}

function Overview({ members, tasks, leaderState, events }: {
  members: ExternalTeamMember[]
  tasks: ExternalTeamTask[]
  leaderState: string
  events: ExternalTeamEventV1[]
}) {
  const { t } = useI18n()
  const leaderDelegation = events.find(event => (
    event.kind === 'message_broadcast'
    && String(event.message?.fromMember || '').toLowerCase().includes('leader')
    && event.message?.content
  ))?.message?.content
  return (
    <div className="team-overview">
      <section className={`team-formation-card ${members.length > 0 ? 'formed' : 'direct'}`}>
        <span className="team-formation-icon">{members.length > 0 ? '✦' : '◆'}</span>
        <div>
          <strong>{members.length > 0 ? t('teamActivity.autonomousTeamFormed') : t('teamActivity.directExecution')}</strong>
          <span>{members.length > 0
            ? t('teamActivity.formationFacts', { members: members.length, tasks: tasks.length })
            : t('teamActivity.directExecutionHint')}</span>
        </div>
      </section>
      <div className="team-member-row team-leader-row">
        <span className="team-tree-glyph">◆</span>
        <div className="team-member-main">
          <strong>{t('teamActivity.leader')}</strong>
          <span>{t('teamActivity.teamLeader')}</span>
        </div>
        <span className={`team-state team-state-${stateClass(leaderState)}`}>{displayState(leaderState)}</span>
      </div>
      {members.map((member, index) => {
        const assignedTasks = tasks.filter(task => task.assignee === member.memberId || task.assignee === member.name)
        const currentTask = taskForMember(tasks, member)
        const memberState = member.executionStatus || member.status
        return (
          <div key={member.memberId} className="team-member-row">
            <span className="team-tree-glyph">{index === members.length - 1 ? '└' : '├'}</span>
            <div className="team-member-main">
              <strong>{member.name || humanize(member.role) || t('teamActivity.teamMember')}</strong>
              <span>{member.role ? humanize(member.role) : t('teamActivity.teammate')}{member.mode ? ` · ${member.mode}` : ''}</span>
              {currentTask && <span className="team-current-task">{currentTask.title || currentTask.content || currentTask.taskId}</span>}
              {assignedTasks.length > 1 && (
                <span className="team-assignment-count">{t('teamActivity.assignedTasks', { count: assignedTasks.length })}</span>
              )}
            </div>
            <div className="team-member-meta">
              <span className={`team-state team-state-${stateClass(memberState)}`}>{displayState(memberState)}</span>
              <CopyId value={member.memberId} />
            </div>
          </div>
        )
      })}
      {members.length === 0 && <div className="team-empty">{t('teamActivity.noMembers')}</div>}
      {(leaderDelegation || tasks.some(task => task.assignee)) && (
        <section className="team-leader-delegation">
          <header>{t('teamActivity.leaderDelegation')}</header>
          <div className="team-assignment-list">
            {members.flatMap(member => {
              const assigned = tasks.filter(task => task.assignee === member.memberId || task.assignee === member.name)
              if (assigned.length === 0) return []
              return [
                <div key={member.memberId}>
                  <strong>{member.name || member.memberId}</strong>
                  <span>{assigned.map(task => task.title || task.taskId).join(' · ')}</span>
                </div>,
              ]
            })}
          </div>
          {leaderDelegation && (
            <details>
              <summary>{t('teamActivity.viewOriginalInstructions')}</summary>
              <p>{leaderDelegation}</p>
            </details>
          )}
        </section>
      )}
    </div>
  )
}

const TASK_COLUMNS = [
  { id: 'pending', label: 'Pending', states: ['', 'pending', 'created', 'ready', 'unblocked'] },
  { id: 'running', label: 'Running', states: ['active', 'claimed', 'in_progress', 'running', 'working'] },
  { id: 'completed', label: 'Completed', states: ['complete', 'completed', 'done', 'finished'] },
  { id: 'failed', label: 'Failed', states: ['cancelled', 'error', 'failed', 'failure'] },
]

function Tasks({ tasks }: { tasks: ExternalTeamTask[] }) {
  const { t } = useI18n()
  const reportsDependencies = tasks.some(task => (task.dependencies?.length ?? 0) > 0)
  const columnLabels: Record<string, string> = {
    pending: t('teamActivity.pending'),
    running: t('teamActivity.running'),
    completed: t('teamActivity.completed'),
    failed: t('teamActivity.failed'),
  }
  return (
    <>
      <div className="team-task-board">
        {TASK_COLUMNS.map(column => {
          const columnTasks = tasks.filter(task => column.states.includes(String(task.status ?? '').toLowerCase()))
          return (
            <section key={column.id} className="team-task-column">
              <header><span>{columnLabels[column.id] || column.label}</span><b>{columnTasks.length}</b></header>
              {columnTasks.map(task => (
                <article key={task.taskId} className="team-task-card">
                  <strong>{task.title || task.content || task.taskId}</strong>
                  {task.assignee && <span>{task.assignee}</span>}
                  {task.updatedAt && <time>{formatTime(task.updatedAt)}</time>}
                  {(task.dependencies?.length ?? 0) > 0 && <small>{t('teamActivity.dependsOn')}: {task.dependencies!.join(', ')}</small>}
                </article>
              ))}
            </section>
          )
        })}
      </div>
      {tasks.length === 0 && <div className="team-empty">{t('teamActivity.noTasks')}</div>}
      {tasks.length > 0 && !reportsDependencies && <div className="team-dependency-note">{t('teamActivity.dependenciesUnavailable')}</div>}
    </>
  )
}

function Timeline({ events }: { events: ExternalTeamEventV1[] }) {
  const { t } = useI18n()
  const [filter, setFilter] = useState<TimelineFilter>('highlights')
  const visible = events.filter(event => eventMatchesFilter(event, filter))
  const filterLabels: Record<TimelineFilter, string> = {
    highlights: t('teamActivity.filterHighlights'),
    all: t('teamActivity.filterAll'),
    members: t('teamActivity.filterMembers'),
    tasks: t('teamActivity.filterTasks'),
    messages: t('teamActivity.filterMessages'),
    errors: t('teamActivity.filterErrors'),
  }
  return (
    <div className="team-timeline-wrap">
      <div className="team-filter-row">
        {(['highlights', 'all', 'members', 'tasks', 'messages', 'errors'] as TimelineFilter[]).map(item => (
          <button key={item} type="button" className={filter === item ? 'active' : ''} onClick={() => setFilter(item)}>
            {filterLabels[item]}
          </button>
        ))}
      </div>
      <div className="team-timeline">
        {visible.map(event => {
          const body = eventBody(event)
          return (
            <div key={event.eventId} className={`team-timeline-event team-event-${eventMatchesFilter(event, 'errors') ? 'errors' : eventCategory(event)}`}>
              <time>{formatTime(event.occurredAt)}</time>
              <span className="team-timeline-dot" />
              <div>
                <strong>{eventTitle(event, t)}</strong>
                {body && <details><summary>{eventDetailLabel(event, t)}</summary><div className="team-event-body">{body}</div></details>}
              </div>
            </div>
          )
        })}
      </div>
      {visible.length === 0 && <div className="team-empty">{t('teamActivity.noMatchingEvents')}</div>}
    </div>
  )
}

function Output({ events, onOpenRuntimeActivity }: {
  events: ExternalTeamEventV1[]
  onOpenRuntimeActivity?: () => void
}) {
  const { t } = useI18n()
  const outputs = events.filter(event => event.kind === 'member_output' || event.kind === 'leader_output')
  if (outputs.length === 0) {
    return (
      <div className="team-empty team-output-empty">
        <span>{t('teamActivity.noOutput')}</span>
        {onOpenRuntimeActivity && <button type="button" onClick={onOpenRuntimeActivity}>{t('teamActivity.openRuntime')}</button>}
      </div>
    )
  }
  return (
    <div className="team-output-list">
      {outputs.map(event => (
        <details key={event.eventId} className="team-output-card" open={event.kind === 'leader_output'}>
          <summary>
            <span>{event.kind === 'leader_output' ? t('teamActivity.leaderOutput') : `${event.member?.name || event.member?.memberId || t('teamActivity.teamMember')} · ${t('teamActivity.output')}`}</span>
            <time>{formatTime(event.occurredAt)}</time>
          </summary>
          <div className="team-output-content"><MarkdownBody content={event.output || ''} /></div>
        </details>
      ))}
    </div>
  )
}

export function JiuwenTeamActivityPanel({
  title,
  coveredRoleIds,
  record,
  fallbackSummary,
  onClose,
  onLoadOlder,
  onSelectInvocation,
  onOpenRuntimeActivity,
}: JiuwenTeamActivityPanelProps) {
  const { t } = useI18n()
  const [tab, setTab] = useState<TeamTab>('overview')
  const summary = record?.summary ?? fallbackSummary
  const events = useMemo(() => record?.events ?? [], [record?.events])
  const tabLabels: Record<TeamTab, string> = {
    overview: t('teamActivity.overview'),
    tasks: t('teamActivity.tasks'),
    timeline: t('teamActivity.timeline'),
    output: t('teamActivity.output'),
  }
  const handleKeyDown = useCallback((event: KeyboardEvent) => {
    if (event.key === 'Escape') onClose()
  }, [onClose])
  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [handleKeyDown])
  return (
    <>
      <div className="exec-panel-backdrop" onClick={onClose} />
      <aside className="exec-panel team-activity-panel" aria-label={t('teamActivity.title')}>
        <header className="exec-panel-header">
          <div className="exec-panel-title-row">
            <IconTimeline />
            <h3 className="exec-panel-title">{title}</h3>
            <span className="exec-badge exec-badge-running">{t('teamActivity.readOnly')}</span>
          </div>
          <button className="exec-panel-close" onClick={onClose} title={t('teamActivity.close')}><IconClose /></button>
        </header>
        <div className="team-activity-identity">
          <div>
            <strong>{externalTeamSummaryText(summary)}</strong>
            <span>{t('teamActivity.providerBoundary')}</span>
          </div>
          {coveredRoleIds && coveredRoleIds.length > 0 && (
            <details className="team-covered-roles">
              <summary>{t('teamActivity.coveredRoles')} · {coveredRoleIds.length}</summary>
              <div>{coveredRoleIds.map(role => <span key={role}>{humanize(role)}</span>)}</div>
            </details>
          )}
        </div>
        <InvocationSelector
          invocations={record?.invocations ?? []}
          selectedInvocationId={record?.externalInvocationId || ''}
          loading={Boolean(record?.loading)}
          onSelect={onSelectInvocation}
        />
        <nav className="team-activity-tabs" aria-label={t('teamActivity.title')}>
          {(['overview', 'tasks', 'timeline', 'output'] as TeamTab[]).map(item => (
            <button key={item} type="button" className={tab === item ? 'active' : ''} onClick={() => setTab(item)}>
              {tabLabels[item]}
              {item === 'tasks' && summary && <span>{summary.counts.tasks}</span>}
              {item === 'timeline' && <span>{events.length}</span>}
            </button>
          ))}
        </nav>
        <div className="exec-panel-body team-activity-body">
          {record?.loading && <div className="team-empty">{t('common.loading')}</div>}
          {!record?.loading && record && !record.available && (
            <div className="team-empty">{record.reason === 'no_team_telemetry' ? t('teamActivity.historyUnavailable') : t('teamActivity.telemetryUnavailable')}</div>
          )}
          {!record?.loading && summary && (record?.available !== false || !!fallbackSummary) && (
            <>
              {tab === 'overview' && <Overview members={summary.members} tasks={summary.tasks} leaderState={summary.leaderState} events={events} />}
              {tab === 'tasks' && <Tasks tasks={summary.tasks} />}
              {tab === 'timeline' && <Timeline events={events} />}
              {tab === 'output' && <Output events={events} onOpenRuntimeActivity={onOpenRuntimeActivity} />}
              {record?.hasMore && onLoadOlder && <button type="button" className="team-load-older" onClick={onLoadOlder}>{t('teamActivity.loadOlder')}</button>}
            </>
          )}
          {!record?.loading && !record && !summary && <div className="team-empty">{t('teamActivity.historyUnavailable')}</div>}
        </div>
      </aside>
    </>
  )
}
