import Phaser from 'phaser'
import type { OfficeScene } from './scenes/OfficeScene'
import type { VisualEvent, VisualSnapshot } from '../types/visual'
import { getOffices, type OfficeConfig } from './map/OfficeStore'
import { AgentState } from './types'

export class GameBridge extends Phaser.Events.EventEmitter {
  private scene: OfficeScene | null = null
  private eventQueue: VisualEvent[] = []
  private snapshotQueue: VisualSnapshot[] = []

  constructor() {
    super()
  }

  override on(event: string | symbol, fn: (...args: any[]) => void, context?: any): this {
    return super.on(event, fn, context)
  }

  override off(event: string | symbol, fn?: (...args: any[]) => void, context?: any, once?: boolean): this {
    return super.off(event, fn, context, once)
  }

  setScene(scene: OfficeScene) {
    this.scene = scene

    for (const snap of this.snapshotQueue) {
      this.applySnapshot(snap)
    }
    this.snapshotQueue = []

    for (const evt of this.eventQueue) {
      this.applyEvent(evt)
    }
    this.eventQueue = []
  }

  getScene(): OfficeScene | null {
    return this.scene
  }

  // ── Called from React side ────────────────────────────

  pushEvent(evt: VisualEvent) {
    if (!this.scene) {
      this.eventQueue.push(evt)
      if (this.eventQueue.length > 500) this.eventQueue.shift()
      return
    }
    this.applyEvent(evt)
  }

  pushSnapshot(snapshot: VisualSnapshot) {
    const agentCount = Object.keys(snapshot.agents ?? {}).length
    if (!this.scene) {
      console.log(`[GameBridge] pushSnapshot queued (scene not ready) — ${agentCount} agents`)
      // A snapshot fully resets the scene, so it supersedes anything queued
      // before it. The game may not be created until the Office page is first
      // opened — keep the queues bounded in the meantime.
      this.snapshotQueue = [snapshot]
      this.eventQueue = []
      return
    }
    console.log(`[GameBridge] pushSnapshot applying now — ${agentCount} agents`)
    this.applySnapshot(snapshot)
  }

  sendToSeat(agentId: string) {
    if (!this.scene) return
    this.scene.behavior.sendToSeat(
      this.scene.ensureAgent(agentId),
    )
  }

  setAgentActive(agentId: string, active: boolean) {
    if (!this.scene) return
    const agent = this.scene.getAgent(agentId)
    if (agent) agent.isActive = active
  }

  setAgentBubble(agentId: string, text: string | null) {
    if (!this.scene) return
    const agent = this.scene.getAgent(agentId)
    if (!agent) return
    if (text) agent.showBubble(text)
    else agent.clearBubble()
  }

  ensureAgent(agentId: string, displayName?: string, officeId?: string, palette?: number, deskId?: string) {
    if (!this.scene) return
    this.scene.ensureAgent(agentId, displayName, false, null, officeId, palette, deskId)
  }

  getCharacterCards() {
    if (!this.scene) return []
    return this.scene.getCharacterCards()
  }

  // ── Office management API ─────────────────────────────

  getOffices(): OfficeConfig[] {
    return getOffices()
  }

  renameOffice(officeId: string, newName: string) {
    if (!this.scene) return
    this.scene.renameOffice(officeId, newName)
    this.emit('officeChanged')
  }

  assignAgentToOffice(agentId: string, officeId: string) {
    if (!this.scene) return
    this.scene.reassignAgent(agentId, officeId)
    this.emit('officeChanged')
  }

  panToOffice(officeId: string) {
    if (!this.scene) return
    this.scene.panToOffice(officeId)
  }

  resetCamera() {
    if (!this.scene) return
    this.scene.resetCameraView()
  }

  /** Re-read `isLocalDaytime()` (URL + localStorage) and refresh skyline / grass if it changed. */
  syncOutdoorLighting() {
    if (!this.scene) return
    this.scene.syncOutdoorLighting()
  }

  rebuildOfficeCollision(officeId: string, mapStr: string[], seats: [number, number][]) {
    if (!this.scene) return
    this.scene.rebuildOfficeCollision(officeId, mapStr, seats)
  }

