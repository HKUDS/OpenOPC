import assert from 'node:assert/strict'

import type { ChatMessage } from '../types/chat'
import {
  analyzeCheckpointMessages,
  isCheckpointCardMetadata,
  isCheckpointType,
  toCheckpointReplyMetadata,
} from './checkpointUtils'

const checkpoint: ChatMessage = {
  id: 'msg-checkpoint',
  channelId: 'session:task-1',
  sender: 'assistant',
  senderName: 'OPC',
  content: 'Please review the delivery.',
  timestamp: 1,
  mentions: [],
  metadata: {
    checkpoint_type: 'company_delivery_feedback',
    checkpoint_id: 'cp-delivery',
    work_item_projection_title: 'CEO Delivery',
    feedback_scope: 'final',
  },
}

assert.equal(isCheckpointType('company_delivery_feedback'), true)
assert.equal(isCheckpointType('company_staffing_selection'), true)
assert.equal(isCheckpointType('action_permission'), true)
assert.equal(isCheckpointCardMetadata(checkpoint.metadata), true)
assert.deepEqual(toCheckpointReplyMetadata(checkpoint.metadata), {
  response_to_checkpoint_id: 'cp-delivery',
  response_to_checkpoint_type: 'company_delivery_feedback',
})
assert.deepEqual(toCheckpointReplyMetadata({
  ...checkpoint.metadata,
  interaction_requester_session_id: 'root-session',
}), {
  response_to_checkpoint_id: 'cp-delivery',
  response_to_checkpoint_type: 'company_delivery_feedback',
  interaction_requester_session_id: 'root-session',
})

const pending = analyzeCheckpointMessages([checkpoint])
assert.deepEqual([...pending.pendingMessageIds], ['msg-checkpoint'])

const legacySelfEvolutionResult: ChatMessage = {
  ...checkpoint,
  id: 'msg-self-evolution-result',
  content: 'Self-evolution finished without writing updates because the agents did not return valid evolution patches.',
  metadata: {
    checkpoint_type: 'company_delivery_feedback',
    checkpoint_id: 'cp-delivery',
    kind: 'company_self_evolution_result',
    self_evolution_completed: true,
  },
}
assert.equal(isCheckpointCardMetadata(legacySelfEvolutionResult.metadata), false)
const ignoredSelfEvolutionResult = analyzeCheckpointMessages([legacySelfEvolutionResult])
assert.deepEqual([...ignoredSelfEvolutionResult.pendingMessageIds], [])
assert.deepEqual([...ignoredSelfEvolutionResult.respondedMessageIds], [])

const duplicatePending = analyzeCheckpointMessages([
  checkpoint,
  {
    ...checkpoint,
    id: 'msg-checkpoint-duplicate',
    channelId: 'session:task-2',
    timestamp: 2,
  },
])
assert.deepEqual([...duplicatePending.pendingMessageIds], ['msg-checkpoint'])
assert.deepEqual([...duplicatePending.respondedMessageIds], [])
assert.deepEqual([...duplicatePending.duplicateMessageIds], ['msg-checkpoint-duplicate'])

const duplicateResponded = analyzeCheckpointMessages([
  {
    ...checkpoint,
    metadata: {
      ...checkpoint.metadata,
      checkpoint_status: 'responded',
    },
  },
  {
    ...checkpoint,
    id: 'msg-checkpoint-duplicate',
    channelId: 'session:task-2',
    timestamp: 2,
    metadata: {
      ...checkpoint.metadata,
      checkpoint_status: 'responded',
    },
  },
])
assert.deepEqual([...duplicateResponded.respondedMessageIds], ['msg-checkpoint'])
assert.deepEqual([...duplicateResponded.duplicateMessageIds], ['msg-checkpoint-duplicate'])

const replyBeforeEngineResolution = analyzeCheckpointMessages([
  checkpoint,
  {
    id: 'msg-user',
    channelId: 'session:task-1',
    sender: 'user',
    senderName: 'You',
    content: 'Please make one more change.',
    timestamp: 2,
    mentions: [],
    metadata: {
      response_to_checkpoint_id: 'cp-delivery',
      response_to_checkpoint_type: 'company_delivery_feedback',
    },
  },
])
assert.deepEqual([...replyBeforeEngineResolution.respondedMessageIds], ['msg-checkpoint'])
assert.deepEqual([...replyBeforeEngineResolution.pendingMessageIds], [])

const responded = analyzeCheckpointMessages([
  {
    ...checkpoint,
    metadata: {
      ...checkpoint.metadata,
      checkpoint_status: 'responded',
      checkpoint_response_message_id: 'msg-user',
    },
  },
])
assert.deepEqual([...responded.respondedMessageIds], ['msg-checkpoint'])

const expiredApproval: ChatMessage = {
  id: 'msg-expired-approval',
  channelId: 'session:task-1',
  sender: 'assistant',
  senderName: 'OPC',
  content: 'Approve external_agent?',
  timestamp: 3,
  mentions: [],
  metadata: {
    checkpoint_type: 'action_permission',
    checkpoint_id: 'esc-expired',
    prompt: 'Approve external_agent?',
    options: [{ id: 'approve_once', label: 'Approve once' }],
    checkpoint_status: 'timeout',
  },
}
const staleApproval: ChatMessage = {
  ...expiredApproval,
  id: 'msg-stale-approval',
  metadata: {
    ...expiredApproval.metadata,
    checkpoint_id: 'esc-stale',
    checkpoint_status: 'stale',
  },
}
const supersededRecruitment: ChatMessage = {
  ...checkpoint,
  id: 'msg-superseded-recruitment',
  metadata: {
    ...checkpoint.metadata,
    checkpoint_type: 'company_recruitment_confirmation',
    checkpoint_id: 'cp-recruit-old',
    checkpoint_status: 'superseded',
  },
}
const ignoredDelivery: ChatMessage = {
  ...checkpoint,
  id: 'msg-ignored-delivery',
  metadata: {
    ...checkpoint.metadata,
    checkpoint_status: 'ignored',
  },
}
const terminal = analyzeCheckpointMessages([expiredApproval, staleApproval, supersededRecruitment, ignoredDelivery])
assert.deepEqual([...terminal.pendingMessageIds], [])
assert.deepEqual([...terminal.respondedMessageIds], ['msg-expired-approval', 'msg-stale-approval', 'msg-superseded-recruitment', 'msg-ignored-delivery'])

for (const checkpointStatus of ['failed', 'outcome_unknown']) {
  const terminalPermission = analyzeCheckpointMessages([{
    ...checkpoint,
    id: `permission-${checkpointStatus}`,
    metadata: {
      checkpoint_type: 'action_permission',
      checkpoint_id: `cp-${checkpointStatus}`,
      checkpoint_status: checkpointStatus,
    },
  }])
  assert.deepEqual([...terminalPermission.pendingMessageIds], [])
  assert.deepEqual([...terminalPermission.respondedMessageIds], [`permission-${checkpointStatus}`])
}

console.log('checkpointUtils.test.ts: OK (checkpoint pending/reply/terminal status handling)')
