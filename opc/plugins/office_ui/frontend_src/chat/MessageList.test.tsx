import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { buildNarrativeMessageItems, copyTextToClipboard, messageTimelineKey, parseProjectUpdatePayload } from './MessageList'
import type { MessageScrollPolicy } from './MessageList'
import type { ChatMessage } from '../types/chat'
import { progressEntryKey } from '../lib/progressEntryKey'

const messageListSource = readFileSync(new URL('./MessageList.tsx', import.meta.url), 'utf8')
const workspacePageSource = readFileSync(new URL('../workspace/WorkspacePage.tsx', import.meta.url), 'utf8')
const supportedScrollPolicies: MessageScrollPolicy[] = ['follow', 'initial-bottom', 'manual']
assert.deepEqual(supportedScrollPolicies, ['follow', 'initial-bottom', 'manual'])
assert.match(
  messageListSource,
  /export type MessageScrollPolicy = 'follow' \| 'initial-bottom' \| 'manual'/,
  'MessageList must expose one unambiguous three-state scroll policy',
)
assert.doesNotMatch(
  workspacePageSource,
  /latestPendingReplyMetadata|checkpointReplyMetadataForComposer/,
  'ordinary composer sends must never inherit an owner checkpoint decision',
)
assert.match(
  workspacePageSource,
  /checkpointRequesterSessionId[\s\S]*requesterSessionId/,
  'taskless interaction cards must submit their durable UI-anchor session actor',
)
assert.match(messageListSource, /scrollPolicy = 'follow'/, 'main transcript behavior should default to follow mode')
assert.doesNotMatch(messageListSource, /useVirtualizer/, 'chat transcript must use stable normal DOM rows')
assert.doesNotMatch(messageListSource, /PROGRAMMATIC_SCROLL_GRACE_MS/, 'scroll behavior must not regress to timer-based intent guessing')
assert.doesNotMatch(
  messageListSource,
  /seenNarrativeMessages|seenProjectUpdates/,
  'MessageList must not independently remove durable rows using rendered content',
)
assert.match(
  messageListSource,
  /\['tool_permission', 'action_permission'\]\.includes\(checkpointType\)/,
  'tool and external-action permissions must share the typed approval panel',
)
assert.match(
  messageListSource,
  /terminalAssistantTurnId\(message\)/,
  'draft suppression must use the shared committed-turn resolver',
)

const progressWithoutServerId = {
  type: 'status_change' as const,
  summary: 'Waiting for reviewer',
  detail: 'Gate entered',
  timestamp: 1234,
}
assert.equal(
  progressEntryKey(progressWithoutServerId),
  progressEntryKey({ ...progressWithoutServerId }),
  'progress identity must derive from stable event fields rather than its array position',
)
assert.doesNotMatch(
  progressEntryKey(progressWithoutServerId),
  /:0$/,
  'progress fallback identity must not carry a shifting array index',
)

const parsedUpdate = parseProjectUpdatePayload(JSON.stringify({
  summary: 'Completed the final Chinese memo with source checks.',
  deliverables: [
    { name: 'source_credibility.md', path: '/workspace/source_credibility.md', status: 'complete' },
  ],
  acceptance_status: [
    { criterion: 'Chinese memo', met: true },
    { criterion: 'Citations', met: true },
  ],
  risks: ['Refresh market data after close.'],
  next_actions: ['Use the memo in CEO aggregation.'],
}))
assert.equal(parsedUpdate?.kind, 'report')
assert.equal(parsedUpdate?.deliverables[0]?.name, 'source_credibility.md')
assert.equal(parsedUpdate?.acceptanceSummary, '2/2 acceptance checks met')
assert.deepEqual(parsedUpdate?.risks, ['Refresh market data after close.'])

const prefixedPayload = JSON.stringify({
  summary: 'Focused QA recheck completed.',
  deliverables: [
    { name: 'qa_recheck.md', path: '/workspace/qa_recheck.md', status: 'complete' },
  ],
})
const parsedPrefixedUpdate = parseProjectUpdatePayload(`**Report #1: Recheck remediated screen**: ${prefixedPayload}`)
assert.equal(parsedPrefixedUpdate?.kind, 'report')
assert.equal(parsedPrefixedUpdate?.title, 'Report #1: Recheck remediated screen')
assert.equal(parsedPrefixedUpdate?.summary, 'Focused QA recheck completed.')

