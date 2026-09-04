import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { WorkItemProgressCard } from './WorkItemProgressCard'
import type { RoleWorkItemSummary } from '../types/kanban'

const currentOwnerRoleWorkItems: Record<string, RoleWorkItemSummary> = {
  cto: {
    roleKey: 'cto',
    roleId: 'cto',
    roleName: 'CTO',
    runtimeStatus: 'idle',
    aggregatedStatus: 'waiting',
    workItems: [
      {
        workItemId: 'wi-review',
        phase: 'awaiting_manager_review',
        kanbanColumn: 'in-review',
        title: 'Implement summary',
        kind: 'execute',
        isReviewTarget: true,
        executorRoleId: 'engineer',
        reviewerRoleId: 'cto',
        createdAt: 10,
        updatedAt: 20,
        executionTurnId: 'runtime-task-1',
        progressLog: [],
      },
    ],
  },
}

const executorRoleWorkItems: Record<string, RoleWorkItemSummary> = {
  engineer: {
    roleKey: 'engineer',
    roleId: 'engineer',
    roleName: 'Engineer',
    runtimeStatus: 'idle',
    aggregatedStatus: 'waiting',
    workItems: [
      {
        workItemId: 'wi-review',
        phase: 'awaiting_manager_review',
        kanbanColumn: 'in-review',
        title: 'Implement summary',
        kind: 'execute',
        isReviewTarget: true,
        executorRoleId: 'engineer',
        reviewerRoleId: 'cto',
        createdAt: 10,
        updatedAt: 20,
        executionTurnId: 'runtime-task-1',
        progressLog: [],
      },
    ],
  },
}

const executorMarkup = renderToStaticMarkup(
  React.createElement(WorkItemProgressCard, {
    workItemLog: [],
    roleWorkItems: currentOwnerRoleWorkItems,
    executorRoleWorkItems,
    isCompanyRuntime: true,
  }),
)

assert.match(executorMarkup, /Execution Progress/)
assert.match(executorMarkup, /Engineer/)
assert.doesNotMatch(executorMarkup, /CTO/)

const fallbackMarkup = renderToStaticMarkup(
  React.createElement(WorkItemProgressCard, {
    workItemLog: [],
    roleWorkItems: currentOwnerRoleWorkItems,
    isCompanyRuntime: true,
  }),
)

assert.match(fallbackMarkup, /CTO/)
assert.doesNotMatch(fallbackMarkup, /Engineer/)

const preparingMarkup = renderToStaticMarkup(
  React.createElement(WorkItemProgressCard, {
    workItemLog: [],
    isCompanyRuntime: true,
  }),
)

assert.match(preparingMarkup, /Execution Progress/)
assert.match(preparingMarkup, /Preparing company roles/)
assert.match(preparingMarkup, /role="status"/)

const jiuwenTeamMarkup = renderToStaticMarkup(
  React.createElement(WorkItemProgressCard, {
    workItemLog: [],
    isCompanyRuntime: true,
    roleWorkItems: {
      cto: {
        roleKey: 'cto',
        roleId: 'cto',
        roleName: 'CTO',
        runtimeStatus: 'tool_active',
        aggregatedStatus: 'active',
        workItems: [{
          workItemId: 'team-wi',
          phase: 'running',
          kanbanColumn: 'in-progress',
          title: 'Research platform architecture',
          selectedExecutionAgent: 'jiuwenswarm',
          executionUnitKind: 'opaque_external_team',
          coveredRoleIds: ['cto', 'senior_engineer'],
          executionTurnId: 'team-turn',
          createdAt: 30,
          updatedAt: 40,
          progressLog: [],
          externalTeamSummary: {
            schemaVersion: 1,
            provider: 'jiuwenswarm',
            mode: 'team_active',
            leaderState: 'synthesizing',
            members: [],
            tasks: [],
            counts: {
              members: 3, membersActive: 2, membersCompleted: 1, membersFailed: 0,
              tasks: 3, tasksActive: 2, tasksCompleted: 1, tasksFailed: 0,
            },
            telemetryIncomplete: false,
          },
        }],
      },
    },
  }),
)
assert.match(jiuwenTeamMarkup, /CTO · JiuwenSwarm-team/)
assert.match(jiuwenTeamMarkup, /3 members · 2 active · Leader synthesizing/)

const nonTeamMarkup = renderToStaticMarkup(
  React.createElement(WorkItemProgressCard, {
    workItemLog: [],
    isCompanyRuntime: true,
    roleWorkItems: {
      cto: {
        ...currentOwnerRoleWorkItems.cto,
        workItems: [{
          ...currentOwnerRoleWorkItems.cto.workItems[0],
          selectedExecutionAgent: 'jiuwen',
          executionUnitKind: 'external_agent',
        }],
      },
    },
  }),
)
assert.doesNotMatch(nonTeamMarkup, /JiuwenSwarm-team/)

const mixedInvocationMarkup = renderToStaticMarkup(
  React.createElement(WorkItemProgressCard, {
    workItemLog: [],
    isCompanyRuntime: true,
    roleWorkItems: {
      cto: {
        roleKey: 'cto', roleId: 'cto', roleName: 'CTO', runtimeStatus: 'idle', aggregatedStatus: 'done',
        workItems: [
          {
            workItemId: 'older-team', phase: 'approved', kanbanColumn: 'done', title: 'Older team run',
            selectedExecutionAgent: 'jiuwenswarm', executionUnitKind: 'opaque_external_team',
            executionTurnId: 'older-team-turn', createdAt: 1, updatedAt: 2, progressLog: [],
          },
          {
            workItemId: 'latest-native', phase: 'approved', kanbanColumn: 'done', title: 'Latest native run',
            selectedExecutionAgent: 'native', executionUnitKind: 'native_agent',
            executionTurnId: 'latest-native-turn', createdAt: 3, updatedAt: 4, progressLog: [],
          },
        ],
      },
    },
  }),
)
assert.doesNotMatch(mixedInvocationMarkup, /JiuwenSwarm-team/)

console.log('WorkItemProgressCard.test.tsx: OK (executor rollup preferred with current-owner fallback)')
