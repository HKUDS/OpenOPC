import assert from 'node:assert/strict'

import { VisualSocketClient } from './wsClient'

type TestSocketClient = {
  handleMessage: (raw: unknown) => void
  pendingQueue: string[]
  pendingSessionDetailRequests: Array<{
    queued: boolean
    settled: boolean
    timeout: ReturnType<typeof setTimeout> | null
  }>
  timeoutSessionDetailRequest: (index: number) => void
}

const deliverAck = (
  client: VisualSocketClient,
  payload: Record<string, unknown>,
) => {
  ;(client as unknown as TestSocketClient).handleMessage(JSON.stringify({
    type: 'ack',
    payload,
  }))
}

const flushPromises = async () => {
  await Promise.resolve()
  await Promise.resolve()
}

const client = new VisualSocketClient('ws://unit.test', {})

// Company Continue keeps the selected UI channel task separate from the
// durable runtime identity used by the checkpoint handoff.
client.sessionResume('project-a', 'ui-task', 'runtime-session', 'checkpoint-1')
const resumeEnvelope = JSON.parse(
  (client as unknown as TestSocketClient).pendingQueue.pop() ?? '{}',
) as Record<string, unknown>
assert.equal(resumeEnvelope.type, 'session_resume')
assert.equal(resumeEnvelope.task_id, 'ui-task')
assert.equal(resumeEnvelope.runtime_session_id, 'runtime-session')
assert.equal(resumeEnvelope.checkpoint_id, 'checkpoint-1')

// A summary and a full request for the same task are distinct correlations.
// Neither Promise may settle merely because the request was queued locally.
const summaryPromise = client.sessionDetail('project-a', 'task-1', { detailLevel: 'summary' })
const fullPromise = client.sessionDetail('project-a', 'task-1', { detailLevel: 'full' })
const summarySettlements: Array<Record<string, unknown>> = []
const fullSettlements: Array<Record<string, unknown>> = []
void summaryPromise.then(payload => { summarySettlements.push(payload) })
void fullPromise.then(payload => { fullSettlements.push(payload) })

await flushPromises()
assert.equal(summarySettlements.length, 0, 'summary Promise must remain pending before its ACK')
assert.equal(fullSettlements.length, 0, 'full Promise must remain pending before its ACK')

deliverAck(client, {
  ok: true,
  action: 'session_detail',
  project_id: 'project-a',
  task_id: 'task-1',
  detail_level: 'full',
  marker: 'full-ack',
})
await flushPromises()
assert.equal(fullSettlements[0]?.marker, 'full-ack', 'the matching full ACK must settle the full request')
assert.equal(summarySettlements.length, 0, 'a full ACK must not settle the same task\'s summary request')

deliverAck(client, {
  ok: true,
  action: 'session_detail',
  project_id: 'project-a',
  task_id: 'task-1',
  detail_level: 'summary',
  marker: 'summary-ack',
})
await flushPromises()
assert.equal(summarySettlements[0]?.marker, 'summary-ack', 'the matching summary ACK must settle the summary request')
assert.equal(summarySettlements[0]?.client_history_page, false, 'a request without a cursor is not a history page')

const historyPagePromise = client.sessionDetail('project-a', 'task-history', {
  detailLevel: 'summary',
  beforeCreatedAt: 123,
})
deliverAck(client, {
  ok: true,
  action: 'session_detail',
  project_id: 'project-a',
  task_id: 'task-history',
  detail_level: 'summary',
})
assert.equal((await historyPagePromise).client_history_page, true, 'a cursor request must be identified as a history page')

// Requests with the same correlation fields are settled in request order.
const firstPromise = client.sessionDetail('project-a', 'task-2', { detailLevel: 'summary' })
const secondPromise = client.sessionDetail('project-a', 'task-2', { detailLevel: 'summary' })
const firstSettlements: Array<Record<string, unknown>> = []
const secondSettlements: Array<Record<string, unknown>> = []
void firstPromise.then(payload => { firstSettlements.push(payload) })
void secondPromise.then(payload => { secondSettlements.push(payload) })

deliverAck(client, {
  ok: true,
  action: 'session_detail',
  project_id: 'project-a',
  task_id: 'task-2',
  detail_level: 'summary',
  marker: 'first-ack',
})
await flushPromises()
assert.equal(firstSettlements[0]?.marker, 'first-ack', 'the first matching ACK must settle the oldest request')
assert.equal(secondSettlements.length, 0, 'the second same-scope request must remain pending after one ACK')

deliverAck(client, {
  ok: true,
  action: 'session_detail',
  project_id: 'project-a',
  task_id: 'task-2',
  detail_level: 'summary',
  marker: 'second-ack',
})
await flushPromises()
assert.equal(secondSettlements[0]?.marker, 'second-ack', 'the next matching ACK must settle the next FIFO request')

// The backend's early store-not-ready path cannot echo request fields. The
// client must still correlate and normalize that error instead of leaving the
// history single-flight Promise pending forever.
const storeNotReadyPromise = client.sessionDetail('project-a', 'task-store', {
  detailLevel: 'summary',
  viewGeneration: 9,
})
const storeNotReadySettlements: Array<Record<string, unknown>> = []
void storeNotReadyPromise.then(payload => { storeNotReadySettlements.push(payload) })
deliverAck(client, {
  ok: false,
  action: 'create_session',
  error: 'store_not_ready',
  project_id: 'project-a',
  view_generation: 9,
})
await flushPromises()
assert.equal(
  storeNotReadySettlements.length,
  0,
  'store_not_ready for another explicit action must not settle a session_detail request',
)
deliverAck(client, {
  ok: false,
  error: 'store_not_ready',
  project_id: 'project-a',
  view_generation: 9,
})
const storeNotReady = await storeNotReadyPromise
assert.equal(storeNotReady.action, 'session_detail')
assert.equal(storeNotReady.task_id, 'task-store')
assert.equal(storeNotReady.detail_level, 'summary')

