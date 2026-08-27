import type { TaskPreferredAgent } from '../types/kanban'

/** Shared agent catalogue for every Office selector and runtime badge. */
export const TASK_AGENT_LABELS: Record<TaskPreferredAgent, string> = {
  native: 'OpenOPC Native',
  codex: 'Codex',
  claude_code: 'Claude Code',
  cursor: 'Cursor',
  opencode: 'OpenCode',
  jiuwen: 'Jiuwen',
  jiuwenswarm: 'JiuwenSwarm Team',
}

export const TASK_AGENT_OPTIONS: TaskPreferredAgent[] = [
  'codex',
  'jiuwen',
  'jiuwenswarm',
  'native',
  'claude_code',
  'cursor',
  'opencode',
]

export const RECRUITMENT_AGENT_OPTIONS: TaskPreferredAgent[] = [
  'native',
  'codex',
  'jiuwen',
  'jiuwenswarm',
  'claude_code',
  'cursor',
  'opencode',
]

export function executionAgentLabel(value?: string): string {
  const normalized = String(value ?? '').trim().toLowerCase().replaceAll('-', '_') as TaskPreferredAgent
  return TASK_AGENT_LABELS[normalized] ?? String(value ?? '').trim()
}
