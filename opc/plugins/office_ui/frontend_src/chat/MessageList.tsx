import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { AttachmentRefMeta, ChatMessage, CheckpointReplyMetadata, InteractionReplyReceipt } from '../types/chat'
import type { ProgressEntry, RoleWorkItemSummary, Session, WorkItemProgressEntry } from '../types/kanban'
import { progressEntryKey } from '../lib/progressEntryKey'
import { stableMessageTimelineKey } from '../lib/messageTimelineIdentity'
import { isMessageVisibleAtDetailLevel, resultSurfaceDedupeKey } from '../lib/workItemSessions'
import { resolveCanonicalTurnId, terminalAssistantTurnId } from '../lib/turnIdentity'
import { IconCopy, IconCheck, IconChat, IconSparkle, IconShield, IconActivity, IconChevron } from './SvgIcons'
import { AgentProgressBlock, AgentProgressEntryCard, INLINE_PROGRESS_ENTRY_TYPES } from './AgentProgressBlock'
import { MarkdownBody } from './MarkdownBody'
import { RecruitmentPanel } from './RecruitmentPanel'
import { StaffingSelectionPanel } from './StaffingSelectionPanel'
import { ReorgPanel } from './ReorgPanel'
import { EscalationPanel } from './EscalationPanel'
import { DeliveryFeedbackPanel } from './DeliveryFeedbackPanel'
import { TaskUserInputPanel } from './TaskUserInputPanel'
import { WorkItemProgressCard } from './WorkItemProgressCard'
import { analyzeCheckpointMessages, isCheckpointCardMetadata, toCheckpointReplyMetadata } from './checkpointUtils'

export { MarkdownBody } from './MarkdownBody'

function formatAttachmentSize(sizeBytes: number): string {
  if (sizeBytes < 1024) return `${sizeBytes}B`
  if (sizeBytes < 1048576) return `${(sizeBytes / 1024).toFixed(0)}KB`
  return `${(sizeBytes / 1048576).toFixed(1)}MB`
}

function attachmentBadgeLabel(mimeType: string, filename: string): string {
  const extension = filename.includes('.') ? filename.split('.').pop()?.toUpperCase() ?? '' : ''
  if (mimeType.startsWith('image/')) return 'IMG'
  if (mimeType === 'application/pdf') return 'PDF'
  if (mimeType.includes('wordprocessingml')) return 'DOC'
  if (mimeType.includes('spreadsheetml') || extension === 'CSV') return 'XLS'
  if (mimeType.includes('presentationml')) return 'PPT'
  if (mimeType.includes('json')) return 'JSON'
  if (mimeType.includes('yaml') || extension === 'YAML' || extension === 'YML') return 'YAML'
  if (mimeType.startsWith('text/')) return 'TXT'
  if (['PY', 'TS', 'TSX', 'JS', 'JSX', 'GO', 'RS', 'RB', 'JAVA', 'C', 'CPP', 'HTML', 'CSS', 'SH'].includes(extension)) return extension
  return extension || 'FILE'
}

function attachmentToneClass(mimeType: string, filename: string): string {
  const label = attachmentBadgeLabel(mimeType, filename)
  if (label === 'IMG') return 'image'
  if (label === 'PDF') return 'pdf'
  if (label === 'DOC' || label === 'XLS' || label === 'PPT') return 'office'
  if (label === 'JSON' || label === 'YAML') return 'data'
  if (label === 'TXT') return 'text'
  if (['PY', 'TS', 'TSX', 'JS', 'JSX', 'GO', 'RS', 'RB', 'JAVA', 'C', 'CPP', 'HTML', 'CSS', 'SH'].includes(label)) return 'code'
  return 'generic'
}

function AttachmentBlock({ refs, onImageClick }: { refs: AttachmentRefMeta[]; onImageClick?: (url: string) => void }) {
  if (!refs || refs.length === 0) return null
  const images = refs.filter(r => r.mime_type?.startsWith('image/'))
  const videos = refs.filter(r => r.mime_type?.startsWith('video/'))
  const files = refs.filter(r => !r.mime_type?.startsWith('image/') && !r.mime_type?.startsWith('video/'))
  const gridClass = images.length === 1 ? '' : images.length <= 3 ? ' cols-2' : ' cols-2'
  const videoGridClass = videos.length === 1 ? '' : videos.length <= 3 ? ' cols-2' : ' cols-2'
  return (
    <div className="msg-attachments">
      {images.length > 0 && (
        <div className={`msg-attachment-grid${gridClass}`}>
          {images.map(r => {
            const url = `/api/attachments/${r.attachment_id}/${r.filename}`
            return (
              <img
                key={r.attachment_id}
                className="msg-attachment-image"
                src={url}
                alt={r.filename}
                loading="lazy"
                onClick={() => onImageClick?.(url)}
              />
            )
          })}
        </div>
      )}
      {videos.length > 0 && (
        <div className={`msg-attachment-grid${videoGridClass}`}>
          {videos.map(r => {
            const url = `/api/attachments/${r.attachment_id}/${r.filename}`
            return (
              <video
                key={r.attachment_id}
                className="msg-attachment-video"
                src={url}
                controls
                preload="metadata"
                playsInline
              />
            )
          })}
        </div>
      )}
      {files.length > 0 && (
        <div className="msg-attachment-files">
          {files.map(r => (
            <a key={r.attachment_id} className="msg-attachment-file-chip" href={`/api/attachments/${r.attachment_id}/${r.filename}`} download title={r.filename}>
              📎 {r.filename}
              <span className="msg-attachment-file-size">{r.size_bytes < 1024 ? `${r.size_bytes}B` : r.size_bytes < 1048576 ? `${(r.size_bytes / 1024).toFixed(0)}KB` : `${(r.size_bytes / 1048576).toFixed(1)}MB`}</span>
            </a>
          ))}
        </div>
      )}
    </div>
  )
}

type ViewKind = 'session' | 'activity' | 'secretary'
type DetailMode = 'summary' | 'full'
export type MessageScrollPolicy = 'follow' | 'initial-bottom' | 'manual'

interface MessageListProps {
  messages: ChatMessage[]
  channelName: string
  viewKind?: ViewKind
  detailMode?: DetailMode
  agentStatus?: string
  currentTool?: string
  toolElapsedMs?: number
  lastToolSummary?: string
  progressLog?: ProgressEntry[]
  draftAssistantText?: string
  draftUpdatedAt?: number
  draftIteration?: number
  draftTurnId?: string
  isCompanyRuntime?: boolean
  workItemLog?: WorkItemProgressEntry[]
  childSessions?: Session[]
  /**
   * Per-role DelegationWorkItem rollup that drives the Execution Progress
   * panel. When present (company-mode primary sessions) it supersedes the
   * legacy session-derived rendering inside ``WorkItemProgressCard``.
   */
  roleWorkItems?: Record<string, RoleWorkItemSummary>
  /** Display-only executor-role rollup for the Execution Progress card. */
  executorRoleWorkItems?: Record<string, RoleWorkItemSummary>
  onInteractionReply?: (text: string, taskId?: string, metadata?: CheckpointReplyMetadata) => Promise<InteractionReplyReceipt>
  onWorkItemClick?: (executionTurnId: string) => void
  onWorkItemOpenSession?: (executionTurnId: string) => void
  onMarkRead?: () => void
  hasOlderHistory?: boolean
  totalMessageCount?: number
  onLoadOlderHistory?: (oldestMessage?: ChatMessage) => Promise<void> | void
  loadingOlderHistory?: boolean
  scrollPolicy?: MessageScrollPolicy
  /** Stable identity for the scroll state owned by this list instance. */
  scrollScope?: string
  showWorkItemRuntimeCard?: boolean
  showRuntimeProgress?: boolean
  renderUserMarkdown?: boolean
}

type TimelineItem =
  | { kind: 'message'; id: string; timestamp: number; msg: ChatMessage; sortOrder: number }
  | { kind: 'progress'; id: string; timestamp: number; entry: ProgressEntry; sortOrder: number }
  | { kind: 'draft'; id: string; timestamp: number; text: string; iteration?: number; sortOrder: number }
  | { kind: 'ops-bundle'; id: string; timestamp: number; events: SystemOpsBundleEvent[]; sortOrder: number }

type TimelineItemSemanticToken = readonly unknown[]
const semanticObjectTokenCache = new WeakMap<object, string>()

function semanticObjectToken(value: unknown): string {
  if (!value || typeof value !== 'object') return ''
  const cached = semanticObjectTokenCache.get(value)
  if (cached !== undefined) return cached
  const serialized = JSON.stringify(value) ?? ''
  semanticObjectTokenCache.set(value, serialized)
  return serialized
}

function timelineItemSemanticToken(item: TimelineItem): TimelineItemSemanticToken {
  if (item.kind === 'message') {
    const { msg } = item
    return [
      item.kind,
      item.id,
      item.timestamp,
      msg.sender,
      msg.senderName,
      msg.senderDeleted,
      msg.content,
      msg.replyToId,
      semanticObjectToken(msg.mentions),
      semanticObjectToken(msg.metadata),
    ]
  }
  if (item.kind === 'progress') {
    const { entry } = item
    return [
      item.kind,
      item.id,
      item.timestamp,
      entry.type,
      entry.summary,
      entry.detail,
      entry.turnId,
      entry.itemId,
      entry.streamId,
      entry.toolCallId,
      entry.permissionGroupKey,
      entry.seq,
      entry.executionMode,
    ]
  }
  if (item.kind === 'draft') {
    return [item.kind, item.id, item.timestamp, item.text, item.iteration]
  }
  const token: unknown[] = [
    item.kind,
    item.id,
    item.timestamp,
  ]
  for (const { msg, classification } of item.events) {
    token.push(
      msg.id,
      msg.timestamp,
      msg.content,
      semanticObjectToken(msg.metadata),
      classification.kind,
      classification.label,
      classification.summary,
      classification.tone,
    )
  }
  return token
}

function semanticTokenArraysEqual(
  left: TimelineItemSemanticToken[] | null,
  right: TimelineItemSemanticToken[],
): boolean {
  if (!left || left.length !== right.length) return false
  return right.every((token, itemIndex) => {
    const previous = left[itemIndex]
    return previous.length === token.length
      && token.every((value, valueIndex) => value === previous[valueIndex])
  })
}

interface ProjectUpdatePayload {
  kind: 'report' | 'review' | 'update'
  title?: string
  summary: string
  verdict?: string
  deliverables: Array<{ name: string; path: string; status?: string }>
  risks: string[]
  nextActions: string[]
  acceptanceSummary?: string
}

interface SystemOpsBundleEvent {
  msg: ChatMessage
  classification: SystemOpsClassification
}

