import assert from 'node:assert/strict'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { chromium, type Browser, type Page } from 'playwright'
import { createServer, type ViteDevServer } from 'vite'

// Run from frontend_src:
//   node --import tsx ./tests/message-list-scroll.spec.ts

const __dirname = dirname(fileURLToPath(import.meta.url))
const FRONTEND_ROOT = resolve(__dirname, '..')

interface ScrollMetrics {
  scrollTop: number
  scrollHeight: number
  clientHeight: number
  bottomGap: number
}

interface FixtureTelemetry {
  markReadCalls: number
  renders: number
  scrollEvents: number
  scrollTopWrites: number
}

async function settle(page: Page, milliseconds = 80): Promise<void> {
  await page.evaluate(() => new Promise<void>((resolveFrame) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolveFrame()))
  }))
  await page.waitForTimeout(milliseconds)
}

async function metrics(page: Page): Promise<ScrollMetrics> {
  return page.locator('.msg-list').evaluate((element) => {
    const list = element as HTMLElement
    return {
      scrollTop: list.scrollTop,
      scrollHeight: list.scrollHeight,
      clientHeight: list.clientHeight,
      bottomGap: list.scrollHeight - list.clientHeight - list.scrollTop,
    }
  })
}

async function gotoFixture(
  page: Page,
  baseUrl: string,
  policy: string,
  extraQuery = '',
): Promise<void> {
  const suffix = extraQuery ? `&${extraQuery}` : ''
  await page.goto(`${baseUrl}tests/message-list-scroll.html?policy=${policy}${suffix}`)
  await page.waitForFunction(() => window.__messageListFixtureReady === true)
  await page.waitForSelector('.msg-list')
  await settle(page, 150)
}

async function resetTelemetry(page: Page): Promise<void> {
  await page.evaluate(() => window.__messageListFixture?.resetTelemetry())
}

async function telemetry(page: Page): Promise<FixtureTelemetry> {
  return page.evaluate(() => {
    const value = window.__messageListFixture?.telemetry()
    if (!value) throw new Error('fixture telemetry is unavailable')
    return value
  })
}

async function appendMessages(page: Page, count: number): Promise<void> {
  await page.evaluate((amount) => window.__messageListFixture?.appendMessages(amount), count)
  await page.waitForSelector(`text=fixture-marker-${String(225 + count - 1).padStart(4, '0')}`)
  await settle(page)
}

async function firstFullyVisibleMarker(page: Page): Promise<{ marker: string; top: number }> {
  return page.locator('.msg-list').evaluate((element) => {
    const list = element as HTMLElement
    const listRect = list.getBoundingClientRect()
    for (const row of Array.from(list.querySelectorAll<HTMLElement>('.msg-row'))) {
      const rect = row.getBoundingClientRect()
      const markerMatch = /fixture-marker-(\d+)/.exec(row.textContent || '')
      const marker = markerMatch?.[0]
      if (markerMatch && Number(markerMatch[1]) % 5 === 0) continue
      if (marker && rect.top >= listRect.top + 1 && rect.bottom <= listRect.bottom - 1) {
        return { marker, top: rect.top }
      }
    }
    throw new Error('no fully visible fixture row found')
  })
}

async function markerTop(page: Page, marker: string): Promise<number> {
  return page.locator('.msg-list').evaluate((element, expectedMarker) => {
    const row = Array.from(element.querySelectorAll<HTMLElement>('.msg-row'))
      .find((candidate) => candidate.textContent?.includes(expectedMarker))
    if (!row) throw new Error(`marker row not found: ${expectedMarker}`)
    return row.getBoundingClientRect().top
  }, marker)
}

async function markerViewportOffset(page: Page, marker: string): Promise<number> {
  return page.locator('.msg-list').evaluate((element, expectedMarker) => {
    const list = element as HTMLElement
    const row = Array.from(list.querySelectorAll<HTMLElement>('.msg-row'))
      .find((candidate) => candidate.textContent?.includes(expectedMarker))
    if (!row) throw new Error(`marker row not found: ${expectedMarker}`)
    return row.getBoundingClientRect().top - list.getBoundingClientRect().top
  }, marker)
}

async function nearestMarkerAbove(page: Page, anchorMarker: string): Promise<{ marker: string; index: number }> {
  return page.locator('.msg-list').evaluate((element, expectedAnchor) => {
    const rows = Array.from(element.querySelectorAll<HTMLElement>('.msg-row'))
    const anchor = rows.find((row) => row.textContent?.includes(expectedAnchor))
    if (!anchor) throw new Error(`anchor row not found: ${expectedAnchor}`)
    const anchorTop = anchor.getBoundingClientRect().top
    for (let index = rows.length - 1; index >= 0; index -= 1) {
      const row = rows[index]
      if (row.getBoundingClientRect().bottom > anchorTop - 1) continue
      const marker = /fixture-marker-(\d+)/.exec(row.textContent || '')
      if (marker) return { marker: marker[0], index: Number(marker[1]) }
    }
    throw new Error('no rendered fixture row exists above the viewport anchor')
  }, anchorMarker)
}

