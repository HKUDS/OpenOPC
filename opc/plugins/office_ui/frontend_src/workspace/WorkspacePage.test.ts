import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

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
  2,
  'markRead must only be invoked by the active viewport and per-session viewport callbacks',
)
assert.match(
  src,
  /const handleMarkRead = useCallback\(\(\) => \{\s*for \(const visibleChannelId of visibleChannelIds\) \{\s*markRead\(visibleChannelId\)\s*\}\s*\}, \[visibleChannelIds, markRead\]\)/,
  'the active transcript viewport must mark every channel represented by the visible company timeline',
)
assert.match(
  src,
  /const handleMarkSessionRead = useCallback\(\(taskId: string\) => \{[\s\S]*?if \(session\) markRead\(session\.channelId\)[\s\S]*?\}, \[sessions, markRead\]\)/,
  'each multi-session transcript viewport callback must own its channel markRead',
)
assert.match(src, /onMarkRead=\{handleMarkRead\}/, 'the active viewport must receive the markRead callback')
assert.match(src, /onSessionMarkRead=\{handleMarkSessionRead\}/, 'multi-session viewports must receive their scoped markRead callback')
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

console.log('WorkspacePage.test.ts: OK (composer, mark-read, and history wiring)')
