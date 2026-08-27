/**
 * Locks in: frontend's PHASE_TO_COLUMN matches backend's
 * ``opc/presentation/kanban.py:STATUS_TO_COLUMN`` and
 * ``opc/layer2_organization/phase.py:_PHASE_TO_COLUMN``.
 *
 * If the backend ever adds, removes, or renames a phase / column, this
 * test surfaces the drift immediately — preventing a class of silent
 * UI bug where a card ends up in the wrong column because the frontend
 * projection fell out of sync with the backend.
 */
import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { KanbanPhase } from '../types/kanban'
import { PHASE_TO_COLUMN, deriveColumnFromPhase } from './phaseHelpers'

const ALL_PHASES: KanbanPhase[] = [
  'queued', 'ready', 'ready_for_rework', 'waiting_dependencies',
  'running', 'waiting_for_peer', 'waiting_for_children', 'paused', 'needs_attention',
  'awaiting_manager_review', 'awaiting_human',
  'approved', 'failed', 'cancelled',
]

describe('PHASE_TO_COLUMN', () => {
  it('covers every KanbanPhase exactly once', () => {
    assert.deepEqual(Object.keys(PHASE_TO_COLUMN).sort(), [...ALL_PHASES].sort())
  })

  it('projects TODO-family phases to "todo"', () => {
    for (const p of ['queued', 'ready', 'ready_for_rework', 'waiting_dependencies'] as KanbanPhase[]) {
      assert.equal(PHASE_TO_COLUMN[p], 'todo')
    }
  })

  it('projects IN-PROGRESS-family phases to "in-progress"', () => {
    for (const p of ['running', 'waiting_for_peer', 'waiting_for_children', 'paused', 'needs_attention'] as KanbanPhase[]) {
      assert.equal(PHASE_TO_COLUMN[p], 'in-progress')
    }
  })

  it('projects IN-REVIEW-family phases to "in-review"', () => {
    for (const p of ['awaiting_manager_review', 'awaiting_human'] as KanbanPhase[]) {
      assert.equal(PHASE_TO_COLUMN[p], 'in-review')
    }
  })

  it('projects terminal phases to "done"', () => {
    for (const p of ['approved', 'failed', 'cancelled'] as KanbanPhase[]) {
      assert.equal(PHASE_TO_COLUMN[p], 'done')
    }
  })
})

describe('deriveColumnFromPhase', () => {
  it('returns "todo" for undefined / null phase', () => {
    assert.equal(deriveColumnFromPhase(undefined), 'todo')
    assert.equal(deriveColumnFromPhase(null), 'todo')
  })

  it('projects each phase using the same table', () => {
    for (const p of ALL_PHASES) {
      assert.equal(deriveColumnFromPhase(p), PHASE_TO_COLUMN[p])
    }
  })
})