const baseMessage = (id: string, content: string, timestamp: number, sender = 'system'): ChatMessage => ({
  id,
  channelId: 'session:root',
  sender,
  senderName: sender === 'user' ? 'You' : 'OPC',
  content,
  timestamp,
  mentions: [],
  metadata: {},
})

assert.equal(
  messageTimelineKey({
    ...baseMessage('checkpoint-message', 'Approval needed', 10),
    metadata: {
      checkpoint_id: 'checkpoint-42',
      canonical_turn_id: 'turn-ignored',
      ui_message_id: 'ui-ignored',
    },
  }),
  'checkpoint:checkpoint-42',
  'checkpoint identity must win so pending/resolved updates reuse one row',
)
assert.equal(
  messageTimelineKey({
    ...baseMessage('assistant-final', 'Final answer', 20, 'assistant'),
    metadata: { canonical_turn_id: 'turn-7', transcript_kind: 'runtime_v2_assistant' },
  }),
  'turn:assistant:turn-7',
  'assistant draft and final surfaces must share the canonical turn key',
)
assert.equal(
  messageTimelineKey({
    ...baseMessage('user-message', 'Question', 30, 'user'),
    metadata: { canonical_turn_id: 'turn-8', ui_message_id: 'ui-8' },
  }),
  'ui:ui-8',
  'a persisted user turn must retain the optimistic ui_message_id key',
)
assert.equal(
  messageTimelineKey({
    ...baseMessage('optimistic-message', 'Local echo', 40, 'user'),
    metadata: { ui_message_id: 'ui-9' },
  }),
  'ui:ui-9',
  'optimistic and persisted user echoes must share ui_message_id identity',
)
assert.equal(
  messageTimelineKey(baseMessage('persistent-message', 'Stored message', 50, 'assistant')),
  'message:persistent-message',
  'messages without stronger runtime identity must fall back to the persistent id',
)
assert.equal(
  messageTimelineKey({
    ...baseMessage('higher-priority-result', 'Final answer', 55, 'assistant'),
    metadata: {
      canonical_turn_id: 'turn-7',
      transcript_kind: 'child_task_result',
      ui_timeline_id: 'turn:assistant:turn-7',
    },
  }),
  'turn:assistant:turn-7',
  'a semantic result replacement must keep the mounted native-final/draft slot',
)

const sharedCompanyTurn = 'company-turn-1'
const companyTurnKeys = [
  messageTimelineKey({
    ...baseMessage('runtime-context', 'Execution context', 60),
    metadata: { kind: 'runtime_v2_user_turn', canonical_turn_id: sharedCompanyTurn },
  }),
  messageTimelineKey({
    ...baseMessage('company-stream-1', 'First company surface', 61, 'assistant'),
    metadata: { kind: 'runtime_v2_company_assistant', canonical_turn_id: sharedCompanyTurn },
  }),
  messageTimelineKey({
    ...baseMessage('company-stream-2', 'Second company surface', 62, 'assistant'),
    metadata: { kind: 'runtime_v2_company_assistant', canonical_turn_id: sharedCompanyTurn },
  }),
  messageTimelineKey({
    ...baseMessage('role-result', 'Role result', 63, 'assistant'),
    metadata: { kind: 'company_role_result', canonical_turn_id: sharedCompanyTurn },
  }),
]
assert.equal(
  companyTurnKeys[1],
  companyTurnKeys[2],
  'company draft/final surfaces for one canonical turn must reuse the same DOM slot',
)
assert.notEqual(
  companyTurnKeys[2],
  companyTurnKeys[3],
  'a separately committed role result keeps its result-delivery identity',
)

const narrativeItems = buildNarrativeMessageItems([
  baseMessage('m1', '[Company:cto::execute::abc] starting Research source reliability', 1000),
  baseMessage('m2', '[Delegating to codex] task=Research source reliability | cmd=codex exec ...', 1100),
  baseMessage('m2b', 'Status digest: Research source reliability', 1150, 'cto'),
  baseMessage('m3', 'The user-visible result is ready.', 1200, 'cto'),
  baseMessage('m4', '[External status] codex started pid=123', 1300),
], { isCompanyRuntime: true, detailMode: 'summary' })

assert.equal(narrativeItems.length, 3)
assert.equal(narrativeItems[0].kind, 'ops-bundle')
assert.equal(narrativeItems[0].kind === 'ops-bundle' ? narrativeItems[0].events.length : 0, 3)
assert.equal(narrativeItems[1].kind, 'message')
assert.equal(narrativeItems[2].kind, 'ops-bundle')