/* ── Agent color palette ─────────────────────────────────────────────── */
const AGENT_PALETTE = [
  '#F59E0B', '#10B981', '#3B82F6', '#8B5CF6',
  '#EC4899', '#06B6D4', '#F97316', '#6366F1',
]

// Module-level cache so color lookups are O(1) across re-renders
const agentColorCache = new Map<string, string>()

function agentColor(sender: string): string {
  const cached = agentColorCache.get(sender)
  if (cached) return cached
  let h = 0
  for (let i = 0; i < sender.length; i++) h = (h * 31 + sender.charCodeAt(i)) | 0
  const color = AGENT_PALETTE[Math.abs(h) % AGENT_PALETTE.length]
  agentColorCache.set(sender, color)
  return color
}

/* ── Work-item event parser ───────────────────────────────────────────── */
const WORK_ITEM_RE = /^\[Company:([^\]]+)\]\s*(.*)$/

interface WorkItemInfo {
  projectionId: string
  workItemName: string
  action: string
  icon: string
  statusClass: string
}

function parseWorkItemEvent(content: string): WorkItemInfo | null {
  const m = WORK_ITEM_RE.exec(content)
  if (!m) return null
  const projectionId = m[1]
  const action = m[2].trim()
  const actionLower = action.toLowerCase()
  const workItemName = projectionId.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())

  let icon = '\u25CF'       // ● default
  let statusClass = 'active'
  if (actionLower.includes('gate passed') || actionLower.includes('approved') || actionLower.includes('completed')) {
    icon = '\u2713'; statusClass = 'passed'
  } else if (actionLower.includes('rejected') || actionLower.includes('reworking')) {
    icon = '\u21BB'; statusClass = 'rejected'
  } else if (actionLower.includes('failed') || actionLower.includes('timed out')) {
    icon = '\u2717'; statusClass = 'failed'
  } else if (actionLower.includes('awaiting')) {
    icon = '\u23F3'; statusClass = 'waiting'
  }
  return { projectionId, workItemName, action, icon, statusClass }
}

const WELCOME: Record<ViewKind, { icon: React.ReactNode; title: string; hint: string }> = {
  session: {
    icon: <IconChat />,
    title: 'New Conversation',
    hint: 'Send a message to start working with your OPC system',
  },
  activity: {
    icon: <IconActivity />,
    title: 'Activity Feed',
    hint: 'Agent activity across all sessions will appear here.',
  },
  secretary: {
    icon: <IconShield />,
    title: 'Secretary',
    hint: 'Manage policies, rules, and preferences for your agents.',
  },
}

/* ── Grouping: consecutive same-sender within 5 min ────────────────── */
const GROUP_WINDOW = 5 * 60_000
const INITIAL_VISIBLE_TIMELINE_ITEMS = 200
const VISIBLE_TIMELINE_STEP = 200
const BOTTOM_GAP_EPSILON_PX = 2

function isNearScrollBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_GAP_EPSILON_PX
}

function isDurableTimelineMessage(message: ChatMessage): boolean {
  // ChatStore reserves `msg-*` for local optimistic echoes. They may render in
  // the timeline immediately, but cannot advance a persistent read cursor
  // until the backend acknowledgement replaces them.
  return !String(message.id ?? '').startsWith('msg-')
}

export function messageTimelineKey(message: ChatMessage): string {
  return stableMessageTimelineKey(message)
}

function formatTime(ts: number) {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
}

function formatFullTime(ts: number) {
  return new Date(ts).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export async function copyTextToClipboard(text: string): Promise<boolean> {
  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Fall through to the textarea path for remote HTTP / denied clipboard contexts.
    }
  }

  if (typeof document === 'undefined' || !document.body) return false
  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.top = '0'
  textarea.style.left = '-9999px'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)

  try {
    textarea.focus()
    textarea.select()
    textarea.setSelectionRange(0, textarea.value.length)
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.body.removeChild(textarea)
  }
}

function isExecutionContextMessage(message: ChatMessage): boolean {
  return String(message.metadata?.transcript_kind ?? '').trim() === 'runtime_v2_user_turn'
}

function compactWhitespace(value: string): string {
  return value.replace(/\s+/g, ' ').trim()
}

function parseJsonObjectText(content: string): Record<string, unknown> | null {
  const trimmed = String(content || '').trim()
  if (!trimmed) return null
  try {
    const parsed = JSON.parse(trimmed)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : null
  } catch {
    return null
  }
}

function extractProjectUpdateJson(content: string): { obj: Record<string, unknown>; title?: string } | null {
  const trimmed = String(content || '').trim()
  if (!trimmed) return null

  if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
    const obj = parseJsonObjectText(trimmed)
    return obj ? { obj } : null
  }

  const firstBrace = trimmed.indexOf('{')
  const lastBrace = trimmed.lastIndexOf('}')
  if (firstBrace <= 0 || lastBrace <= firstBrace) return null

  const rawPrefix = trimmed.slice(0, firstBrace).trim()
  const prefix = rawPrefix.replace(/\*/g, '').replace(/:\s*$/, '').trim()
  if (!/\b(report|review)\b/i.test(prefix)) return null

  const obj = parseJsonObjectText(trimmed.slice(firstBrace, lastBrace + 1))
  if (!obj) return null
  return { obj, title: prefix || undefined }
}

