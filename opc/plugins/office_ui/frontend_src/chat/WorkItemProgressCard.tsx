import React, { useMemo } from 'react'
import type { ReactNode } from 'react'
import type {
  AgentAnimStatus,
  ProgressEntry,
  RoleAggregatedStatus,
  RoleWorkItemActivitySection,
  RoleWorkItemRow,
  RoleWorkItemSummary,
  Session,
  WorkItemProgressEntry,
} from '../types/kanban'
import { IconWorkItem } from './SvgIcons'
import { getWorkItemAssignmentLabel, humanizeWorkItemRoleId } from '../lib/workItemIdentity'
import { getExecutionTurnId } from '../lib/workItemRuntimeIds'
import { executionAgentLabel } from '../lib/externalAgents'
import type { ExternalTeamActivityRecord } from '../stores/ExternalTeamActivityStore'
import type { ExternalTeamActivitySummary } from '../types/externalTeamActivity'
import { externalTeamSummaryText, isJiuwenOpaqueTeam } from '../lib/externalTeamActivity'

interface WorkItemProgressCardProps {
  workItemLog: WorkItemProgressEntry[]
  /**
   * Per-role DelegationWorkItem rollup. When present, drives the panel
   * (1 row = 1 work item; review work items appear under reviewer role).
   * Falls back to ``childSessions`` derivation only when undefined / empty
   * (legacy non-company-mode runs).
   * Source: ``snapshot_builder._build_role_work_items_for_session``.
   */
  roleWorkItems?: Record<string, RoleWorkItemSummary>
  /** Display-only executor-role rollup. Company/org Execution Progress
   *  prefers this so worker chips remain visible while awaiting review. */
  executorRoleWorkItems?: Record<string, RoleWorkItemSummary>
  /** Legacy session-based source. Kept for non-company-mode and for the
   *  case where a primary session has no ``roleWorkItems`` payload yet. */
  childSessions?: Session[]
  /** Company/org mode uses work-item rollups as the only role source. */
  isCompanyRuntime?: boolean
  onWorkItemClick?: (executionTurnId: string) => void
  getExternalTeamActivity?: (taskId: string, invocationId?: string) => ExternalTeamActivityRecord | undefined
}

type WorkItemStatus = 'active' | 'done' | 'failed' | 'waiting' | 'pending'

interface WorkItemInfo {
  projectionId: string
  title: string
  roleName?: string
  status: WorkItemStatus
  executionTurnId?: string
  executionAgent?: string
  externalTeamSummary?: ExternalTeamActivitySummary
}

interface RoleTurnInfo {
  executionTurnId: string
  title: string
  status: WorkItemStatus
  statusLabel: string       // kanban column label (To do / In progress / In review / Done)
  columnId: string          // todo | in_progress | in_review | done
  updatedAt: number
  /** Set when the row was derived from a DelegationWorkItem rather than
   *  a runtime Session. Used as the React key and for inline activity
   *  expansion. */
  workItemId?: string
  /** Per-row activity entries (already filtered by work-item projection
   *  on the backend). Empty for session-derived rows. */
  progressLog?: ProgressEntry[]
  activitySections?: RoleWorkItemActivitySection[]
  /** True when this row sits under the reviewer because the work item is
   *  in an ``in_review`` phase. */
  isReviewTarget?: boolean
}

interface RoleSummaryInfo {
  roleKey: string
  executionTurnId: string   // default click target = latest turn
  title: string             // role display name
  status: WorkItemStatus    // aggregated across all turns
  statusLabel: string       // kanban label for aggregated status
  executionAgent?: string
  roleName?: string
  updatedAt: number         // most recent turn's updatedAt
  turns: RoleTurnInfo[]     // chronological ASC (oldest first, newest last)
  /** Live tracker state (only set for work-item-driven summaries). When
   *  ``reflecting`` / ``tool_active`` the chip shows the orange pulse;
   *  otherwise the chip's colour is governed by ``status`` alone. */
  runtimeStatus?: AgentAnimStatus
  externalTeamSummary?: ExternalTeamActivitySummary
}