async function clickHistoryWithoutScrolling(page: Page): Promise<void> {
  await page.locator('.msg-history-load-btn').evaluate((element) => {
    ;(element as HTMLButtonElement).click()
  })
}

async function runFollowAndBrowsingCases(page: Page, baseUrl: string): Promise<void> {
  await gotoFixture(page, baseUrl, 'follow')

  const initial = await metrics(page)
  assert.ok(initial.scrollHeight > initial.clientHeight * 8, 'fixture must exercise a long, 200+ row transcript')
  assert.ok(Math.abs(initial.bottomGap) <= 2, `follow policy should initially settle at bottom; gap=${initial.bottomGap}`)

  // Regression for scrollToEnd -> onMarkRead -> state -> changed callback ->
  // layout effect -> scrollToEnd. Once settled, an idle transcript must not
  // keep invoking markRead, re-rendering itself, or even assigning scrollTop.
  await resetTelemetry(page)
  await page.waitForTimeout(2_000)
  const idle = await telemetry(page)
  assert.equal(idle.markReadCalls, 0, 'idle bottom transcript must not run a markRead feedback loop')
  assert.equal(idle.renders, 0, 'idle bottom transcript must not re-render from markRead feedback')
  assert.equal(idle.scrollEvents, 0, 'idle bottom transcript must not keep writing its scroll position')
  assert.equal(idle.scrollTopWrites, 0, 'idle bottom transcript must perform zero direct scrollTop writes for two seconds')

  await resetTelemetry(page)
  await appendMessages(page, 5)
  const afterAppend = await metrics(page)
  const followTelemetry = await telemetry(page)
  assert.ok(Math.abs(afterAppend.bottomGap) <= 2, `follow policy must stay at bottom after append; gap=${afterAppend.bottomGap}`)
  assert.ok(followTelemetry.markReadCalls <= 1, `one append batch may mark read at most once; calls=${followTelemetry.markReadCalls}`)
  assert.ok(followTelemetry.scrollEvents <= 1, `one append batch may scroll at most once; events=${followTelemetry.scrollEvents}`)
  assert.ok(followTelemetry.scrollTopWrites <= 1, `one append batch may assign scrollTop at most once; writes=${followTelemetry.scrollTopWrites}`)

  await page.evaluate(() => window.__messageListFixture?.growDraft(4_000))
  await page.waitForSelector('.msg-row-draft')
  await settle(page, 150)
  const afterDraftGrowth = await metrics(page)
  assert.ok(
    Math.abs(afterDraftGrowth.bottomGap) <= 2,
    `follow policy must absorb a large live-reply height change; gap=${afterDraftGrowth.bottomGap}`,
  )

  // Use a real browser wheel input, not a synthetic scrollTop-only change, to
  // transition from FOLLOWING to BROWSING.
  const list = page.locator('.msg-list')
  await list.hover()
  await page.mouse.wheel(0, -1_100)
  await settle(page)
  const detached = await metrics(page)
  assert.ok(detached.bottomGap > 400, `wheel-up must detach from bottom immediately; gap=${detached.bottomGap}`)
  const anchorBefore = await firstFullyVisibleMarker(page)

  await resetTelemetry(page)
  await page.evaluate(() => {
    window.__messageListFixture?.appendMessages(15)
    window.__messageListFixture?.growDraft(5_000)
  })
  await page.waitForSelector('text=fixture-marker-0244')
  await settle(page, 150)
  const anchorAfterTop = await markerTop(page, anchorBefore.marker)
  const browsingTelemetry = await telemetry(page)
  assert.ok(
    Math.abs(anchorAfterTop - anchorBefore.top) <= 1,
    `browsing anchor moved by ${anchorAfterTop - anchorBefore.top}px during append storm`,
  )
  assert.equal(browsingTelemetry.markReadCalls, 0, 'browsing updates must not mark the detached transcript read')
  assert.equal(browsingTelemetry.scrollEvents, 0, 'browsing updates must not write the transcript scroll position')

  // A Markdown row which grows above the anchor is the same geometry change
  // produced by an image load or an expanded approval card. Native anchoring
  // plus the single controller must hold the visible row exactly in place.
  const rowAbove = await nearestMarkerAbove(page, anchorBefore.marker)
  await page.evaluate(({ index }) => window.__messageListFixture?.growMessage(index, 6_000), rowAbove)
  await settle(page, 150)
  const anchorAfterHeightChange = await markerTop(page, anchorBefore.marker)
  const afterHeightChange = await metrics(page)
  assert.ok(
    Math.abs(anchorAfterHeightChange - anchorBefore.top) <= 1,
    `browsing anchor moved by ${anchorAfterHeightChange - anchorBefore.top}px when ${rowAbove.marker} grew above it`,
  )
  assert.ok(afterHeightChange.bottomGap > 400, 'height growth above a browsing anchor must not pull the transcript to bottom')
  assert.equal(
    await page.locator('.msg-list').getAttribute('data-viewport-mode'),
    'browsing',
    'height growth must retain browsing mode',
  )

  // Result surfaces can be replaced by a higher-priority durable projection
  // and full-sync payloads rebuild every message object. Neither operation may
  // give JavaScript permission to write the browsing scroll position.
  await resetTelemetry(page)
  await page.locator('.msg-list').evaluate((element, marker) => {
    const row = Array.from(element.querySelectorAll<HTMLElement>('.msg-timeline-row'))
      .find(candidate => candidate.textContent?.includes(marker))
    if (!row) throw new Error(`result anchor row is missing: ${marker}`)
    row.dataset.identityProbe = 'stable-result-row'
  }, anchorBefore.marker)
  const anchorIndex = Number(anchorBefore.marker.slice('fixture-marker-'.length))
  await page.evaluate((index) => window.__messageListFixture?.upgradeResultSurface(index), anchorIndex)
  await settle(page, 120)
  const resultReplacement = await page.locator('.msg-list').evaluate((element, marker) => {
    const row = Array.from(element.querySelectorAll<HTMLElement>('.msg-timeline-row'))
      .find(candidate => candidate.textContent?.includes(marker))
    if (!row) throw new Error(`replaced result anchor row is missing: ${marker}`)
    return {
      top: row.getBoundingClientRect().top,
      probe: row.dataset.identityProbe,
      key: row.dataset.timelineKey,
    }
  }, anchorBefore.marker)
  assert.ok(Math.abs(resultReplacement.top - anchorBefore.top) <= 1, 'result-surface replacement must preserve the browsing anchor')
  assert.equal(resultReplacement.probe, 'stable-result-row', 'result-surface replacement must reuse the anchored DOM node')
  assert.equal(resultReplacement.key, `turn:assistant:fixture-turn-${anchorIndex}`, 'result replacement must retain the first surface timeline key')
  await page.evaluate(() => window.__messageListFixture?.repeatFullSync())
  await settle(page, 120)
  assert.ok(
    Math.abs(await markerTop(page, anchorBefore.marker) - anchorBefore.top) <= 1,
    'repeated full sync must preserve the browsing anchor',
  )
  const syncTelemetry = await telemetry(page)
  assert.equal(syncTelemetry.scrollTopWrites, 0, 'result upgrade and repeated full sync must not directly write browsing scrollTop')

  // Resizing the panel changes the viewport rather than the message content.
  // The controller restores the same row offset through its sole observer.
  const offsetBeforeResize = await markerViewportOffset(page, anchorBefore.marker)
  await page.setViewportSize({ width: 1280, height: 620 })
  await settle(page, 150)
  const offsetAfterResize = await markerViewportOffset(page, anchorBefore.marker)
  assert.ok(
    Math.abs(offsetAfterResize - offsetBeforeResize) <= 1,
    `panel resize moved the browsing anchor by ${offsetAfterResize - offsetBeforeResize}px`,
  )
  await page.setViewportSize({ width: 1280, height: 800 })
  await settle(page, 150)

  // First expose the 25 locally-windowed rows, then exercise the real remote
  // history callback which prepends 40 rows. DOM clicks avoid Playwright
  // scrolling the history button into view before the assertion.
  await clickHistoryWithoutScrolling(page)
  await settle(page, 120)
  const anchorAfterLocalHistory = await markerTop(page, anchorBefore.marker)
  assert.ok(
    Math.abs(anchorAfterLocalHistory - anchorBefore.top) <= 1,
    `browsing anchor moved by ${anchorAfterLocalHistory - anchorBefore.top}px when the local window expanded`,
  )
  await clickHistoryWithoutScrolling(page)
  await page.waitForSelector('text=history-marker-00-039')
  await settle(page, 150)
  const anchorAfterPrepend = await markerTop(page, anchorBefore.marker)
  const afterPrepend = await metrics(page)
  assert.ok(
    Math.abs(anchorAfterPrepend - anchorBefore.top) <= 1,
    `browsing anchor moved by ${anchorAfterPrepend - anchorBefore.top}px after history prepend`,
  )
  assert.ok(afterPrepend.bottomGap > 400, 'history prepend must not pull a browsing transcript to bottom')
  assert.equal(
    await page.locator('.msg-list').getAttribute('data-viewport-mode'),
    'browsing',
    'history prepend must retain browsing mode',
  )

  const jumpToLatest = page.getByRole('button', { name: /(?:latest|最新)/i })
  await jumpToLatest.waitFor({ state: 'visible' })
  await jumpToLatest.click()
  await settle(page)
  const resumed = await metrics(page)
  assert.ok(Math.abs(resumed.bottomGap) <= 2, `jump-to-latest must resume follow mode; gap=${resumed.bottomGap}`)
}