// A sent request timeout releases its caller, but leaves a settled FIFO
// tombstone so a late ACK cannot be mis-correlated to a newer request.
const timeoutClient = new VisualSocketClient('ws://unit.test', {})
const sentPromise = timeoutClient.sessionDetail('project-a', 'task-timeout', {
  detailLevel: 'summary',
})
const sentSettlements: Array<Record<string, unknown>> = []
void sentPromise.then(payload => { sentSettlements.push(payload) })
const timeoutInternals = timeoutClient as unknown as TestSocketClient
timeoutInternals.pendingSessionDetailRequests[0].queued = false
timeoutInternals.pendingQueue = []
timeoutInternals.timeoutSessionDetailRequest(0)
await flushPromises()
assert.equal(sentSettlements[0]?.error, 'request_timeout', 'sent timeout must release the loading caller')
assert.equal(timeoutInternals.pendingSessionDetailRequests.length, 1, 'sent timeout must retain a FIFO tombstone')
assert.equal(timeoutInternals.pendingSessionDetailRequests[0].settled, true, 'the retained request must be a settled tombstone')
assert.equal(timeoutInternals.pendingSessionDetailRequests[0].timeout, null, 'sent request timer must be released')

const afterTimeoutPromise = timeoutClient.sessionDetail('project-a', 'task-timeout', {
  detailLevel: 'summary',
})
const afterTimeoutSettlements: Array<Record<string, unknown>> = []
void afterTimeoutPromise.then(payload => { afterTimeoutSettlements.push(payload) })
deliverAck(timeoutClient, {
  ok: true,
  action: 'session_detail',
  project_id: 'project-a',
  task_id: 'task-timeout',
  detail_level: 'summary',
  marker: 'old-request-ack',
})
await flushPromises()
assert.equal(sentSettlements.length, 1, 'a late ACK must be consumed by the older tombstone')
assert.equal(sentSettlements[0]?.error, 'request_timeout', 'a late ACK cannot resettle the timed-out Promise')
assert.equal(afterTimeoutSettlements.length, 0, 'the first ACK must not settle the newer same-scope request')
assert.equal(timeoutInternals.pendingSessionDetailRequests.length, 1, 'the newer request must remain pending')

deliverAck(timeoutClient, {
  ok: true,
  action: 'session_detail',
  project_id: 'project-a',
  task_id: 'task-timeout',
  detail_level: 'summary',
  marker: 'current-request-ack',
})
await flushPromises()
assert.equal(afterTimeoutSettlements[0]?.marker, 'current-request-ack', 'the next ACK must settle the newer request')

const queuedTimeoutClient = new VisualSocketClient('ws://unit.test', {})
const queuedTimeout = queuedTimeoutClient.sessionDetail('project-a', 'task-queued-timeout', {
  beforeMessageId: 'older-message',
})
const queuedTimeoutInternals = queuedTimeoutClient as unknown as TestSocketClient
queuedTimeoutInternals.timeoutSessionDetailRequest(0)
const queuedTimeoutFailure = await queuedTimeout
assert.equal(queuedTimeoutFailure.error, 'request_timeout', 'a request still queued locally may time out')
assert.equal(queuedTimeoutFailure.client_history_page, true, 'synthetic failures must preserve history-page correlation')
assert.equal(queuedTimeoutInternals.pendingSessionDetailRequests.length, 0)
assert.equal(queuedTimeoutInternals.pendingQueue.length, 0)

const disconnectClient = new VisualSocketClient('ws://unit.test', {})
const disconnected = disconnectClient.sessionDetail('project-a', 'task-disconnect')
disconnectClient.disconnect()
assert.equal((await disconnected).error, 'disconnected', 'disconnect must settle every pending detail request')

const saturatedClient = new VisualSocketClient('ws://unit.test', {})
;(saturatedClient as unknown as TestSocketClient).pendingQueue = Array.from({ length: 100 }, () => '{}')
const saturated = await saturatedClient.sessionDetail('project-a', 'task-saturated')
assert.equal(saturated.error, 'send_queue_full', 'a saturated transport queue must fail immediately')

// An explicitly retired client must detach transport callbacks before close.
// Otherwise a delayed onclose from an old endpoint can overwrite the status
// and policy loaded by its replacement.
const originalWebSocket = globalThis.WebSocket
let closeCalls = 0
class ClosingSocket {
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  onclose: ((event: CloseEvent) => void) | null = null

  close(): void {
    closeCalls += 1
    assert.equal(this.onopen, null)
    assert.equal(this.onmessage, null)
    assert.equal(this.onerror, null)
    assert.equal(this.onclose, null)
  }
}

try {
  globalThis.WebSocket = ClosingSocket as unknown as typeof WebSocket
  const endpointStatuses: string[] = []
  const endpointClient = new VisualSocketClient('ws://retired.test', {
    onStatus: status => { endpointStatuses.push(status) },
  })
  endpointClient.connect()
  endpointClient.disconnect()
  assert.deepEqual(endpointStatuses, ['connecting'])
  assert.equal(closeCalls, 1)
} finally {
  globalThis.WebSocket = originalWebSocket
}

console.log('wsClient.test.ts: OK (session_detail correlation and lifecycle cleanup)')
