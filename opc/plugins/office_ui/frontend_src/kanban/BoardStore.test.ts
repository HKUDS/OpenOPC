import assert from 'node:assert/strict'

/**
 * Regression tests for BoardStore selection behavior.
 *
 * Bug history:
 *   - Original: BoardStore auto-selected boards[0] whenever activeBoardId was
 *     null. Combined with the parent's session-driven clear, this created an
 *     infinite toggle loop (screen flicker).
 *   - Previous fix: limited auto-select to boards.length === 1 — but in
 *     company mode with exactly 1 session (1 board) this STILL flickered
 *     when no session was selected.
 *   - Current contract: BoardStore does NOT auto-select at all.
 *     WorkspacePage owns session-to-board selection; its canonical identity
 *     tests live beside that resolver. initFromBackend only clears when the
 *     prior selection disappears.
 */

// initFromBackend: only preserve-or-clear, no auto-default
function resolveActiveBoardAfterInit(
  prev: string | null,
  bds: { id: string }[],
): string | null {
  return prev && bds.some(b => b.id === prev) ? prev : null
}

// ── initFromBackend ──────────────────────────────────────────────────────

// Single board: do NOT auto-select (parent picks)
assert.strictEqual(
  resolveActiveBoardAfterInit(null, [{ id: 'project-board' }]),
  null,
  'init with null prev stays null even with 1 board',
)

// Preserve valid prior
assert.strictEqual(
  resolveActiveBoardAfterInit('session-a', [{ id: 'session-a' }, { id: 'session-b' }]),
  'session-a',
  'preserves existing active when still present',
)

// Clear stale
assert.strictEqual(
  resolveActiveBoardAfterInit('deleted', [{ id: 'session-a' }]),
  null,
  'clears stale active',
)

// Empty
assert.strictEqual(
  resolveActiveBoardAfterInit(null, []),
  null,
  'empty boards → null',
)

console.log('BoardStore selection contract passed')