async function runInputIntentCases(page: Page, baseUrl: string): Promise<void> {
  const list = page.locator('.msg-list')

  await gotoFixture(page, baseUrl, 'follow')
  await list.focus()
  await page.keyboard.press('PageUp')
  await settle(page)
  assert.equal(await list.getAttribute('data-viewport-mode'), 'browsing', 'PageUp must immediately enter browsing mode')
  assert.ok((await metrics(page)).bottomGap > 100, 'PageUp must detach the transcript from the tail')

  await gotoFixture(page, baseUrl, 'follow')
  const listBox = await list.boundingBox()
  if (!listBox) throw new Error('scrollbar fixture has no bounding box')
  const beforeDrag = await metrics(page)
  const scrollbarWidth = await list.evaluate((element) => {
    const viewport = element as HTMLElement
    return viewport.offsetWidth - viewport.clientWidth
  })
  const scrollbarX = listBox.x + listBox.width - Math.max(2, scrollbarWidth / 2)
  const scrollbarTrackY = listBox.y + beforeDrag.clientHeight / 2
  await page.mouse.click(scrollbarX, scrollbarTrackY)
  await settle(page)
  const afterDrag = await metrics(page)
  assert.equal(
    await list.getAttribute('data-viewport-mode'),
    'browsing',
    `scrollbar interaction must immediately enter browsing mode; width=${scrollbarWidth} before=${JSON.stringify(beforeDrag)} after=${JSON.stringify(afterDrag)}`,
  )
  assert.ok(afterDrag.bottomGap > 100, 'scrollbar interaction must detach the transcript from the tail')

  // Some assistive/native scroll paths expose only the resulting scroll
  // event. A downward move while already browsing must replace the stored
  // anchor so a later resize cannot restore an older viewport position.
  await gotoFixture(page, baseUrl, 'follow')
  await list.hover()
  await page.mouse.wheel(0, -2_400)
  await settle(page)
  await list.evaluate((element) => {
    const viewport = element as HTMLElement
    viewport.scrollTop += 1_000
  })
  await settle(page)
  const nativeDownAnchor = await firstFullyVisibleMarker(page)
  const nativeDownOffset = await markerViewportOffset(page, nativeDownAnchor.marker)
  await page.setViewportSize({ width: 1280, height: 650 })
  await settle(page, 150)
  assert.ok(
    Math.abs(await markerViewportOffset(page, nativeDownAnchor.marker) - nativeDownOffset) <= 1,
    'untagged downward browsing scroll must become the anchor used by a later viewport resize',
  )
  await page.setViewportSize({ width: 1280, height: 800 })
  await settle(page, 120)
}

