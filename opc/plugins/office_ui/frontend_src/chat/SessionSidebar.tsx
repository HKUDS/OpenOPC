import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useVirtualizer } from '@tanstack/react-virtual'
import type { Session } from '../types/kanban'
import { IconPlus, IconSearch, IconActivity, IconShield, IconTrash, IconWorkItem } from './SvgIcons'
import { getSessionRuntimeStatus } from '../lib/sessionRuntime'
import { deriveCompanyRuntimeDisplayStatus } from '../lib/workItemSessions'
import { useI18n } from '../i18n'

interface SessionSidebarProps {
  sessions: Session[]
  activeSessionId: string | null
  activeChannel?: string | null
  secretaryChannelId?: string
  unreadCounts?: Record<string, number>
  onSelect: (taskId: string | null) => void
  onCreateSession: () => void
  onDeleteSession: (taskId: string) => void
  onSelectSecretary?: () => void
}

const STATUS_DOT: Record<string, string> = {
  idle: 'status-idle',
  done: 'status-done',
  pending: 'status-pending',
  failed: 'status-failed',
  cancelled: 'status-cancelled',
  blocked: 'status-blocked',
}

function sessionDotClass(session: Session): string {
  const displayStatus = deriveCompanyRuntimeDisplayStatus(session) ?? session.status
  const runtimeStatus = getSessionRuntimeStatus({ ...session, status: displayStatus })
  if (runtimeStatus === 'tool_active') return 'status-tool-active'
  if (runtimeStatus === 'reflecting') return 'status-reflecting'
  if (displayStatus === 'running') return 'status-running-idle'
  return STATUS_DOT[displayStatus] ?? 'status-pending'
}

type TFunction = ReturnType<typeof useI18n>['t']

function relativeTime(ts: number, t: TFunction): string {
  const diff = Date.now() - ts
  if (diff < 60_000) return t('session.justNow')
  if (diff < 3_600_000) return t('session.minutes', { count: Math.floor(diff / 60_000) })
  if (diff < 86_400_000) return t('session.hours', { count: Math.floor(diff / 3_600_000) })
  return t('session.days', { count: Math.floor(diff / 86_400_000) })
}

function dateGroup(ts: number): string {
  const now = new Date()
  const d = new Date(ts)
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const yesterday = new Date(today.getTime() - 86_400_000)
  if (d >= today) return 'Today'
  if (d >= yesterday) return 'Yesterday'
  return 'Earlier'
}

function groupLabel(group: 'Today' | 'Yesterday' | 'Earlier', hasRuntimeSessions: boolean, t: TFunction): string {
  const label = group === 'Today'
    ? t('session.today')
    : group === 'Yesterday'
      ? t('session.yesterday')
      : t('session.earlier')
  return hasRuntimeSessions ? `${label} ${t('session.runtimeSuffix')}` : label
}

interface SessionTree {
  session: Session
  children: Session[]
}

type SidebarRow =
  | { kind: 'group'; group: 'Today' | 'Yesterday' | 'Earlier' }
  | { kind: 'primary'; node: SessionTree }
  | { kind: 'child'; parentTaskId: string; child: Session; childIndex: number; childCount: number }
  | { kind: 'child-count'; node: SessionTree }

function buildSessionTree(sessions: Session[]): SessionTree[] {
  const childMap = new Map<string, Session[]>()
  const primarySessions: Session[] = []

  for (const s of sessions) {
    if (s.mode === 'child' && s.parentSessionId) {
      const siblings = childMap.get(s.parentSessionId) ?? []
      siblings.push(s)
      childMap.set(s.parentSessionId, siblings)
    } else {
      primarySessions.push(s)
    }
  }

  return primarySessions.map(p => ({
    session: p,
    children: Array.from(new Map(
      [...(p.sessionId ? (childMap.get(p.sessionId) ?? []) : []), ...(childMap.get(p.taskId) ?? [])]
        .map(child => [child.taskId, child]),
    ).values()),
  }))
}

