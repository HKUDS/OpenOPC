import { Draggable } from '@hello-pangea/dnd'
import type { AgentInfo } from '../types/visual'
import { PRIORITY_META, AGENT_STATUS_LABEL, type KanbanTask } from '../types/kanban'
import { getWorkItemRoleLabel, humanizeWorkItemRoleId } from '../lib/workItemIdentity'
import { getLinkedRuntimeTaskId } from '../lib/workItemRuntimeIds'
import { useI18n } from '../i18n'

const STATUS_BADGE: Record<string, { label: string; color: string }> = {
  todo: { label: 'To do', color: '#9ca3af' },
  in_progress: { label: 'In progress', color: '#f59e0b' },
  in_review: { label: 'In review', color: '#fbbf24' },
  done: { label: 'Done', color: '#34d399' },
  running: { label: 'Running', color: '#34d399' },
  idle: { label: 'Idle', color: '#6366f1' },
  blocked: { label: 'Blocked', color: '#f97316' },
  awaiting_peer: { label: 'Awaiting', color: '#fbbf24' },
  awaiting_manager_review: { label: 'Mgr Review', color: '#fbbf24' },
  awaiting_human: { label: 'Human Review', color: '#fbbf24' },
  awaiting_review: { label: 'In Review', color: '#fbbf24' },
  failed: { label: 'Failed', color: '#ef4444' },
  cancelled: { label: 'Cancelled', color: '#9ca3af' },
}

interface KanbanCardProps {
  task: KanbanTask
  index: number
  agents: AgentInfo[]
  officeMap?: Record<string, string>
  companyMode?: boolean
  isSelected?: boolean
  onClick: (task: KanbanTask) => void
  onStart?: (taskId: string) => void
}