function stringFromUnknown(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function stringsFromUnknownList(value: unknown, limit = 4): string[] {
  if (!Array.isArray(value)) return []
  const out: string[] = []
  for (const item of value) {
    const text = typeof item === 'string'
      ? item.trim()
      : item && typeof item === 'object' && 'summary' in item
        ? stringFromUnknown((item as Record<string, unknown>).summary)
        : ''
    if (text) out.push(text)
    if (out.length >= limit) break
  }
  return out
}

function deliverablesFromUnknown(value: unknown): ProjectUpdatePayload['deliverables'] {
  if (!Array.isArray(value)) return []
  const out: ProjectUpdatePayload['deliverables'] = []
  for (const item of value) {
    if (!item || typeof item !== 'object') continue
    const rec = item as Record<string, unknown>
    const path = stringFromUnknown(rec.path)
    const name = stringFromUnknown(rec.name) || path.split('/').filter(Boolean).pop() || 'Artifact'
    if (!path && !name) continue
    out.push({
      name,
      path,
      status: stringFromUnknown(rec.status) || undefined,
    })
  }
  return out
}

function acceptanceSummaryFromUnknown(value: unknown): string | undefined {
  if (!Array.isArray(value)) return undefined
  const total = value.filter(item => item && typeof item === 'object').length
  if (!total) return undefined
  const met = value.filter((item) => {
    const rec = item as Record<string, unknown>
    return rec.met === true
  }).length
  return `${met}/${total} acceptance checks met`
}

export function parseProjectUpdatePayload(content: string): ProjectUpdatePayload | null {
  const extracted = extractProjectUpdateJson(content)
  if (!extracted) return null
  const { obj, title } = extracted

  const summary = stringFromUnknown(obj.summary)
  const verdict = stringFromUnknown(obj.review_verdict)
  const deliverables = deliverablesFromUnknown(obj.deliverables)
  const risks = stringsFromUnknownList(obj.risks)
  const nextActions = stringsFromUnknownList(obj.next_actions)
  const acceptanceSummary = acceptanceSummaryFromUnknown(obj.acceptance_status)

  if (!summary && !verdict && deliverables.length === 0 && !acceptanceSummary) return null

  const kind: ProjectUpdatePayload['kind'] = verdict
    ? 'review'
    : (deliverables.length > 0 || acceptanceSummary ? 'report' : 'update')

  return {
    kind,
    title,
    summary,
    verdict: verdict || undefined,
    deliverables,
    risks,
    nextActions,
    acceptanceSummary,
  }
}

/* ── Memoized message-row sub-components ─────────────────────────────── *
 * Extracting each row type into React.memo prevents re-rendering the
 * entire visible list when only a single new message is appended.       */

interface ProgressRowProps {
  entry: ProgressEntry
  showDate: boolean
  dateStr: string
  compact?: boolean
}
const ProgressRow = React.memo(function ProgressRow({ entry, showDate, dateStr, compact = false }: ProgressRowProps) {
  return (
    <div>
      {showDate && <div className="msg-date-sep"><span>{dateStr}</span></div>}
      <div className={`msg-row agent msg-row-inline-progress${compact ? ' compact' : ''}`}>
        <div className="msg-avatar agent-avatar"><IconSparkle /></div>
        <div className="msg-body msg-inline-progress-body">
          <div className="msg-inline-progress-shell">
            <AgentProgressEntryCard entry={entry} />
          </div>
        </div>
      </div>
    </div>
  )
})

interface WorkItemRowProps { msg: ChatMessage; showDate: boolean; dateStr: string }
const WorkItemRow = React.memo(function WorkItemRow({ msg, showDate, dateStr }: WorkItemRowProps) {
  const workItem = parseWorkItemEvent(msg.content)
  if (!workItem) return null
  return (
    <div>
      {showDate && <div className="msg-date-sep"><span>{dateStr}</span></div>}
      <div className={`work-item-divider work-item-divider-${workItem.statusClass}`}>
        <div className="work-item-divider-line" />
        <span className="work-item-divider-label">
          <span className="work-item-divider-icon">{workItem.icon}</span>
          {workItem.workItemName}
          <span className="work-item-divider-action">{workItem.action}</span>
        </span>
        <div className="work-item-divider-line" />
      </div>
    </div>
  )
})

interface SystemRowProps { msg: ChatMessage; showDate: boolean; dateStr: string }
const SystemRow = React.memo(function SystemRow({ msg, showDate, dateStr }: SystemRowProps) {
  return (
    <div>
      {showDate && <div className="msg-date-sep"><span>{dateStr}</span></div>}
      <div className="msg-system"><span>{msg.content}</span></div>
    </div>
  )
})

interface ContextRowProps { msg: ChatMessage; showDate: boolean; dateStr: string }
const ContextRow = React.memo(function ContextRow({ msg, showDate, dateStr }: ContextRowProps) {
  return (
    <div>
      {showDate && <div className="msg-date-sep"><span>{dateStr}</span></div>}
      <div className="msg-row agent msg-row-context">
        <div className="msg-avatar agent-avatar msg-avatar-context"><IconShield /></div>
        <div className="msg-body">
          <div className="msg-content-agent-card msg-context-card">
            <div className="msg-context-label">Execution Context</div>
            <MarkdownBody content={msg.content} />
          </div>
        </div>
      </div>
    </div>
  )
})

interface DraftRowProps {
  text: string
  timestamp: number
  iteration?: number
  showDate: boolean
  dateStr: string
}
const DraftRow = React.memo(function DraftRow({ text, timestamp, iteration, showDate, dateStr }: DraftRowProps) {
  return (
    <div>
      {showDate && <div className="msg-date-sep"><span>{dateStr}</span></div>}
      <div className="msg-row agent msg-row-draft">
        <div className="msg-avatar agent-avatar msg-avatar-draft"><IconSparkle /></div>
        <div className="msg-body">
          <div className="msg-agent-header">
            <span className="msg-sender">Live Reply</span>
            <span className="msg-time" title={formatFullTime(timestamp)}>
              {formatTime(timestamp)}
            </span>
          </div>
          <div className="msg-content-agent-card msg-draft-card">
            {iteration ? <div className="msg-draft-label">Turn {iteration}</div> : null}
            <MarkdownBody content={text} collapseMode="never" />
          </div>
        </div>
      </div>
    </div>
  )
})

interface UserRowProps {
  msg: ChatMessage
  showDate: boolean
  dateStr: string
  isGrouped: boolean
  isCopied: boolean
  onCopy: (id: string, content: string) => void
  onImageClick: (url: string) => void
  renderUserMarkdown: boolean
}
const UserRow = React.memo(function UserRow({ msg, showDate, dateStr, isGrouped, isCopied, onCopy, onImageClick, renderUserMarkdown }: UserRowProps) {
  return (
    <div>
      {showDate && <div className="msg-date-sep"><span>{dateStr}</span></div>}
      <div className={`msg-row user${isGrouped ? ' grouped' : ''}`}>
        <div className="msg-body msg-body-user">
          <div className="msg-content-user">
            {renderUserMarkdown ? (
              <MarkdownBody content={msg.content} className="msg-content-user-markdown" />
            ) : (
              msg.content
            )}
          </div>
          <AttachmentBlock refs={(msg.metadata as any)?.attachment_refs} onImageClick={onImageClick} />
          <div className="msg-meta-user">
            <span className="msg-time" title={formatFullTime(msg.timestamp)}>{formatTime(msg.timestamp)}</span>
            <button className="msg-action-btn" onClick={() => onCopy(msg.id, msg.content)} title="Copy">
              {isCopied ? <IconCheck /> : <IconCopy />}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
})

/**
 * Recognise OPC's operational system messages — the ones that historically
 * dumped raw command strings ("[Delegating to codex] task=... | cmd=codex
 * exec -C /Users/...") straight into the transcript. We pull these out into
 * a one-line collapsed log row instead of a full agent-card so the chat
 * reads as conversation, not as terminal output.
 */
interface SystemOpsClassification {
  kind: 'delegation' | 'external_status' | 'external_resume' | 'external_error' | 'company_event' | 'other'
  label: string
  summary: string
  tone: 'info' | 'success' | 'warning' | 'danger'
}

function classifySystemOps(content: string): SystemOpsClassification | null {
  const trimmed = String(content || '').trim()
  if (!trimmed) return null
  // [Delegating to codex] task=... | model=... | cmd=...
  const delegate = trimmed.match(/^\[Delegating to ([^\]]+)\]/)
  if (delegate) {
    const agent = delegate[1].trim()
    const taskMatch = trimmed.match(/task=([^|]+?)(?:\||$)/)
    const task = taskMatch ? taskMatch[1].trim() : ''
    return {
      kind: 'delegation',
      label: `Delegating to ${agent}`,
      summary: task,
      tone: 'info',
    }
  }
  if (/^\[External resume\]/i.test(trimmed)) {
    const sessionMatch = trimmed.match(/codex:[a-z0-9-]+:([a-f0-9-]+)/i)
    return {
      kind: 'external_resume',
      label: 'External resume',
      summary: sessionMatch ? `Restored session ${sessionMatch[1].slice(0, 8)}…` : 'Resumed prior session',
      tone: 'info',
    }
  }
  const inbox = trimmed.match(/^\[External inbox\]\s*(.+)$/i)
  if (inbox) {
    return {
      kind: 'external_status',
      label: 'External inbox',
      summary: compactWhitespace(inbox[1]),
      tone: 'info',
    }
  }
  const status = trimmed.match(/^\[External status\]\s*(.+)$/i)
  if (status) {
    const head = status[1].split(/\(|;/)[0].trim()
    return {
      kind: 'external_status',
      label: 'External status',
      summary: head || status[1].trim(),
      tone: 'info',
    }
  }
  if (/^\[External error\]/i.test(trimmed) || /^\[External failure\]/i.test(trimmed)) {
    return {
      kind: 'external_error',
      label: 'External error',
      summary: trimmed.replace(/^\[[^\]]+\]\s*/, '').split('\n')[0].slice(0, 140),
      tone: 'danger',
    }
  }
  // [Company:cto::execute::5dbd78ae] completed / starting / parked …
  const company = trimmed.match(/^\[Company:([^\]]+)\]\s*(.+)$/)
  if (company) {
    const scope = company[1].split('::')
    const role = scope[0] || 'company'
    const verb = company[2].trim()
    const lc = verb.toLowerCase()
    const tone: SystemOpsClassification['tone'] =
      lc.includes('completed') || lc.includes('done') ? 'success' :
      lc.includes('blocked') || lc.includes('failed') ? 'warning' :
      'info'
    return {
      kind: 'company_event',
      label: `${role.toUpperCase()} · ${scope[1] ?? ''}`.replace(/ · $/, ''),
      summary: verb,
      tone,
    }
  }
  const routineRoleStatus = trimmed.match(/^(Review needed|Status digest|Blocked|Completion):\s*(.+)$/i)
  if (routineRoleStatus) {
    const label = routineRoleStatus[1].replace(/\b\w/g, c => c.toUpperCase())
    const summary = compactWhitespace(routineRoleStatus[2])
    return {
      kind: 'company_event',
      label,
      summary,
      tone: /^Blocked$/i.test(routineRoleStatus[1]) ? 'warning' : 'info',
    }
  }
  const noDelegation = trimmed.match(/^NO_DELEGATION_JUSTIFICATION:\s*(.+)$/i)
  if (noDelegation) {
    return {
      kind: 'company_event',
      label: 'Delegation check',
      summary: compactWhitespace(noDelegation[1]).slice(0, 180),
      tone: 'info',
    }
  }
  return null
}

function systemOpsBundleEventForMessage(
  message: ChatMessage,
  options: {
    isCompanyRuntime: boolean | undefined
    detailMode: DetailMode
  },
): SystemOpsBundleEvent | null {
  const { isCompanyRuntime, detailMode } = options
  if (!isCompanyRuntime || detailMode === 'full') return null
  if (isCheckpointCardMetadata(message.metadata)) return null

  const classification = classifySystemOps(message.content)
  if (!classification) return null

  const isOperationalSender = message.sender === 'system' || message.metadata?.type === 'system'
  const isCompanyEvent = classification.kind === 'company_event' || message.content.startsWith('[Company:')
  if (!isOperationalSender && !isCompanyEvent) return null

  return { msg: message, classification }
}

export function buildNarrativeMessageItems(
  messages: ChatMessage[],
  options: {
    isCompanyRuntime?: boolean
    detailMode?: DetailMode
  } = {},
): TimelineItem[] {
  const { isCompanyRuntime = false, detailMode = 'summary' } = options
  const items: TimelineItem[] = []
  let bundle: SystemOpsBundleEvent[] = []
  let bundleSortOrder = 0

  const flushBundle = () => {
    if (bundle.length === 0) return
    const first = bundle[0].msg
    items.push({
      kind: 'ops-bundle',
      id: `ops:${messageTimelineKey(first)}`,
      timestamp: first.timestamp,
      events: bundle,
      sortOrder: bundleSortOrder,
    })
    bundle = []
  }

  messages.forEach((msg, idx) => {
    const sortOrder = idx * 2 + 1
    const ops = systemOpsBundleEventForMessage(msg, { isCompanyRuntime, detailMode })
    if (ops) {
      if (bundle.length === 0) bundleSortOrder = sortOrder
      bundle.push(ops)
      return
    }
    flushBundle()
    items.push({
      kind: 'message',
      id: messageTimelineKey(msg),
      timestamp: msg.timestamp,
      msg,
      sortOrder,
    })
  })
  flushBundle()
  return items
}

interface SystemOpsRowProps {
  msg: ChatMessage
  showDate: boolean
  dateStr: string
  isGrouped: boolean
  classification: SystemOpsClassification
}

const SystemOpsRow = React.memo(function SystemOpsRow({ msg, showDate, dateStr, classification }: SystemOpsRowProps) {
  const [expanded, setExpanded] = useState(false)
  const hasDetails = msg.content.trim() !== `[${classification.label}] ${classification.summary}`.trim()
    && msg.content.trim().length > classification.summary.length + 4
  return (
    <div>
      {showDate && <div className="msg-date-sep"><span>{dateStr}</span></div>}
      <div
        className={`msg-ops-row msg-ops-tone-${classification.tone}${expanded ? ' expanded' : ''}`}
        onClick={hasDetails ? () => setExpanded(v => !v) : undefined}
        role={hasDetails ? 'button' : undefined}
        tabIndex={hasDetails ? 0 : -1}
      >
        <span className="msg-ops-dot" aria-hidden="true" />
        <span className="msg-ops-label">{classification.label}</span>
        {classification.summary && (
          <span className="msg-ops-summary" title={classification.summary}>{classification.summary}</span>
        )}
        <span className="msg-ops-time" title={formatFullTime(msg.timestamp)}>{formatTime(msg.timestamp)}</span>
        {hasDetails && (
          <span className={`msg-ops-chevron${expanded ? ' open' : ''}`} aria-hidden="true">
            <IconChevron down={expanded} />
          </span>
        )}
      </div>
      {hasDetails && expanded && (
        <div className="msg-ops-details">
          <pre className="msg-ops-details-pre">{msg.content}</pre>
        </div>
      )}
    </div>
  )
})

interface OpsBundleRowProps {
  events: SystemOpsBundleEvent[]
  showDate: boolean
  dateStr: string
}

