import React, { useCallback, useMemo, useState } from 'react'
import type { ChatMessageMeta, CheckpointReplyMetadata } from '../types/chat'
import { MarkdownBody } from './MarkdownBody'

interface DeliveryFeedbackPanelProps {
  meta: ChatMessageMeta
  onReply: (text: string, metadata?: CheckpointReplyMetadata) => Promise<boolean>
  responded: boolean
  submitting?: boolean
}

function firstLine(text: string): string {
  return text.split('\n').map((line) => line.trim()).find(Boolean) ?? text
}

function checkpointStatusLabel(status: string): string {
  switch (status) {
    case 'ignored':
      return 'Ignored'
    case 'timeout':
    case 'timed_out':
    case 'expired':
      return 'Expired'
    case 'stale':
    case 'invalid':
      return 'Inactive'
    case 'superseded':
      return 'Superseded'
    case 'cancelled':
    case 'canceled':
      return 'Cancelled'
    case 'resolved':
      return 'Resolved'
    default:
      return 'Responded'
  }
}

export const DeliveryFeedbackPanel = React.memo(function DeliveryFeedbackPanel({
  meta, onReply, responded, submitting = false,
}: DeliveryFeedbackPanelProps) {
  const isResponded = responded
  const [feedback, setFeedback] = useState('')
  const checkpointStatus = String(meta.checkpoint_status ?? '').trim().toLowerCase()
  const resolvedLabel = checkpointStatusLabel(checkpointStatus)
  const prompt = String(meta.prompt ?? meta.summary ?? '').trim()
  const baseTitle = String(meta.work_item_projection_title ?? firstLine(prompt) ?? 'Human Review').trim() || 'Human Review'
  const title = `${baseTitle} (for self-evolution)`
  const summary = String(meta.summary ?? '').trim()
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
  const actionsDisabled = isResponded || submitting

  const buildReplyMetadata = useCallback((kind: NonNullable<CheckpointReplyMetadata['checkpoint_reply_kind']>, text = ''): CheckpointReplyMetadata => {
    const checkpointId = String(meta.checkpoint_id ?? '').trim()
    if (!checkpointId) {
      throw new Error('Delivery self-evolution reply requires checkpoint_id metadata.')
    }
    const metadata: CheckpointReplyMetadata = {
      response_to_checkpoint_id: checkpointId,
      response_to_checkpoint_type: 'company_delivery_feedback',
      checkpoint_reply_kind: kind,
    }
    if (kind === 'approve' || kind === 'feedback') {
      metadata.self_evolution_trigger = true
      metadata.human_feedback_text = text
    }
    return metadata
  }, [meta.checkpoint_id])

  const handleApprove = useCallback(async () => {
    if (actionsDisabled) return
    const metadata = buildReplyMetadata('approve')
    await onReply('I fully agree with this delivery.', metadata)
  }, [actionsDisabled, buildReplyMetadata, onReply])

  const handleFeedback = useCallback(async () => {
    const text = feedback.trim()
    if (actionsDisabled || !text) return
    const metadata = buildReplyMetadata('feedback', text)
    const accepted = await onReply(text, metadata)
    if (accepted) setFeedback('')
  }, [actionsDisabled, buildReplyMetadata, feedback, onReply])

  const handleIgnore = useCallback(async () => {
    if (actionsDisabled) return
    const metadata = buildReplyMetadata('ignore')
    await onReply('Ignore this self-evolution review.', metadata)
  }, [actionsDisabled, buildReplyMetadata, onReply])

  return (
    <div className="ckpt-panel ckpt-delivery-feedback">
      <div className="ckpt-header">
        <div className="ckpt-icon ckpt-icon-user-input">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M8 1.5L14 5v4.5c0 2.25-1.8 4.25-6 5-4.2-.75-6-2.75-6-5V5L8 1.5Z" />
            <path d="M5.5 8.25L7.25 10l3.25-4" />
          </svg>
        </div>
        <div className="ckpt-title">{title}</div>
        <span className="ckpt-badge ckpt-badge-scope">
          {String(meta.feedback_scope ?? 'final').replace(/_/g, ' ')}
        </span>
        {isResponded && <span className="ckpt-badge ckpt-badge-responded">{resolvedLabel}</span>}
      </div>

      {summary && summary !== title && (
        <div className="ckpt-section">
          <div className="ckpt-section-title">Summary</div>
          <MarkdownBody content={summary} className="ckpt-markdown" />
        </div>
      )}
      {prompt && (
        <div className="ckpt-section">
          <div className="ckpt-section-title">Review Request</div>
          <MarkdownBody content={prompt} className="ckpt-markdown" />
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

      {!isResponded && (
        <div className="ckpt-actions ckpt-actions-inline-feedback">
          <button className="ckpt-btn ckpt-btn-approve" onClick={handleApprove} disabled={actionsDisabled}>
            Fully Agree
          </button>
          <button className="ckpt-btn ckpt-btn-cancel" onClick={handleIgnore} disabled={actionsDisabled}>
            Ignore
          </button>
          <textarea
            className="ckpt-feedback-input ckpt-feedback-inline-input"
            placeholder="Feedback for self-evolution..."
            value={feedback}
            onChange={event => setFeedback(event.target.value)}
            disabled={actionsDisabled}
            rows={2}
          />
          <button className="ckpt-btn ckpt-btn-feedback" onClick={handleFeedback} disabled={actionsDisabled || !feedback.trim()}>
            Send Feedback
          </button>
        </div>
      )}
    </div>
  )
})
