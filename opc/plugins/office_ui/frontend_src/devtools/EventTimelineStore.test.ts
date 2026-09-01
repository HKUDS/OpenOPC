import assert from 'node:assert/strict'
import { EventTimelineStore } from './EventTimelineStore'
import type { VisualEvent } from '../types/visual'

function event(id: string): VisualEvent {
  return {
    event_id: id,
    type: `type-${id}`,
    timestamp: Number(id),
    agent_id: 'agent',
    data: {},
  }
}

const store = new EventTimelineStore(3)
const initial = store.getSnapshot()
let notifications = 0
const unsubscribe = store.subscribe(() => { notifications += 1 })

store.replace([event('1'), event('2'), event('3'), event('4')])
assert.deepEqual(store.getSnapshot().map(item => item.event_id), ['2', '3', '4'])
assert.notEqual(store.getSnapshot(), initial, 'a published snapshot must have a new identity')

store.append(event('5'))
assert.deepEqual(store.getSnapshot().map(item => item.event_id), ['3', '4', '5'])
assert.equal(notifications, 2)

unsubscribe()
store.append(event('6'))
assert.equal(notifications, 2, 'unsubscribed views must not receive hidden timeline updates')

const singleItemStore = new EventTimelineStore(1)
singleItemStore.append(event('1'))
singleItemStore.append(event('2'))
assert.deepEqual(singleItemStore.getSnapshot().map(item => item.event_id), ['2'])

console.log('EventTimelineStore.test.ts: OK (bounded external diagnostic timeline)')