async function runTouchIntentCase(browser: Browser, baseUrl: string): Promise<void> {
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, hasTouch: true })
  try {
    await gotoFixture(page, baseUrl, 'follow')
    const list = page.locator('.msg-list')
    const box = await list.boundingBox()
    if (!box) throw new Error('touch fixture has no bounding box')
    const cdp = await page.context().newCDPSession(page)
    const x = box.x + box.width / 2
    const startY = box.y + box.height * 0.45
    await cdp.send('Input.dispatchTouchEvent', {
      type: 'touchStart',
      touchPoints: [{ x, y: startY }],
    })
    for (const delta of [35, 75, 120, 170]) {
      await cdp.send('Input.dispatchTouchEvent', {
        type: 'touchMove',
        touchPoints: [{ x, y: startY + delta }],
      })
    }
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] })
    await settle(page, 150)
    assert.equal(await list.getAttribute('data-viewport-mode'), 'browsing', 'native touch swipe must enter browsing mode')
    assert.ok((await metrics(page)).bottomGap > 100, 'native touch swipe must detach the transcript from the tail')
  } finally {
    await page.close()
  }
}

async function runTimelineIdentityCases(page: Page, baseUrl: string): Promise<void> {
  await gotoFixture(page, baseUrl, 'follow')
  await page.evaluate(() => window.__messageListFixture?.addSharedTurnCompanyRows([4]))
  await page.waitForSelector('text=shared-company-row-4')
  await settle(page)
  await page.locator('.msg-list').evaluate((element) => {
    const row = Array.from(element.querySelectorAll<HTMLElement>('.msg-timeline-row'))
      .find(candidate => candidate.textContent?.includes('shared-company-row-4'))
    if (!row) throw new Error('mounted shared-turn owner is missing')
    row.dataset.identityProbe = 'shared-turn-owner'
  })
  await page.evaluate(() => window.__messageListFixture?.addSharedTurnCompanyRows([0, 1, 2, 3]))
  await page.waitForSelector('text=shared-company-row-3')
  await settle(page)
  const sharedTurnKeys = await page.evaluate(() => [0, 1, 2, 3, 4].map((index) => {
    const row = Array.from(document.querySelectorAll<HTMLElement>('.msg-timeline-row'))
      .find((candidate) => candidate.textContent?.includes(`shared-company-row-${index}`))
    if (!row?.dataset.timelineKey) throw new Error(`shared company row ${index} is missing a timeline key`)
    return row.dataset.timelineKey
  }))
  assert.equal(new Set(sharedTurnKeys).size, 5, `company rows sharing a canonical turn need unique DOM keys: ${sharedTurnKeys.join(', ')}`)
  assert.equal(
    await page.locator('.msg-list').evaluate((element) => {
      const row = Array.from(element.querySelectorAll<HTMLElement>('.msg-timeline-row'))
        .find(candidate => candidate.textContent?.includes('shared-company-row-4'))
      return row?.dataset.identityProbe
    }),
    'shared-turn-owner',
    'inserting an older duplicate-turn row must not remount the existing owner',
  )

  await gotoFixture(page, baseUrl, 'follow')
  const resultMarker = 'fixture-marker-0211'
  const initialResultIdentity = await page.locator('.msg-list').evaluate((element, marker) => {
    const row = Array.from(element.querySelectorAll<HTMLElement>('.msg-timeline-row'))
      .find(candidate => candidate.textContent?.includes(marker))
    if (!row) throw new Error('cross-channel result fixture row is missing')
    row.dataset.identityProbe = 'cross-channel-result-owner'
    return { key: row.dataset.timelineKey }
  }, resultMarker)
  await page.evaluate(() => window.__messageListFixture?.mergeResultGroupOrder(211, true))
  await settle(page, 120)
  await page.evaluate(() => window.__messageListFixture?.mergeResultGroupOrder(211, false))
  await settle(page, 120)
  const reorderedResult = await page.locator('.msg-list').evaluate((element, marker) => {
    const row = Array.from(element.querySelectorAll<HTMLElement>('.msg-timeline-row'))
      .find(candidate => candidate.textContent?.includes(marker))
    if (!row) throw new Error('reordered cross-channel result row is missing')
    return { key: row.dataset.timelineKey, probe: row.dataset.identityProbe }
  }, resultMarker)
  assert.equal(reorderedResult.key, initialResultIdentity.key, 'cross-channel result group order must not change its mounted timeline key')
  assert.equal(reorderedResult.probe, 'cross-channel-result-owner', 'cross-channel result group order must reuse the mounted DOM row')

  await gotoFixture(page, baseUrl, 'follow')
  await page.evaluate(() => window.__messageListFixture?.growDraft(9_000))
  await page.waitForSelector('.msg-row-draft')
  await settle(page)
  const draftGeometry = await page.evaluate(() => {
    const list = document.querySelector<HTMLElement>('.msg-list')
    window.__rememberedDraftTimelineRow = document.querySelector(
      '[data-timeline-key="turn:assistant:fixture-live-turn"]',
    )
    const row = window.__rememberedDraftTimelineRow as HTMLElement | null
    if (!list || !row) throw new Error('live draft timeline wrapper is missing')
    list.scrollTop = Math.max(0, row.offsetTop + row.offsetHeight / 2 - list.clientHeight / 2)
    return { height: row.getBoundingClientRect().height }
  })
  await settle(page, 120)
  assert.equal(
    await page.locator('.msg-list').getAttribute('data-viewport-mode'),
    'browsing',
    'moving into the middle of a long draft must enter browsing mode',
  )
  const draftScrollTop = (await metrics(page)).scrollTop
  await resetTelemetry(page)
  await page.evaluate(() => window.__messageListFixture?.finalizeDraft())
  await page.waitForFunction(() => {
    const row = document.querySelector('[data-timeline-key="turn:assistant:fixture-live-turn"]')
    return !!row && !row.querySelector('.msg-row-draft')
  })
  await settle(page)
  const finalizedDraft = await page.evaluate(() => {
    const list = document.querySelector<HTMLElement>('.msg-list')
    const committed = document.querySelector<HTMLElement>('[data-timeline-key="turn:assistant:fixture-live-turn"]')
    if (!list || !committed) throw new Error('committed live turn is missing')
    return {
      reused: committed === window.__rememberedDraftTimelineRow,
      height: committed.getBoundingClientRect().height,
      scrollTop: list.scrollTop,
      mode: list.dataset.viewportMode,
      collapsed: !!committed.querySelector('.msg-collapse-toggle'),
    }
  })
  assert.equal(finalizedDraft.reused, true, 'draft -> runtime_v2_company_assistant final must reuse the same outer timeline DOM node')
  assert.equal(finalizedDraft.collapsed, false, 'a mounted expanded draft must not auto-collapse when its final arrives')
  assert.ok(
    finalizedDraft.height >= draftGeometry.height - 4,
    `draft -> final must not collapse the long turn (${draftGeometry.height}px -> ${finalizedDraft.height}px)`,
  )
  assert.ok(Math.abs(finalizedDraft.scrollTop - draftScrollTop) <= 1, 'draft -> final must preserve a browsing viewport inside the turn')
  assert.equal(finalizedDraft.mode, 'browsing', 'draft -> final must not resume following while the user is browsing')
  assert.equal((await telemetry(page)).scrollTopWrites, 0, 'draft -> final must not programmatically write browsing scrollTop')
}

