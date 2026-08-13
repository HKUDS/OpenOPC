export interface ProjectIdPolicy {
  readonly version: 1
  readonly pattern: string
  matches(value: string): boolean
}

const SUPPORTED_POLICY_VERSION = 1

export function compileProjectIdPolicy(value: unknown): ProjectIdPolicy | null {
  if (!value || typeof value !== 'object') return null
  const raw = value as Record<string, unknown>
  if (raw.version !== SUPPORTED_POLICY_VERSION || typeof raw.pattern !== 'string' || !raw.pattern) {
    return null
  }

  try {
    const expression = new RegExp(`^(?:${raw.pattern})$`)
    return Object.freeze({
      version: SUPPORTED_POLICY_VERSION,
      pattern: raw.pattern,
      matches(candidate: string): boolean {
        const match = expression.exec(candidate)
        return match !== null && match[0] === candidate
      },
    })
  } catch {
    return null
  }
}

export function toProjectSlug(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-|-$/g, '')
}
