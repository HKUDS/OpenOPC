import assert from 'node:assert/strict'
import test from 'node:test'

import { executionAgentLabel, TASK_AGENT_LABELS } from './externalAgents'

test('JiuwenSwarm execution units use stable user-facing names', () => {
  assert.equal(TASK_AGENT_LABELS.jiuwen, 'JiuwenSwarm-single')
  assert.equal(TASK_AGENT_LABELS.jiuwenswarm, 'JiuwenSwarm-team')
  assert.equal(executionAgentLabel('JIUWENSWARM'), 'JiuwenSwarm-team')
})
