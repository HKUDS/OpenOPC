import { useCallback, useEffect, useMemo, useState } from 'react'
import type {
  AgentAnimStatus,
  ProgressEntry,
  RoleAggregatedStatus,
  RoleWorkItemActivitySection,
  RoleWorkItemRow,
  RoleWorkItemSummary,
  Session,
} from '../types/kanban'
import type { AgentInfo } from '../types/visual'
import { AgentProgressBlock } from './AgentProgressBlock'
import { MarkdownBody } from './MessageList'
import { IconClose, IconHandoff, IconTimeline, IconSearch, IconChevron } from './SvgIcons'
import { TERMINAL_SESSION_STATUSES, getSessionRuntimeStatus, isSessionWorking } from '../lib/sessionRuntime'
import { getWorkItemRoleLabel } from '../lib/workItemIdentity'
import { executionAgentLabel } from '../lib/externalAgents'

interface AgentWorkPanelProps {
  sessions: Session[]
  /**
   * When provided (company-mode primary sessions), the panel renders one
   * row per role with the per-role aggregated status from the backend.
   * Falls back to the legacy ``sessions`` view when undefined / empty.
   */
  roleWorkItems?: Record<string, RoleWorkItemSummary>
  isCompanyRuntime?: boolean
  agents: AgentInfo[]
  onOpenChildDetail?: (taskId: string) => void
  onOpenExecutionPanel?: (taskId: string) => void
}

interface AgentWorkPanelLegacyViewProps {
  sessions: Session[]
  agents: AgentInfo[]
  onOpenChildDetail?: (taskId: string) => void
  onOpenExecutionPanel?: (taskId: string) => void
}

type StatusFilter = 'all' | 'active' | 'idle' | 'pending'

function agentStatusOf(s: Session): AgentAnimStatus {
  return getSessionRuntimeStatus(s)
}

function statusSortKey(s: Session): number {
  const st = agentStatusOf(s)
  if (st === 'tool_active') return 0
  if (st === 'reflecting') return 1
  if (s.status === 'running') return 2
  if (s.status === 'pending') return 3
  if (s.status === 'done') return 5
  if (s.status === 'failed') return 6
  return 4
}

function elapsed(ts: number): string {
  const sec = Math.floor((Date.now() - ts) / 1000)
  if (sec < 5) return 'now'
  if (sec < 60) return `${sec}s`
  const min = Math.floor(sec / 60)
  if (min < 60) return `${min}m`
  return `${Math.floor(min / 60)}h`
}

function lastActivity(entries: ProgressEntry[]): string {
  if (entries.length === 0) return '\u2014'
  return elapsed(entries[entries.length - 1].timestamp)
}

function activitySummary(s: Session): string {
  if (s.status === 'done') return 'Completed'
  if (s.status === 'failed') return 'Failed'
  if (s.status === 'cancelled') return 'Cancelled'

  const st = agentStatusOf(s)
  if (st === 'tool_active' && s.currentTool) return s.currentTool
  if (st === 'reflecting') return 'Thinking\u2026'
  if (s.status === 'pending') return 'Pending'

  const log = s.progressLog
  if (log.length > 0) {
    const last = log[log.length - 1]
    if (last.type === 'work_item_started') return `Work item: ${last.summary}`
    if (last.type === 'tool_call') return last.summary
    if (last.detail) return last.detail.replace(/\s+/g, ' ').trim()
    return last.summary
  }
  return 'Idle'
}

function terminalIcon(status: string): string | null {
  if (status === 'done') return '\u2713'
  if (status === 'failed') return '\u2717'
  if (status === 'cancelled') return '\u2014'
  return null
}

function terminalClass(status: string): string {
  if (status === 'done') return 'awp-terminal-done'
  if (status === 'failed') return 'awp-terminal-failed'
  if (status === 'cancelled') return 'awp-terminal-cancelled'
  return ''
}

const RESULT_BANNER: Record<string, { cls: string; text: string }> = {
  done: { cls: 'awp-result-done', text: 'Work item completed successfully' },
  failed: { cls: 'awp-result-failed', text: 'Work item failed' },
  cancelled: { cls: 'awp-result-cancelled', text: 'Work item cancelled' },
}

/** Role-aggregated panel: one row per role, sourced from the
 *  DelegationWorkItem rollup. Renders when ``roleWorkItems`` is non-empty;
 *  the legacy session-based panel below is preserved verbatim as a fallback
 *  for non-company runs.
 */
