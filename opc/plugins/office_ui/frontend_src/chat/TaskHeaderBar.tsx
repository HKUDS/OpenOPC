import React, { useCallback, useEffect, useRef, useState } from 'react'
import type { Session } from '../types/kanban'
import type { AgentInfo } from '../types/visual'
import { IconStop, IconBoard, IconTool, IconCheck } from './SvgIcons'
import { getSessionRuntimeStatus, isSessionWorking } from '../lib/sessionRuntime'
import { getWorkItemRoleLabel } from '../lib/workItemIdentity'
import { executionAgentLabel } from '../lib/externalAgents'

interface TaskHeaderBarProps {
  session: Session
  agents: AgentInfo[]
  onTitleChange: (taskId: string, title: string) => void
  onViewOnBoard?: () => void
  onStop?: () => void
  onComplete?: () => void
  onResume?: () => void
}

function relativeTime(ts: number): string {
  const diff = Date.now() - ts
  if (diff < 60_000) return 'just now'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`
  return `${Math.floor(diff / 86_400_000)}d ago`
}

const STATUS_META: Record<string, { color: string; label: string }> = {
  running:   { color: 'var(--green)',          label: 'Running' },
  idle:      { color: 'var(--accent)',         label: 'Idle' },
  done:      { color: 'var(--green)',          label: 'Done' },
  pending:   { color: 'var(--text-secondary)', label: 'Pending' },
  failed:    { color: 'var(--red)',            label: 'Failed' },
  cancelled: { color: 'var(--text-secondary)', label: 'Cancelled' },
  blocked:   { color: 'var(--yellow)',         label: 'Blocked' },
  awaiting_human: { color: 'var(--yellow)', label: 'Waiting for review' },
  awaiting_manager_review: { color: 'var(--yellow)', label: 'Manager review' },
  awaiting_review: { color: 'var(--yellow)', label: 'Waiting for review' },
  awaiting_peer: { color: 'var(--yellow)', label: 'Waiting for peer' },
}

const HUMAN_REVIEW_STATUSES = new Set([
  'awaiting_human',
  'awaiting_manager_review',
  'awaiting_review',
  'awaiting_peer',
])

export function TaskHeaderBar({ session, agents, onTitleChange, onViewOnBoard, onStop, onComplete, onResume }: TaskHeaderBarProps) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(session.title)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    if (!editing) setDraft(session.title)
  }, [session.title, editing])

  const commitTitle = useCallback(() => {
    const trimmed = draft.trim()
    if (trimmed && trimmed !== session.title) {
      onTitleChange(session.taskId, trimmed)
    } else {
      setDraft(session.title)
    }
    setEditing(false)
  }, [draft, session.title, session.taskId, onTitleChange])

  const startEditing = useCallback(() => {
    setDraft(session.title)
    setEditing(true)
    setTimeout(() => inputRef.current?.select(), 0)
  }, [session.title])

  const assignees = session.assigneeIds
    .map(id => agents.find(a => a.agent_id === id))
    .filter(Boolean) as AgentInfo[]
  const execMode = String(session.execMode ?? '').trim().toLowerCase()
  const isCompanyHeaderSession = execMode === 'company'
    || execMode === 'org'
    || execMode === 'custom'
    || !!session.isCompanyRuntime
    || !!session.workItemProjectionId
    || !!session.workItemRoleId
    || !!session.workItemRoleName
  const showAssigneeAvatars = assignees.length > 0 && !isCompanyHeaderSession

  const runtimeControlState = session.runtimeControlState ?? (session.status === 'running' ? 'running' : 'idle')
  const isSuspending = runtimeControlState === 'suspending'
  const isResuming = runtimeControlState === 'resuming'
  const isSuspended = runtimeControlState === 'suspended'
  const isRunning = session.status === 'running' && !isSuspending && !isSuspended && !isResuming
  const isAwaitingReview = HUMAN_REVIEW_STATUSES.has(session.status)
  const canStop = (session.canStop ?? session.status === 'running') && !isSuspending && !isSuspended && !isResuming
  const canResume = (
    session.canResume
    ?? (isSuspended || (!isAwaitingReview && !isRunning && session.status !== 'done' && session.status !== 'pending'))
  ) && !isSuspending && !isResuming
  const meta = STATUS_META[session.status] ?? STATUS_META.pending
  const statusLabel = isSuspending ? 'Stopping' : isSuspended ? 'Suspended' : isResuming ? 'Resuming' : meta.label
  const roleLabel = getWorkItemRoleLabel(session)
  const runtimeStatus = getSessionRuntimeStatus(session)
  const isWorking = isSessionWorking(session)
  const liveTool = session.displayTool || session.currentTool
  // Sticky tool label tied to the RUN lifecycle (not the transient agentStatus).
  // The native runtime reports an 'idle'/'reflecting' state with no current_tool
  // between consecutive tool calls; reacting to that blanks the pill for a frame
  // and makes the command flicker once per call. Instead, keep showing the last
  // non-empty command for as long as the session is running, and drop it only
  // when the run stops — so the pill holds steady and just swaps to the next tool.
  const [stickyTool, setStickyTool] = useState<string | undefined>(liveTool || undefined)
  useEffect(() => {
    if (!isRunning) { setStickyTool(undefined); return }
    if (liveTool) setStickyTool(liveTool)
  }, [isRunning, liveTool])
  const hasApprovalMetrics = typeof session.pendingPermissionCount === 'number' && session.pendingPermissionCount > 0
  const showRuntimeMetrics = hasApprovalMetrics
  const statusDotColor = isSuspending
    ? 'var(--yellow)'
    : isSuspended
      ? 'var(--text-secondary)'
      : runtimeStatus === 'tool_active'
    ? 'var(--green)'
    : runtimeStatus === 'reflecting'
      ? 'var(--yellow)'
      : meta.color

  return (
    <div className="task-header-shell">
      <div className="task-header-bar">
        <div className="task-header-left">
        {editing ? (
          <input
            ref={inputRef}
            className="task-title-input"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onBlur={commitTitle}
            onKeyDown={e => {
              if (e.key === 'Enter') commitTitle()
              if (e.key === 'Escape') { setDraft(session.title); setEditing(false) }
            }}
          />
        ) : (
          <span className="task-title" onClick={startEditing} title="Click to edit">
            {session.title}
          </span>
        )}

        <span className="task-status-pill" data-status={session.status} data-working={isWorking ? 'true' : 'false'}>
          <span className="task-status-dot" style={{ background: statusDotColor }} />
          {statusLabel}
        </span>

        {isRunning && stickyTool && (
          <span className="task-tool-pill">
            <IconTool />
            <code>{stickyTool}</code>
            {session.currentTool && typeof session.toolElapsedMs === 'number' && session.toolElapsedMs > 0 && (
              <span>{session.toolElapsedMs}ms</span>
            )}
          </span>
        )}

        {session.lastToolSummary && (
          <span className="task-projection-pill" title={session.lastToolSummary}>
            {session.lastToolSummary.slice(0, 48)}
          </span>
        )}

        {/* Projection id (e.g. "attention::seat::team::cto::cto::review::f42cf81f")
            is an internal debug identifier — useful in the Info tab but
            adds noise to the header bar. Surface it via title-tooltip on
            the role pill rather than as a wide chip. */}

        {roleLabel && (
          <span className="task-role-pill" title={`Role: ${roleLabel}`}>
            {roleLabel}
          </span>
        )}

        {session.employeeAssignment?.name && (
          <span className="task-employee-pill" title={`Employee: ${session.employeeAssignment.name}${session.employeeAssignment.category ? ` (${session.employeeAssignment.category})` : ''}`}>
            <span className="task-employee-icon">&#x1F464;</span>
            {session.employeeAssignment.name}
          </span>
        )}

        {session.selectedExecutionAgent && (
          <span
            className="task-agent-pill"
            title={`Execution Agent: ${executionAgentLabel(session.selectedExecutionAgent)}`}
          >
            <span className="task-agent-icon">&#x2699;</span>
            {executionAgentLabel(session.selectedExecutionAgent)}
          </span>
        )}

        </div>

        <div className="task-header-right">
        {showAssigneeAvatars && (
          <div className="task-header-avatars">
            {assignees.slice(0, 3).map(a => (
              <span key={a.agent_id} className="task-header-avatar" title={a.name}>
                {a.name.charAt(0).toUpperCase()}
              </span>
            ))}
          </div>
        )}

        <span className="task-header-time" title={new Date(session.createdAt).toLocaleString()}>
          {relativeTime(session.createdAt)}
        </span>

        {(canStop || isSuspending) && onStop && (
          <button className="task-stop-btn" onClick={onStop} title="Stop task" disabled={!canStop}>
            <IconStop />
            <span>{isSuspending ? 'Stopping...' : 'Stop'}</span>
          </button>
        )}

        {canResume && onResume && session.status !== 'done' && (
          <button
            className="task-resume-btn"
            onClick={onResume}
            title="Resume prior runtime (re-awaken original team, no new plan)"
          >
            <span>Continue</span>
          </button>
        )}

        {onComplete && (
          <button className="task-done-btn" onClick={onComplete} title="Mark task as done">
            <IconCheck />
            <span>Done</span>
          </button>
        )}

        {onViewOnBoard && (
          <button className="task-board-btn" onClick={onViewOnBoard} title="View on Board">
            <IconBoard />
          </button>
        )}
        </div>
      </div>

      {showRuntimeMetrics && (
        <div className="task-header-metrics">
          {hasApprovalMetrics && (
            <div className="task-runtime-metric task-runtime-metric-approval" title="Pending approvals">
              <span className="task-runtime-metric-label">Approvals</span>
              <span className="task-runtime-metric-value">{session.pendingPermissionCount}</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