const longResult = 'Completed the focused recheck and produced the QA artifact with caveats for downstream aggregation.'
const dedupedProjectUpdates = buildNarrativeMessageItems([
  baseMessage('u1', prefixedPayload, 2000, 'qa_analyst'),
  baseMessage('u2', `**Report #1: Recheck remediated screen**: ${prefixedPayload}`, 2000, 'qa_analyst'),
], { isCompanyRuntime: true, detailMode: 'summary' })
assert.equal(
  dedupedProjectUpdates.length,
  2,
  'renderer must preserve distinct project-update rows; upstream identity owns consolidation',
)
assert.equal(dedupedProjectUpdates[0].kind, 'message')
assert.equal(dedupedProjectUpdates[0].kind === 'message' ? dedupedProjectUpdates[0].msg.id : '', 'u1')

const identicalDurableNarratives = buildNarrativeMessageItems([
  baseMessage('same-content-1', longResult, 2500, 'qa_analyst'),
  baseMessage('same-content-2', longResult, 2500, 'qa_analyst'),
], { isCompanyRuntime: true, detailMode: 'summary' })
assert.deepEqual(
  identicalDurableNarratives.map(item => item.kind === 'message' ? item.msg.id : item.id),
  ['same-content-1', 'same-content-2'],
  'distinct stable identities must survive even when sender, timestamp, and content are identical',
)

const ambiguousNarrativeMessages = buildNarrativeMessageItems([
  baseMessage('n1', longResult, 3000, 'qa_analyst'),
  baseMessage('n2', `Recheck remediated ten-bagger candidate screen: ${longResult}`, 3000, 'qa_analyst'),
], { isCompanyRuntime: true, detailMode: 'summary' })
assert.equal(
  ambiguousNarrativeMessages.length,
  2,
  'plain narrative prefixes are content and must not be stripped to guess message identity',
)

const duplicatedResultSurface = buildNarrativeMessageItems([
  {
    ...baseMessage('r1', longResult, 4000, 'chao'),
    metadata: {
      source: 'engine',
      transcript_kind: 'child_task_result',
      result_delivery_id: 'delivery-r1',
    },
  },
  {
    ...baseMessage('r2', `Deliver final result to user: ${longResult}`, 4500, 'system'),
    senderName: 'Company Member',
    metadata: {
      source: 'engine',
      transcript_kind: 'child_result',
      result_delivery_id: 'delivery-r1',
    },
  },
], { isCompanyRuntime: true, detailMode: 'summary' })
assert.equal(
  duplicatedResultSurface.length,
  2,
  'MessageList must not run a second result consolidator after the store/company projection',
)

const fullItems = buildNarrativeMessageItems([
  baseMessage('m1', '[Company:cto::execute::abc] starting Research source reliability', 1000),
], { isCompanyRuntime: true, detailMode: 'full' })
assert.equal(fullItems[0].kind, 'message')

const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
const originalDocument = Object.getOwnPropertyDescriptor(globalThis, 'document')
Object.defineProperty(globalThis, 'navigator', {
  configurable: true,
  value: {
    clipboard: {
      writeText: async () => {
        throw new Error('clipboard denied')
      },
    },
  },
})

let selectedValue = ''
let appendedNode: any = null
Object.defineProperty(globalThis, 'document', {
  configurable: true,
  value: {
    body: {
      appendChild: (node: any) => {
        appendedNode = node
      },
      removeChild: (node: any) => {
        assert.equal(node, appendedNode)
        appendedNode = null
      },
    },
    createElement: () => ({
      value: '',
      style: {},
      setAttribute: () => {},
      focus: () => {},
      select: function () {
        selectedValue = this.value
      },
      setSelectionRange: () => {},
    }),
    execCommand: (command: string) => command === 'copy',
  },
})
assert.equal(await copyTextToClipboard('fallback copy text'), true)
assert.equal(selectedValue, 'fallback copy text')
assert.equal(appendedNode, null)

if (originalNavigator) {
  Object.defineProperty(globalThis, 'navigator', originalNavigator)
} else {
  delete (globalThis as any).navigator
}
if (originalDocument) {
  Object.defineProperty(globalThis, 'document', originalDocument)
} else {
  delete (globalThis as any).document
}

console.log('MessageList.test.tsx: OK (scroll contract + stable timeline identity + narrative helpers)')
