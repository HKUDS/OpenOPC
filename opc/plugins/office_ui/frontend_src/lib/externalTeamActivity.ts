import type { ExternalTeamActivitySummary } from '../types/externalTeamActivity'

export function isJiuwenOpaqueTeam(value: {
  selectedExecutionAgent?: string
  executionUnitKind?: string
} | null | undefined): boolean {
  return value?.selectedExecutionAgent === 'jiuwenswarm'
    && value?.executionUnitKind === 'opaque_external_team'
}

export function externalTeamSummaryText(summary: ExternalTeamActivitySummary | undefined): string {
  if (!summary) return 'Team starting…'
  if (summary.telemetryIncomplete) return 'Internal telemetry unavailable'
  const { counts } = summary
  if (summary.mode === 'starting' || summary.mode === 'unknown') return 'Team starting…'
  if (summary.mode === 'leader_only' && counts.members === 0 && counts.tasks === 0) {
    return 'Leader executing directly'
  }
  if (summary.mode === 'failed') {
    return counts.members > 0 ? `${counts.members} members · Team error` : 'Team error'
  }
  if (summary.mode === 'completed') {
    if (counts.members === 0) return 'Leader completed directly'
    return `${counts.members} members · ${counts.tasksCompleted}/${counts.tasks} tasks completed`
  }
  const memberText = `${counts.members} members · ${counts.membersActive} active`
  return summary.leaderState === 'synthesizing'
    ? `${memberText} · Leader synthesizing`
    : memberText
}
