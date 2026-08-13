import React, { useCallback, useMemo, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'

import '../index.css'
import { MessageList } from '../chat/MessageList'
import { WorkItemProgressCard } from '../chat/WorkItemProgressCard'
import { __chatStoreTestUtils } from '../chat/ChatStore'
import { mergeConversationMessages } from '../lib/workItemSessions'
import type { ChatMessage } from '../types/chat'
import type { ProgressEntry, RoleWorkItemSummary } from '../types/kanban'

type ScrollPolicy = 'follow' | 'initial-bottom' | 'manual'

interface FixtureTelemetry {
  markReadCalls: number
  renders: number
  scrollEvents: number
  scrollTopWrites: number
}

interface MessageListFixtureApi {
  appendMessages(count: number): void
  appendProgressEntries(count: number): void
  addSharedTurnCompanyRows(indices?: number[]): void
  finalizeDraft(): void
  growDraft(characters: number): void
  growMessage(index: number, characters: number): void
  growTailLayout(pixels: number): void
  mergeResultGroupOrder(index: number, parentFirst: boolean): void
  repeatFullSync(): void
  resolveCheckpoint(): void
  resetTelemetry(): void
  setExternalRoleCount(count: number): void
  telemetry(): FixtureTelemetry
  upgradeResultSurface(index: number): void
}

declare global {
  interface Window {
    __messageListFixture?: MessageListFixtureApi
    __messageListFixtureReady?: boolean
    __rememberedCheckpointRow?: Element | null
    __rememberedDraftTimelineRow?: Element | null
  }
}

const CHANNEL_ID = 'session:scroll-regression'
const BASE_TIMESTAMP = Date.UTC(2026, 6, 13, 8, 0, 0)
const INITIAL_MESSAGE_COUNT = 225
const CHECKPOINT_INDEX = 110
const CHECKPOINT_ID = 'fixture-checkpoint-110'
const CHECKPOINT_INDICES = new Set([0, 50, 70, 90, CHECKPOINT_INDEX, 130, 150, 170, 190])
const SHARED_COMPANY_TURN_ID = 'fixture-shared-company-turn'
const LIVE_TURN_ID = 'fixture-live-turn'
const INITIAL_PROGRESS_COUNT = 100

// Count direct JS writes, including no-op writes which do not dispatch a
// scroll event. This catches the historical idle scrollToEnd feedback loop.
const scrollTopProbe = { writes: 0 }
const scrollTopDescriptor = Object.getOwnPropertyDescriptor(Element.prototype, 'scrollTop')
if (scrollTopDescriptor?.get && scrollTopDescriptor.set && scrollTopDescriptor.configurable) {
  Object.defineProperty(Element.prototype, 'scrollTop', {
    ...scrollTopDescriptor,
    set(value: number) {
      if ((this as Element).classList?.contains('msg-list')) scrollTopProbe.writes += 1
      scrollTopDescriptor.set!.call(this, value)
    },
  })
}

function messageContent(index: number): string {
  const detail = index % 37 === 0
    ? `\n\n${'Long-form company result with citations, caveats, and acceptance evidence. '.repeat(85)}`
    : index % 9 === 0
    ? '\n\nThis deliberately longer paragraph exercises dynamic Markdown height without relying on a simplified test-only row. '.repeat(3)
    : '\n\nA stable production transcript row.'
  return `fixture-marker-${String(index).padStart(4, '0')} ${detail}`
}

function buildMessage(index: number): ChatMessage {
  if (CHECKPOINT_INDICES.has(index)) {
    const checkpointId = `fixture-checkpoint-${index}`
    return {
      id: `fixture-message-${index}`,
      channelId: CHANNEL_ID,
      sender: 'system',
      senderName: 'OPC',
      content: 'Checkpoint fixture payload',
      timestamp: BASE_TIMESTAMP + index * 1_000,
      mentions: [],
      metadata: {
        checkpoint_type: 'human_escalation',
        checkpoint_id: checkpointId,
        escalation_id: checkpointId,
        escalation_type: 'decision_needed',
        prompt: `Approval checkpoint ${index}\nKeep this card at its chronological position.`,
        summary: 'Chronological checkpoint regression fixture',
        options: [
          { id: 'approve', label: 'Approve' },
          { id: 'deny', label: 'Deny' },
        ],
      },
    }
  }

  const isUser = index % 5 === 0
  return {
    id: `fixture-message-${index}`,
    channelId: CHANNEL_ID,
    sender: isUser ? 'user' : `fixture-agent-${index % 4}`,
    senderName: isUser ? 'You' : `Fixture Agent ${index % 4}`,
    content: messageContent(index),
    timestamp: BASE_TIMESTAMP + index * 1_000,
    mentions: [],
    metadata: isUser
      ? { ui_message_id: `fixture-ui-${index}` }
      : {
        source: 'engine',
        canonical_turn_id: `fixture-turn-${index}`,
        transcript_kind: 'runtime_v2_assistant',
      },
  }
}

function buildInitialMessages(): ChatMessage[] {
  return Array.from({ length: INITIAL_MESSAGE_COUNT }, (_, index) => buildMessage(index))
}

function buildHistoryMessage(batch: number, offset: number): ChatMessage {
  return {
    id: `history-message-${batch}-${offset}`,
    channelId: CHANNEL_ID,
    sender: `history-agent-${offset % 3}`,
    senderName: `History Agent ${offset % 3}`,
    content: `history-marker-${String(batch).padStart(2, '0')}-${String(offset).padStart(3, '0')}\n\nA prepended historical row.`,
    timestamp: BASE_TIMESTAMP - ((batch + 1) * 100_000) + offset * 1_000,
    mentions: [],
    metadata: { canonical_turn_id: `history-turn-${batch}-${offset}` },
  }
}

function buildSharedTurnCompanyMessage(index: number, transcriptKind: string): ChatMessage {
  return {
    id: `shared-company-message-${index}`,
    channelId: CHANNEL_ID,
    sender: `company-role-${index}`,
    senderName: `Company Role ${index}`,
    content: `shared-company-row-${index} — independent committed company surface`,
    timestamp: BASE_TIMESTAMP + 2_000_000 + index,
    mentions: [],
    metadata: {
      canonical_turn_id: SHARED_COMPANY_TURN_ID,
      transcript_kind: transcriptKind,
    },
  }
}

function buildProgressEntry(index: number): ProgressEntry {
  return {
    type: 'status_change',
    summary: `progress-marker-${String(index).padStart(4, '0')}`,
    detail: `Stable no-id status event ${index}`,
    timestamp: BASE_TIMESTAMP + 4_000_000 + index * 1_000,
  }
}

function buildRoleWorkItems(count: number): Record<string, RoleWorkItemSummary> {
  return Object.fromEntries(Array.from({ length: count }, (_, index) => {
    const roleId = `fixture-role-${index}`
    return [roleId, {
      roleKey: roleId,
      roleId,
      roleName: `Fixture Role ${index}`,
      runtimeStatus: index % 3 === 0 ? 'reflecting' : 'idle',
      aggregatedStatus: index % 3 === 0 ? 'active' : 'pending',
      workItems: [{
        workItemId: `fixture-work-item-${index}`,
        workItemProjectionId: `fixture-projection-${index}`,
        phase: index % 3 === 0 ? 'running' : 'queued',
        kanbanColumn: index % 3 === 0 ? 'in_progress' : 'todo',
        title: `Fixture Work Item ${index}`,
        executorRoleId: roleId,
        executorRoleName: `Fixture Role ${index}`,
        createdAt: BASE_TIMESTAMP + index,
        updatedAt: BASE_TIMESTAMP + index,
        executionTurnId: `fixture-role-turn-${index}`,
        progressLog: [],
      }],
    } satisfies RoleWorkItemSummary]
  }))
}

function Fixture() {
  const query = useMemo(() => new URLSearchParams(window.location.search), [])
  const policy = (query.get('policy') || 'follow') as ScrollPolicy
  const progressFixture = query.get('progress') === '1'
  const externalProgressFixture = query.get('externalProgress') === '1'
  const emptySummaryFixture = query.get('empty') === '1'
  const [messages, setMessages] = useState<ChatMessage[]>(() => {
    const initial = buildInitialMessages()
    if (!emptySummaryFixture) return initial
    return initial.map(message => ({
      ...message,
      metadata: { ...(message.metadata ?? {}), detail_visibility: 'full' },
    }))
  })
  const [progressLog, setProgressLog] = useState<ProgressEntry[]>(() => (
    progressFixture
      ? Array.from({ length: INITIAL_PROGRESS_COUNT }, (_, index) => buildProgressEntry(index))
      : []
  ))
  const [externalRoleWorkItems, setExternalRoleWorkItems] = useState<Record<string, RoleWorkItemSummary>>({})
  const [draftText, setDraftText] = useState('')
  const draftTextRef = useRef(draftText)
  draftTextRef.current = draftText
  const historyBatchRef = useRef(0)
  // This state deliberately changes from markRead. The former implementation
  // accidentally made that state update part of a scroll-to-bottom feedback
  // loop; a correct MessageList must not care that this callback is recreated.
  const [, setReadVersion] = useState(0)
  const telemetryRef = useRef<FixtureTelemetry>({ markReadCalls: 0, renders: 0, scrollEvents: 0, scrollTopWrites: 0 })
  telemetryRef.current.renders += 1

  const appendMessages = useCallback((count: number) => {
    setMessages((current) => {
      const start = current.reduce((max, message) => {
        const match = /^fixture-message-(\d+)$/.exec(message.id)
        return match ? Math.max(max, Number(match[1]) + 1) : max
      }, INITIAL_MESSAGE_COUNT)
      return [
        ...current,
        ...Array.from({ length: count }, (_, offset) => buildMessage(start + offset)),
      ]
    })
  }, [])

  const appendProgressEntries = useCallback((count: number) => {
    setProgressLog((current) => {
      const start = current.reduce((max, entry) => {
        const match = /^progress-marker-(\d+)$/.exec(entry.summary)
        return match ? Math.max(max, Number(match[1]) + 1) : max
      }, INITIAL_PROGRESS_COUNT)
      return [
        ...current,
        ...Array.from({ length: count }, (_, offset) => buildProgressEntry(start + offset)),
      ].slice(-INITIAL_PROGRESS_COUNT)
    })
  }, [])

  const growDraft = useCallback((characters: number) => {
    setDraftText((current) => `${current}${' live-reply-content'.repeat(Math.max(1, Math.ceil(characters / 19)))}`)
  }, [])

  const growMessage = useCallback((index: number, characters: number) => {
    setMessages((current) => current.map((message) => message.id === `fixture-message-${index}`
      ? {
        ...message,
        content: `${message.content}\n\n${'Expanded content above the viewport anchor. '.repeat(Math.max(1, Math.ceil(characters / 44)))}`,
      }
      : message))
  }, [])

  const repeatFullSync = useCallback(() => {
    setMessages((current) => current.map((message) => ({
      ...message,
      metadata: message.metadata ? { ...message.metadata } : undefined,
    })))
  }, [])

  const growTailLayout = useCallback((pixels: number) => {
    const tail = Array.from(document.querySelectorAll<HTMLElement>('.msg-timeline-row')).at(-1)
    if (!tail) throw new Error('tail timeline row is missing')
    let spacer = tail.querySelector<HTMLElement>('[data-late-layout-spacer]')
    if (!spacer) {
      spacer = document.createElement('div')
      spacer.dataset.lateLayoutSpacer = 'true'
      tail.appendChild(spacer)
    }
    spacer.style.height = `${pixels}px`
  }, [])

  const upgradeResultSurface = useCallback((index: number) => {
    setMessages((current) => {
      const existing = current.find(message => message.id === `fixture-message-${index}`)
      if (!existing || existing.sender === 'user') return current
      return __chatStoreTestUtils.dedupeMessages([
        ...current,
        {
          ...existing,
          id: `fixture-upgraded-result-${index}`,
          sender: 'fixture-company-role',
          senderName: 'Fixture Company Role',
          metadata: {
            ...existing.metadata,
            source: 'engine',
            canonical_turn_id: `fixture-upgraded-turn-${index}`,
            transcript_kind: 'child_task_result',
            detail_visibility: 'summary',
          },
        },
      ])
    })
  }, [])

  const mergeResultGroupOrder = useCallback((index: number, parentFirst: boolean) => {
    const native = buildMessage(index)
    if (native.sender === 'user') throw new Error('result group fixture needs an assistant row')
    const authoritative: ChatMessage = {
      ...native,
      id: `fixture-cross-channel-result-${index}`,
      channelId: 'session:scroll-regression-child',
      sender: 'fixture-company-role',
      senderName: 'Fixture Company Role',
      timestamp: native.timestamp + 10,
      metadata: {
        ...native.metadata,
        canonical_turn_id: `fixture-authoritative-turn-${index}`,
        transcript_kind: 'child_task_result',
      },
    }
    const selected = mergeConversationMessages(parentFirst
      ? [[authoritative], [native]]
      : [[native], [authoritative]])
    setMessages((current) => [
      ...current.filter(message => (
        message.id !== native.id
        && message.id !== authoritative.id
      )),
      ...selected,
    ])
  }, [])

  const addSharedTurnCompanyRows = useCallback((indices = [0, 1, 2, 3, 4]) => {
    const transcriptKinds = [
      'company_role_result',
      'child_result',
      'runtime_v2_intermediate_assistant',
      'runtime_v2_assistant',
      'runtime_v2_assistant',
    ]
    setMessages((current) => {
      const existingIds = new Set(current.map(message => message.id))
      return [
        ...current,
        ...indices
          .filter(index => !existingIds.has(`shared-company-message-${index}`))
          .map(index => buildSharedTurnCompanyMessage(index, transcriptKinds[index] ?? 'runtime_v2_assistant')),
      ]
    })
  }, [])

  const finalizeDraft = useCallback(() => {
    const committedContent = draftTextRef.current.trim() || 'fixture-live-final-content — committed assistant response'
    setDraftText('')
    setMessages((current) => current.some((message) => message.id === 'fixture-live-final')
      ? current
      : [
        ...current,
        {
          id: 'fixture-live-final',
          channelId: CHANNEL_ID,
          sender: 'fixture-agent-final',
          senderName: 'Fixture Final Agent',
          content: committedContent,
          timestamp: BASE_TIMESTAMP + 3_000_000,
          mentions: [],
          metadata: {
            canonical_turn_id: LIVE_TURN_ID,
            result_delivery_id: `result:${LIVE_TURN_ID}:attempt:0`,
            transcript_kind: 'runtime_v2_company_assistant',
          },
        },
      ])
  }, [])

  const loadOlderHistory = useCallback(() => {
    const batch = historyBatchRef.current
    historyBatchRef.current += 1
    setMessages((current) => [
      ...Array.from({ length: 40 }, (_, offset) => buildHistoryMessage(batch, offset)),
      ...current,
    ])
  }, [])

  const resolveCheckpoint = useCallback(() => {
    setMessages((current) => {
      if (current.some((message) => message.id === 'fixture-checkpoint-response')) return current
      return [
        ...current,
        {
          id: 'fixture-checkpoint-response',
          channelId: CHANNEL_ID,
          sender: 'user',
          senderName: 'You',
          content: 'Approved fixture checkpoint',
          timestamp: BASE_TIMESTAMP + 1_000_000,
          mentions: [],
          metadata: {
            ui_message_id: 'fixture-checkpoint-response-ui',
            response_to_checkpoint_id: CHECKPOINT_ID,
            response_to_checkpoint_type: 'human_escalation',
            checkpoint_reply_kind: 'approve',
          },
        },
      ]
    })
  }, [])

  const resetTelemetry = useCallback(() => {
    telemetryRef.current.markReadCalls = 0
    telemetryRef.current.renders = 0
    telemetryRef.current.scrollEvents = 0
    scrollTopProbe.writes = 0
  }, [])

  window.__messageListFixture = {
    appendMessages,
    appendProgressEntries,
    addSharedTurnCompanyRows,
    finalizeDraft,
    growDraft,
    growMessage,
    growTailLayout,
    mergeResultGroupOrder,
    repeatFullSync,
    resolveCheckpoint,
    resetTelemetry,
    setExternalRoleCount: (count: number) => setExternalRoleWorkItems(buildRoleWorkItems(count)),
    telemetry: () => ({ ...telemetryRef.current, scrollTopWrites: scrollTopProbe.writes }),
    upgradeResultSurface,
  }

  const handleMarkRead = () => {
    telemetryRef.current.markReadCalls += 1
    setReadVersion((value) => value + 1)
  }

  // Use an intersection type so the fixture can be committed alongside the
  // implementation change: the old component ignores scrollPolicy and fails
  // these checks; the new public contract consumes it.
  const ProductionMessageList = MessageList as React.ComponentType<
    React.ComponentProps<typeof MessageList> & { scrollPolicy: ScrollPolicy }
  >

  React.useEffect(() => {
    window.__messageListFixtureReady = true
    return () => {
      window.__messageListFixtureReady = false
      delete window.__messageListFixture
    }
  }, [])

  React.useEffect(() => {
    const list = document.querySelector('.msg-list')
    const countScroll = () => {
      telemetryRef.current.scrollEvents += 1
    }
    list?.addEventListener('scroll', countScroll, { passive: true })
    return () => list?.removeEventListener('scroll', countScroll)
  }, [])

  return (
    <main className="app-shell message-list-scroll-fixture" data-theme="paper">
      <div className="message-list-scroll-fixture-header">
        Production MessageList fixture — {policy}
      </div>
      <section className="message-list-scroll-fixture-body">
        {externalProgressFixture && (
          <div className="ctx-work-item-progress">
            <WorkItemProgressCard
              workItemLog={[]}
              roleWorkItems={externalRoleWorkItems}
              isCompanyRuntime
            />
          </div>
        )}
        <ProductionMessageList
          messages={messages}
          channelName="Scroll regression"
          viewKind="session"
          detailMode={progressFixture ? 'full' : 'summary'}
          scrollPolicy={policy}
          draftAssistantText={draftText}
          draftUpdatedAt={BASE_TIMESTAMP + 900_000}
          draftTurnId={LIVE_TURN_ID}
          isCompanyRuntime
          onSend={() => undefined}
          onMarkRead={handleMarkRead}
          hasOlderHistory
          totalMessageCount={messages.length + 100}
          onLoadOlderHistory={loadOlderHistory}
          progressLog={progressLog}
          showWorkItemRuntimeCard={false}
          showRuntimeProgress={progressFixture}
        />
      </section>
    </main>
  )
}

const style = document.createElement('style')
style.textContent = `
  .message-list-scroll-fixture {
    display: flex;
    flex-direction: column;
    width: 620px;
    height: 720px;
    max-height: 92vh;
    margin: 4vh auto;
    border: 1px solid var(--border);
    border-radius: 12px;
  }
  .message-list-scroll-fixture-header {
    flex: 0 0 44px;
    display: flex;
    align-items: center;
    padding: 0 16px;
    border-bottom: 1px solid var(--border);
    background: var(--bg-elevated);
    color: var(--text-secondary);
    font-size: 12px;
  }
  .message-list-scroll-fixture-body {
    flex: 1 1 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
`
document.head.appendChild(style)

const root = document.getElementById('root')
if (!root) throw new Error('MessageList fixture root is missing')
createRoot(root).render(<Fixture />)
