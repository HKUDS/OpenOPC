import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const officeSource = readFileSync(join(here, 'OfficePage.tsx'), 'utf8')
const appSource = readFileSync(join(here, '..', 'App.tsx'), 'utf8')

assert.doesNotMatch(
  appSource,
  /const \[uiTick, setUiTick\]|const \[events, setEvents\]/,
  'high-frequency Office and diagnostic revisions must not live in App',
)
assert.match(appSource, /<OfficePage[\s\S]*?active=\{activePage === 'office'\}/)
assert.match(appSource, /eventTimeline\.append\(evt\)/)

for (const eventName of ['eventApplied', 'snapshotApplied', 'officeChanged']) {
  assert.match(
    officeSource,
    new RegExp(`bridge\\.on\\('${eventName}', schedule\\)`),
    `OfficePage must observe ${eventName} locally`,
  )
}
assert.match(
  officeSource,
  /if \(!activeRef\.current \|\| timer !== null\) return/,
  'hidden Office pages must not publish cosmetic revisions',
)
assert.match(officeSource, /export const OfficePage = memo\(/, 'unrelated App renders must stop at the OfficePage boundary')

console.log('OfficePage.test.ts: OK (Office refresh is isolated from App)')