async function runProgressCapIdentityCase(page: Page, baseUrl: string): Promise<void> {
  await gotoFixture(page, baseUrl, 'follow', 'progress=1')
  await page.waitForSelector('text=progress-marker-0099')
  const list = page.locator('.msg-list')
  const before = await list.evaluate((element) => {
    const viewport = element as HTMLElement
    const row = Array.from(viewport.querySelectorAll<HTMLElement>('.msg-timeline-row'))
      .find(candidate => candidate.textContent?.includes('progress-marker-0040'))
    if (!row) throw new Error('progress cap anchor row is missing')
    viewport.scrollTop += row.getBoundingClientRect().top - viewport.getBoundingClientRect().top - 80
    row.dataset.identityProbe = 'stable-progress-row'
    return { key: row.dataset.timelineKey }
  })
  await settle(page, 120)
  assert.equal(await list.getAttribute('data-viewport-mode'), 'browsing', 'scrolling to an old progress row must enter browsing mode')
  const topBefore = await list.evaluate((element) => {
    const row = Array.from(element.querySelectorAll<HTMLElement>('.msg-timeline-row'))
      .find(candidate => candidate.textContent?.includes('progress-marker-0040'))
    if (!row) throw new Error('progress cap anchor row disappeared before append')
    return row.getBoundingClientRect().top
  })

  await resetTelemetry(page)
  await page.evaluate(() => window.__messageListFixture?.appendProgressEntries(1))
  await page.waitForSelector('text=progress-marker-0100')
  await settle(page, 150)
  const after = await list.evaluate((element) => {
    const viewport = element as HTMLElement
    const row = Array.from(viewport.querySelectorAll<HTMLElement>('.msg-timeline-row'))
      .find(candidate => candidate.textContent?.includes('progress-marker-0040'))
    if (!row) throw new Error('progress cap anchor row disappeared after append')
    return {
      key: row.dataset.timelineKey,
      probe: row.dataset.identityProbe,
      top: row.getBoundingClientRect().top,
      mode: viewport.dataset.viewportMode,
    }
  })
  assert.equal(after.key, before.key, 'progress key must survive the 100-entry window shift')
  assert.equal(after.probe, 'stable-progress-row', 'React must retain the anchored progress DOM node')
  assert.ok(Math.abs(after.top - topBefore) <= 1, `progress cap moved browsing anchor by ${after.top - topBefore}px`)
  assert.equal(after.mode, 'browsing', 'progress cap append must retain browsing mode')
  assert.equal((await telemetry(page)).scrollTopWrites, 0, 'progress cap append must not programmatically write browsing scrollTop')
  assert.equal(await page.getByText('progress-marker-0000').count(), 0, 'the fixture must actually evict the oldest progress row')
}