/** Kanban-column labels used both on the role's aggregate chip and on
 *  each per-turn row.  Matches the column headers shown on the kanban
 *  board so the vocabulary stays consistent across views. */
const COLUMN_LABELS: Record<string, string> = {
  todo: 'To do',
  in_progress: 'In progress',
  in_review: 'In review',
  done: 'Done',
}

/** Map the WorkItemStatus derived from a Session back to the kanban column
 *  it sits in.  Keeps the label logic in one place rather than diverging
 *  between the role chip and the per-turn row. */
function workItemStatusToColumnId(status: WorkItemStatus, fallbackColumnId?: string): string {
  if (fallbackColumnId && fallbackColumnId in COLUMN_LABELS) return fallbackColumnId
  switch (status) {
    case 'active':  return 'in_progress'
    case 'waiting': return 'in_review'
    case 'done':    return 'done'
    case 'failed':  return 'done'
    case 'pending': return 'todo'
    default:        return 'todo'
  }
}

function labelForColumnId(columnId: string): string {
  return COLUMN_LABELS[columnId] ?? COLUMN_LABELS.todo
}

/** Priority used to aggregate status across a role's turns so the main
 *  chip reflects "any turn still in flight" rather than whatever was
 *  updated last. */
const STATUS_AGGREGATE_PRIORITY: Record<WorkItemStatus, number> = {
  active: 0,
  waiting: 1,
  failed: 2,
  done: 3,
  pending: 4,
}

function aggregateStatus(statuses: WorkItemStatus[]): WorkItemStatus {
  if (statuses.length === 0) return 'pending'
  let best: WorkItemStatus = statuses[0]
  let bestRank = STATUS_AGGREGATE_PRIORITY[best] ?? 99
  for (const s of statuses) {
    const rank = STATUS_AGGREGATE_PRIORITY[s] ?? 99
    if (rank < bestRank) {
      best = s
      bestRank = rank
    }
  }
  return best
}

/** Check if a string looks like a UUID or long hex id */
function isUuidLike(s: string): boolean {
  return s.length > 12 && /^[0-9a-f-]+$/i.test(s.replace(/_/g, ''))
}