  getSeatsForOffice(officeId: string): Array<{ id: string; assigned: boolean; assignedTo: string | null }> {
    if (!this.scene) return []
    return this.scene.seats
      .filter(s => s.id.startsWith(`${officeId}-desk-`) || s.id.startsWith(`${officeId}-leader-`))
      .map(s => ({ id: s.id, assigned: s.assigned, assignedTo: s.assignedTo }))
  }

  changeAgentSeat(agentId: string, seatId: string) {
    if (!this.scene) return
    this.scene.changeAgentSeat(agentId, seatId)
    this.emit('officeChanged')
  }

  // ── Chat + Kanban game integration ──────────────────────

  notifyChannelMessage(agentIds: string[], text: string) {
    if (!this.scene) return
    for (const id of agentIds) {
      const agent = this.scene.getAgent(id)
      if (agent) {
        agent.showBubble(text.slice(0, 30))
        setTimeout(() => agent.clearBubble(), 4000)
      }
    }
  }

  triggerCrossOfficeMeeting(agentIds: string[]) {
    if (!this.scene) return
    for (const id of agentIds) {
      const agent = this.scene.getAgent(id)
      if (agent) {
        this.scene.behavior.moveToZone(agent, 'meetingRoom')
      }
    }
  }

  triggerCelebration(agentId: string) {
    if (!this.scene) return
    const agent = this.scene.getAgent(agentId)
    if (agent) {
      agent.showBubble('🎉 Done!')
      setTimeout(() => agent.clearBubble(), 3000)
    }
  }

  // ── Internal ──────────────────────────────────────────

  private applyEvent(evt: VisualEvent) {
    if (!this.scene) return
    this.scene.behavior.applyEvent(evt)
    this.emit('eventApplied', evt)
  }

  private applySnapshot(snapshot: VisualSnapshot) {
    if (!this.scene) {
      console.warn('[GameBridge] applySnapshot called but scene is null')
      return
    }

    // Snapshot old agents to array first (avoid mutating Map during iteration)
    const oldIds = Array.from(this.scene.agents.keys())
    console.log('[GameBridge] applySnapshot — clearing', oldIds.length, 'old agents:', oldIds)
    for (const id of oldIds) {
      this.scene.removeAgent(id)
    }

    const timeline = snapshot.timeline ?? []
    for (const evt of timeline) {
      this.scene.behavior.applyEvent(evt)
    }

    const agentEntries = Object.entries(snapshot.agents ?? {})
    console.log('[GameBridge] applySnapshot — adding', agentEntries.length, 'agents:', agentEntries.map(([id]) => id))
    for (const [id, info] of agentEntries) {
      const agentData = info as {
        name?: string; role_name?: string; office_id?: string
        status?: string; runtime_status?: string; current_tool?: string | null
        appearance?: { palette?: number; hue_shift?: number; seat_zone?: string; desk_id?: string }
      }
      const name = agentData.name || agentData.role_name || id
      const officeId = agentData.office_id
      const palette = agentData.appearance?.palette
      const deskId = agentData.appearance?.desk_id
      try {
        const agent = this.scene.ensureAgent(id, name, false, null, officeId, palette, deskId)
        const runtimeStatus = agentData.runtime_status || agentData.status
        if (runtimeStatus === 'tool_active') {
          agent.currentTool = agentData.current_tool ?? null
          agent.isActive = true
          agent.setAgentState(AgentState.TYPE)
        } else if (runtimeStatus === 'reflecting') {
          agent.currentTool = agentData.current_tool ?? 'Reflect'
          agent.isActive = false
          agent.setAgentState(AgentState.REFLECT)
        }
        console.log(`[GameBridge]   ✓ ensured ${id} in ${officeId} palette=${palette}`)
      } catch (err) {
        console.error(`[GameBridge]   ✗ ensureAgent failed for ${id}:`, err)
      }
    }

    if (timeline.length === 0 && agentEntries.length === 0) {
      console.warn('[GameBridge] applySnapshot — empty snapshot, creating fallback agent')
      this.scene.ensureAgent('openopc-main', 'OpenOPC')
    }

    console.log('[GameBridge] applySnapshot done — total agents:', this.scene.agents.size)
    this.emit('snapshotApplied', snapshot)
  }
}