async function runExternalProgressGeometryCase(page: Page, baseUrl: string): Promise<void> {
  await gotoFixture(page, baseUrl, 'follow', 'externalProgress=1')
  const list = page.locator('.msg-list')
  await list.hover()
  await page.mouse.wheel(0, -1_300)
  await settle(page)
  const anchor = await firstFullyVisibleMarker(page)
  const offsetBefore = await markerViewportOffset(page, anchor.marker)
  const heightBefore = await list.evaluate(element => (element as HTMLElement).clientHeight)

  await resetTelemetry(page)
  await page.evaluate(() => window.__messageListFixture?.setExternalRoleCount(12))
  await page.waitForSelector('text=Fixture Role 11')
  await settle(page, 150)

  const heightAfter = await list.evaluate(element => (element as HTMLElement).clientHeight)
  const offsetAfter = await markerViewportOffset(page, anchor.marker)
  const progressTelemetry = await telemetry(page)
  assert.equal(heightAfter, heightBefore, 'role additions must not resize the message viewport')
  assert.ok(
    Math.abs(offsetAfter - offsetBefore) <= 1,
    `external Execution Progress moved the browsing anchor by ${offsetAfter - offsetBefore}px`,
  )
  assert.equal(progressTelemetry.scrollTopWrites, 0, 'external role additions must not write browsing scrollTop')
  assert.equal(
    await list.getAttribute('data-viewport-mode'),
    'browsing',
    'external role additions must retain browsing mode',
  )
}

