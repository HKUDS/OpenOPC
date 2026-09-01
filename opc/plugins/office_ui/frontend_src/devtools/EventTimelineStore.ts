import type { VisualEvent } from '../types/visual'

/**
 * React-independent bounded event timeline.
 *
 * WebSocket traffic writes here without touching the App component.  The
 * developer overlay subscribes only while its event view is mounted, so a
 * token stream cannot invalidate the normal Workspace/Office page tree just
 * to maintain hidden diagnostic history.
 */
export class EventTimelineStore {
  private events: readonly VisualEvent[] = []
  private readonly listeners = new Set<() => void>()
  private readonly maxItems: number

  constructor(maxItems = 80) {
    this.maxItems = Math.max(1, Math.floor(maxItems))
  }

  getSnapshot = (): readonly VisualEvent[] => this.events

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => { this.listeners.delete(listener) }
  }

  replace(events: readonly VisualEvent[]): void {
    this.publish(events.slice(-this.maxItems))
  }

  append(event: VisualEvent): void {
    this.publish([...this.events, event].slice(-this.maxItems))
  }

  private publish(events: readonly VisualEvent[]): void {
    this.events = events
    for (const listener of [...this.listeners]) listener()
  }
}