function trimString(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function firstNonEmpty(...values: unknown[]): string {
  for (const value of values) {
    const text = trimString(value)
    if (text) return text
  }
  return ''
}

function formatExecutionAgent(value?: string): string {
  const normalized = trimString(value)
  if (!normalized) return ''
  return executionAgentLabel(normalized) || humanizeWorkItemRoleId(normalized)
}

function renderProjectionIcon(status: WorkItemStatus): ReactNode {
  if (status === 'done') return <span className="wi-projection-icon">&#10003;</span>
  if (status === 'active') {
    // Always pulse when the role is "active". Active aggregates two cases
    // — (a) tracker is reflecting/tool_active right now, or (b) at least
    // one work item is in an in-progress phase. Both read as "this role
    // is working" to the user, so both should breathe. The earlier
    // refactor that gated pulse on runtime tracker only made finished-but-
    // running phases look static, which the user reported as a regression.
    return <span className="wi-projection-icon wi-projection-pulse">&#9679;</span>
  }
  if (status === 'failed') return <span className="wi-projection-icon">&#10007;</span>
  if (status === 'waiting') return <span className="wi-projection-icon">&#9679;</span>
  return null
}

const AGGREGATED_TO_WORK_ITEM_STATUS: Record<RoleAggregatedStatus, WorkItemStatus> = {
  active: 'active',
  waiting: 'waiting',
  pending: 'pending',
  done: 'done',
  failed: 'failed',
}

/** Backend ``kanban_column`` returns hyphenated ids (``in-progress`` /
 *  ``in-review``); this card's CSS classes use underscored forms. The
 *  conversion lives here so the rest of the file keeps speaking one
 *  vocabulary. */
function normalizeColumnId(columnId: string): string {
  if (!columnId) return 'todo'
  if (columnId === 'in-progress') return 'in_progress'
  if (columnId === 'in-review') return 'in_review'
  return columnId
}

/** Per-row status used for icon + chip colour. Mirrors the backend
 *  phase → column mapping (``opc/layer2_organization/phase.py``) but
 *  reduced to the 5 UI states the chip renders. */
function phaseAggregateForRow(phase: string): WorkItemStatus {
  switch (phase) {
    case 'running':
    case 'waiting_for_peer':
    case 'waiting_for_children':
    case 'paused':
    case 'needs_attention':
      return 'active'
    case 'awaiting_manager_review':
    case 'awaiting_human':
    case 'queued':
    case 'ready':
    case 'ready_for_rework':
    case 'waiting_dependencies':
      return 'waiting'
    case 'approved':
      return 'done'
    case 'failed':
    case 'cancelled':
      return 'failed'
    default:
      return 'pending'
  }
}

function statusFromSession(session: Session): WorkItemStatus {
  const status = trimString(session.status).toLowerCase()
  if (status === 'done' || status === 'delivered') return 'done'
  if (status === 'failed' || status === 'cancelled') return 'failed'
  if (
    status === 'awaiting_peer'
    || status === 'awaiting_manager_review'
    || status === 'awaiting_human'
    || status === 'awaiting_review'
    || status === 'blocked'
    || status === 'paused'
    || status === 'awaiting_owner'
  ) {
    return 'waiting'
  }
  if (status === 'running' || status === 'deliverable' || status === 'active' || status === 'ready') return 'active'
  return 'pending'
}

export function WorkItemProgressCard({
  workItemLog,
  roleWorkItems,
  executorRoleWorkItems,
  childSessions,
  isCompanyRuntime = false,
  onWorkItemClick,
  getExternalTeamActivity,
}: WorkItemProgressCardProps) {
  // Build child session lookup for enriching display names
  const sessionByTaskId = useMemo(() => {
    const map = new Map<string, Session>()
    for (const s of childSessions ?? []) {
      map.set(s.taskId, s)
      const executionTurnId = getExecutionTurnId(s)
      if (executionTurnId) map.set(executionTurnId, s)
    }
    return map
  }, [childSessions])

  // Build ordered work-item list from log entries — used
  // only as a fallback when no child sessions exist yet, because the
  // work-item log emits one projection id per runtime turn which means a single
  // role can appear multiple times (execute + each review + attention).
  // For kanban-push runs we'd rather derive the pipeline from
  // roleSummaries below, which already groups by workItemRoleId.
  const workItemLogWorkItems = useMemo(() => {
    const map = new Map<string, WorkItemInfo>()
    const order: string[] = []

    for (const entry of workItemLog) {
      const entryExecutionTurnId = getExecutionTurnId(entry)
      const projectionId = entry.workItemProjectionId ?? entryExecutionTurnId ?? ''
      if (!projectionId) continue

      if (!map.has(projectionId)) {
        order.push(projectionId)
        // Determine display title — prefer roleName, avoid UUID fallback
        const rawTitle = entry.workItemProjectionTitle ?? projectionId
        const title = entry.roleName || (!isUuidLike(rawTitle) ? rawTitle : 'Agent')
        map.set(projectionId, {
          projectionId,
          title,
          roleName: entry.roleName,
          status: 'pending',
          executionTurnId: entryExecutionTurnId,
        })
      }

      const info = map.get(projectionId)!
      // Update with latest role/title info
      if (entry.roleName) {
        info.roleName = entry.roleName
        info.title = entry.roleName
      } else if (entry.workItemProjectionTitle && !isUuidLike(entry.workItemProjectionTitle)) {
        info.title = entry.workItemProjectionTitle
      }
      if (entryExecutionTurnId) info.executionTurnId = entryExecutionTurnId

      // Update status based on event type
      switch (entry.type) {
        case 'work_item_started': info.status = 'active'; break
        case 'gate_approved': info.status = 'done'; break
        case 'gate_rejected': info.status = 'active'; break
        case 'awaiting_manager_review':
        case 'awaiting_human':
        case 'awaiting_review':
        case 'awaiting_peer': info.status = 'waiting'; break
        case 'work_item_failed':
        case 'deadlock': info.status = 'failed'; break
      }
    }

    // Enrich from child sessions
    for (const info of map.values()) {
      if (!info.executionTurnId) continue
      const session = sessionByTaskId.get(info.executionTurnId)
      if (!session) continue
      const label = getWorkItemAssignmentLabel(session)
      if (label) info.title = label
      if (session.workItemRoleName) info.roleName = session.workItemRoleName
      if (session.status === 'done') info.status = 'done'
      else if (session.status === 'failed') info.status = 'failed'
    }

    return order.map(id => map.get(id)!)
  }, [workItemLog, sessionByTaskId])

  const taskOrder = useMemo(() => {
    const map = new Map<string, number>()
    for (const entry of workItemLog) {
      const taskId = getExecutionTurnId(entry)
      if (taskId && !map.has(taskId)) map.set(taskId, map.size)
    }
    return map
  }, [workItemLog])

  // Primary path: drive rows directly from the per-role DelegationWorkItem
  // rollup the backend ships in ``session.role_work_items``. This is the
  // fix for "1 row should = 1 work item" — the legacy session-driven
  // derivation below treats every runtime Task as its own row, which
  // double-counts rework turns and silently drops queued / review-target
  // work items. See ``plan/bug-breezy-dragonfly.md`` for the full root-cause
  // breakdown.
  const displayRoleWorkItems = executorRoleWorkItems ?? roleWorkItems
  const roleSummariesFromWorkItems = useMemo<RoleSummaryInfo[]>(() => {
    if (!displayRoleWorkItems) return []
    const summaries: RoleSummaryInfo[] = []
    for (const summary of Object.values(displayRoleWorkItems)) {
      if (!summary || !Array.isArray(summary.workItems) || summary.workItems.length === 0) continue
      // Backend already sorts ASC by createdAt; defensive copy + re-sort
      // here means a prop-mutation upstream can't reorder the rows.
      const ordered = [...summary.workItems].sort((a, b) => a.createdAt - b.createdAt)
      const turns: RoleTurnInfo[] = ordered.map((row: RoleWorkItemRow) => {
        const columnId = normalizeColumnId(row.kanbanColumn)
        const phaseToStatus = phaseAggregateForRow(row.phase)
        return {
          executionTurnId: row.executionTurnId ?? '',
          title: trimString(row.title) || trimString(row.executorRoleName) || trimString(row.executorRoleId) || 'Work item',
          status: phaseToStatus,
          statusLabel: labelForColumnId(columnId),
          columnId,
          updatedAt: row.updatedAt,
          workItemId: row.workItemId,
          progressLog: row.progressLog,
          activitySections: row.activitySections ?? [],
          isReviewTarget: row.isReviewTarget,
        }
      })
      const aggregatedStatus = AGGREGATED_TO_WORK_ITEM_STATUS[summary.aggregatedStatus] ?? 'pending'
      const aggregatedColumnId = workItemStatusToColumnId(aggregatedStatus)
      const latest = turns[turns.length - 1]
      // Chip click target: prefer the latest turn's runtime task. When the
      // last work item has no execution turn yet (queued / never dispatched),
      // walk back to find the most recent dispatched turn.
      const fallbackExecutionTurnId = [...turns].reverse().find(t => !!t.executionTurnId)?.executionTurnId ?? ''
      const focusedExecutionTurnId = latest.executionTurnId || fallbackExecutionTurnId
      const focusedRow = [...ordered].reverse().find(row => row.executionTurnId === focusedExecutionTurnId)
      const opaqueTeamRow = isJiuwenOpaqueTeam(focusedRow) ? focusedRow : undefined
      summaries.push({
        roleKey: summary.roleKey,
        executionTurnId: focusedExecutionTurnId,
        title: opaqueTeamRow
          ? `${summary.roleName || summary.roleId} · JiuwenSwarm-team`
          : (summary.roleName || summary.roleId),
        status: aggregatedStatus,
        statusLabel: labelForColumnId(aggregatedColumnId),
        roleName: summary.roleName,
        updatedAt: turns.reduce((max, t) => Math.max(max, t.updatedAt), 0),
        turns,
        runtimeStatus: summary.runtimeStatus,
        executionAgent: formatExecutionAgent(opaqueTeamRow?.selectedExecutionAgent),
        externalTeamSummary: opaqueTeamRow
          ? (getExternalTeamActivity?.(
              focusedExecutionTurnId,
              opaqueTeamRow.externalTeamSummary?.externalInvocationId,
            )?.summary ?? opaqueTeamRow.externalTeamSummary)
          : undefined,
      })
    }
    // Stable order: roles first appearing in time go left.
    summaries.sort((a, b) => {
      const aFirst = a.turns[0]?.updatedAt ?? Number.POSITIVE_INFINITY
      const bFirst = b.turns[0]?.updatedAt ?? Number.POSITIVE_INFINITY
      return aFirst - bFirst
    })
    return summaries
  }, [displayRoleWorkItems, getExternalTeamActivity])

  const roleSummariesFromSessions = useMemo<RoleSummaryInfo[]>(() => {
    const sessions = [...(childSessions ?? [])]
    if (sessions.length === 0) return []

    // Group by workItemRoleId (fall back to the assignee when missing
    // so legacy non-company-mode sessions still get a stable key).
    const groups = new Map<string, Session[]>()
    const groupOrder: string[] = []
    for (const session of sessions) {
      const roleKey = firstNonEmpty(
        session.workItemRoleId,
        session.assigneeIds[0],
        session.taskId,         // last-resort singleton
      )
      if (!groups.has(roleKey)) {
        groups.set(roleKey, [])
        groupOrder.push(roleKey)
      }
      groups.get(roleKey)!.push(session)
    }

    // Sort role groups by the earliest work-item-log/updatedAt position
    // of any turn in the group so the bar reads left-to-right in the
    // order roles first appeared.
    groupOrder.sort((leftKey, rightKey) => {
      const leftSessions = groups.get(leftKey) ?? []
      const rightSessions = groups.get(rightKey) ?? []
      const leftOrder = Math.min(
        ...leftSessions.map(s => taskOrder.get(getExecutionTurnId(s) || s.taskId) ?? Number.POSITIVE_INFINITY),
      )
      const rightOrder = Math.min(
        ...rightSessions.map(s => taskOrder.get(getExecutionTurnId(s) || s.taskId) ?? Number.POSITIVE_INFINITY),
      )
      if (leftOrder !== rightOrder) return leftOrder - rightOrder
      const leftUpdated = Math.min(...leftSessions.map(s => s.updatedAt))
      const rightUpdated = Math.min(...rightSessions.map(s => s.updatedAt))
      return leftUpdated - rightUpdated
    })

    const summaries: RoleSummaryInfo[] = []
    for (const roleKey of groupOrder) {
      const roleSessions = groups.get(roleKey) ?? []
      if (roleSessions.length === 0) continue

      // Build one turn entry per session, sorted chronologically (ASC).
      // Per design: "a 最前面, c 最后面" — first-happened goes first.
      // Each turn shows only its kanban column (To do / In progress /
      // In review / Done).  No synthetic "kind" classification.
      const turns: RoleTurnInfo[] = roleSessions
        .slice()
        .sort((a, b) => a.updatedAt - b.updatedAt)
        .map((session) => {
          const status = statusFromSession(session)
          const executionTurnId = getExecutionTurnId(session)
          const columnId = workItemStatusToColumnId(
            status,
            String((session as Session & { columnId?: string }).columnId ?? '').trim() || undefined,
          )
          return {
            executionTurnId: executionTurnId || session.taskId,
            title: firstNonEmpty(
              getWorkItemAssignmentLabel(session),
              session.title,
              session.workItemRoleName,
              session.workItemRoleId,
              'Turn',
            ),
            status,
            statusLabel: labelForColumnId(columnId),
            columnId,
            updatedAt: session.updatedAt,
          }
        })

      // The newest turn is the one the user probably wants to open when
      // they click the main chip — "what is this role doing right now"
      // beats "what did this role do first".
      const latest = turns[turns.length - 1]

      const aggregatedStatus = aggregateStatus(turns.map(t => t.status))
      const aggregatedColumnId = workItemStatusToColumnId(aggregatedStatus)

      // Role name beats task title on the main chip so the chip always
      // reads as "this role" regardless of which of its turns is loaded
      // as latest.
      const representative = roleSessions[0]
      const title = firstNonEmpty(
        representative.workItemRoleName,
        representative.workItemRoleId,
        representative.assigneeIds[0],
        getWorkItemAssignmentLabel(representative),
        representative.title,
        'Agent',
      )

      summaries.push({
        roleKey,
        executionTurnId: latest.executionTurnId,
        title,
        status: aggregatedStatus,
        statusLabel: labelForColumnId(aggregatedColumnId),
        executionAgent: formatExecutionAgent(
          representative.selectedExecutionAgent ?? representative.preferredAgent,
        ),
        roleName: representative.workItemRoleName,
        updatedAt: Math.max(...roleSessions.map(s => s.updatedAt)),
        turns,
      })
    }

    return summaries
  }, [childSessions, taskOrder])

  // Pick the work-item-driven summaries when present. In company/org
  // mode, runtime sessions are only audit targets; they must not synthesize
  // role rows because that recreates the role/session mix-up seen in new37.
  const roleSummaries = useMemo<RoleSummaryInfo[]>(() => (
    roleSummariesFromWorkItems.length > 0
      ? roleSummariesFromWorkItems
      : (isCompanyRuntime ? [] : roleSummariesFromSessions)
  ), [isCompanyRuntime, roleSummariesFromWorkItems, roleSummariesFromSessions])

  // Top-level pipeline: one chip per role, derived from roleSummaries
  // when available (kanban-push runs). Fall back to work-item-log work items
  // when child sessions haven't been serialized yet.
  const workItems = useMemo<WorkItemInfo[]>(() => {
    if (roleSummaries.length > 0) {
      return roleSummaries.map(role => ({
        projectionId: role.roleKey,
        title: role.title,
        roleName: role.roleName,
        status: role.status,
        executionTurnId: role.executionTurnId,
        executionAgent: role.executionAgent,
        externalTeamSummary: role.externalTeamSummary,
      }))
    }
    return isCompanyRuntime ? [] : workItemLogWorkItems
  }, [isCompanyRuntime, roleSummaries, workItemLogWorkItems])

  const isPreparingCompanyRuntime = isCompanyRuntime && roleSummaries.length === 0
  if (!isCompanyRuntime && workItemLog.length === 0 && workItems.length === 0 && roleSummaries.length === 0) return null

  return (
    <div className="wi-progress-card">
      <div className="wi-progress-header">
        <IconWorkItem />
        <span>Execution Progress</span>
      </div>

      {workItems.length > 0 && (
        <div className="wi-progress-pipeline">
          {workItems.map((workItem, i) => (
            <div key={workItem.projectionId} className="wi-projection-group">
              <button
                type="button"
                className={`wi-projection-chip wi-projection-${workItem.status}`}
                onClick={() => onWorkItemClick?.(workItem.executionTurnId || '')}
                title={`Open Runtime Session${workItem.roleName ? `: ${workItem.title} (${workItem.roleName})` : `: ${workItem.title}`}`}
              >
                <span className="wi-projection-label">{workItem.title}</span>
                {workItem.executionAgent && (
                  <span className="wi-projection-agent">{workItem.executionAgent}</span>
                )}
                {workItem.externalTeamSummary && (
                  <span className="wi-projection-team-summary">{externalTeamSummaryText(workItem.externalTeamSummary)}</span>
                )}
                {renderProjectionIcon(workItem.status)}
              </button>
              {i < workItems.length - 1 && <span className="wi-projection-connector">&rarr;</span>}
            </div>
          ))}
        </div>
      )}

      {isPreparingCompanyRuntime && (
        <div className="wi-progress-pipeline wi-progress-pipeline-empty" role="status">
          Preparing company roles…
        </div>
      )}

    </div>
  )
}
