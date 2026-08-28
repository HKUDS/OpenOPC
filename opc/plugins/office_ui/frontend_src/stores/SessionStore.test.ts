import assert from 'node:assert/strict'
import type { Session } from '../types/kanban'
import {
  mergePartialSessionSnapshot,
  preserveSessionNativePermission,
} from './SessionStore'

function makeSession(taskId: string, projectId = 'default', title = taskId): Session {
  return {
    projectId,
    taskId,
    channelId: `session:${taskId}`,
    title,
    status: 'running',
    columnId: 'in-progress',
    assigneeIds: [],
    priority: null,
    tags: [],
    progressLog: [],
    createdAt: 1,
    updatedAt: 1,
    messageCount: 0,
  }
}

const existingRoot = makeSession('root', 'default', 'Existing root')
const existingChild = {
  ...makeSession('child', 'default', 'Existing child'),
  roleWorkItems: {
    ceo: {
      roleKey: 'ceo',
      roleId: 'ceo',
      roleName: 'CEO',
      runtimeStatus: 'reflecting' as const,
      aggregatedStatus: 'active' as const,
      workItems: [],
    },
  },
}
const otherProject = makeSession('other', 'archive')

const incomingRoot = {
  ...makeSession('root', 'default', 'Indexed root'),
  latestPreview: 'updated by project index',
}
const incomingNew = makeSession('new-session', 'default', 'Indexed new session')

const merged = mergePartialSessionSnapshot(
  [existingRoot, existingChild, otherProject],
  [incomingRoot, incomingNew],
  'default',
)

assert.deepEqual(
  merged.map(session => session.taskId),
  ['root', 'child', 'new-session'],
  'partial project index keeps existing current-project sessions and appends newly indexed sessions',
)
assert.equal(merged[0]?.title, 'Indexed root', 'incoming index rows replace matching existing rows')
assert.equal(
  merged[1]?.roleWorkItems?.ceo.roleName,
  'CEO',
  'existing runtime detail survives when the partial index omits that session',
)
assert.equal(
  merged.some(session => session.projectId === 'archive'),
  false,
  'partial project index merge stays project-scoped',
)

const persistedPermission = {
  ...makeSession('permission-session'),
  nativeApprovalLevel: 'full-access' as const,
  nativePermissionScopeId: 'permission-scope',
}
assert.deepEqual(
  preserveSessionNativePermission(persistedPermission, {
    nativeApprovalLevel: undefined,
    nativePermissionScopeId: undefined,
  }),
  {
    nativeApprovalLevel: 'full-access',
    nativePermissionScopeId: 'permission-scope',
  },
  'partial session payloads cannot erase persisted native permission state',
)
assert.deepEqual(
  preserveSessionNativePermission(persistedPermission, {
    nativeApprovalLevel: 'read-only',
    nativePermissionScopeId: 'updated-scope',
  }),
  {
    nativeApprovalLevel: 'read-only',
    nativePermissionScopeId: 'updated-scope',
  },
  'explicit server permission updates replace the persisted session values',
)

console.log('SessionStore partial snapshot merge contract passed')
