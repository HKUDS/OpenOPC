import type { ChatMessage, ChatMessageMeta, CheckpointReplyMetadata } from '../types/chat'

const CHECKPOINT_TYPES = new Set([
  'company_work_item_gate',
  'company_delivery_feedback',
  'company_staffing_selection',
  'company_recruitment_confirmation',
  'company_reorg_pending',
  'task_user_input',
  'tool_permission',
  'action_permission',
  'route_clarification',
  'company_runtime_selection',
  'company_run_failure_review',
])

const TERMINAL_CHECKPOINT_STATUSES = new Set([
  'responded',
  'resolved',
  'timeout',
  'timed_out',
  'expired',
  'stale',
  'superseded',
  'ignored',
  'cancelled',
  'canceled',
  'invalid',
  'answered',
  'consuming',
  'failed',
  'outcome_unknown',
])

export function isCheckpointType(value: string | undefined): boolean {
  return CHECKPOINT_TYPES.has(String(value ?? '').trim())
}

export function isCheckpointCardMetadata(meta: ChatMessageMeta | undefined): boolean {
  if (!isCheckpointType(meta?.checkpoint_type)) {
    return false
  }
  if (meta?.self_evolution_completed) {
    return false
  }
  if (String(meta?.kind ?? '').trim() === 'company_self_evolution_result') {
    return false
  }
  return true
}

export function isCheckpointResolved(meta: ChatMessageMeta | undefined): boolean {
  const status = String(meta?.checkpoint_status ?? '').trim().toLowerCase()
  if (TERMINAL_CHECKPOINT_STATUSES.has(status)) {
    return true
  }
  return !!String(meta?.checkpoint_response_message_id ?? '').trim()
}

export function toCheckpointReplyMetadata(meta: ChatMessageMeta | undefined): CheckpointReplyMetadata | undefined {
  const checkpointId = String(meta?.checkpoint_id ?? '').trim()
  if (!checkpointId) {
    return undefined
  }
  const checkpointType = String(meta?.checkpoint_type ?? '').trim()
  const requesterTaskId = String(meta?.interaction_requester_task_id ?? '').trim()
  const requesterSessionId = String(meta?.interaction_requester_session_id ?? '').trim()
  return {
    response_to_checkpoint_id: checkpointId,
    response_to_checkpoint_type: checkpointType || undefined,
    ...(requesterTaskId ? { interaction_requester_task_id: requesterTaskId } : {}),
    ...(requesterSessionId ? { interaction_requester_session_id: requesterSessionId } : {}),
  }
}

export function isResponseForCheckpoint(message: ChatMessage, checkpointMeta: ChatMessageMeta | undefined): boolean {
  if (message.sender !== 'user') {
    return false
  }
  const checkpointId = String(checkpointMeta?.checkpoint_id ?? '').trim()
  if (!checkpointId) {
    return false
  }
  const replyMeta = message.metadata
  if (String(replyMeta?.response_to_checkpoint_id ?? '').trim() === checkpointId) {
    return true
  }
  return false
}

export function analyzeCheckpointMessages(messages: ChatMessage[]): {
  pendingMessageIds: Set<string>
  respondedMessageIds: Set<string>
  duplicateMessageIds: Set<string>
} {
  const pendingMessageIds = new Set<string>()
  const respondedMessageIds = new Set<string>()
  const duplicateMessageIds = new Set<string>()
  const latestCheckpointReplyIndex = new Map<string, number>()
  const seenCheckpointIds = new Set<string>()

  for (let i = 0; i < messages.length; i++) {
    const message = messages[i]
    if (message.sender !== 'user') continue
    const replyMeta = message.metadata
    const checkpointId = String(replyMeta?.response_to_checkpoint_id ?? '').trim()
    if (checkpointId) {
      latestCheckpointReplyIndex.set(checkpointId, i)
    }
  }

  for (let i = 0; i < messages.length; i++) {
    const message = messages[i]
    const checkpointMeta = message.metadata
    if (!isCheckpointCardMetadata(checkpointMeta)) {
      continue
    }

    const checkpointId = String(checkpointMeta?.checkpoint_id ?? '').trim()
    if (checkpointId && seenCheckpointIds.has(checkpointId)) {
      duplicateMessageIds.add(message.id)
      continue
    }
    if (checkpointId) {
      seenCheckpointIds.add(checkpointId)
    }

    const hasLaterCheckpointReply = !!checkpointId && (latestCheckpointReplyIndex.get(checkpointId) ?? -1) > i
    if (isCheckpointResolved(checkpointMeta) || hasLaterCheckpointReply) {
      respondedMessageIds.add(message.id)
      continue
    }

    pendingMessageIds.add(message.id)
  }

  return {
    pendingMessageIds,
    respondedMessageIds,
    duplicateMessageIds,
  }
}
