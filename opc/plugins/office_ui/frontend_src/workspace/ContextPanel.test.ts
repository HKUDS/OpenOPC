import assert from 'node:assert/strict'
import { createElement, type ComponentProps } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import type { Session } from '../types/kanban'
import {
  ContextPanel,
  composerExecModeForSession,
  conversationHasOlderHistory,
  latestSessionPreviewMessage,
  sessionPreviewText,
  sessionHasMoreForDetail,
} from './ContextPanel'

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    projectId: 'default',
    taskId: 'task-1',
    channelId: 'session:task-1',
    title: 'Task',
    status: 'running',
    columnId: 'in-progress',
    assigneeIds: [],
    priority: null,
    tags: [],
    progressLog: [],
    createdAt: 1,
    updatedAt: 2,
    messageCount: 1,
    mode: 'primary',
    ...overrides,
  }
}

assert.equal(
  composerExecModeForSession(makeSession({
    execMode: 'task',
    companyProfile: 'corporate',
    isCompanyRuntime: true,
    workItemProjectionId: 'stale-company-marker',
  }), 'company'),
  'task',
)

assert.equal(
  composerExecModeForSession(makeSession({
    isCompanyRuntime: true,
    workItemProjectionId: 'legacy-company-marker',
  }), 'task'),
  'company',
)

assert.equal(
  composerExecModeForSession(makeSession({
    execMode: 'org',
    companyProfile: 'custom',
    orgId: 'quantum_harbor',
  }), 'task'),
  'org',
)

const independentlyPaged = makeSession({
  hasMore: false,
  summaryHasMore: false,
  fullHasMore: true,
  messageCount: 400,
})
assert.equal(sessionHasMoreForDetail(independentlyPaged, 'summary'), false)
assert.equal(sessionHasMoreForDetail(independentlyPaged, 'full'), true)
assert.equal(
  conversationHasOlderHistory([independentlyPaged], 200, 'summary'),
  false,
  'a full-detail ACK must not reopen summary history',
)
assert.equal(
  conversationHasOlderHistory([independentlyPaged], 200, 'full'),
  true,
  'full history must retain its own cursor state',
)
assert.equal(
  conversationHasOlderHistory([independentlyPaged], 200, 'full', false),
  true,
  'a known scoped cursor remains loadable while a company turn is running',
)

const summaryOnlyState = makeSession({
  hasMore: false,
  summaryHasMore: true,
  messageCount: 400,
})
assert.equal(
  sessionHasMoreForDetail(summaryOnlyState, 'full'),
  undefined,
  'generic hasMore is no longer authoritative after a scoped policy is known',
)
assert.equal(
  conversationHasOlderHistory([makeSession({ messageCount: 400 })], 200, 'summary', false),
  false,
  'only the racy message-count fallback is suppressed during active generation',
)

const openSessions = [
  makeSession({ taskId: 'task-1', channelId: 'session:task-1', title: 'First' }),
  makeSession({ taskId: 'task-2', channelId: 'session:task-2', title: 'Second' }),
  makeSession({ taskId: 'task-3', channelId: 'session:task-3', title: 'Third' }),
]
const multiSessionMessages = Object.fromEntries(openSessions.map((session, index) => [
  session.taskId,
  [{
    id: `message-${index}`,
    channelId: session.channelId,
    sender: 'assistant',
    senderName: 'OPC',
    content: `Preview ${index + 1}`,
    timestamp: index + 1,
    mentions: [],
    metadata: {},
  }],
]))

assert.equal(
  latestSessionPreviewMessage([
    { ...multiSessionMessages['task-1'][0], id: 'blank', content: '   ' },
    multiSessionMessages['task-1'][0],
  ])?.id,
  'message-0',
  'preview selection must ignore empty trailing transport rows',
)
assert.equal(sessionPreviewText('  first\n\nsecond  '), 'first second')
assert.equal(sessionPreviewText('abcdefgh', 5), 'abcd…', 'preview DOM content must remain bounded')

const panelProps: ComponentProps<typeof ContextPanel> = {
  panelState: 'open',
  width: 600,
  onResizeMouseDown: () => {},
  isResizing: false,
  activeView: { kind: 'session', taskId: 'task-1' },
  activeSession: openSessions[0],
  activeTask: null,
  linkedTaskSession: null,
  linkedTaskSessionMessages: [],
  childDetailSession: null,
  messages: [],
  childDetailMessages: [],
  allSessions: openSessions,
  openSessions,
  multiViewSessions: openSessions,
  multiSessionMessages,
  agents: [],
  childSessions: [],
  taskPreferredAgent: 'native',
  nativeApprovalDefault: 'auto',
  channelId: 'session:task-1',
  channelName: 'First',
  secretaryChannelId: 'secretary:project-a',
  multiSessionView: false,
  panelTab: 'chat',
  onPanelTabChange: () => {},
  onTitleChange: () => {},
  onCollapse: () => {},
  onExpand: () => {},
  onMaximize: () => {},
  onComposerSend: () => {},
  onInteractionReply: async () => ({
    ok: true,
    accepted: true,
    checkpoint_id: 'checkpoint',
    checkpoint_type: 'test',
    client_request_id: 'request',
  }),
  onWorkItemClick: () => {},
  onMarkRead: () => {},
}

const count = (markup: string, pattern: RegExp): number => [...markup.matchAll(pattern)].length
const singleViewMarkup = renderToStaticMarkup(createElement(ContextPanel, panelProps))
assert.equal(count(singleViewMarkup, /class="msg-list-shell/g), 1, 'single view must mount exactly one transcript')
assert.equal(count(singleViewMarkup, /class="msg-composer/g), 1, 'single view must mount exactly one composer')
assert.match(
  singleViewMarkup,
  /data-session-render-key="default:task-1:session:task-1"/,
  'the complete chat subtree must expose one canonical project/session/channel identity',
)

const multiViewMarkup = renderToStaticMarkup(createElement(ContextPanel, { ...panelProps, multiSessionView: true }))
assert.equal(count(multiViewMarkup, /class="msg-list-shell/g), 0, 'multi view must not mount full transcript scroll roots')
assert.equal(count(multiViewMarkup, /class="msg-composer/g), 0, 'multi view must not mount background composers')
assert.equal(
  count(multiViewMarkup, /data-session-preview-id=/g),
  openSessions.length,
  'multi view must render one lightweight preview per selected session',
)

console.log('ContextPanel lifecycle and composer identity checks passed')
