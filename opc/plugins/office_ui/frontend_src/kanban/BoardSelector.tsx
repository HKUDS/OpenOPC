import type { KanbanBoard } from '../types/kanban'
import { useI18n } from '../i18n'

interface BoardSelectorProps {
  boards: KanbanBoard[]
  activeBoardId: string | null
  onSelect: (id: string) => void
}

export function BoardSelector({ boards, activeBoardId, onSelect }: BoardSelectorProps) {
  const { translateMaybe } = useI18n()
  return (
    <div className="board-selector">
      <div className="board-tabs">
        {boards.map(b => (
          <button
            key={b.id}
            className={`board-tab${b.id === activeBoardId ? ' active' : ''}`}
            style={{ '--board-color': b.color } as React.CSSProperties}
            onClick={() => onSelect(b.id)}
          >
            <span className="board-tab-dot" style={{ background: b.color }} />
            {translateMaybe('kanban.column', b.name) || b.name}
          </button>
        ))}
      </div>
    </div>
  )
}
