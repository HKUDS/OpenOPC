export const THEMES = [
  { name: 'midnight' },
  { name: 'neon' },
  { name: 'paper' },
  { name: 'retro' },
  { name: 'terminal' },
  { name: 'cozy' },
  { name: 'openopc', default: true },
] as const

export type ThemeName = (typeof THEMES)[number]['name']

const defaultThemes = THEMES.filter(theme => 'default' in theme && theme.default)
if (defaultThemes.length !== 1) throw new Error('Theme registry must contain exactly one default')

export const DEFAULT_THEME: ThemeName = defaultThemes[0].name
export const THEME_STORAGE_KEY = 'opc_office_theme'

const themeNames: ReadonlySet<string> = new Set(THEMES.map(theme => theme.name))

export function isThemeName(value: unknown): value is ThemeName {
  return typeof value === 'string' && themeNames.has(value)
}

export function themeMessageKey(theme: ThemeName): `theme.${ThemeName}` {
  return `theme.${theme}`
}

type StorageReader = Pick<Storage, 'getItem'>
type StorageWriter = Pick<Storage, 'setItem'>

export function loadStoredTheme(storage?: StorageReader): ThemeName {
  try {
    const saved = (storage ?? globalThis.localStorage).getItem(THEME_STORAGE_KEY)
    return isThemeName(saved) ? saved : DEFAULT_THEME
  } catch {
    return DEFAULT_THEME
  }
}

export function saveStoredTheme(theme: ThemeName, storage?: StorageWriter): void {
  try {
    (storage ?? globalThis.localStorage).setItem(THEME_STORAGE_KEY, theme)
  } catch {
    // Storage can be unavailable in private or policy-restricted contexts.
  }
}