const OpsBundleRow = React.memo(function OpsBundleRow({ events, showDate, dateStr }: OpsBundleRowProps) {
  const [expanded, setExpanded] = useState(false)
  if (events.length === 0) return null

  const first = events[0].msg
  const last = events[events.length - 1].msg
  const counts = events.reduce<Record<string, number>>((acc, event) => {
    const label = event.classification.kind === 'company_event'
      ? 'company'
      : event.classification.kind.startsWith('external')
        ? 'runtime'
        : 'delegation'
    acc[label] = (acc[label] ?? 0) + 1
    return acc
  }, {})
  const summary = Object.entries(counts)
    .map(([label, count]) => `${count} ${label}`)
    .join(' · ')
  const title = `${events.length} technical event${events.length === 1 ? '' : 's'} hidden`

  return (
    <div>
      {showDate && <div className="msg-date-sep"><span>{dateStr}</span></div>}
      <button
        type="button"
        className={`msg-ops-bundle${expanded ? ' expanded' : ''}`}
        onClick={() => setExpanded(v => !v)}
        title={`${formatFullTime(first.timestamp)} - ${formatFullTime(last.timestamp)}`}
      >
        <span className="msg-ops-bundle-dot" />
        <span className="msg-ops-bundle-title">{title}</span>
        <span className="msg-ops-bundle-summary">{summary}</span>
        <span className="msg-ops-bundle-time">{formatTime(first.timestamp)}</span>
        <span className="msg-ops-bundle-chevron"><IconChevron down={expanded} /></span>
      </button>
      {expanded && (
        <div className="msg-ops-bundle-details">
          {events.map(({ msg, classification }) => (
            <div key={msg.id} className={`msg-ops-bundle-event msg-ops-tone-${classification.tone}`}>
              <span className="msg-ops-dot" />
              <span className="msg-ops-label">{classification.label}</span>
              <span className="msg-ops-summary">{classification.summary}</span>
              <span className="msg-ops-time" title={formatFullTime(msg.timestamp)}>{formatTime(msg.timestamp)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
})

interface ProjectUpdateRowProps {
  msg: ChatMessage
  payload: ProjectUpdatePayload
  showDate: boolean
  dateStr: string
  isCopied: boolean
  onCopy: (id: string, content: string) => void
}

function projectUpdateLabel(payload: ProjectUpdatePayload): string {
  if (payload.kind === 'review') {
    const verdict = payload.verdict ? payload.verdict.replace(/_/g, ' ') : 'review'
    return `Review · ${verdict}`
  }
  if (payload.kind === 'report') return 'Report'
  return 'Update'
}

const ProjectUpdateRow = React.memo(function ProjectUpdateRow({ msg, payload, showDate, dateStr, isCopied, onCopy }: ProjectUpdateRowProps) {
  const color = agentColor(msg.sender === 'system' ? (msg.senderName || 'OPC') : msg.sender)
  const displayName = msg.senderDeleted ? '[Deleted]' : msg.senderName
  const label = projectUpdateLabel(payload)
  const summary = payload.summary || payload.acceptanceSummary || label

  return (
    <div>
      {showDate && <div className="msg-date-sep"><span>{dateStr}</span></div>}
      <div className="msg-row agent msg-row-project-update">
        <div className="msg-avatar agent-avatar" style={{ background: color }}>
          {displayName.charAt(0).toUpperCase()}
        </div>
        <div className="msg-body">
          <div className="msg-agent-header">
            <span className="msg-sender" style={{ color }}>{displayName}</span>
            <span className="msg-time" title={formatFullTime(msg.timestamp)}>{formatTime(msg.timestamp)}</span>
          </div>
          <div className={`msg-project-update-card msg-project-update-${payload.kind}`}>
            <div className="msg-project-update-head">
              <span className="msg-project-update-label">{label}</span>
              {payload.acceptanceSummary && (
                <span className="msg-project-update-chip">{payload.acceptanceSummary}</span>
              )}
            </div>
            {payload.title && <div className="msg-project-update-title">{payload.title}</div>}
            <MarkdownBody content={summary} className="msg-project-update-summary" />
            {payload.deliverables.length > 0 && (
              <div className="msg-project-update-section">
                <div className="msg-project-update-section-label">Outputs</div>
                <div className="msg-project-update-artifacts">
                  {payload.deliverables.slice(0, 6).map((artifact, index) => (
                    <div key={`${artifact.path || artifact.name}-${index}`} className="msg-project-update-artifact">
                      <span className="msg-project-update-artifact-name">{artifact.name}</span>
                      {artifact.status && <span className="msg-project-update-artifact-status">{artifact.status}</span>}
                      {artifact.path && <code className="msg-project-update-artifact-path">{artifact.path}</code>}
                    </div>
                  ))}
                </div>
              </div>
            )}
            {(payload.risks.length > 0 || payload.nextActions.length > 0) && (
              <div className="msg-project-update-grid">
                {payload.risks.length > 0 && (
                  <div className="msg-project-update-section">
                    <div className="msg-project-update-section-label">Caveats</div>
                    <ul className="msg-project-update-list">
                      {payload.risks.map((risk, index) => <li key={`risk-${index}`}>{risk}</li>)}
                    </ul>
                  </div>
                )}
                {payload.nextActions.length > 0 && (
                  <div className="msg-project-update-section">
                    <div className="msg-project-update-section-label">Next</div>
                    <ul className="msg-project-update-list">
                      {payload.nextActions.map((action, index) => <li key={`next-${index}`}>{action}</li>)}
                    </ul>
                  </div>
                )}
              </div>
            )}
            <div className="msg-card-actions">
              <button className="msg-action-btn" onClick={() => onCopy(msg.id, msg.content)} title="Copy raw update">
                {isCopied ? <><IconCheck /> <span>Copied</span></> : <><IconCopy /> <span>Copy raw</span></>}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
})

interface AgentRowProps {
  msg: ChatMessage
  showDate: boolean
  dateStr: string
  isGrouped: boolean
  isCopied: boolean
  isCheckpointResponded: boolean
  suppressCheckpointPanel: boolean
  keepExpanded?: boolean
  onCopy: (id: string, content: string) => void
  onInteractionReply?: (text: string, taskId?: string, metadata?: CheckpointReplyMetadata) => Promise<InteractionReplyReceipt>
}
const AgentRow = React.memo(function AgentRow({ msg, showDate, dateStr, isGrouped, isCopied, isCheckpointResponded, suppressCheckpointPanel, keepExpanded, onCopy, onInteractionReply }: AgentRowProps) {
  const isDeleted = !!msg.senderDeleted
  const displayName = isDeleted ? '[Deleted]' : msg.senderName
  const color = agentColor(msg.sender === 'system' ? (msg.senderName || 'OPC') : msg.sender)
  const replyTaskId = msg.channelId.startsWith('session:')
    ? msg.channelId.slice('session:'.length)
    : (msg.metadata?.taskId ?? msg.metadata?.task_id)
  const checkpointType = String(msg.metadata?.checkpoint_type ?? '').trim()
  const checkpointReplyMeta = toCheckpointReplyMetadata(msg.metadata)
  const hasCheckpointPanel = isCheckpointCardMetadata(msg.metadata) && !suppressCheckpointPanel
  const [interactionAccepted, setInteractionAccepted] = useState(false)
  const [interactionSubmitting, setInteractionSubmitting] = useState(false)
  const [interactionError, setInteractionError] = useState('')
  const interactionRequestIdRef = useRef<string>('')
  if (!interactionRequestIdRef.current) {
    const cryptoApi = globalThis.crypto
    interactionRequestIdRef.current = cryptoApi && typeof cryptoApi.randomUUID === 'function'
      ? `interaction-ui-${cryptoApi.randomUUID()}`
      : `interaction-ui-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`
  }
  const checkpointResponded = isCheckpointResponded || interactionAccepted
  const submitInteraction = useCallback(async (
    text: string,
    metadata?: CheckpointReplyMetadata,
  ) => {
    if (!onInteractionReply || interactionSubmitting || checkpointResponded) return false
    setInteractionSubmitting(true)
    setInteractionError('')
    try {
      const receipt = await onInteractionReply(text, replyTaskId, {
        ...checkpointReplyMeta,
        ...metadata,
        ui_message_id: interactionRequestIdRef.current,
      })
      if (receipt.ok && receipt.accepted) {
        setInteractionAccepted(true)
        return true
      } else {
        setInteractionError(receipt.reason || receipt.error || 'Decision was not accepted.')
        return false
      }
    } catch (error) {
      setInteractionError(error instanceof Error ? error.message : 'Decision was not accepted.')
      return false
    } finally {
      setInteractionSubmitting(false)
    }
  }, [checkpointReplyMeta, checkpointResponded, interactionSubmitting, onInteractionReply, replyTaskId])

  return (
    <div>
      {showDate && <div className="msg-date-sep"><span>{dateStr}</span></div>}
      <div className={`msg-row agent${isGrouped ? ' grouped' : ''}${isDeleted ? ' deleted-sender' : ''}`}>
        {!isGrouped ? (
          <div className="msg-avatar agent-avatar" style={{ background: color }}>
            {displayName.charAt(0).toUpperCase()}
          </div>
        ) : (
          <div className="msg-avatar-spacer" />
        )}
        <div className="msg-body">
          {!isGrouped && (
            <div className="msg-agent-header">
              <span className={`msg-sender${isDeleted ? ' deleted' : ''}`} style={{ color }}>
                {displayName}
              </span>
              <span className="msg-time" title={formatFullTime(msg.timestamp)}>{formatTime(msg.timestamp)}</span>
            </div>
          )}
          {msg.replyToId && <div className="msg-reply-indicator">Replying to previous message</div>}
          {hasCheckpointPanel ? (
            <>
              {checkpointType === 'company_recruitment_confirmation' && (
                <RecruitmentPanel
                  meta={msg.metadata!}
                  onReply={(text, extraMetadata) => submitInteraction(text, extraMetadata)}
                  responded={checkpointResponded}
                />
              )}
              {checkpointType === 'company_staffing_selection' && (
                <StaffingSelectionPanel
                  meta={msg.metadata!}
                  onReply={(text, extraMetadata) => { void submitInteraction(text, extraMetadata) }}
                  responded={checkpointResponded}
                />
              )}
              {checkpointType === 'company_work_item_gate' && (
                <EscalationPanel
                  meta={msg.metadata!}
                  onReply={(text, extraMetadata) => { void submitInteraction(text, extraMetadata) }}
                  responded={checkpointResponded}
                  submitting={interactionSubmitting}
                />
              )}
              {checkpointType === 'company_delivery_feedback' && (
                <DeliveryFeedbackPanel
                  meta={msg.metadata!}
                  onReply={(text, extraMetadata) => submitInteraction(text, extraMetadata)}
                  responded={checkpointResponded}
                  submitting={interactionSubmitting}
                />
              )}
              {checkpointType === 'company_reorg_pending' && (
                <ReorgPanel
                  meta={msg.metadata!}
                  onReply={(text) => { void submitInteraction(text) }}
                  responded={checkpointResponded}
                />
              )}
              {['tool_permission', 'action_permission'].includes(checkpointType) && (
                <EscalationPanel
                  meta={msg.metadata!}
                  onReply={(text, extraMetadata) => { void submitInteraction(text, extraMetadata) }}
                  responded={checkpointResponded}
                  submitting={interactionSubmitting}
                />
              )}
              {['task_user_input', 'route_clarification', 'company_runtime_selection', 'company_run_failure_review'].includes(checkpointType) && (
                <TaskUserInputPanel
                  meta={msg.metadata!}
                  onReply={(text, extraMetadata) => { void submitInteraction(text, extraMetadata) }}
                  responded={checkpointResponded}
                />
              )}
              {interactionSubmitting && <div className="ckpt-escalation-hint">Saving decision…</div>}
              {interactionError && <div className="ckpt-escalation-hint" role="alert">{interactionError}</div>}
            </>
          ) : (
            <div className="msg-content-agent-card" style={{ borderLeftColor: color }}>
              <MarkdownBody content={msg.content} collapseMode={keepExpanded ? 'never' : 'auto'} />
              <div className="msg-card-actions">
                <button className="msg-action-btn" onClick={() => onCopy(msg.id, msg.content)} title="Copy message">
                  {isCopied ? <><IconCheck /> <span>Copied</span></> : <><IconCopy /> <span>Copy</span></>}
                </button>
                {isGrouped && (
                  <span className="msg-time msg-time-inline" title={formatFullTime(msg.timestamp)}>{formatTime(msg.timestamp)}</span>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
})

type ViewportMode = 'following' | 'browsing'

interface ViewportAnchor {
  key: string
  viewportOffset: number
}

interface TimelineWindowState {
  scope: string
  startKey: string | null
}

export const MessageList = React.memo(function MessageList({
  messages,
  channelName,
  viewKind = 'session',
  detailMode = 'summary',
  agentStatus,
  currentTool,
  toolElapsedMs,
  lastToolSummary,
  progressLog,
  draftAssistantText,
  draftUpdatedAt,
  draftIteration,
  draftTurnId,
  isCompanyRuntime,
  workItemLog,
  childSessions,
  roleWorkItems,
  executorRoleWorkItems,
  onInteractionReply,
  onWorkItemClick,
  onMarkRead,
  hasOlderHistory = false,
  totalMessageCount,
  onLoadOlderHistory,
  loadingOlderHistory = false,
  scrollPolicy = 'follow',
  scrollScope: scrollScopeProp,
  showWorkItemRuntimeCard = true,
  showRuntimeProgress = true,
  renderUserMarkdown = false,
}: MessageListProps) {
  const scrollScope = scrollScopeProp ?? `${viewKind}:${detailMode}:${channelName}`
  const listRef = useRef<HTMLDivElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const [lightboxUrl, setLightboxUrl] = useState<string | null>(null)
  const initialMode: ViewportMode = scrollPolicy === 'follow' ? 'following' : 'browsing'
  const [viewportMode, setViewportMode] = useState<ViewportMode>(initialMode)
  const [unseenCount, setUnseenCount] = useState(0)
  const [atStrictBottom, setAtStrictBottom] = useState(false)
  const modeRef = useRef<ViewportMode>(initialMode)
  const policyRef = useRef<MessageScrollPolicy>(scrollPolicy)
  const initialScrollPendingRef = useRef(scrollPolicy !== 'manual')
  const atBottomRef = useRef(false)
  const anchorRef = useRef<ViewportAnchor | null>(null)
  const lastScrollTopRef = useRef(0)
  const userGestureRef = useRef(false)
  const userIntentRef = useRef<'none' | 'up' | 'down'>('none')
  const touchYRef = useRef<number | null>(null)
  const detachedTailTimestampRef = useRef(0)
  const knownPersistentKeysRef = useRef<Set<string>>(new Set())
  const lastMarkedReadKeyRef = useRef<string | null>(null)
  const latestPersistentReadKeyRef = useRef<string | null>(null)
  const onMarkReadRef = useRef(onMarkRead)
  const latestTimelineTimestampRef = useRef(0)
  const logicalContentRevisionRef = useRef(0)
  const previousTimelineTokensRef = useRef<TimelineItemSemanticToken[] | null>(null)
  const initialBottomLayoutRef = useRef<{ active: boolean; revision: number }>({ active: false, revision: 0 })
  const hasRenderableContentRef = useRef(false)
  const historyRequestRef = useRef<{ scope: string; startKey: string; beforeIndex: number } | null>(null)
  const anchorRestorePendingRef = useRef(false)
  const pendingFocusKeyRef = useRef<string | null>(null)
  const viewportSizeRef = useRef<{ width: number; height: number } | null>(null)
  const previousTimelineKeysRef = useRef<string[]>([])
  const messageKeyAssignmentsRef = useRef<{
    scope: string
    assignments: Map<string, Map<string, string>>
  }>({ scope: scrollScope, assignments: new Map() })
  const resultKeyOwnerRef = useRef<{ scope: string; owners: Map<string, string> }>({
    scope: scrollScope,
    owners: new Map(),
  })
  const streamedTurnKeysRef = useRef<{ scope: string; keys: Set<string> }>({
    scope: scrollScope,
    keys: new Set(),
  })
  onMarkReadRef.current = onMarkRead

  const workItemLogLen = workItemLog?.length ?? 0
  const workItemCount = workItemLogLen + (childSessions?.length ?? 0)
  const filteredMessages = useMemo(() => {
    const visibleMessages = messages.filter(message => isMessageVisibleAtDetailLevel(message, detailMode))
    if (!isCompanyRuntime) return visibleMessages
    return visibleMessages.filter((message) => {
      const isCompanySystemMessage = message.metadata?.type === 'system' && message.content.startsWith('[Company:')
      if (isCompanySystemMessage) return false
      if (!showRuntimeProgress && message.content.startsWith('[Company:')) return false
      return true
    })
  }, [detailMode, isCompanyRuntime, messages, showRuntimeProgress])

  const checkpointAnalysis = useMemo(
    () => analyzeCheckpointMessages(filteredMessages),
    [filteredMessages],
  )
  const pendingCheckpointMessages = useMemo(
    () => viewKind === 'session' && !!onInteractionReply
      ? filteredMessages.filter(message => checkpointAnalysis.pendingMessageIds.has(message.id))
      : [],
    [checkpointAnalysis.pendingMessageIds, filteredMessages, onInteractionReply, viewKind],
  )
  // Checkpoint cards never leave the chronological transcript. Their pending
  // state only controls the fixed reminder rendered outside the scroll flow.
  const timelineMessages = filteredMessages

  const thinkingProgressTurnIds = useMemo(() => {
    const ids = new Set<string>()
    for (const entry of progressLog ?? []) {
      if (entry.type !== 'thinking') continue
      const turnId = String(entry.turnId ?? '').trim()
      if (turnId) ids.add(turnId)
    }
    return ids
  }, [progressLog])
  const synthesizedThinkingEntries = useMemo(() => {
    const entries: ProgressEntry[] = []
    for (const message of timelineMessages) {
      const thinking = String(message.metadata?.runtime_thinking ?? '').trim()
      if (!thinking) continue
      const turnId = resolveCanonicalTurnId(message.metadata)
      if (turnId && thinkingProgressTurnIds.has(turnId)) continue
      entries.push({
        type: 'thinking' as const,
        summary: 'Thinking',
        detail: thinking,
        timestamp: Math.max(0, message.timestamp - 1),
        turnId: turnId || undefined,
        itemId: turnId ? `${turnId}:thinking` : `thinking:${message.id}`,
        streamId: turnId ? `${turnId}:thinking` : `thinking:${message.id}`,
        executionMode: String(message.metadata?.execution_mode ?? '').trim() || undefined,
      })
    }
    return entries
  }, [thinkingProgressTurnIds, timelineMessages])
  const inlineProgressEntries = useMemo(
    () => showRuntimeProgress
      ? [
        ...(progressLog ?? []).filter(entry => INLINE_PROGRESS_ENTRY_TYPES.has(entry.type)),
        ...synthesizedThinkingEntries,
      ].sort((a, b) => a.timestamp - b.timestamp)
      : [],
    [progressLog, showRuntimeProgress, synthesizedThinkingEntries],
  )
  const secondaryProgressEntries = useMemo(
    () => showRuntimeProgress
      ? (progressLog ?? []).filter(entry => !INLINE_PROGRESS_ENTRY_TYPES.has(entry.type))
      : [],
    [progressLog, showRuntimeProgress],
  )
  const timelineProgressEntries = useMemo(
    () => !showRuntimeProgress
      ? []
      : detailMode === 'full'
        ? (progressLog ?? [])
        : inlineProgressEntries,
    [detailMode, inlineProgressEntries, progressLog, showRuntimeProgress],
  )
  const bottomProgressEntries = secondaryProgressEntries
  const committedTurnIds = useMemo(() => {
    const ids = new Set<string>()
    for (const message of timelineMessages) {
      const turnId = terminalAssistantTurnId(message)
      if (turnId) ids.add(turnId)
    }
    return ids
  }, [timelineMessages])
  const draftTimelineItem = useMemo<TimelineItem | null>(() => {
    const text = String(draftAssistantText ?? '').trim()
    const turnId = String(draftTurnId ?? '').trim()
    if (!text || (turnId && committedTurnIds.has(turnId))) return null
    return {
      kind: 'draft',
      id: turnId ? `turn:assistant:${turnId}` : 'draft:active',
      timestamp: draftUpdatedAt ?? timelineMessages[timelineMessages.length - 1]?.timestamp ?? 0,
      text,
      iteration: draftIteration,
      sortOrder: Number.MAX_SAFE_INTEGER,
    }
  }, [committedTurnIds, draftAssistantText, draftIteration, draftTurnId, draftUpdatedAt, timelineMessages])
  useLayoutEffect(() => {
    if (streamedTurnKeysRef.current.scope !== scrollScope) {
      streamedTurnKeysRef.current = { scope: scrollScope, keys: new Set() }
    }
    if (draftTimelineItem) streamedTurnKeysRef.current.keys.add(draftTimelineItem.id)
  }, [draftTimelineItem, scrollScope])
  const isAgentWorking = !!agentStatus && agentStatus !== 'idle'
  const hasProgressLog = bottomProgressEntries.length > 0
  const showProgressBlock = showRuntimeProgress && detailMode !== 'full' && (isAgentWorking || hasProgressLog)
  const timelineBuild = useMemo(() => {
    const committed: TimelineItem[] = [
      ...buildNarrativeMessageItems(timelineMessages, { isCompanyRuntime, detailMode }),
      ...timelineProgressEntries.map((entry, idx) => ({
        kind: 'progress' as const,
        id: `progress:${progressEntryKey(entry)}`,
        timestamp: entry.timestamp,
        entry,
        sortOrder: idx * 2,
      })),
    ].sort((a, b) => a.timestamp - b.timestamp || a.sortOrder - b.sortOrder)
    const previousResultOwners = resultKeyOwnerRef.current.scope === scrollScope
      ? resultKeyOwnerRef.current.owners
      : new Map<string, string>()
    const nextResultOwners = new Map<string, string>()
    committed.forEach((item, index) => {
      if (item.kind !== 'message') return
      const resultKey = resultSurfaceDedupeKey(item.msg)
      if (!resultKey) return
      const owner = previousResultOwners.get(resultKey) ?? item.id
      nextResultOwners.set(resultKey, owner)
      if (owner !== item.id) committed[index] = { ...item, id: owner }
    })
    const duplicateKeyGroups = new Map<string, Array<{ index: number; identity: string }>>()
    committed.forEach((item, index) => {
      if (item.kind !== 'message') return
      const identity = `${item.msg.channelId}\u0000${item.msg.id}`
      const group = duplicateKeyGroups.get(item.id) ?? []
      group.push({ index, identity })
      duplicateKeyGroups.set(item.id, group)
    })
    const previousAssignments = messageKeyAssignmentsRef.current.scope === scrollScope
      ? messageKeyAssignmentsRef.current.assignments
      : new Map<string, Map<string, string>>()
    const nextAssignments = new Map<string, Map<string, string>>()
    for (const [baseKey, group] of duplicateKeyGroups) {
      const previous = previousAssignments.get(baseKey)
      const assigned = new Map<string, string>()
      const usedKeys = new Set<string>()
      // Preserve every mounted identity first, even if an older row was just
      // inserted ahead of it or the former base-key owner disappeared.
      for (const entry of group) {
        const previousKey = previous?.get(entry.identity)
        if (!previousKey || usedKeys.has(previousKey)) continue
        assigned.set(entry.identity, previousKey)
        usedKeys.add(previousKey)
      }
      for (const entry of group) {
        if (assigned.has(entry.identity)) continue
        const assignedKey = usedKeys.has(baseKey)
          ? `${baseKey}:message:${encodeURIComponent(entry.identity)}`
          : baseKey
        assigned.set(entry.identity, assignedKey)
        usedKeys.add(assignedKey)
      }
      nextAssignments.set(baseKey, assigned)
      for (const entry of group) {
        const assignedKey = assigned.get(entry.identity)
        if (!assignedKey || assignedKey === baseKey) continue
        const item = committed[entry.index]
        if (item.kind !== 'message') continue
        committed[entry.index] = {
          ...item,
          id: assignedKey,
        }
      }
    }
    // A live turn is one stable tail slot. Its updatedAt never re-sorts it.
    if (draftTimelineItem) committed.push(draftTimelineItem)
    return { items: committed, messageKeyAssignments: nextAssignments, resultKeyOwners: nextResultOwners }
  }, [detailMode, draftTimelineItem, isCompanyRuntime, scrollScope, timelineMessages, timelineProgressEntries])
  const timelineItems = timelineBuild.items
  const timelineSemanticTokens = useMemo(
    () => timelineItems.map(timelineItemSemanticToken),
    [timelineItems],
  )
  useLayoutEffect(() => {
    messageKeyAssignmentsRef.current = {
      scope: scrollScope,
      assignments: timelineBuild.messageKeyAssignments,
    }
    resultKeyOwnerRef.current = { scope: scrollScope, owners: timelineBuild.resultKeyOwners }
  }, [scrollScope, timelineBuild.messageKeyAssignments, timelineBuild.resultKeyOwners])
  const timelineMessagesRef = useRef(timelineMessages)
  timelineMessagesRef.current = timelineMessages
  latestTimelineTimestampRef.current = timelineMessages.reduce(
    (latest, message) => Math.max(latest, message.timestamp),
    0,
  )

  const [windowState, setWindowState] = useState<TimelineWindowState>({ scope: scrollScope, startKey: null })
  const configuredStartKey = windowState.scope === scrollScope ? windowState.startKey : null
  const defaultStartIndex = Math.max(0, timelineItems.length - INITIAL_VISIBLE_TIMELINE_ITEMS)
  const configuredStartIndex = configuredStartKey
    ? timelineItems.findIndex(item => item.id === configuredStartKey)
    : -1
  let recoveredStartIndex = -1
  if (configuredStartKey && configuredStartIndex < 0) {
    const previousKeys = previousTimelineKeysRef.current
    const previousStartIndex = previousKeys.indexOf(configuredStartKey)
    const currentKeyIndexes = new Map(timelineItems.map((item, index) => [item.id, index]))
    for (let index = Math.max(0, previousStartIndex); index < previousKeys.length; index += 1) {
      const currentIndex = currentKeyIndexes.get(previousKeys[index])
      if (currentIndex !== undefined) {
        recoveredStartIndex = currentIndex
        break
      }
    }
    if (recoveredStartIndex < 0) {
      for (let index = previousStartIndex - 1; index >= 0; index -= 1) {
        const currentIndex = currentKeyIndexes.get(previousKeys[index])
        if (currentIndex !== undefined) {
          recoveredStartIndex = currentIndex
          break
        }
      }
    }
  }
  const visibleStartIndex = configuredStartIndex >= 0
    ? configuredStartIndex
    : recoveredStartIndex >= 0
      ? recoveredStartIndex
      : defaultStartIndex
  const visibleTimelineItems = useMemo(
    () => timelineItems.slice(visibleStartIndex),
    [timelineItems, visibleStartIndex],
  )
  const hiddenTimelineCount = visibleStartIndex
  const timelineItemsRef = useRef(timelineItems)
  timelineItemsRef.current = timelineItems

  useLayoutEffect(() => {
    const startKey = timelineItems[visibleStartIndex]?.id ?? null
    if (windowState.scope === scrollScope && windowState.startKey === startKey) return
    if (windowState.scope === scrollScope && windowState.startKey) {
      anchorRestorePendingRef.current = true
    }
    setWindowState({ scope: scrollScope, startKey })
  }, [scrollScope, timelineItems, visibleStartIndex, windowState.scope, windowState.startKey])

  useLayoutEffect(() => {
    const request = historyRequestRef.current
    if (!request || request.scope !== scrollScope) return
    const currentIndex = timelineItems.findIndex(item => item.id === request.startKey)
    if (currentIndex <= request.beforeIndex) return
    const nextIndex = Math.max(0, currentIndex - VISIBLE_TIMELINE_STEP)
    historyRequestRef.current = null
    anchorRestorePendingRef.current = true
    setWindowState({ scope: scrollScope, startKey: timelineItems[nextIndex]?.id ?? null })
  }, [scrollScope, timelineItems])

  useLayoutEffect(() => {
    previousTimelineKeysRef.current = timelineItems.map(item => item.id)
  }, [timelineItems])

  /* ── Pre-process dates + sender grouping ───────────────────────── */
  const processed = useMemo(() => {
    let lastDate = ''
    return visibleTimelineItems.map((item, idx) => {
      const dateStr = new Date(item.timestamp).toLocaleDateString()
      const showDate = dateStr !== lastDate
      if (showDate) lastDate = dateStr
      const prev = idx > 0 ? visibleTimelineItems[idx - 1] : null
      const isGrouped = item.kind === 'message'
        && prev?.kind === 'message'
        && !showDate
        && prev.msg.sender === item.msg.sender
        && prev.msg.senderName === item.msg.senderName
        && item.msg.metadata?.type !== 'system'
        && prev.msg.metadata?.type !== 'system'
        && !(item.msg.metadata as any)?.is_work_item_event
        && !(prev.msg.metadata as any)?.is_work_item_event
        && item.msg.timestamp - prev.msg.timestamp < GROUP_WINDOW
      return { item, showDate, dateStr, isGrouped }
    })
  }, [visibleTimelineItems])

  const hasWorkItemRuntimeCard = !!(
    detailMode !== 'full'
    && showWorkItemRuntimeCard
    && isCompanyRuntime
    && workItemCount > 0
  )
  const hasRenderableContent = timelineItems.length > 0 || hasWorkItemRuntimeCard || showProgressBlock
  hasRenderableContentRef.current = hasRenderableContent

  const findTimelineElement = useCallback((key: string): HTMLElement | null => {
    const rows = contentRef.current?.querySelectorAll<HTMLElement>('[data-timeline-key]')
    if (!rows) return null
    for (const row of rows) {
      if (row.dataset.timelineKey === key) return row
    }
    return null
  }, [])

  const updateAtBottom = useCallback((next: boolean) => {
    atBottomRef.current = next
    setAtStrictBottom(previous => previous === next ? previous : next)
  }, [])

  const markLatestRead = useCallback(() => {
    const key = latestPersistentReadKeyRef.current
    const markRead = onMarkReadRef.current
    if (!key || !markRead || lastMarkedReadKeyRef.current === key) return
    lastMarkedReadKeyRef.current = key
    markRead()
  }, [])

  const captureViewportAnchor = useCallback(() => {
    const list = listRef.current
    const content = contentRef.current
    if (!list || !content) return
    const listRect = list.getBoundingClientRect()
    const rows = content.querySelectorAll<HTMLElement>('[data-timeline-key]')
    for (const row of rows) {
      const rect = row.getBoundingClientRect()
      if (rect.bottom <= listRect.top + 0.5) continue
      anchorRef.current = {
        key: row.dataset.timelineKey ?? '',
        viewportOffset: rect.top - listRect.top,
      }
      return
    }
    anchorRef.current = null
  }, [])

  const restoreViewportAnchor = useCallback(() => {
    const list = listRef.current
    const anchor = anchorRef.current
    if (!list || !anchor?.key) return
    const row = findTimelineElement(anchor.key)
    if (!row) {
      captureViewportAnchor()
      return
    }
    const delta = (
      row.getBoundingClientRect().top - list.getBoundingClientRect().top
    ) - anchor.viewportOffset
    if (Math.abs(delta) > 0.5) list.scrollTop += delta
    anchor.viewportOffset = row.getBoundingClientRect().top - list.getBoundingClientRect().top
    lastScrollTopRef.current = list.scrollTop
    updateAtBottom(isNearScrollBottom(list))
  }, [captureViewportAnchor, findTimelineElement, updateAtBottom])

  const setMode = useCallback((next: ViewportMode) => {
    modeRef.current = next
    setViewportMode(previous => previous === next ? previous : next)
  }, [])

  const enterBrowsing = useCallback(() => {
    initialBottomLayoutRef.current.active = false
    if (modeRef.current !== 'browsing') {
      detachedTailTimestampRef.current = latestTimelineTimestampRef.current
      setUnseenCount(0)
      setMode('browsing')
    }
    captureViewportAnchor()
  }, [captureViewportAnchor, setMode])

  const writeBottom = useCallback(() => {
    const list = listRef.current
    if (!list) return
    const target = Math.max(0, list.scrollHeight - list.clientHeight)
    if (Math.abs(list.scrollTop - target) > 0.5) list.scrollTop = target
    lastScrollTopRef.current = list.scrollTop
    updateAtBottom(isNearScrollBottom(list))
  }, [updateAtBottom])

  const resumeFollowing = useCallback(() => {
    if (policyRef.current !== 'follow') return
    setMode('following')
    setUnseenCount(0)
    anchorRef.current = null
    writeBottom()
    markLatestRead()
  }, [markLatestRead, setMode, writeBottom])

  const reachLatestFromUser = useCallback(() => {
    if (policyRef.current === 'follow') {
      resumeFollowing()
      return
    }
    initialBottomLayoutRef.current.active = false
    writeBottom()
    captureViewportAnchor()
    setUnseenCount(0)
    markLatestRead()
  }, [captureViewportAnchor, markLatestRead, resumeFollowing, writeBottom])

  const reconcileViewport = useCallback(() => {
    const list = listRef.current
    if (!list || !hasRenderableContentRef.current) return
    updateAtBottom(isNearScrollBottom(list))
    if (initialScrollPendingRef.current) {
      initialScrollPendingRef.current = false
      if (policyRef.current === 'initial-bottom') {
        initialBottomLayoutRef.current = {
          active: true,
          revision: logicalContentRevisionRef.current,
        }
      }
      writeBottom()
      if (policyRef.current !== 'follow') {
        setMode('browsing')
      }
      return
    }
    const initialBottomLayout = initialBottomLayoutRef.current
    if (
      policyRef.current === 'initial-bottom'
      && initialBottomLayout.active
      && initialBottomLayout.revision === logicalContentRevisionRef.current
    ) {
      writeBottom()
      return
    }
    if (policyRef.current === 'follow' && modeRef.current === 'following') {
      writeBottom()
      return
    }
    if (anchorRestorePendingRef.current) {
      anchorRestorePendingRef.current = false
      restoreViewportAnchor()
    }
  }, [restoreViewportAnchor, setMode, updateAtBottom, writeBottom])

  useLayoutEffect(() => {
    policyRef.current = scrollPolicy
    const nextMode: ViewportMode = scrollPolicy === 'follow' ? 'following' : 'browsing'
    modeRef.current = nextMode
    setViewportMode(nextMode)
    initialScrollPendingRef.current = scrollPolicy !== 'manual'
    updateAtBottom(false)
    anchorRef.current = null
    historyRequestRef.current = null
    anchorRestorePendingRef.current = false
    pendingFocusKeyRef.current = null
    viewportSizeRef.current = null
    initialBottomLayoutRef.current = { active: false, revision: logicalContentRevisionRef.current }
    knownPersistentKeysRef.current = new Set(
      timelineMessagesRef.current.filter(isDurableTimelineMessage).map(messageTimelineKey),
    )
    detachedTailTimestampRef.current = latestTimelineTimestampRef.current
    lastMarkedReadKeyRef.current = null
    setUnseenCount(0)
  }, [scrollPolicy, scrollScope, updateAtBottom])

  useLayoutEffect(() => {
    const previous = previousTimelineTokensRef.current
    const unchanged = semanticTokenArraysEqual(previous, timelineSemanticTokens)
    if (unchanged) return
    previousTimelineTokensRef.current = timelineSemanticTokens
    logicalContentRevisionRef.current += 1
    const initialBottomLayout = initialBottomLayoutRef.current
    if (!initialBottomLayout.active || initialBottomLayout.revision === logicalContentRevisionRef.current) return
    initialBottomLayout.active = false
    captureViewportAnchor()
  }, [captureViewportAnchor, timelineSemanticTokens])

  useLayoutEffect(() => {
    const list = listRef.current
    const content = contentRef.current
    if (!list || !content) return
    viewportSizeRef.current = { width: list.clientWidth, height: list.clientHeight }
    reconcileViewport()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver((entries) => {
      const viewportEntry = entries.find(entry => entry.target === list)
      if (viewportEntry) {
        const previous = viewportSizeRef.current
        const next = { width: list.clientWidth, height: list.clientHeight }
        if (previous && (previous.width !== next.width || previous.height !== next.height)) {
          anchorRestorePendingRef.current = modeRef.current === 'browsing'
        }
        viewportSizeRef.current = next
      }
      reconcileViewport()
    })
    observer.observe(list)
    observer.observe(content)
    return () => observer.disconnect()
  }, [reconcileViewport, scrollPolicy, scrollScope])

  useEffect(() => {
    const list = listRef.current
    if (!list) return
    lastScrollTopRef.current = list.scrollTop

    const handleWheel = (event: WheelEvent) => {
      userIntentRef.current = event.deltaY < 0 ? 'up' : event.deltaY > 0 ? 'down' : 'none'
      if (event.deltaY < 0 && list.scrollTop > 0) enterBrowsing()
      if (event.deltaY > 0 && isNearScrollBottom(list)) resumeFollowing()
    }
    const handlePointerDown = () => {
      // Overlay scrollbars report no physical gutter. Treat any pointer-held
      // scroll inside the viewport as explicit user control; ordinary clicks
      // clear this flag on pointerup without changing the viewport mode.
      userGestureRef.current = true
    }
    const finishPointerGesture = () => {
      userGestureRef.current = false
      userIntentRef.current = 'none'
      if (modeRef.current === 'browsing') captureViewportAnchor()
    }
    const handleTouchStart = (event: TouchEvent) => {
      touchYRef.current = event.touches[0]?.clientY ?? null
      userGestureRef.current = true
    }
    const handleTouchMove = (event: TouchEvent) => {
      const nextY = event.touches[0]?.clientY
      const previousY = touchYRef.current
      if (nextY === undefined || previousY === null) return
      if (nextY > previousY + 1) {
        userIntentRef.current = 'up'
        enterBrowsing()
      } else if (nextY < previousY - 1) {
        userIntentRef.current = 'down'
      }
      touchYRef.current = nextY
    }
    const handleTouchEnd = () => {
      touchYRef.current = null
      finishPointerGesture()
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      if (target && target !== list && (
        target.isContentEditable
        || ['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON'].includes(target.tagName)
      )) return
      if (['ArrowUp', 'PageUp', 'Home'].includes(event.key) || (event.key === ' ' && event.shiftKey)) {
        userIntentRef.current = 'up'
        enterBrowsing()
      } else if (['ArrowDown', 'PageDown', 'End'].includes(event.key) || event.key === ' ') {
        userIntentRef.current = 'down'
        if (event.key === 'End') reachLatestFromUser()
      }
    }
    const handleKeyUp = () => {
      userIntentRef.current = 'none'
    }
    const handleScroll = () => {
      const previous = lastScrollTopRef.current
      const next = list.scrollTop
      const atBottom = isNearScrollBottom(list)
      const wasAtBottom = atBottomRef.current
      const userDriven = userGestureRef.current || userIntentRef.current !== 'none'
      // A native scrollbar thumb drag does not consistently dispatch its
      // pointerdown to the scroll element (overlay scrollbars in particular).
      // FOLLOWING never writes upward, so an observed upward scroll away from
      // the strict bottom is itself sufficient user intent without a timer.
      if (next < previous - 0.5 && !atBottom) {
        enterBrowsing()
      } else if (userDriven) {
        if (userIntentRef.current === 'up') {
          enterBrowsing()
        } else if (atBottom && (next > previous + 0.5 || userIntentRef.current === 'down')) {
          resumeFollowing()
        }
        if (modeRef.current === 'browsing') captureViewportAnchor()
        if (!userGestureRef.current) userIntentRef.current = 'none'
      } else if (next > previous + 0.5 && atBottom) {
        // Symmetric fallback for a native scrollbar drag to the strict tail.
        // Controller writes update lastScrollTopRef before their scroll event,
        // so they cannot take this branch.
        resumeFollowing()
      } else if (modeRef.current === 'browsing' && Math.abs(next - previous) > 0.5) {
        // Native/assistive scrolling does not always expose a wheel, key, or
        // pointer marker. Any non-controller movement while browsing becomes
        // the new viewport anchor before a later resize or history prepend.
        captureViewportAnchor()
      }
      lastScrollTopRef.current = next
      updateAtBottom(atBottom)
      if (
        !wasAtBottom
        && atBottom
        && (userDriven || next > previous + 0.5)
      ) {
        markLatestRead()
      }
    }

    list.addEventListener('wheel', handleWheel, { passive: true })
    list.addEventListener('pointerdown', handlePointerDown)
    window.addEventListener('pointerup', finishPointerGesture)
    window.addEventListener('pointercancel', finishPointerGesture)
    window.addEventListener('blur', finishPointerGesture)
    list.addEventListener('touchstart', handleTouchStart, { passive: true })
    list.addEventListener('touchmove', handleTouchMove, { passive: true })
    list.addEventListener('touchend', handleTouchEnd, { passive: true })
    list.addEventListener('touchcancel', handleTouchEnd, { passive: true })
    list.addEventListener('keydown', handleKeyDown)
    list.addEventListener('keyup', handleKeyUp)
    list.addEventListener('scroll', handleScroll, { passive: true })
    return () => {
      list.removeEventListener('wheel', handleWheel)
      list.removeEventListener('pointerdown', handlePointerDown)
      window.removeEventListener('pointerup', finishPointerGesture)
      window.removeEventListener('pointercancel', finishPointerGesture)
      window.removeEventListener('blur', finishPointerGesture)
      list.removeEventListener('touchstart', handleTouchStart)
      list.removeEventListener('touchmove', handleTouchMove)
      list.removeEventListener('touchend', handleTouchEnd)
      list.removeEventListener('touchcancel', handleTouchEnd)
      list.removeEventListener('keydown', handleKeyDown)
      list.removeEventListener('keyup', handleKeyUp)
      list.removeEventListener('scroll', handleScroll)
    }
  }, [captureViewportAnchor, enterBrowsing, markLatestRead, reachLatestFromUser, resumeFollowing, updateAtBottom])

  const durableTimelineMessages = useMemo(
    () => timelineMessages.filter(isDurableTimelineMessage),
    [timelineMessages],
  )
  const persistentMessageKeys = useMemo(
    () => durableTimelineMessages.map(messageTimelineKey),
    [durableTimelineMessages],
  )
  useEffect(() => {
    const current = new Set(persistentMessageKeys)
    if (knownPersistentKeysRef.current.size === 0) {
      knownPersistentKeysRef.current = current
      return
    }
    if (modeRef.current === 'browsing') {
      let added = 0
      for (const message of durableTimelineMessages) {
        const key = messageTimelineKey(message)
        if (!knownPersistentKeysRef.current.has(key) && message.timestamp >= detachedTailTimestampRef.current) added += 1
      }
      if (added > 0) setUnseenCount(previous => previous + added)
    } else {
      setUnseenCount(0)
    }
    knownPersistentKeysRef.current = current
  }, [durableTimelineMessages, persistentMessageKeys])

  const latestPersistentMessage = durableTimelineMessages.reduce<ChatMessage | undefined>(
    (latest, message) => !latest || message.timestamp > latest.timestamp ? message : latest,
    undefined,
  )
  const latestPersistentReadKey = latestPersistentMessage
    ? `${messageTimelineKey(latestPersistentMessage)}:${latestPersistentMessage.timestamp}`
    : null
  latestPersistentReadKeyRef.current = latestPersistentReadKey
  useEffect(() => {
    if (!latestPersistentReadKey) return
    const list = listRef.current
    const atBottom = !!list && isNearScrollBottom(list)
    updateAtBottom(atBottom)
    if (modeRef.current !== 'following' && !atBottom) return
    markLatestRead()
  }, [latestPersistentReadKey, markLatestRead, updateAtBottom, viewportMode])

  const copyMsg = useCallback((id: string, content: string) => {
    void copyTextToClipboard(content).then((copied) => {
      if (!copied) return
      setCopiedId(id)
      setTimeout(() => setCopiedId(null), 1500)
    })
  }, [])

  const handleLoadOlder = useCallback(async () => {
    captureViewportAnchor()
    anchorRestorePendingRef.current = true
    if (visibleStartIndex > 0) {
      const nextIndex = Math.max(0, visibleStartIndex - VISIBLE_TIMELINE_STEP)
      setWindowState({ scope: scrollScope, startKey: timelineItems[nextIndex]?.id ?? null })
      return
    }
    if (!hasOlderHistory || loadingOlderHistory || !onLoadOlderHistory) return
    historyRequestRef.current = configuredStartKey
      ? { scope: scrollScope, startKey: configuredStartKey, beforeIndex: visibleStartIndex }
      : null
    await onLoadOlderHistory(filteredMessages[0])
  }, [
    captureViewportAnchor,
    configuredStartKey,
    filteredMessages,
    hasOlderHistory,
    loadingOlderHistory,
    onLoadOlderHistory,
    scrollScope,
    timelineItems,
    visibleStartIndex,
  ])

  const jumpToLatest = reachLatestFromUser

  const centerTimelineRow = useCallback((key: string): boolean => {
    const list = listRef.current
    const row = findTimelineElement(key)
    if (!list || !row) return false
    const listRect = list.getBoundingClientRect()
    const rowRect = row.getBoundingClientRect()
    list.scrollTop += rowRect.top - listRect.top - Math.max(0, (list.clientHeight - rowRect.height) / 2)
    lastScrollTopRef.current = list.scrollTop
    updateAtBottom(isNearScrollBottom(list))
    captureViewportAnchor()
    return true
  }, [captureViewportAnchor, findTimelineElement, updateAtBottom])

  useLayoutEffect(() => {
    const key = pendingFocusKeyRef.current
    if (!key || !centerTimelineRow(key)) return
    pendingFocusKeyRef.current = null
  }, [centerTimelineRow, visibleTimelineItems])

  const focusPendingCheckpoint = useCallback(() => {
    const pending = pendingCheckpointMessages[0]
    if (!pending) return
    enterBrowsing()
    anchorRestorePendingRef.current = false
    const key = messageTimelineKey(pending)
    if (centerTimelineRow(key)) return
    const itemIndex = timelineItems.findIndex(item => item.id === key)
    if (itemIndex < 0) return
    pendingFocusKeyRef.current = key
    const nextStartIndex = Math.max(0, itemIndex - 2)
    setWindowState({ scope: scrollScope, startKey: timelineItems[nextStartIndex]?.id ?? key })
  }, [centerTimelineRow, enterBrowsing, pendingCheckpointMessages, scrollScope, timelineItems])

  const renderProcessedRow = useCallback((row: typeof processed[number]) => {
    const { item, showDate, dateStr, isGrouped } = row
    if (item.kind === 'progress') {
      return <ProgressRow entry={item.entry} showDate={showDate} dateStr={dateStr} compact={detailMode !== 'full'} />
    }
    if (item.kind === 'draft') {
      return <DraftRow text={item.text} timestamp={item.timestamp} iteration={item.iteration} showDate={showDate} dateStr={dateStr} />
    }
    if (item.kind === 'ops-bundle') {
      return <OpsBundleRow events={item.events} showDate={showDate} dateStr={dateStr} />
    }

    const { msg } = item
    const keepExpanded = streamedTurnKeysRef.current.scope === scrollScope
      && streamedTurnKeysRef.current.keys.has(item.id)
    const isSystem = msg.metadata?.type === 'system'
    const isExecutionContext = isExecutionContextMessage(msg)
    const isWorkItemEvent = !keepExpanded && (
      !!(msg.metadata as any)?.is_work_item_event || msg.content.startsWith('[Company:')
    )
    const hasCpPanel = isCheckpointCardMetadata(msg.metadata)
    const isUser = msg.sender === 'user'
    const projectUpdate = !keepExpanded && !isUser && !hasCpPanel
      ? parseProjectUpdatePayload(msg.content)
      : null
    if (isWorkItemEvent && !hasCpPanel) return <WorkItemRow msg={msg} showDate={showDate} dateStr={dateStr} />
    if (projectUpdate) {
      return <ProjectUpdateRow msg={msg} payload={projectUpdate} showDate={showDate} dateStr={dateStr} isCopied={copiedId === msg.id} onCopy={copyMsg} />
    }
    if (isExecutionContext && !hasCpPanel) return <ContextRow msg={msg} showDate={showDate} dateStr={dateStr} />
    if (isSystem && !hasCpPanel) return <SystemRow msg={msg} showDate={showDate} dateStr={dateStr} />
    if (isUser) {
      return (
        <UserRow
          msg={msg} showDate={showDate} dateStr={dateStr} isGrouped={isGrouped}
          isCopied={copiedId === msg.id} onCopy={copyMsg} onImageClick={setLightboxUrl}
          renderUserMarkdown={renderUserMarkdown}
        />
      )
    }
    if (msg.sender === 'system' && !isCheckpointCardMetadata(msg.metadata)) {
      const ops = classifySystemOps(msg.content)
      if (ops) return <SystemOpsRow msg={msg} showDate={showDate} dateStr={dateStr} isGrouped={isGrouped} classification={ops} />
    }
    return (
      <AgentRow
        msg={msg} showDate={showDate} dateStr={dateStr} isGrouped={isGrouped}
        isCopied={copiedId === msg.id}
        isCheckpointResponded={checkpointAnalysis.respondedMessageIds.has(msg.id)}
        suppressCheckpointPanel={checkpointAnalysis.duplicateMessageIds.has(msg.id)}
        keepExpanded={keepExpanded}
        onCopy={copyMsg} onInteractionReply={onInteractionReply}
      />
    )
  }, [checkpointAnalysis, copiedId, copyMsg, detailMode, onInteractionReply, renderUserMarkdown, scrollScope])

  const welcome = WELCOME[viewKind]

  return (
    <div className={`msg-list-shell msg-list-shell-${viewportMode}`}>
      <div
        className={`msg-list msg-list-${viewportMode}`}
        ref={listRef}
        tabIndex={0}
        data-scroll-policy={scrollPolicy}
        data-viewport-mode={viewportMode}
      >
        <div className="msg-list-content" ref={contentRef}>
          {!hasRenderableContent && (
            <div className="msg-welcome">
              <div className="msg-welcome-icon">{welcome.icon}</div>
              <div className="msg-welcome-title">{channelName || welcome.title}</div>
              <div className="msg-welcome-hint">{welcome.hint}</div>
            </div>
          )}

          {(hiddenTimelineCount > 0 || hasOlderHistory) && (
            <div className="msg-history-hint">
              <button className="msg-history-load-btn" onClick={() => { void handleLoadOlder() }} disabled={loadingOlderHistory}>
                {loadingOlderHistory
                  ? 'Loading older messages...'
                  : hiddenTimelineCount > 0
                    ? `Load ${Math.min(VISIBLE_TIMELINE_STEP, hiddenTimelineCount)} older messages`
                    : 'Load older messages'}
              </button>
              <span className="msg-history-meta">
                Showing {visibleTimelineItems.length} of {totalMessageCount ?? timelineItems.length}
              </span>
            </div>
          )}

          {processed.map(row => (
            <div key={row.item.id} className="msg-timeline-row" data-timeline-key={row.item.id}>
              {renderProcessedRow(row)}
            </div>
          ))}

          {hasWorkItemRuntimeCard && (
            <div className="msg-row agent">
              <div className="msg-avatar agent-avatar"><IconSparkle /></div>
              <div className="msg-body">
                <WorkItemProgressCard
                  workItemLog={workItemLog ?? []}
                  roleWorkItems={roleWorkItems}
                  executorRoleWorkItems={executorRoleWorkItems}
                  childSessions={childSessions}
                  isCompanyRuntime={isCompanyRuntime}
                  onWorkItemClick={onWorkItemClick}
                />
              </div>
            </div>
          )}

          {showProgressBlock && (
            <div className="msg-row agent agent-working-row">
              <div className="msg-avatar agent-avatar"><IconSparkle /></div>
              <div className="msg-body">
                <AgentProgressBlock entries={bottomProgressEntries} agentStatus={agentStatus} currentTool={currentTool} toolElapsedMs={toolElapsedMs} lastToolSummary={lastToolSummary} expandedByDefault />
              </div>
            </div>
          )}
          <div className="msg-end-anchor" />
        </div>
      </div>

      {(pendingCheckpointMessages.length > 0 || (viewportMode === 'browsing' && !atStrictBottom)) && (
        <div className="msg-list-floating-actions">
          {pendingCheckpointMessages.length > 0 && (
            <button type="button" className="msg-list-float-btn msg-list-pending-btn" onClick={focusPendingCheckpoint}>
              Pending actions · {pendingCheckpointMessages.length}
            </button>
          )}
          {viewportMode === 'browsing' && !atStrictBottom && (
            <button type="button" className="msg-list-float-btn msg-list-latest-btn" onClick={jumpToLatest}>
              {unseenCount > 0 ? `${unseenCount} new · ` : ''}Back to latest
            </button>
          )}
        </div>
      )}

      {lightboxUrl && (
        <div className="lightbox-overlay" onClick={() => setLightboxUrl(null)}>
          <img className="lightbox-img" src={lightboxUrl} alt="Preview" />
        </div>
      )}
    </div>
  )
})