const ROLE_STATUS_LABEL: Record<RoleAggregatedStatus, string> = {
  active: 'Working',
  waiting: 'Waiting',
  pending: 'Pending',
  done: 'Completed',
  failed: 'Failed',
}

const ROLE_STATUS_SORT: Record<RoleAggregatedStatus, number> = {
  active: 0,
  waiting: 1,
  pending: 2,
  done: 3,
  failed: 4,
}

function summarizeRoleActivity(summary: RoleWorkItemSummary): string {
  if (summary.runtimeStatus === 'tool_active') return 'Running tool…'
  if (summary.runtimeStatus === 'reflecting') return 'Thinking…'
  const label = ROLE_STATUS_LABEL[summary.aggregatedStatus] ?? 'Pending'
  if (summary.aggregatedStatus === 'done') {
    return `${label} · ${summary.workItems.length} work item${summary.workItems.length === 1 ? '' : 's'}`
  }
  return label
}

function lastRoleActivity(summary: RoleWorkItemSummary): string {
  if (summary.workItems.length === 0) return '—'
  const ts = summary.workItems.reduce((max, w) => Math.max(max, w.updatedAt), 0)
  if (!ts) return '—'
  return elapsed(ts)
}

function roleWorkItemSessionStatus(row: RoleWorkItemRow): string | undefined {
  if (row.phase === 'failed') return 'failed'
  if (row.phase === 'cancelled') return 'cancelled'
  if (row.kanbanColumn === 'done') return 'done'
  return undefined
}

function WorkItemActivitySections({
  sections,
  fallbackEntries,
  sessionStatus,
}: {
  sections?: RoleWorkItemActivitySection[]
  fallbackEntries?: ProgressEntry[]
  sessionStatus?: string
}) {
  const visibleSections = (sections ?? []).filter(section => (
    (section.entries?.length ?? 0) > 0 || !!section.runtimeTaskId
  ))

  if (visibleSections.length > 0) {
    return (
      <div className="wf-role-turn-activity-sections">
        {visibleSections.map((section, index) => {
          const entries = section.entries ?? []
          const key = `${section.runtimeTaskId || section.kind}:${index}`
          return (
            <section key={key} className="wf-role-turn-activity-section">
              <div className="wf-role-turn-activity-section-head">
                <span className="wf-role-turn-activity-section-title">{section.title}</span>
                {section.roleName && (
                  <span className="wf-role-turn-activity-section-role">{section.roleName}</span>
                )}
                {entries.length > 0 && (
                  <span className="wf-role-turn-activity-section-count">{entries.length}</span>
                )}
              </div>
              {entries.length > 0 ? (
                <AgentProgressBlock
                  entries={entries}
                  sessionStatus={sessionStatus}
                  expandedByDefault
                />
              ) : (
                <div className="wf-role-turn-empty-activity">No runtime activity yet</div>
              )}
            </section>
          )
        })}
      </div>
    )
  }

  if (!fallbackEntries || fallbackEntries.length === 0) {
    return <div className="wf-role-turn-empty-activity">No runtime activity yet</div>
  }
  return (
    <AgentProgressBlock
      entries={fallbackEntries}
      sessionStatus={sessionStatus}
      expandedByDefault
    />
  )
}