async function runHiddenPendingCase(page: Page, baseUrl: string): Promise<void> {
  await gotoFixture(page, baseUrl, 'follow')
  const initiallyHidden = await page.evaluate(() => !Array.from(document.querySelectorAll<HTMLElement>('.ckpt-title'))
    .some((element) => element.textContent?.trim() === 'Approval checkpoint 0'))
  assert.equal(initiallyHidden, true, 'the oldest pending checkpoint must begin outside the 200-row DOM window')

  await page.getByRole('button', { name: /Pending actions/i }).click()
  await page.waitForFunction(() => document
    .querySelector<HTMLElement>('[data-timeline-key="checkpoint:fixture-checkpoint-0"]')
    ?.textContent?.includes('Approval checkpoint 0'))
  await settle(page, 120)
  const focused = await page.evaluate(() => {
    const list = document.querySelector<HTMLElement>('.msg-list')
    const row = document.querySelector<HTMLElement>('[data-timeline-key="checkpoint:fixture-checkpoint-0"]')
    if (!list || !row) throw new Error('focused pending checkpoint row is missing')
    const listRect = list.getBoundingClientRect()
    const rowRect = row.getBoundingClientRect()
    return {
      mode: list.dataset.viewportMode,
      visible: rowRect.bottom > listRect.top && rowRect.top < listRect.bottom,
      bottomGap: list.scrollHeight - list.clientHeight - list.scrollTop,
    }
  })
  assert.equal(focused.mode, 'browsing', 'locating an old pending checkpoint must enter browsing mode')
  assert.equal(focused.visible, true, 'pending reminder must locate the checkpoint outside the initial 200-row window')
  assert.ok(focused.bottomGap > 100, 'locating an old pending checkpoint must not jump back to latest')
}

async function runCheckpointCase(page: Page, baseUrl: string): Promise<void> {
  await gotoFixture(page, baseUrl, 'follow')
  await page.waitForSelector('text=Approval checkpoint 110')
  await page.waitForSelector('text=fixture-marker-0111')

  const chronological = await page.evaluate(() => {
    const checkpointRow = document
      .querySelector<HTMLElement>('[data-timeline-key="checkpoint:fixture-checkpoint-110"]')
      ?.querySelector<HTMLElement>('.msg-row')
    const nextRow = Array.from(document.querySelectorAll<HTMLElement>('.msg-row'))
      .find((element) => element.textContent?.includes('fixture-marker-0111'))
    if (!checkpointRow || !nextRow) throw new Error('checkpoint chronology nodes are missing')
    window.__rememberedCheckpointRow = checkpointRow
    return !!(checkpointRow.compareDocumentPosition(nextRow) & Node.DOCUMENT_POSITION_FOLLOWING)
  })
  assert.equal(chronological, true, 'pending checkpoint must remain before its chronological successor')

  await page.evaluate(() => window.__messageListFixture?.resolveCheckpoint())
  await page.waitForSelector('text=Approved fixture checkpoint')
  await settle(page)
  const retainedNode = await page.evaluate(() => {
    const currentRow = document
      .querySelector<HTMLElement>('[data-timeline-key="checkpoint:fixture-checkpoint-110"]')
      ?.querySelector<HTMLElement>('.msg-row')
    return !!currentRow
      && currentRow === window.__rememberedCheckpointRow
      && document.contains(window.__rememberedCheckpointRow ?? null)
  })
  assert.equal(retainedNode, true, 'resolving a checkpoint must update the same chronological DOM row')
}