function SessionItem({
  session,
  isActive,
  isChild,
  isLast,
  unreadCount,
  onSelect,
  onDelete,
}: {
  session: Session
  isActive: boolean
  isChild?: boolean
  isLast?: boolean
  unreadCount?: number
  onSelect: () => void
  onDelete: () => void
}) {
  const { t, translateMaybe } = useI18n()
  const [showDelete, setShowDelete] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const agentLabel = session.assigneeIds.length > 0
    ? session.assigneeIds[0].replace(/^agent-/, '')
    : null
  const displayStatus = deriveCompanyRuntimeDisplayStatus(session) ?? session.status

  useEffect(() => {
    if (!confirming) return
    const timer = setTimeout(() => setConfirming(false), 3000)
    return () => clearTimeout(timer)
  }, [confirming])

  return (
    <button
      className={`session-item${isActive ? ' active' : ''}${isChild ? ' session-item-child' : ''}`}
      onClick={onSelect}
      onMouseEnter={() => setShowDelete(true)}
      onMouseLeave={() => setShowDelete(false)}
    >
      {isChild && (
        <span className={`session-tree-line${isLast ? ' last' : ''}`} />
      )}
      <span className={`session-dot ${sessionDotClass(session)}`} />
      <div className="session-item-content">
        <span className="session-item-title">
          {isChild && agentLabel && (
            <span className="session-agent-tag">{agentLabel}</span>
          )}
          {!isChild && session.isCompanyRuntime && (
            <span className="session-runtime-badge" title={t('session.companyRuntime')}><IconWorkItem /></span>
          )}
          {session.title}
        </span>
        <span className="session-item-meta">
          {translateMaybe('kanban.status', displayStatus) || displayStatus} · {relativeTime(session.updatedAt, t)}
        </span>
      </div>
      {!!unreadCount && unreadCount > 0 && (
        <span className="session-unread-badge">{unreadCount > 99 ? '99+' : unreadCount}</span>
      )}
      {showDelete && !isChild && !confirming && (
        <button
          className="session-delete-btn"
          onClick={e => { e.stopPropagation(); setConfirming(true) }}
          title={t('common.delete')}
        >
          <IconTrash />
        </button>
      )}
      {confirming && (
        <span className="session-confirm-delete" onClick={e => e.stopPropagation()}>
          <button className="session-confirm-yes" onClick={() => { setConfirming(false); onDelete() }}>{t('common.delete')}</button>
          <button className="session-confirm-no" onClick={() => setConfirming(false)}>{t('common.cancel')}</button>
        </span>
      )}
    </button>
  )
}