function AgentWorkPanelRoleView({
  roleWorkItems,
  onOpenChildDetail,
  onOpenExecutionPanel,
}: {
  roleWorkItems: Record<string, RoleWorkItemSummary>
  onOpenChildDetail?: (taskId: string) => void
  onOpenExecutionPanel?: (taskId: string) => void
}) {
  const [selectedRoleKey, setSelectedRoleKey] = useState<string | null>(null)
  const [selectedWorkItemId, setSelectedWorkItemId] = useState<string | null>(null)
  const [filter, setFilter] = useState<StatusFilter>('all')
  const [search, setSearch] = useState('')
  const [showSearch, setShowSearch] = useState(false)

  const summaries = useMemo(() => Object.values(roleWorkItems), [roleWorkItems])

  const sorted = useMemo(() => {
    let list = summaries.slice()
    if (filter === 'active') {
      list = list.filter(s => s.aggregatedStatus === 'active')
    } else if (filter === 'idle') {
      list = list.filter(s => s.aggregatedStatus === 'done' || s.aggregatedStatus === 'failed')
    } else if (filter === 'pending') {
      list = list.filter(s => s.aggregatedStatus === 'pending' || s.aggregatedStatus === 'waiting')
    }
    if (search.trim()) {
      const q = search.toLowerCase()
      list = list.filter(s =>
        s.roleName.toLowerCase().includes(q)
        || s.roleId.toLowerCase().includes(q)
        || s.workItems.some(w => w.title.toLowerCase().includes(q)),
      )
    }
    list.sort((a, b) => {
      const aRank = ROLE_STATUS_SORT[a.aggregatedStatus] ?? 99
      const bRank = ROLE_STATUS_SORT[b.aggregatedStatus] ?? 99
      if (aRank !== bRank) return aRank - bRank
      return a.roleName.localeCompare(b.roleName)
    })
    return list
  }, [summaries, filter, search])

  // Reset selection if the role disappears (rare, but possible during run
  // teardown). Stale selection would otherwise crash the detail view.
  useEffect(() => {
    if (selectedRoleKey && !summaries.some(s => s.roleKey === selectedRoleKey)) {
      setSelectedRoleKey(null)
      setSelectedWorkItemId(null)
    }
  }, [summaries, selectedRoleKey])

  const activeCount = summaries.filter(s => s.aggregatedStatus === 'active').length
  const selected = selectedRoleKey ? roleWorkItems[selectedRoleKey] ?? null : null
  const selectedWorkItem: RoleWorkItemRow | null = (selected && selectedWorkItemId)
    ? selected.workItems.find(w => w.workItemId === selectedWorkItemId) ?? null
    : null

  if (summaries.length === 0) return null

  return (
    <div className="awp">
      <div className="awp-header">
        <span className="awp-title">
          Agents
          <span className="awp-count">
            {activeCount > 0
              ? `${activeCount}/${summaries.length} active`
              : `${summaries.length}`}
          </span>
        </span>
        <div className="awp-controls">
          <select
            className="awp-filter"
            value={filter}
            onChange={e => setFilter(e.target.value as StatusFilter)}
          >
            <option value="all">All</option>
            <option value="active">Active</option>
            <option value="idle">Idle / Done</option>
            <option value="pending">Pending</option>
          </select>
          <button
            className={`awp-search-toggle${showSearch ? ' active' : ''}`}
            onClick={() => setShowSearch(v => !v)}
            title="Search agents"
          >
            <IconSearch />
          </button>
        </div>
      </div>

      {showSearch && (
        <div className="awp-search-bar">
          <input
            className="awp-search-input"
            type="text"
            placeholder="Search agents or work items..."
            value={search}
            onChange={e => setSearch(e.target.value)}
            autoFocus
          />
        </div>
      )}

      <div className="awp-list">
        {sorted.map((summary) => {
          const isSelected = selectedRoleKey === summary.roleKey
          const isActive = summary.aggregatedStatus === 'active'
          const isLive = summary.runtimeStatus === 'reflecting' || summary.runtimeStatus === 'tool_active'
          const isTerminal = summary.aggregatedStatus === 'done' || summary.aggregatedStatus === 'failed'
          return (
            <button
              key={summary.roleKey}
              className={`awp-row${isSelected ? ' awp-row-selected' : ''}${isActive ? ' awp-row-active' : ''}${isTerminal ? ` awp-row-terminal awp-terminal-${summary.aggregatedStatus}` : ''}`}
              onClick={() => {
                setSelectedRoleKey(prev => (prev === summary.roleKey ? null : summary.roleKey))
                setSelectedWorkItemId(null)
              }}
              title={`${summary.roleName} — ${ROLE_STATUS_LABEL[summary.aggregatedStatus]}`}
            >
              <span className={`awp-dot${isLive ? ' awp-dot-active' : ''} awp-dot-${summary.aggregatedStatus}`} />
              <div className="awp-row-info">
                <span className="awp-row-name">{summary.roleName}</span>
                <span className="awp-row-projection">
                  {summary.workItems.length} work item{summary.workItems.length === 1 ? '' : 's'}
                </span>
              </div>
              <span className="awp-row-activity">{summarizeRoleActivity(summary)}</span>
              <span className="awp-row-time">{lastRoleActivity(summary)}</span>
            </button>
          )
        })}
        {sorted.length === 0 && (
          <div className="awp-empty">
            {search ? 'No matching agents' : 'No agents in this filter'}
          </div>
        )}
      </div>

      {selected && (
        <div className="awp-detail">
          <div className="awp-detail-header">
            <div className="awp-detail-identity">
              <div className="awp-detail-avatar">
                {selected.roleName.charAt(0).toUpperCase()}
              </div>
              <div className="awp-detail-meta">
                <span className="awp-detail-name">{selected.roleName}</span>
                <span className="awp-detail-role">{ROLE_STATUS_LABEL[selected.aggregatedStatus]}</span>
              </div>
            </div>
            <div className="awp-detail-actions">
              <button
                className="awp-detail-close"
                onClick={() => { setSelectedRoleKey(null); setSelectedWorkItemId(null) }}
                title="Close detail"
              >
                <IconClose />
              </button>
            </div>
          </div>
          <div className="awp-detail-body">
            <div className="awp-detail-section">
              <div className="awp-detail-section-label">
                <IconTimeline />
                <span>Work items</span>
              </div>
              <ul className="wf-role-turns">
                {selected.workItems.map((row) => {
                  const expanded = selectedWorkItemId === row.workItemId
                  const columnId = (row.kanbanColumn === 'in-progress')
                    ? 'in_progress'
                    : (row.kanbanColumn === 'in-review' ? 'in_review' : row.kanbanColumn)
                  return (
                    <li key={row.workItemId} className={`wf-role-turn${expanded ? ' wf-role-turn-expanded' : ''}`}>
                      <button
                        type="button"
                        className="wf-role-turn-button"
                        onClick={() => setSelectedWorkItemId(prev => (prev === row.workItemId ? null : row.workItemId))}
                      >
                        <span className={`wf-role-turn-column wf-role-turn-column-${columnId}`}>
                          {row.kanbanColumn.replace('-', ' ').replace(/^./, c => c.toUpperCase())}
                        </span>
                        <span className="wf-role-turn-title">
                          {row.isReviewTarget && <span className="wf-role-turn-tag">Review</span>}
                          {row.title}
                        </span>
                        <span className="wf-role-turn-time">{elapsed(row.updatedAt)}</span>
                        <span className="wf-role-turn-chevron">
                          <IconChevron down={expanded} />
                        </span>
                      </button>
                      {expanded && (
                        <div className="wf-role-turn-activity">
                          <WorkItemActivitySections
                            sections={row.activitySections}
                            fallbackEntries={row.progressLog}
                            sessionStatus={roleWorkItemSessionStatus(row)}
                          />
                          {row.executionTurnId && (
                            <button
                              type="button"
                              className="wf-role-turn-open-session"
                              onClick={(event) => {
                                event.stopPropagation()
                                if (row.executionTurnId) {
                                  (onOpenExecutionPanel ?? onOpenChildDetail)?.(row.executionTurnId)
                                }
                              }}
                            >
                              Open runtime session
                            </button>
                          )}
                        </div>
                      )}
                    </li>
                  )
                })}
              </ul>
            </div>
            {selectedWorkItem === null && selected.workItems.length === 0 && (
              <div className="awp-detail-empty">No work items yet</div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function AgentWorkPanelLegacyView({ sessions, agents, onOpenChildDetail, onOpenExecutionPanel }: AgentWorkPanelLegacyViewProps) {
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [filter, setFilter] = useState<StatusFilter>('all')
  const [search, setSearch] = useState('')
  const [showSearch, setShowSearch] = useState(false)

  // Reset selection when parent session changes (sessions list swaps entirely)
  useEffect(() => {
    if (selectedTaskId && !sessions.some(s => s.taskId === selectedTaskId)) {
      setSelectedTaskId(null)
    }
  }, [sessions, selectedTaskId])

  const sorted = useMemo(() => {
    let list = [...sessions]
    if (filter === 'active') {
      list = list.filter(isSessionWorking)
    } else if (filter === 'idle') {
      list = list.filter(s =>
        TERMINAL_SESSION_STATUSES.has(s.status) || (agentStatusOf(s) === 'idle' && s.status !== 'pending'),
      )
    } else if (filter === 'pending') {
      list = list.filter(s => s.status === 'pending')
    }
    if (search.trim()) {
      const q = search.toLowerCase()
      list = list.filter(s => {
        const name = s.assigneeIds[0] ?? s.title
        return name.toLowerCase().includes(q) || s.title.toLowerCase().includes(q)
      })
    }
    list.sort((a, b) => statusSortKey(a) - statusSortKey(b))
    return list
  }, [sessions, filter, search])

  const activeCount = sessions.filter(isSessionWorking).length

  const selected = useMemo(() => {
    if (!selectedTaskId) return null
    return sessions.find(s => s.taskId === selectedTaskId) ?? null
  }, [sessions, selectedTaskId])

  const selectedAgent = useMemo(() => {
    if (!selected) return undefined
    const id = selected.assigneeIds[0]
    return id ? agents.find(a => a.agent_id === id) : undefined
  }, [selected, agents])

  const handleSelect = useCallback((taskId: string) => {
    setSelectedTaskId(prev => {
      if (prev === taskId) {
        onOpenChildDetail?.(taskId)
        return prev
      }
      return taskId
    })
  }, [onOpenChildDetail])

  if (sessions.length === 0) return null

  return (
    <div className="awp">
      {/* Header */}
      <div className="awp-header">
        <span className="awp-title">
          Agents
          <span className="awp-count">
            {activeCount > 0
              ? `${activeCount}/${sessions.length} active`
              : `${sessions.length}`}
          </span>
        </span>
        <div className="awp-controls">
          <select
            className="awp-filter"
            value={filter}
            onChange={e => setFilter(e.target.value as StatusFilter)}
          >
            <option value="all">All</option>
            <option value="active">Active</option>
            <option value="idle">Idle / Done</option>
            <option value="pending">Pending</option>
          </select>
          <button
            className={`awp-search-toggle${showSearch ? ' active' : ''}`}
            onClick={() => setShowSearch(v => !v)}
            title="Search agents"
          >
            <IconSearch />
          </button>
        </div>
      </div>

      {showSearch && (
        <div className="awp-search-bar">
          <input
            className="awp-search-input"
            type="text"
            placeholder="Search agents..."
            value={search}
            onChange={e => setSearch(e.target.value)}
            autoFocus
          />
        </div>
      )}

      {/* Compact list */}
      <div className="awp-list">
        {sorted.map(s => {
          const isActive = isSessionWorking(s)
          const isTerminal = TERMINAL_SESSION_STATUSES.has(s.status)
          const isSelected = selectedTaskId === s.taskId
          const agentName = s.assigneeIds[0]?.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) ?? s.title
          const projectionLabel = s.workItemProjectionId?.replace(/_/g, ' ') ?? ''
          const tIcon = terminalIcon(s.status)
          const tCls = terminalClass(s.status)

          return (
            <button
              key={s.taskId}
              className={`awp-row${isSelected ? ' awp-row-selected' : ''}${isActive ? ' awp-row-active' : ''}${isTerminal ? ` awp-row-terminal ${tCls}` : ''}`}
              onClick={() => handleSelect(s.taskId)}
              onDoubleClick={() => onOpenChildDetail?.(s.taskId)}
              title={onOpenChildDetail ? 'Click to inspect, click again or double-click to open full context' : undefined}
            >
              {tIcon ? (
                <span className={`awp-terminal-icon ${tCls}`}>{tIcon}</span>
              ) : (
                <span className={`awp-dot${isActive ? ' awp-dot-active' : ''}`} />
              )}
              <div className="awp-row-info">
                <span className="awp-row-name">{agentName}</span>
                {projectionLabel && <span className="awp-row-projection">{projectionLabel}</span>}
              </div>
              <span className="awp-row-activity">{activitySummary(s)}</span>
              <span className="awp-row-time">{lastActivity(s.progressLog)}</span>
            </button>
          )
        })}
        {sorted.length === 0 && (
          <div className="awp-empty">
            {search ? 'No matching agents' : 'No agents in this filter'}
          </div>
        )}
      </div>

      {/* Detail view for selected agent */}
      {selected && (
        <div className="awp-detail">
          <div className="awp-detail-header">
            <div className="awp-detail-identity">
              <div className={`awp-detail-avatar${TERMINAL_SESSION_STATUSES.has(selected.status) ? ` ${terminalClass(selected.status)}` : ''}`}>
                {(selectedAgent?.name ?? selected.assigneeIds[0] ?? '?').charAt(0).toUpperCase()}
              </div>
              <div className="awp-detail-meta">
                <span className="awp-detail-name">
                  {selectedAgent?.name ?? selected.assigneeIds[0] ?? selected.title}
                </span>
                {getWorkItemRoleLabel(selected) && (
                  <span className="awp-detail-role">
                    {getWorkItemRoleLabel(selected)}
                  </span>
                )}
                {selected.employeeAssignment?.name && (
                  <span className="awp-detail-employee">{selected.employeeAssignment.name}</span>
                )}
                {selected.selectedExecutionAgent && (
                  <span className="awp-detail-employee">
                    Agent: {executionAgentLabel(selected.selectedExecutionAgent)}
                  </span>
                )}
                {selected.workItemProjectionId && (
                  <span className="awp-detail-projection">
                    {selected.workItemProjectionId.replace(/_/g, ' ')}
                  </span>
                )}
              </div>
            </div>
            <div className="awp-detail-actions">
              {onOpenChildDetail && (
                <button
                  className="awp-detail-expand"
                  onClick={() => onOpenChildDetail(selected.taskId)}
                  title="Open full context"
                >
                  <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden="true">
                    <path d="M2.5 3.25h9a1 1 0 011 1v5.5a1 1 0 01-1 1h-4l-2.75 2v-2h-2.25a1 1 0 01-1-1v-5.5a1 1 0 011-1Z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
                    <path d="M4.5 5.5h5M4.5 7.5h3.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
                  </svg>
                </button>
              )}
              {onOpenExecutionPanel && (
                <button
                  className="awp-detail-expand"
                  onClick={() => onOpenExecutionPanel(selected.taskId)}
                  title="Open execution panel"
                >
                  <IconTimeline />
                </button>
              )}
              <button
                className="awp-detail-close"
                onClick={() => setSelectedTaskId(null)}
                title="Close detail"
              >
                <IconClose />
              </button>
            </div>
          </div>

          <div className="awp-detail-body">
            {selected.handoffContext && (
              <div className="awp-detail-section">
                <div className="awp-detail-section-label">
                  <IconHandoff />
                  <span>Handoff</span>
                </div>
                <div className="msg-content-agent-card">
                  <MarkdownBody content={selected.handoffContext} className="awp-detail-handoff awp-detail-handoff-markdown" />
                </div>
              </div>
            )}

            <div className="awp-detail-section">
              <div className="awp-detail-section-label">
                <IconTimeline />
                <span>Activity</span>
              </div>
              <AgentProgressBlock
                entries={selected.progressLog}
                agentStatus={selected.agentStatus}
                currentTool={selected.currentTool}
                toolElapsedMs={selected.toolElapsedMs}
                lastToolSummary={selected.lastToolSummary}
                sessionStatus={selected.status}
                expandedByDefault
              />
              {selected.progressLog.length === 0 && !selected.agentStatus && !TERMINAL_SESSION_STATUSES.has(selected.status) && (
                <div className="awp-detail-empty">No activity recorded yet</div>
              )}
            </div>

            {TERMINAL_SESSION_STATUSES.has(selected.status) && RESULT_BANNER[selected.status] && (
              <div className={`awp-result-banner ${RESULT_BANNER[selected.status].cls}`}>
                <span className="awp-result-icon">{terminalIcon(selected.status)}</span>
                <span>{RESULT_BANNER[selected.status].text}</span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

export function AgentWorkPanel({
  sessions,
  roleWorkItems,
  isCompanyRuntime = false,
  agents,
  onOpenChildDetail,
  onOpenExecutionPanel,
}: AgentWorkPanelProps) {
  // Prefer the work-item-driven view whenever the backend provides it:
  // it is the single source of truth for "1 row = 1 work item".
  if (roleWorkItems && Object.keys(roleWorkItems).length > 0) {
    return (
      <AgentWorkPanelRoleView
        roleWorkItems={roleWorkItems}
        onOpenChildDetail={onOpenChildDetail}
        onOpenExecutionPanel={onOpenExecutionPanel}
      />
    )
  }
  if (isCompanyRuntime) return null

  return (
    <AgentWorkPanelLegacyView
      sessions={sessions}
      agents={agents}
      onOpenChildDetail={onOpenChildDetail}
      onOpenExecutionPanel={onOpenExecutionPanel}
    />
  )
}
