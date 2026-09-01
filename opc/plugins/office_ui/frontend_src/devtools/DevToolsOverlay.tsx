import { useMemo, useState, useSyncExternalStore } from 'react'
import { useI18n } from '../i18n'
import type { VisualEvent, VisualSnapshot } from '../types/visual'
import type { EventTimelineStore } from './EventTimelineStore'

function truncateJson(data: unknown, maxLen = 120): string {
  const serialized = JSON.stringify(data) ?? ''
  return serialized.length <= maxLen ? serialized : `${serialized.slice(0, maxLen)}\u2026`
}

interface EventTimelineProps {
  store: EventTimelineStore
  snapshot: VisualSnapshot | null
  eventTypeFilter: string
  onEventTypeFilterChange: (value: string) => void
}

function EventTimeline({
  store,
  snapshot,
  eventTypeFilter,
  onEventTypeFilterChange,
}: EventTimelineProps) {
  const { t } = useI18n()
  const events = useSyncExternalStore(store.subscribe, store.getSnapshot)
  const eventTypes = useMemo(
    () => ['all', ...Array.from(new Set(events.map(event => event.type)))],
    [events],
  )
  const filteredEvents = useMemo(
    () => (eventTypeFilter === 'all'
      ? events.slice().reverse()
      : events.filter(event => event.type === eventTypeFilter).reverse()),
    [eventTypeFilter, events],
  )
  const evolutionPhases = useMemo(() => {
    const recent = events.slice(-40)
    return {
      trace: recent.some(event => event.type === 'tool_start' || event.type === 'tool_done'),
      reflect: recent.some(event => event.type === 'reflect_start' || event.type === 'reflect_done'),
      synthesize: recent.some(event => event.type === 'skill_synthesized'),
    }
  }, [events])

  return (
    <>
      <div className="dev-group">
        <div className="dev-label">{t('dev.evolution')}</div>
        <div className="evo-pipeline">
          {(['Trace', 'Reflect', 'Synthesize', 'Practice', 'Lifecycle'] as const).map((phase, index) => {
            const key = phase.toLowerCase() as keyof typeof evolutionPhases
            const active = key in evolutionPhases
              ? evolutionPhases[key as 'trace' | 'reflect' | 'synthesize']
              : false
            const phaseLabelKey = `dev.phase.${key}` as Parameters<typeof t>[0]
            return (
              <div key={phase} className="evo-phase-group">
                {index > 0 && <div className="evo-connector" />}
                <div className={`evo-node ${active ? 'active' : ''}`}>
                  <div className="evo-dot" />
                  <span className="evo-label">{t(phaseLabelKey)}</span>
                </div>
              </div>
            )
          })}
        </div>
        <div className="list">
          {(snapshot?.skills.recent ?? []).slice(-6).reverse().map((item, index) => (
            <div className="list-row" key={`${item.skill_name}-${item.timestamp}-${index}`}>
              <span>{item.skill_name}</span>
              <span className="muted mono">{item.version}</span>
            </div>
          ))}
        </div>
      </div>
      <div className="dev-group">
        <div className="dev-label">
          {t('dev.events')}
          <select
            className="inline-select"
            value={eventTypeFilter}
            onChange={event => onEventTypeFilterChange(event.target.value)}
          >
            {eventTypes.map(type => <option key={type} value={type}>{type}</option>)}
          </select>
        </div>
        <div className="event-log">
          {filteredEvents.slice(0, 30).map(event => (
            <div key={event.event_id} className="log-row">
              <span className="log-time">
                {new Date(event.timestamp * 1000).toLocaleTimeString([], {
                  hour: '2-digit',
                  minute: '2-digit',
                  second: '2-digit',
                })}
              </span>
              <span className="log-type">{event.type}</span>
              <span className="log-agent">{event.agent_id}</span>
              <span className="log-data">{truncateJson(event.data)}</span>
            </div>
          ))}
        </div>
      </div>
    </>
  )
}

interface DevToolsOverlayProps {
  open: boolean
  store: EventTimelineStore
  snapshot: VisualSnapshot | null
  wsUrlInput: string
  onWsUrlInputChange: (value: string) => void
  onApplyWsUrl: () => void
  onClose: () => void
}

export function DevToolsOverlay({
  open,
  store,
  snapshot,
  wsUrlInput,
  onWsUrlInputChange,
  onApplyWsUrl,
  onClose,
}: DevToolsOverlayProps) {
  const { t } = useI18n()
  const [eventTypeFilter, setEventTypeFilter] = useState('all')
  if (!open) return null

  return (
    <div className="dev-overlay">
      <div className="dev-header">
        <span className="dev-title">{t('dev.tools')}</span>
        <button className="icon-btn" onClick={onClose}>✕</button>
      </div>
      <div className="dev-group">
        <div className="dev-label">{t('dev.connection')}</div>
        <div className="input-row">
          <input
            value={wsUrlInput}
            onChange={event => onWsUrlInputChange(event.target.value)}
            placeholder="ws://..."
          />
          <button className="send-btn" onClick={onApplyWsUrl}>↩</button>
        </div>
      </div>
      <EventTimeline
        store={store}
        snapshot={snapshot}
        eventTypeFilter={eventTypeFilter}
        onEventTypeFilterChange={setEventTypeFilter}
      />
      {Object.keys(snapshot?.channels ?? {}).length > 0 && (
        <div className="dev-group">
          <div className="dev-label">{t('dev.channels')}</div>
          {Object.entries(snapshot?.channels ?? {}).map(([name, info]) => (
            <div className="list-row" key={name}>
              <span>{name}</span>
              <span className="muted">{String((info as { last_type?: string }).last_type ?? 'idle')}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
