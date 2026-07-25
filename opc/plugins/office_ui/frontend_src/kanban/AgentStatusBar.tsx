import { useMemo } from 'react'
import type { AgentInfo } from '../types/visual'
import type { AgentAnimStatus, KanbanTask } from '../types/kanban'
import { AGENT_STATUS_LABEL } from '../types/kanban'
import { useI18n } from '../i18n'

interface AgentStatusBarProps {
  agents: AgentInfo[]
  tasks: KanbanTask[]
}

interface AgentState {
  agent: AgentInfo
  status: AgentAnimStatus
  currentTool?: string
  taskDisplayId?: string
}

export function AgentStatusBar({ agents, tasks }: AgentStatusBarProps) {
  const { t, translateMaybe } = useI18n()
  const agentStates = useMemo<AgentState[]>(() => {
    const tasksById = new Map(tasks.map((task) => [task.id, task]))
    return agents.map(agent => {
      // Find the first active (non-idle) task for this agent
      const activeTask = tasks.find(
        t => t.assigneeIds.includes(agent.agent_id)
          && t.agentStatus && t.agentStatus !== 'idle'
      )
      const runtimeTask = agent.current_task_id ? tasksById.get(agent.current_task_id) : undefined
      return {
        agent,
        status: (activeTask?.agentStatus ?? agent.runtime_status ?? 'idle') as AgentAnimStatus,
        currentTool: activeTask?.currentTool ?? agent.current_tool,
        taskDisplayId: activeTask?.displayId ?? runtimeTask?.displayId,
      }
    })
  }, [agents, tasks])

  if (agents.length === 0) return null

  const activeCount = agentStates.filter(s => s.status !== 'idle').length

  return (
    <div className="agent-status-bar">
      <span className="agent-status-summary">
        {activeCount > 0
          ? t('agent.summary.active', { active: activeCount, total: agents.length })
          : t(agents.length !== 1 ? 'agent.summary.countPlural' : 'agent.summary.count', { count: agents.length })}
      </span>
      <div className="agent-status-chips">
        {agentStates.map(({ agent, status, currentTool, taskDisplayId }) => {
          const statusLabel = status === 'tool_active' && currentTool
            ? currentTool
            : translateMaybe('agent.status', status) || AGENT_STATUS_LABEL[status]
          return (
            <div
              key={agent.agent_id}
              className={`agent-status-chip status-${status}`}
              title={`${agent.name}: ${statusLabel}${taskDisplayId ? ` (${taskDisplayId})` : ''}`}
            >
              <span className="agent-status-avatar">{agent.name.charAt(0).toUpperCase()}</span>
              <span className="agent-status-name">{agent.name}</span>
              {status !== 'idle' && (
                <>
                  <span className="kanban-runtime-dot" />
                  <span className="agent-status-detail">
                    {statusLabel}
                  </span>
                </>
              )}
              {taskDisplayId && (
                <span className="agent-status-task">{taskDisplayId}</span>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
