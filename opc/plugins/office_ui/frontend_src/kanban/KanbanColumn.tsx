import { useCallback, useRef, useState } from 'react'
import { Droppable } from '@hello-pangea/dnd'
import type { AgentInfo } from '../types/visual'
import type { KanbanColumn as KanbanColumnType, KanbanTask } from '../types/kanban'
import { KanbanCard } from './KanbanCard'
import { useI18n } from '../i18n'

interface KanbanColumnProps {
  column: KanbanColumnType
  tasks: KanbanTask[]
  agents: AgentInfo[]
  officeMap?: Record<string, string>
  companyMode?: boolean
  selectedTaskId?: string | null
  onCardClick: (task: KanbanTask) => void
  onStartTask?: (taskId: string) => void
  onQuickCreate?: (title: string) => void
}

export function KanbanColumn({ column, tasks, agents, officeMap, companyMode, selectedTaskId, onCardClick, onStartTask, onQuickCreate }: KanbanColumnProps) {
  const { t, translateMaybe } = useI18n()
  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState('')
  const committedRef = useRef(false)

  const commitAdd = useCallback(() => {
    if (committedRef.current) return  // guard: prevent double-fire from Enter + onBlur
    committedRef.current = true
    const title = draft.trim()
    if (title && onQuickCreate) {
      onQuickCreate(title)
    }
    setDraft('')
    setAdding(false)
  }, [draft, onQuickCreate])

  return (
    <div className="kanban-column">
      <div className="kanban-column-header">
        <span className="kanban-col-dot" style={{ background: column.color }} />
        <span className="kanban-col-label">{translateMaybe('kanban.column', column.name) || column.name}</span>
        <span className="kanban-col-count">{tasks.length}</span>
        {onQuickCreate && (
          <button className="kanban-col-add" title={t('kanban.addTask')} onClick={() => { committedRef.current = false; setAdding(true) }}>+</button>
        )}
      </div>

      {adding && (
        <div className="kanban-quick-add">
          <input
            className="kanban-quick-input"
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => {
              if (e.key === 'Enter') { e.preventDefault(); commitAdd() }
              if (e.key === 'Escape') { committedRef.current = true; setDraft(''); setAdding(false) }
            }}
            onBlur={commitAdd}
            placeholder={t('kanban.taskTitle')}
            autoFocus
          />
        </div>
      )}

      <Droppable droppableId={column.id} isDropDisabled={!!companyMode}>
        {(provided) => (
          <div ref={provided.innerRef} {...provided.droppableProps} className="kanban-col-body">
            {tasks.length === 0 && !adding && (
              <div className="kanban-empty"><span className="kanban-empty-icon">·</span></div>
            )}
            {tasks.map((task, index) => (
              <KanbanCard
                key={task.id}
                task={task}
                index={index}
                agents={agents}
                officeMap={officeMap}
                companyMode={companyMode}
                isSelected={task.id === selectedTaskId}
                onClick={onCardClick}
                onStart={onStartTask}
              />
            ))}
            {provided.placeholder}
          </div>
        )}
      </Droppable>
    </div>
  )
}
