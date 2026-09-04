import React from 'react'
import type { Session } from '../types/kanban'
import type { ExternalTeamActivityRecord } from '../stores/ExternalTeamActivityStore'
import { externalTeamSummaryText, isJiuwenOpaqueTeam } from '../lib/externalTeamActivity'
import { IconTimeline } from './SvgIcons'

export function ExternalTeamSummaryCard({
  session,
  record,
  onOpen,
}: {
  session: Session
  record?: ExternalTeamActivityRecord
  onOpen?: (taskId: string) => void
}) {
  if (!isJiuwenOpaqueTeam(session)) return null
  const summary = record?.summary ?? session.externalTeamSummary
  return (
    <button type="button" className="external-team-summary-card" onClick={() => onOpen?.(session.executionTurnId || session.taskId)}>
      <IconTimeline />
      <span className="external-team-summary-main">
        <strong>JiuwenSwarm Team</strong>
        <small>{record?.loading ? 'Loading team activity…' : externalTeamSummaryText(summary)}</small>
      </span>
      <span className="external-team-summary-open">Team Activity →</span>
    </button>
  )
}
