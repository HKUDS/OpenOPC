import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { Session } from '../types/kanban'
import {
  MULTI_SESSION_PREVIEW_LIMIT,
  resolveWorkspaceBoardId,
  selectMultiViewSessions,
} from './WorkspacePage'

const here = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(here, 'WorkspacePage.tsx'), 'utf8')

assert.match(src, /makeOptimisticUserMessageId/, 'ordinary composer sends must create a stable optimistic ui_message_id')
assert.match(src, /chatStore\.sendMessage/, 'ordinary composer sends must echo the user message locally before backend response')
assert.match(src, /ui_message_id: uiMessageId/, 'optimistic local message and websocket metadata must share ui_message_id')
assert.doesNotMatch(
  src,
  /\bcheckpointReplyId\b/,
  'legacy checkpoint composer state must not be restored',
)
const interactionReplyStart = src.indexOf('const handleInteractionReply = useCallback')
const interactionReplyEnd = src.indexOf('const handleOpenWorkItemSession = useCallback', interactionReplyStart)
assert.ok(
  interactionReplyStart >= 0 && interactionReplyEnd > interactionReplyStart,
  'explicit durable interaction reply handler must be present',
)
const interactionReplySrc = src.slice(interactionReplyStart, interactionReplyEnd)
assert.match(
  interactionReplySrc,
  /metadata\?\.response_to_checkpoint_id/,
  'interaction replies must carry the durable checkpoint id',
)
assert.match(
  interactionReplySrc,
  /metadata\?\.response_to_checkpoint_type/,
  'interaction replies must carry the durable checkpoint type',
)
assert.match(
  interactionReplySrc,
  /return onInteractionReply\(\{[\s\S]*?checkpointId,[\s\S]*?checkpointType,[\s\S]*?clientRequestId,[\s\S]*?requesterTaskId: targetTaskId,[\s\S]*?requesterSessionId,[\s\S]*?decision,/,
  'checkpoint decisions must use the explicit ACK-correlated interaction transport',
)
assert.doesNotMatch(
  interactionReplySrc,
  /dispatchSessionSend|chatStore\.sendMessage/,
  'checkpoint decisions must not fall back through the ordinary composer transport',
)
assert.match(src, /const \{ markRead \} = chatStore/, 'workspace must consume the stable markRead action directly')
assert.doesNotMatch(src, /chatStore\.markRead/, 'workspace mark-read callbacks must not depend on the aggregate chatStore object')
assert.equal(
  [...src.matchAll(/\bmarkRead\(/g)].length,
  1,
  'only the single active transcript viewport may mark chat channels as read',
)
assert.match(
  src,
  /const handleMarkRead = useCallback\(\(\) => \{\s*for \(const visibleChannelId of visibleChannelIds\) \{\s*markRead\(visibleChannelId\)\s*\}\s*\}, \[visibleChannelIds, markRead\]\)/,
  'the active transcript viewport must mark every channel represented by the visible company timeline',
)
assert.match(src, /onMarkRead=\{handleMarkRead\}/, 'the active viewport must receive the markRead callback')
assert.doesNotMatch(
  src,
  /handleMarkSessionRead|onSessionMarkRead/,
  'multi-session previews must not create transcript read side effects',
)
assert.match(
  src,
  /const outgoing = metadata\?\.ui_message_id\s*\?\s*metadata\s*:\s*\{ \.\.\.\(metadata \?\? \{\}\), ui_message_id: makeOptimisticUserMessageId\(\) \}/,
  'every session send must carry a client-generated ui_message_id so the backend can deduplicate re-deliveries',
)

const requestHistoryStart = src.indexOf('const requestSessionHistory = useCallback')
const requestHistoryEnd = src.indexOf('const isSessionHistoryLoading = useCallback', requestHistoryStart)
assert.ok(requestHistoryStart >= 0 && requestHistoryEnd > requestHistoryStart, 'history request implementation must be present')
const requestHistorySrc = src.slice(requestHistoryStart, requestHistoryEnd)
assert.doesNotMatch(
  requestHistorySrc,
  /setTimeout/,
  'history single-flight completion must follow the transport Promise, not a fixed 800ms timer',
)
assert.match(
  requestHistorySrc,
  /Promise\.resolve\(request\)[\s\S]*?\.finally\(\(\) => \{[\s\S]*?historyRequestInFlightRef\.current\.delete\(requestKey\)/,
  'history single-flight state must be released only when the transport request settles',
)
assert.match(
  requestHistorySrc,
  /const generation = historyRequestGenerationRef\.current[\s\S]*?const requestKey = \[\s*generation,/,
  'history claims must be scoped to a project generation',
)
assert.match(
  requestHistorySrc,
  /historyRequestInFlightRef\.current\.delete\(requestKey\)\s*if \(historyRequestGenerationRef\.current !== generation\) return/,
  'an old project Promise must not clear loading state for a newer project generation',
)
assert.match(
  requestHistorySrc,
  /oldestMessage && targetChannelId && oldestMessage\.channelId !== targetChannelId[\s\S]*?getChannelMessagesRef\.current\(targetChannelId\)\.find\([\s\S]*?isMessageVisibleAtDetailLevel\(message, detailLevel\)/,
  'multi-channel history must use a selected target cursor visible to the requested detail policy',
)
assert.match(
  src,
  /if \(autoHistoryRequestRef\.current\.scope !== activeSessionId\) \{\s*autoHistoryRequestRef\.current\.scope = activeSessionId\s*autoHistoryRequestRef\.current\.active\.clear\(\)/,
  'switching the active transcript scope must clear prior auto-history claims',
)
assert.match(
  src,
  /const historyTargets = activeConversation\.timelineSessions\.length > 0\s*\? activeConversation\.timelineSessions/,
  'company history must enumerate the root and child timeline sessions',
)
assert.match(
  src,
  /const detailLevel = isCompanyConversation\(activeSession, childSessions\.length\)\s*\? 'summary'[\s\S]*?requestSessionHistory\(\s*session\.taskId,\s*undefined,\s*detailLevel/,
  'company root and child history requests must use summary detail independently',
)

function previewSession(taskId: string): Session {
  return {
    projectId: 'project-a',
    taskId,
    channelId: `session:${taskId}`,
    title: taskId,
    status: 'pending',
    columnId: 'todo',
    assigneeIds: [],
    priority: null,
    tags: [],
    progressLog: [],
    createdAt: 1,
    updatedAt: 1,
    messageCount: 0,
    mode: 'primary',
  }
}

const manyOpenSessions = Array.from({ length: 20 }, (_, index) => previewSession(`task-${index + 1}`))
const selectedPreviews = selectMultiViewSessions(manyOpenSessions, 'task-2')
assert.equal(selectedPreviews.length, MULTI_SESSION_PREVIEW_LIMIT, 'multi view must remain bounded as tabs accumulate')
assert.equal(selectedPreviews.at(-1)?.taskId, 'task-2', 'the active session must always remain visible in multi view')
assert.deepEqual(
  selectedPreviews.slice(0, -1).map(session => session.taskId),
  ['task-18', 'task-19', 'task-20'],
  'remaining preview slots should contain the most recently opened sessions',
)
assert.deepEqual(selectMultiViewSessions(manyOpenSessions, null, 0), [], 'a zero preview limit must mount nothing')
assert.deepEqual(
  selectMultiViewSessions(manyOpenSessions, 'task-2', 1).map(session => session.taskId),
  ['task-2'],
  'a one-card limit must contain only the active session',
)

const sessionBoardIds = ['session-1', 'session-2', 'session-3']
assert.equal(
  resolveWorkspaceBoardId({
    boardIds: sessionBoardIds,
    sessionScopedBoardIds: sessionBoardIds,
    activeSessionBoardId: 'session-2',
    activeTaskBoardId: null,
    currentActiveBoardId: 'session-1',
  }),
  'session-2',
  'selecting another chat must switch away from the previous chat board',
)
assert.equal(
  resolveWorkspaceBoardId({
    boardIds: sessionBoardIds,
    sessionScopedBoardIds: sessionBoardIds,
    activeSessionBoardId: 'session-3',
    activeTaskBoardId: null,
    currentActiveBoardId: 'session-1',
  }),
  'session-3',
  'task-mode and company-mode chats use the same canonical session-board identity',
)
assert.equal(
  resolveWorkspaceBoardId({
    boardIds: ['session-1'],
    sessionScopedBoardIds: ['session-1'],
    activeSessionBoardId: 'session-pending',
    activeTaskBoardId: null,
    currentActiveBoardId: 'session-1',
  }),
  null,
  'a session whose board has not arrived must never inherit another chat board',
)
assert.equal(
  resolveWorkspaceBoardId({
    boardIds: ['project-board'],
    sessionScopedBoardIds: [],
    activeSessionBoardId: 'task-1',
    activeTaskBoardId: null,
    currentActiveBoardId: null,
  }),
  'project-board',
  'ordinary project-wide task boards retain their existing fallback',
)
assert.equal(
  resolveWorkspaceBoardId({
    boardIds: sessionBoardIds,
    sessionScopedBoardIds: sessionBoardIds,
    activeSessionBoardId: 'session-1',
    activeTaskBoardId: 'session-2',
    currentActiveBoardId: 'session-1',
  }),
  'session-2',
  'an explicitly opened work-item detail keeps its owning board selected',
)

assert.match(
  src,
  /if \(!multiSessionView\) return \{\}/,
  'inactive tabs must not project or load messages while single view is active',
)
assert.match(
  src,
  /setOpenSessionIds\(\[\]\)[\s\S]*?setMultiSessionView\(false\)[\s\S]*?setChildDetailTaskId\(null\)/,
  'project switches must clear project-scoped navigation and view state atomically',
)
assert.doesNotMatch(
  src,
  /const isCompanyMode = execMode/,
  'existing chat and board isolation must not depend on the global new-chat default mode',
)
assert.match(
  src,
  /<AgentStatusBar agents=\{agents\} tasks=\{activeBoardTasks\}/,
  'agent status rollups must be scoped to the active chat board',
)

console.log('WorkspacePage.test.ts: OK (single transcript lifecycle, composer, mark-read, and history wiring)')