async function runPolicyCases(page: Page, baseUrl: string): Promise<void> {
  await gotoFixture(page, baseUrl, 'manual')
  const manual = await metrics(page)
  assert.ok(manual.scrollTop <= 2, `manual policy must not perform initial scrolling; scrollTop=${manual.scrollTop}`)

  await gotoFixture(page, baseUrl, 'initial-bottom')
  const initial = await metrics(page)
  assert.ok(Math.abs(initial.bottomGap) <= 2, `initial-bottom must initially reach bottom; gap=${initial.bottomGap}`)
  await page.evaluate(() => window.__messageListFixture?.repeatFullSync())
  await settle(page, 100)
  await page.evaluate(() => window.__messageListFixture?.growTailLayout(1_000))
  await settle(page, 150)
  const afterEquivalentSyncLayout = await metrics(page)
  assert.ok(
    Math.abs(afterEquivalentSyncLayout.bottomGap) <= 2,
    `equivalent full sync must not end initial-bottom late-layout following; gap=${afterEquivalentSyncLayout.bottomGap}`,
  )
  await gotoFixture(page, baseUrl, 'initial-bottom')
  await page.evaluate(() => window.__messageListFixture?.appendMessages(6))
  await page.waitForSelector('text=fixture-marker-0230')
  await settle(page)
  const afterAppend = await metrics(page)
  assert.ok(afterAppend.bottomGap > 100, 'initial-bottom policy must not follow later appends')

  // initial-bottom remains a browsing policy after its first layout. The
  // return affordance must exist even before new data, and reaching the tail
  // through explicit keyboard input must mark the latest durable row once.
  await gotoFixture(page, baseUrl, 'initial-bottom')
  const list = page.locator('.msg-list')
  await list.hover()
  await page.mouse.wheel(0, -1_100)
  await settle(page)
  const latestButton = page.locator('.msg-list-latest-btn')
  await latestButton.waitFor({ state: 'visible' })
  assert.equal((await latestButton.textContent())?.trim(), 'Back to latest', 'browsing needs a return control before new messages arrive')
  await resetTelemetry(page)
  await appendMessages(page, 1)
  assert.equal((await telemetry(page)).markReadCalls, 0, 'detached initial-bottom append must remain unread')
  assert.match((await latestButton.textContent()) ?? '', /1 new/, 'a durable tail append must increment the detached counter')
  await list.focus()
  await page.keyboard.press('End')
  await settle(page, 120)
  assert.ok(Math.abs((await metrics(page)).bottomGap) <= 2, 'End must reach the strict bottom under initial-bottom policy')
  assert.equal((await telemetry(page)).markReadCalls, 1, 'user reaching strict bottom must mark the latest row exactly once')
  await page.waitForFunction(() => !document.querySelector('.msg-list-latest-btn'))

  // Old history is earlier than the detach boundary and must never be counted
  // as a new tail delivery, including after the local 200-row window expands.
  await gotoFixture(page, baseUrl, 'initial-bottom')
  await list.hover()
  await page.mouse.wheel(0, -1_100)
  await settle(page)
  await clickHistoryWithoutScrolling(page)
  await settle(page, 100)
  await clickHistoryWithoutScrolling(page)
  await page.waitForSelector('text=history-marker-00-039')
  await settle(page, 120)
  assert.equal(
    (await page.locator('.msg-list-latest-btn').textContent())?.trim(),
    'Back to latest',
    'prepended history must not increment the detached new-message counter',
  )

  await gotoFixture(page, baseUrl, 'initial-bottom', 'empty=1')
  assert.equal(await page.locator('.msg-timeline-row').count(), 0, 'empty-summary fixture must begin without a visible timeline row')
  await page.getByRole('button', { name: 'Load older messages' }).click()
  await page.waitForSelector('text=history-marker-00-039')
  assert.ok(await page.locator('.msg-timeline-row').count() > 0, 'an empty filtered summary must still continue history pagination')
}

async function main(): Promise<void> {
  let server: ViteDevServer | undefined
  let browser: Browser | undefined
  const pageErrors: string[] = []
  const consoleErrors: string[] = []

  try {
    server = await createServer({
      root: FRONTEND_ROOT,
      logLevel: 'error',
      server: { host: '127.0.0.1', port: 0, strictPort: false },
    })
    await server.listen()
    const address = server.httpServer?.address()
    if (!address || typeof address === 'string') throw new Error('Vite did not expose a TCP port')
    const baseUrl = `http://127.0.0.1:${address.port}/`

    browser = await chromium.launch({ ignoreDefaultArgs: ['--hide-scrollbars'] })
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } })
    page.on('pageerror', (error) => pageErrors.push(error.message))
    page.on('console', (message) => {
      if (message.type() === 'error') consoleErrors.push(message.text())
    })

    await runFollowAndBrowsingCases(page, baseUrl)
    await runInputIntentCases(page, baseUrl)
    await runTouchIntentCase(browser, baseUrl)
    await runTimelineIdentityCases(page, baseUrl)
    await runProgressCapIdentityCase(page, baseUrl)
    await runExternalProgressGeometryCase(page, baseUrl)
    await runHiddenPendingCase(page, baseUrl)
    await runCheckpointCase(page, baseUrl)
    await runPolicyCases(page, baseUrl)

    assert.deepEqual(pageErrors, [], `browser page errors:\n${pageErrors.join('\n')}`)
    assert.deepEqual(consoleErrors, [], `browser console errors:\n${consoleErrors.join('\n')}`)
    console.log('message-list-scroll.spec.ts: OK (scroll writes, follow, browsing geometry/history, stable keys, checkpoints, policies)')
  } finally {
    await browser?.close()
    await server?.close()
  }
}

await main()