export function SessionSidebar({ sessions, activeSessionId, activeChannel, secretaryChannelId, unreadCounts, onSelect, onCreateSession, onDeleteSession, onSelectSecretary }: SessionSidebarProps) {
  const { t } = useI18n()
  const [search, setSearch] = useState('')
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const hasRuntimeSessions = sessions.some(session =>
    session.isCompanyRuntime
    || session.mode === 'child'
    || session.execMode === 'company'
    || session.execMode === 'org'
    || session.execMode === 'custom'
  )

  const toggleCollapse = useCallback((taskId: string) => {
    setCollapsed(prev => {
      const next = new Set(prev)
      if (next.has(taskId)) next.delete(taskId)
      else next.add(taskId)
      return next
    })
  }, [])

  const filtered = useMemo(() => {
    if (!search.trim()) return sessions
    const q = search.toLowerCase()
    return sessions.filter(s => s.title.toLowerCase().includes(q))
  }, [sessions, search])

  const tree = useMemo(() => buildSessionTree(filtered), [filtered])

  const grouped = useMemo(() => {
    const groups: Record<string, SessionTree[]> = { Today: [], Yesterday: [], Earlier: [] }
    for (const node of tree) {
      const g = dateGroup(node.session.createdAt)
      groups[g]?.push(node)
    }
    return groups
  }, [tree])

  const rows = useMemo<SidebarRow[]>(() => {
    const nextRows: SidebarRow[] = []
    for (const group of ['Today', 'Yesterday', 'Earlier'] as const) {
      const items = grouped[group]
      if (!items || items.length === 0) continue
      nextRows.push({ kind: 'group', group })
      for (const node of items) {
        nextRows.push({ kind: 'primary', node })
        const hasChildren = node.children.length > 0
        const isCollapsed = collapsed.has(node.session.taskId)
        if (hasChildren && isCollapsed) {
          nextRows.push({ kind: 'child-count', node })
        } else if (hasChildren) {
          node.children.forEach((child, idx) => {
            nextRows.push({
              kind: 'child',
              parentTaskId: node.session.taskId,
              child,
              childIndex: idx,
              childCount: node.children.length,
            })
          })
        }
      }
    }
    return nextRows
  }, [collapsed, grouped])

  const useVirtualRows = rows.length > 120
  const listRef = useRef<HTMLDivElement | null>(null)
  const rowVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => listRef.current,
    estimateSize: (index) => {
      const row = rows[index]
      if (row?.kind === 'group') return 26
      if (row?.kind === 'child-count') return 30
      return 54
    },
    overscan: 8,
  })

  const renderPrimaryRow = useCallback((node: SessionTree) => {
    const hasChildren = node.children.length > 0
    const isCollapsed = collapsed.has(node.session.taskId)
    return (
      <div className="session-tree-node">
        <div className="session-tree-primary">
          {hasChildren && (
            <button
              className={`session-expand-btn${isCollapsed ? ' collapsed' : ''}`}
              onClick={e => {
                e.stopPropagation()
                toggleCollapse(node.session.taskId)
              }}
              aria-label={isCollapsed ? t('session.expand') : t('session.collapse')}
            />
          )}
          <SessionItem
            session={node.session}
            isActive={node.session.taskId === activeSessionId}
            unreadCount={unreadCounts?.[node.session.channelId]}
            onSelect={() => onSelect(node.session.taskId)}
            onDelete={() => onDeleteSession(node.session.taskId)}
          />
        </div>
      </div>
    )
  }, [activeSessionId, collapsed, onDeleteSession, onSelect, toggleCollapse, unreadCounts, t])

  const renderVirtualRow = useCallback((row: SidebarRow) => {
    if (row.kind === 'group') {
      return (
        <div className="session-group-label">
          {groupLabel(row.group, hasRuntimeSessions, t)}
        </div>
      )
    }
    if (row.kind === 'primary') {
      return renderPrimaryRow(row.node)
    }
    if (row.kind === 'child-count') {
      return (
        <button
          className="session-child-count"
          onClick={() => toggleCollapse(row.node.session.taskId)}
        >
          {t(row.node.children.length > 1 ? 'session.subTasks' : 'session.subTask', { count: row.node.children.length })}
        </button>
      )
    }
    return (
      <div className="session-children">
        <SessionItem
          session={row.child}
          isActive={row.child.taskId === activeSessionId}
          isChild
          isLast={row.childIndex === row.childCount - 1}
          unreadCount={unreadCounts?.[row.child.channelId]}
          onSelect={() => onSelect(row.child.taskId)}
          onDelete={() => onDeleteSession(row.child.taskId)}
        />
      </div>
    )
  }, [activeSessionId, hasRuntimeSessions, onDeleteSession, onSelect, renderPrimaryRow, toggleCollapse, unreadCounts, t])

  return (
    <div className="session-sidebar">
      <button className="session-new-btn" onClick={onCreateSession}>
        <IconPlus />
        <span>{t('session.newChat')}</span>
      </button>

      {onSelectSecretary && (
        <button
          className={`session-nav-btn${activeChannel === secretaryChannelId ? ' active' : ''}`}
          onClick={onSelectSecretary}
        >
          <IconShield />
          <span>{t('session.secretary')}</span>
          {!!(secretaryChannelId && unreadCounts?.[secretaryChannelId]) && unreadCounts![secretaryChannelId!] > 0 && (
            <span className="session-unread-badge">{unreadCounts![secretaryChannelId!] > 99 ? '99+' : unreadCounts![secretaryChannelId!]}</span>
          )}
        </button>
      )}

      <div className="session-search-wrap">
        <IconSearch />
        <input
          className="session-search"
          placeholder={t('session.search')}
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
      </div>

      <div ref={listRef} className={`session-list${useVirtualRows ? ' session-list-virtualized' : ''}`}>
        {useVirtualRows ? (
          <div
            style={{
              height: rowVirtualizer.getTotalSize(),
              width: '100%',
              position: 'relative',
            }}
          >
            {rowVirtualizer.getVirtualItems().map(virtualRow => (
              <div
                key={virtualRow.key}
                data-index={virtualRow.index}
                ref={rowVirtualizer.measureElement}
                style={{
                  position: 'absolute',
                  top: 0,
                  left: 0,
                  width: '100%',
                  transform: `translateY(${virtualRow.start}px)`,
                }}
              >
                {renderVirtualRow(rows[virtualRow.index])}
              </div>
            ))}
          </div>
        ) : (['Today', 'Yesterday', 'Earlier'] as const).map(group => {
          const items = grouped[group]
          if (!items || items.length === 0) return null
          return (
          <div key={group} className="session-group">
              <div className="session-group-label">{groupLabel(group, hasRuntimeSessions, t)}</div>
              {items.map(node => {
                const hasChildren = node.children.length > 0
                const isCollapsed = collapsed.has(node.session.taskId)
                return (
                  <div key={node.session.taskId} className="session-tree-node">
                    <div className="session-tree-primary">
                      {hasChildren && (
                        <button
                          className={`session-expand-btn${isCollapsed ? ' collapsed' : ''}`}
                          onClick={e => {
                            e.stopPropagation()
                            toggleCollapse(node.session.taskId)
                          }}
                          aria-label={isCollapsed ? t('session.expand') : t('session.collapse')}
                        />
                      )}
                      <SessionItem
                        session={node.session}
                        isActive={node.session.taskId === activeSessionId}
                        unreadCount={unreadCounts?.[node.session.channelId]}
                        onSelect={() => onSelect(node.session.taskId)}
                        onDelete={() => onDeleteSession(node.session.taskId)}
                      />
                    </div>

                    {hasChildren && !isCollapsed && (
                      <div className="session-children">
                        {node.children.map((child, idx) => (
                          <SessionItem
                            key={child.taskId}
                            session={child}
                            isActive={child.taskId === activeSessionId}
                            isChild
                            isLast={idx === node.children.length - 1}
                            unreadCount={unreadCounts?.[child.channelId]}
                            onSelect={() => onSelect(child.taskId)}
                            onDelete={() => onDeleteSession(child.taskId)}
                          />
                        ))}
                      </div>
                    )}

                    {hasChildren && isCollapsed && (
                      <button
                        className="session-child-count"
                        onClick={() => toggleCollapse(node.session.taskId)}
                      >
                        {t(node.children.length > 1 ? 'session.subTasks' : 'session.subTask', { count: node.children.length })}
                      </button>
                    )}
                  </div>
                )
              })}
            </div>
          )
        })}

        {filtered.length === 0 && (
          <div className="session-empty">{t('session.noSessions')}</div>
        )}
      </div>

      <button
        className={`session-nav-btn session-activity-btn${activeSessionId === null && activeChannel !== secretaryChannelId ? ' active' : ''}`}
        onClick={() => onSelect(null)}
      >
        <IconActivity />
        <span>{t('session.activity')}</span>
      </button>
    </div>
  )
}
