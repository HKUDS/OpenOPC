import React, { useCallback, useMemo } from 'react'
import type { ChatMessageMeta, CheckpointReplyMetadata, InteractionOption } from '../types/chat'
import { MarkdownBody } from './MarkdownBody'

interface EscalationPanelProps {
  meta: ChatMessageMeta
  onReply: (text: string, metadata?: Partial<CheckpointReplyMetadata>) => void
  responded: boolean
  submitting?: boolean
}

function firstLine(text: string): string {
  return text.split('\n').map((line) => line.trim()).find(Boolean) ?? text
}

function checkpointStatusLabel(status: string): string {
  switch (status) {
    case 'timeout':
    case 'timed_out':
    case 'expired':
      return 'Expired'
    case 'stale':
    case 'invalid':
      return 'Inactive'
    case 'cancelled':
    case 'canceled':
      return 'Cancelled'
    case 'failed':
      return 'Failed'
    case 'outcome_unknown':
      return 'Outcome unknown'
    case 'resolved':
      return 'Resolved'
    default:
      return 'Responded'
  }
}

export const EscalationPanel = React.memo(function EscalationPanel({
  meta, onReply, responded, submitting = false,
}: EscalationPanelProps) {
  const isResponded = responded
  const checkpointStatus = String(meta.checkpoint_status ?? '').trim().toLowerCase()
  const resolvedLabel = checkpointStatusLabel(checkpointStatus)
  const prompt = String(meta.prompt ?? meta.summary ?? '')
  const lines = useMemo(
    () => prompt.split('\n').map((line) => line.trim()).filter(Boolean),
    [prompt],
  )
  const title = firstLine(prompt).replace(/^\[[^\]]+\]\s*/, '') || 'Action Required'
  const isPermission = ['tool_permission', 'action_permission'].includes(String(meta.checkpoint_type ?? ''))
  const displayTitle = meta.checkpoint_type === 'tool_permission'
    ? 'Tool Authorization'
    : meta.checkpoint_type === 'action_permission'
      ? 'Action Authorization'
      : title
  const permissionBadge = isPermission
    ? String(meta.checkpoint_type).replace(/_/g, ' ')
    : String(meta.escalation_type ?? 'decision_needed').replace(/_/g, ' ')
  // Non-permission cards use the first prompt line as their card title. A
  // typed permission card has a protocol title instead, so its whole prompt
  // remains request content; otherwise a one-line command/request vanishes.
  const request = isPermission
    ? prompt
    : lines.slice(1).join('\n').trim()
  const summary = String(meta.summary ?? '').trim()
  const options = (meta.options ?? []).filter((opt): opt is InteractionOption => !!opt?.id)
  const activeSubagents = useMemo(
    () => (meta.active_subagents ?? []).filter((item) => !!item && typeof item === 'object'),
    [meta.active_subagents],
  )
  const permissionRequests = useMemo(
    () => (meta.permission_requests ?? []).filter((item) => !!item && typeof item === 'object'),
    [meta.permission_requests],
  )
  const worktreePath = String(meta.worktree_path ?? '').trim()
  const hasRuntimeState = activeSubagents.length > 0 || permissionRequests.length > 0 || !!worktreePath

  const handleReply = useCallback((option: InteractionOption) => {
    if (isResponded || submitting) return
    onReply(option.label || option.id, {
      interaction_option_id: option.id,
      interaction_option_label: option.label || option.id,
      checkpoint_reply_kind: option.id.includes('deny') ? 'deny' : 'approve',
    })
  }, [isResponded, onReply, submitting])

  return (
    <div className="ckpt-panel ckpt-escalation">
      <div className="ckpt-header">
        <div className="ckpt-icon ckpt-icon-escalation">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M8 1.5L14.5 13H1.5L8 1.5Z" />
            <path d="M8 5.5V9" />
            <circle cx="8" cy="11.5" r="0.75" fill="currentColor" stroke="none" />
          </svg>
        </div>
        <div className="ckpt-title">{displayTitle}</div>
        <span className="ckpt-badge ckpt-badge-scope">
          {permissionBadge}
        </span>
        {isResponded && <span className="ckpt-badge ckpt-badge-responded">{resolvedLabel}</span>}
      </div>

      {summary && summary !== title && (
        <div className="ckpt-section">
          <div className="ckpt-section-title">Summary</div>
          <MarkdownBody
            content={summary}
            className="ckpt-markdown"
            collapseMode={isPermission ? 'never' : 'auto'}
          />
        </div>
      )}

      {request && (
        <div className="ckpt-section">
          <div className="ckpt-section-title">Request</div>
          {isPermission ? (
            <pre className="ckpt-permission-verbatim"><code>{request}</code></pre>
          ) : (
            <MarkdownBody content={request} className="ckpt-markdown" />
          )}
        </div>
      )}

      {hasRuntimeState && (
        <details className="ckpt-runtime-details">
          <summary>Runtime State</summary>
          <div className="ckpt-runtime-body">
            {worktreePath && <div>Worktree: <code>{worktreePath}</code></div>}
            {activeSubagents.length > 0 && <div>Active subagents: {activeSubagents.length}</div>}
            {permissionRequests.length > 0 && <div>Pending permission records: {permissionRequests.length}</div>}
          </div>
        </details>
      )}

      {!isResponded && options.length > 0 && (
        <div className="ckpt-actions ckpt-escalation-actions">
          {options.map((option) => (
            <button
              key={option.id}
              className={`ckpt-btn ${option.id.includes('deny') ? 'ckpt-btn-deny' : 'ckpt-btn-approve'}${submitting ? ' ckpt-btn-submitting' : ''}`}
              onClick={() => handleReply(option)}
              disabled={submitting}
            >
              {option.label || option.id}
            </button>
          ))}
        </div>
      )}

      {!isResponded && meta.default_action && (
        <div className="ckpt-escalation-hint">
          Default on timeout: <code>{meta.default_action}</code>
        </div>
      )}
    </div>
  )
})