export function KanbanCard({ task, index, agents, officeMap, companyMode, isSelected, onClick, onStart }: KanbanCardProps) {
  const { t, translateMaybe } = useI18n()
  const assignees = task.assigneeIds
    .map(id => agents.find(a => a.agent_id === id))
    .filter(Boolean) as AgentInfo[]
  const priority = task.priority ? PRIORITY_META[task.priority] : null

  const crossOffice = officeMap && assignees.length > 1 &&
    new Set(assignees.map(a => officeMap[a.agent_id]).filter(Boolean)).size > 1

  const runtimeActive = task.agentStatus && task.agentStatus !== 'idle'
  const depCount = task.dependencies?.length ?? 0
  // Hide status badge when runtime bar is showing (avoids "Running" + "Thinking..." redundancy)
  // Also hide for 'pending' (default state, no badge needed in todo column)
  const phaseBadge = task.phase
  const statusBadge = (!runtimeActive && phaseBadge && phaseBadge !== 'ready')
    ? STATUS_BADGE[phaseBadge] ?? null : null
  const employee = task.employeeAssignment
  const roleLabel = getWorkItemRoleLabel(task)
  const gate = task.workItemGate
  const managerLabel = humanizeWorkItemRoleId(task.managerRoleId)
  const blockerLabel = (task.blockedReason ?? '').trim()
  const reworkLabel = (task.reworkFeedback ?? '').trim()
  const linkedRuntimeTaskId = getLinkedRuntimeTaskId(task)
  const runtimeStatusLabel = task.agentStatus
    ? (translateMaybe('agent.status', task.agentStatus) || AGENT_STATUS_LABEL[task.agentStatus] || task.agentStatus)
    : ''

  return (
    <Draggable draggableId={task.id} index={index} isDragDisabled={!!companyMode}>
      {(provided, snapshot) => (
        <div
          ref={provided.innerRef}
          {...provided.draggableProps}
          {...provided.dragHandleProps}
          className={`kanban-card${snapshot.isDragging ? ' is-dragging' : ''}${runtimeActive ? ' is-active' : ''}${isSelected ? ' is-selected' : ''}`}
          data-task-id={task.id}
          onMouseUp={e => { if (e.button === 0 && !snapshot.isDragging) onClick(task) }}
        >
          <div className="kanban-card-top">
            <span className="kanban-card-id">{task.displayId}</span>
            {statusBadge && (
              <span className="kanban-status-badge" style={{ color: statusBadge.color }}>
                <span style={{ display: 'inline-block', width: 6, height: 6, borderRadius: '50%', background: statusBadge.color, marginRight: 3 }} />
                {translateMaybe('kanban.status', phaseBadge) || statusBadge.label}
              </span>
            )}
            {depCount > 0 && (
              <span className="kanban-dep-badge" title={t('kanban.upstreamDeps', { count: depCount })}>{t('kanban.dep', { count: depCount })}</span>
            )}
            {crossOffice && <span className="kanban-cross-badge" title={t('kanban.crossOffice')}>&#x21C4;</span>}
            {onStart && (
              <button
                className="kanban-start-btn"
                title={companyMode ? t('kanban.startWorkItem') : t('kanban.startTask')}
                onClick={e => { e.stopPropagation(); onStart(task.id) }}
              >
                &#x25B6;
              </button>
            )}
          </div>

          <p className="kanban-card-title">{task.title}</p>

          {(roleLabel || employee?.name || gate?.type || task.originChannel || task.workItemProjectionId) && (
            <div className="kanban-card-meta-row">
              {roleLabel && (
                <span className="kanban-meta-badge kanban-role-badge" title={`${t('common.role')}: ${roleLabel}`}>
                  {roleLabel}
                </span>
              )}
              {employee?.name && (
                <span className="kanban-meta-badge kanban-employee-badge" title={`${t('common.employee')}: ${employee.name}${employee.category ? ` (${employee.category})` : ''}`}>
                  <span className="kanban-meta-icon">&#x1F464;</span>
                  {employee.name}
                </span>
              )}
              {task.workItemProjectionId && (
                <span className="kanban-meta-badge kanban-projection-badge" title={`${t('common.projection')}: ${task.workItemProjectionId}`}>
                  {task.workItemProjectionId}
                </span>
              )}
              {gate?.type && (
                <span className={`kanban-meta-badge kanban-gate-badge kanban-gate-${gate.type}`} title={`${t('common.gate')}: ${gate.type}${gate.reviewerRole ? ` by ${gate.reviewerRole}` : ''}`}>
                  {gate.type === 'review' ? '\u2709' : gate.type === 'approval' ? '\u2713' : '\u270B'}
                  {gate.type}
                </span>
              )}
              {task.originChannel && (
                <span className="kanban-meta-badge kanban-origin-badge" title={`${t('common.origin')}: ${task.originChannel}`}>
                  #{task.originChannel}
                </span>
              )}
              {managerLabel && (
                <span className="kanban-meta-badge" title={`${t('common.manager')}: ${managerLabel}`}>
                  {managerLabel}
                </span>
              )}
            </div>
          )}

          {(blockerLabel || reworkLabel || linkedRuntimeTaskId) && (
            <div className="kanban-card-tags">
              {blockerLabel && <span className="kanban-tag">{blockerLabel}</span>}
              {reworkLabel && <span className="kanban-tag">{reworkLabel}</span>}
              {linkedRuntimeTaskId && (
                <span className="kanban-tag" title={t('kanban.executionTurn', { id: linkedRuntimeTaskId })}>
                  {t('common.runtime')}
                </span>
              )}
            </div>
          )}

          {runtimeActive && (
            <div className={`kanban-card-runtime status-${task.agentStatus}`}>
              <span className="kanban-runtime-dot" />
              <span className="kanban-runtime-label">
                {task.agentStatus === 'tool_active' && task.currentTool
                  ? task.currentTool
                  : runtimeStatusLabel}
              </span>
            </div>
          )}

          {task.tags.length > 0 && (
            <div className="kanban-card-tags">
              {task.tags.slice(0, 3).map(tag => (
                <span key={tag} className="kanban-tag">{tag}</span>
              ))}
            </div>
          )}

          {(priority || assignees.length > 0) && (
            <div className="kanban-card-footer">
              <div className="kanban-card-footer-left">
                {priority && (
                  <span className="kanban-priority" style={{ color: priority.color }} title={priority.label}>
                    {priority.symbol}
                  </span>
                )}
              </div>
              <div className="kanban-assignee-group">
                {assignees.slice(0, 3).map(a => (
                  <span key={a.agent_id} className="kanban-assignee-badge" title={a.name}>
                    {a.name.charAt(0).toUpperCase()}
                  </span>
                ))}
                {assignees.length > 3 && (
                  <span className="kanban-assignee-badge kanban-assignee-more">+{assignees.length - 3}</span>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </Draggable>
  )
}
