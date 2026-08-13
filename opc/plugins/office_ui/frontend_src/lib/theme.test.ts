import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  DEFAULT_THEME,
  THEMES,
  THEME_STORAGE_KEY,
  isThemeName,
  loadStoredTheme,
  saveStoredTheme,
  themeMessageKey,
} from './theme'

test('the registry is unique and contains the only default declaration', () => {
  const names = THEMES.map(theme => theme.name)
  assert.equal(new Set(names).size, names.length)
  assert.equal(THEMES.filter(theme => 'default' in theme && theme.default).length, 1)
  assert.equal(DEFAULT_THEME, 'openopc')
})

test('every registered theme is accepted and restored', () => {
  for (const { name } of THEMES) {
    assert.equal(isThemeName(name), true)
    assert.equal(themeMessageKey(name), `theme.${name}`)
    assert.equal(loadStoredTheme({ getItem: () => name }), name)
  }
})

test('missing, obsolete, and inaccessible preferences fall back safely', () => {
  assert.equal(loadStoredTheme({ getItem: () => null }), DEFAULT_THEME)
  assert.equal(loadStoredTheme({ getItem: () => 'unknown-theme' }), DEFAULT_THEME)
  assert.equal(loadStoredTheme({ getItem: () => { throw new DOMException('blocked', 'SecurityError') } }), DEFAULT_THEME)
  assert.equal(loadStoredTheme(), DEFAULT_THEME)
})

test('saving uses the stable key and never leaks storage failures', () => {
  const writes: Array<[string, string]> = []
  saveStoredTheme('paper', { setItem: (key, value) => { writes.push([key, value]) } })
  assert.deepEqual(writes, [[THEME_STORAGE_KEY, 'paper']])
  assert.doesNotThrow(() => {
    saveStoredTheme('paper', { setItem: () => { throw new DOMException('blocked', 'SecurityError') } })
  })
})

test('every registered theme has a matching CSS implementation', () => {
  const css = readFileSync(new URL('../index.css', import.meta.url), 'utf8')
  for (const { name } of THEMES) {
    const selector = `.app-shell[data-theme="${name}"]`
    const blockStart = css.indexOf(selector)
    const blockEnd = css.indexOf('}', blockStart)
    assert.notEqual(blockStart, -1, `missing CSS implementation for ${name}`)
    assert.match(css.slice(blockStart, blockEnd), /--bg:/, `theme ${name} must define its background token`)
  }
})
