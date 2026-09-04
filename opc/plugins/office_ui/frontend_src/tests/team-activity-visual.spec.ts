import assert from 'node:assert/strict'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { chromium } from 'playwright'
import { createServer } from 'vite'

const FRONTEND_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')

const server = await createServer({
  root: FRONTEND_ROOT,
  logLevel: 'error',
  server: { host: '127.0.0.1', port: 0, strictPort: false },
})
let browser
try {
  await server.listen()
  const address = server.httpServer?.address()
  if (!address || typeof address === 'string') throw new Error('Vite did not expose a TCP port')
  const baseUrl = `http://127.0.0.1:${address.port}/`
  browser = await chromium.launch()
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  const pageErrors: string[] = []
  page.on('pageerror', error => pageErrors.push(error.message))
  await page.goto(`${baseUrl}tests/team-activity-visual.html`)
  await page.waitForFunction(() => window.__teamActivityFixtureReady === true)

  assert.equal(await page.locator('.team-member-row:not(.team-leader-row)').count(), 3)
  assert.equal(await page.getByText('Autonomous team formed', { exact: true }).count(), 1)
  assert.equal(await page.getByText('Main collaboration', { exact: true }).count(), 1)
  assert.equal(await page.locator('.team-leader-delegation').count(), 1)
  assert.equal(
    await page.locator('.team-member-row').getByText('Senior Engineer', { exact: true }).count(),
    0,
    'covered roles must not become observed members',
  )

  await page.getByRole('button', { name: /Tasks/ }).click()
  assert.equal(await page.locator('.team-task-column').count(), 4)
  assert.equal(await page.locator('.team-task-card').count(), 4)

  await page.getByRole('button', { name: /Timeline/ }).click()
  assert.ok(await page.getByRole('button', { name: 'Highlights' }).getAttribute('class').then(value => value?.includes('active')))
  assert.equal(await page.getByText(/Status Changed/, { exact: false }).count(), 0)
  await page.getByRole('button', { name: 'Errors' }).click()
  assert.equal(await page.locator('.team-timeline-event').count(), 3)

  await page.getByRole('button', { name: /Output/ }).click()
  assert.equal(await page.locator('.team-output-card').count(), 2)

  await page.setViewportSize({ width: 600, height: 800 })
  const mobileWidth = await page.locator('.team-activity-panel').evaluate(element => element.getBoundingClientRect().width)
  assert.ok(Math.abs(mobileWidth - 600) <= 1, `mobile panel width=${mobileWidth}`)

  await page.setViewportSize({ width: 1440, height: 900 })
  await page.getByRole('button', { name: /Overview/ }).click()
  const screenshotPath = process.env.TEAM_ACTIVITY_SCREENSHOT
  if (screenshotPath) await page.screenshot({ path: screenshotPath })
  assert.deepEqual(pageErrors, [])
  console.log('team-activity-visual.spec.ts: OK')
} finally {
  await browser?.close()
  await server.close()
}
