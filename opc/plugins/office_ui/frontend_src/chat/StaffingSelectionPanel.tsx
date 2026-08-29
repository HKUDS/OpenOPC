import React, { useCallback, useEffect, useMemo, useState } from 'react'
import type {
  CheckpointReplyMetadata,
  ChatMessageMeta,
  StaffingEmployeeOption,
  StaffingRoleEntry,
  StaffingSelectionValue,
  StaffingTemplateOption,
} from '../types/chat'
import type { TaskPreferredAgent } from '../types/kanban'
import { RECRUITMENT_AGENT_OPTIONS, TASK_AGENT_LABELS, TASK_AGENT_OPTIONS } from '../lib/externalAgents'

const DEFAULT_ROLE_AGENT: TaskPreferredAgent = 'codex'
const DEFAULT_RECRUITMENT_AGENT: TaskPreferredAgent = 'native'

type StaffingOption =
  | { kind: 'employee'; id: string; name: string; subtitle: string; category: string; searchText: string }
  | { kind: 'template'; id: string; name: string; subtitle: string; category: string; searchText: string }
  | { kind: 'fallback'; id: ''; name: string; subtitle: string; category: string; searchText: string }

interface StaffingSelectionPanelProps {
  meta: ChatMessageMeta
  onReply: (text: string, metadata?: CheckpointReplyMetadata) => void
  responded: boolean
}

function normalizeSelection(value: StaffingSelectionValue | undefined): StaffingSelectionValue {
  if (!value) return { kind: 'fallback' }
  if (value.kind === 'employee') {
    const id = String(value.id ?? value.employee_id ?? '').trim()
    return id ? { kind: 'employee', id } : { kind: 'fallback' }
  }
  if (value.kind === 'template') {
    const id = String(value.id ?? value.template_id ?? '').trim()
    return id ? { kind: 'template', id } : { kind: 'fallback' }
  }
  return { kind: 'fallback' }
}

function selectionKey(value: StaffingSelectionValue | undefined): string {
  const normalized = normalizeSelection(value)
  return normalized.kind === 'fallback' ? 'fallback:' : `${normalized.kind}:${normalized.id ?? ''}`
}

function buildOptions(
  role: StaffingRoleEntry,
  employees: StaffingEmployeeOption[],
  templates: StaffingTemplateOption[],
): StaffingOption[] {
  const roleId = String(role.role_id ?? '').trim()
  const roleLabel = String(role.role_label ?? '').trim()
  const roleText = `${roleId} ${roleLabel} ${role.role_responsibility ?? ''}`.toLowerCase()
  const sameRoleIds = new Set((role.same_role_employee_ids ?? []).map(item => String(item ?? '').trim()).filter(Boolean))
  const templateScore = (template: StaffingTemplateOption): number => {
    const templateText = `${template.template_id ?? ''} ${template.template_name ?? ''} ${template.category ?? ''} ${(template.domains ?? []).join(' ')} ${(template.tags ?? []).join(' ')}`.toLowerCase()
    const categoryTerm = String(template.category ?? '').toLowerCase()
    let score = 0
    for (const token of roleText.split(/[^a-z0-9]+/).filter(token => token.length >= 3)) {
      if (templateText.includes(token) || (categoryTerm && token.includes(categoryTerm))) score += 1
    }
    if (templateText.includes(roleId.replace(/_/g, '-')) || templateText.includes(roleId.replace(/_/g, ' '))) score += 2
    return score
  }
  const employeeOptions = employees.map((employee): StaffingOption & { rank: number } => {
    const id = String(employee.employee_id ?? '').trim()
    const name = String(employee.employee_name ?? id).trim() || id
    const employeeRole = String(employee.role_id ?? '').trim()
    const category = String(employee.category ?? '').trim()
    const subtitle = [employeeRole, category].filter(Boolean).join(' · ') || id
    return {
      kind: 'employee',
      id,
      name,
      subtitle,
      category,
      searchText: `${id} ${name} ${employeeRole} ${category} ${(employee.domains ?? []).join(' ')} ${(employee.tags ?? []).join(' ')}`.toLowerCase(),
      rank: sameRoleIds.has(id) || employeeRole === roleId ? 0 : 2,
    }
  }).filter(option => option.id).sort((a, b) => a.rank - b.rank || a.name.localeCompare(b.name))
  const templateOptions = templates.map((template): StaffingOption & { rank: number } => {
    const id = String(template.template_id ?? '').trim()
    const name = String(template.template_name ?? id).trim() || id
    const category = String(template.category ?? '').trim()
    const subtitle = [category, id].filter(Boolean).join(' · ') || id
    return {
      kind: 'template',
      id,
      name,
      subtitle,
      category,
      searchText: `${id} ${name} ${category} ${(template.domains ?? []).join(' ')} ${(template.tags ?? []).join(' ')}`.toLowerCase(),
      rank: templateScore(template),
    }
  }).filter(option => option.id).sort((a, b) => b.rank - a.rank || a.name.localeCompare(b.name))
  return [
    ...employeeOptions,
    ...templateOptions,
    { kind: 'fallback', id: '', name: 'Fallback role-only', subtitle: 'No employee override', category: 'fallback', searchText: 'fallback role only no employee override' },
  ]
}

function optionForSelection(options: StaffingOption[], selection: StaffingSelectionValue | undefined): StaffingOption {
  const key = selectionKey(selection)
  return options.find(option => `${option.kind}:${option.id}` === key) ?? options[0]
}

function optionMatches(option: StaffingOption, query: string): boolean {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean)
  if (terms.length === 0) return true
  return terms.every(term => option.searchText.includes(term))
}

function buildSelectionsFromMeta(meta: ChatMessageMeta, roles: StaffingRoleEntry[]): Record<string, StaffingSelectionValue> {
  const initial: Record<string, StaffingSelectionValue> = {}
  const persisted = meta.staffing_selections ?? {}
  for (const role of roles) {
    const roleId = String(role.role_id ?? '').trim()
    if (!roleId) continue
    initial[roleId] = normalizeSelection(persisted[roleId] ?? role.default_selection)
  }
  return initial
}

function buildRoleAgentsFromMeta(meta: ChatMessageMeta, roles: StaffingRoleEntry[]): Record<string, TaskPreferredAgent> {
  const persisted = meta.recruitment_role_agents ?? {}
  const initial: Record<string, TaskPreferredAgent> = {}
  for (const role of roles) {
    const roleId = String(role.role_id ?? '').trim()
    if (!roleId) continue
    initial[roleId] = persisted[roleId] ?? role.selected_agent ?? role.default_agent ?? DEFAULT_ROLE_AGENT
  }
  return initial
}

export function buildDynamicTeamCoverage(
  roles: StaffingRoleEntry[],
  roleAgents: Record<string, TaskPreferredAgent>,
): {
  boundaryByRole: Map<string, string>
  coveredRoleIdsByBoundary: Map<string, string[]>
} {
  const parentByRole = new Map(
    roles.map(role => [String(role.role_id ?? '').trim(), String(role.reports_to ?? '').trim()]),
  )
  const candidates = new Set(
    roles
      .map(role => String(role.role_id ?? '').trim())
      .filter(roleId => roleId && roleAgents[roleId] === 'jiuwenswarm'),
  )
  const hasSelectedAncestor = (roleId: string): boolean => {
    const seen = new Set<string>()
    let cursor = parentByRole.get(roleId) ?? ''
    while (cursor && cursor !== 'owner' && !seen.has(cursor)) {
      if (candidates.has(cursor)) return true
      seen.add(cursor)
      cursor = parentByRole.get(cursor) ?? ''
    }
    return false
  }
  const boundaries = new Set([...candidates].filter(roleId => !hasSelectedAncestor(roleId)))
  const boundaryByRole = new Map<string, string>()
  const coveredRoleIdsByBoundary = new Map<string, string[]>()
  for (const role of roles) {
    const roleId = String(role.role_id ?? '').trim()
    if (!roleId) continue
    let cursor = roleId
    const seen = new Set<string>()
    let boundary = ''
    while (cursor && cursor !== 'owner' && !seen.has(cursor)) {
      if (boundaries.has(cursor)) {
        boundary = cursor
        break
      }
      seen.add(cursor)
      cursor = parentByRole.get(cursor) ?? ''
    }
    if (!boundary) continue
    boundaryByRole.set(roleId, boundary)
    coveredRoleIdsByBoundary.set(
      boundary,
      [...(coveredRoleIdsByBoundary.get(boundary) ?? []), roleId],
    )
  }
  return { boundaryByRole, coveredRoleIdsByBoundary }
}

function hasSubmittedCheckpointMetadata(meta: ChatMessageMeta): boolean {
  return Boolean(
    String(meta.checkpoint_response_message_id ?? '').trim()
    || String(meta.checkpoint_responded_at ?? '').trim()
    || String(meta.staffing_action ?? '').trim()
  )
}

export const StaffingSelectionPanel = React.memo(function StaffingSelectionPanel({
  meta, onReply, responded,
}: StaffingSelectionPanelProps) {
  const roles = useMemo(() => meta.staffing_roles ?? [], [meta.staffing_roles])
  const employees = meta.staffing_pool?.employees ?? []
  const templates = meta.staffing_pool?.templates ?? []
  const optionsByRole = useMemo(() => {
    const next: Record<string, StaffingOption[]> = {}
    for (const role of roles) {
      const roleId = String(role.role_id ?? '').trim()
      if (roleId) next[roleId] = buildOptions(role, employees, templates)
    }
    return next
  }, [employees, roles, templates])
  const [queries, setQueries] = useState<Record<string, string>>({})
  const [selections, setSelections] = useState<Record<string, StaffingSelectionValue>>(() => buildSelectionsFromMeta(meta, roles))
  const [roleAgents, setRoleAgents] = useState<Record<string, TaskPreferredAgent>>(() => buildRoleAgentsFromMeta(meta, roles))
  const [recruitmentAgent, setRecruitmentAgent] = useState<TaskPreferredAgent>(meta.recruitment_agent ?? DEFAULT_RECRUITMENT_AGENT)
  const isResponded = responded
  const recommendAutoRecruit = meta.recommended_action === 'auto_recruit' && templates.length > 0
  const dynamicTeamCoverage = useMemo(
    () => buildDynamicTeamCoverage(roles, roleAgents),
    [roleAgents, roles],
  )

  useEffect(() => {
    if (!isResponded || !hasSubmittedCheckpointMetadata(meta)) return
    setSelections(buildSelectionsFromMeta(meta, roles))
    setRoleAgents(buildRoleAgentsFromMeta(meta, roles))
    setRecruitmentAgent(meta.recruitment_agent ?? DEFAULT_RECRUITMENT_AGENT)
  }, [isResponded, meta, roles])

  useEffect(() => {
    setRecruitmentAgent(meta.recruitment_agent ?? DEFAULT_RECRUITMENT_AGENT)
  }, [meta.recruitment_agent])

  const buildReplyMetadata = useCallback((action: 'manual_approve' | 'auto_recruit'): CheckpointReplyMetadata => {
    const checkpointId = String(meta.checkpoint_id ?? '').trim()
    if (!checkpointId) {
      throw new Error('Staffing checkpoint reply requires checkpoint_id metadata.')
    }
    return {
      response_to_checkpoint_id: checkpointId,
      response_to_checkpoint_type: 'company_staffing_selection',
      staffing_action: action,
      staffing_selections: selections,
      recruitment_agent: recruitmentAgent,
      recruitment_role_agents: roles.reduce<Record<string, TaskPreferredAgent>>((acc, role) => {
        const roleId = String(role.role_id ?? '').trim()
        if (!roleId) return acc
        acc[roleId] = roleAgents[roleId] ?? role.selected_agent ?? role.default_agent ?? DEFAULT_ROLE_AGENT
        return acc
      }, {}),
    }
  }, [meta.checkpoint_id, recruitmentAgent, roleAgents, roles, selections])

  const handleApprove = useCallback(() => {
    if (isResponded) return
    onReply('approve', buildReplyMetadata('manual_approve'))
  }, [buildReplyMetadata, isResponded, onReply])

  const handleAutoRecruit = useCallback(() => {
    if (isResponded) return
    onReply('auto recruit', buildReplyMetadata('auto_recruit'))
  }, [buildReplyMetadata, isResponded, onReply])

  return (
    <div className="ckpt-panel ckpt-staffing">
      <div className="ckpt-header">
        <div className="ckpt-icon ckpt-icon-staffing">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M6 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z" />
            <path d="M1.5 14c.6-2.5 2.2-4 4.5-4s3.9 1.5 4.5 4" />
            <path d="M12.5 3.5v5M10 6h5" />
          </svg>
        </div>
        <div className="ckpt-title">Manual Staffing</div>
        <span className="ckpt-badge ckpt-badge-profile">{meta.company_profile || 'corporate'}</span>
        {recommendAutoRecruit && !isResponded && <span className="ckpt-badge ckpt-badge-scope">Recruit recommended</span>}
        {isResponded && <span className="ckpt-badge ckpt-badge-responded">Responded</span>}
      </div>

      {meta.summary && <div className="ckpt-summary">{meta.summary}</div>}

      <div className="ckpt-recruiter-agent">
        <label className="ckpt-agent-label" htmlFor={`staffing-recruiter-agent-${meta.checkpoint_id}`}>
          Recruiter Agent
        </label>
        <select
          id={`staffing-recruiter-agent-${meta.checkpoint_id}`}
          className="ckpt-agent-select"
          value={recruitmentAgent}
          onChange={event => setRecruitmentAgent(event.target.value as TaskPreferredAgent)}
          disabled={isResponded}
        >
          {RECRUITMENT_AGENT_OPTIONS.map(agent => (
            <option key={agent} value={agent}>{TASK_AGENT_LABELS[agent]}</option>
          ))}
        </select>
      </div>

      <div className="ckpt-staffing-grid">
        {roles.map(role => {
          const roleId = String(role.role_id ?? '').trim()
          const persistedExternalTeam = role.staffing_locked && role.staffing_mode === 'opaque_external_team'
          const dynamicBoundaryRoleId = dynamicTeamCoverage.boundaryByRole.get(roleId) ?? ''
          const dynamicTeamBoundary = dynamicBoundaryRoleId === roleId
          const dynamicTeamCovered = Boolean(dynamicBoundaryRoleId)
          const externallyStaffed = persistedExternalTeam || dynamicTeamCovered
          const coveredRoleIds = persistedExternalTeam
            ? (role.covered_role_ids ?? [roleId])
            : (dynamicTeamCoverage.coveredRoleIdsByBoundary.get(dynamicBoundaryRoleId) ?? [roleId])
          const options = optionsByRole[roleId] ?? [{ kind: 'fallback', id: '', name: 'Fallback role-only', subtitle: 'No employee override', category: 'fallback', searchText: 'fallback role only no employee override' }]
          const selected = optionForSelection(options, selections[roleId])
          const query = queries[roleId] ?? ''
          const visibleOptions = options.filter(option => optionMatches(option, query)).slice(0, 8)
          return (
            <div key={roleId} className="ckpt-staffing-card">
              <div className="ckpt-proposal-header">
                <span className="ckpt-role-name">{roleId}</span>
                <span className={`ckpt-badge ckpt-badge-${externallyStaffed ? 'external-team' : selected.kind}`}>
                  {externallyStaffed ? 'JiuwenSwarm-team' : selected.kind}
                </span>
              </div>
              {role.role_label && role.role_label !== roleId && (
                <div className="ckpt-role-labels">
                  <span className="ckpt-field-tag">{role.role_label}</span>
                </div>
              )}
              {externallyStaffed ? (
                <div className="ckpt-external-team-staffing">
                  <b>{dynamicTeamCovered && !dynamicTeamBoundary ? `Covered by Team at ${dynamicBoundaryRoleId}` : 'Internally staffed external team'}</b>
                  <span>One opaque Team covers: {coveredRoleIds.join(', ')}</span>
                  <span>No OPC employees are recruited for these covered roles.</span>
                </div>
              ) : <div className="ckpt-staffing-selected">
                <div className="ckpt-cand-name">{selected.name}</div>
                <div className="ckpt-cand-meta">
                  <span className="ckpt-cand-category">{selected.category}</span>
                  <span className="ckpt-domain-tag">{selected.subtitle}</span>
                </div>
              </div>}
              {!externallyStaffed && <input
                className="ckpt-staffing-search"
                value={query}
                onChange={event => setQueries(current => ({ ...current, [roleId]: event.target.value }))}
                placeholder="Search employees or templates..."
                disabled={isResponded}
              />}
              {!externallyStaffed && <div className="ckpt-staffing-options">
                {visibleOptions.map(option => {
                  const active = `${option.kind}:${option.id}` === selectionKey(selections[roleId])
                  return (
                    <button
                      key={`${option.kind}:${option.id}`}
                      className={`ckpt-staffing-option${active ? ' active' : ''}`}
                      onClick={() => setSelections(current => ({ ...current, [roleId]: { kind: option.kind, id: option.id } }))}
                      disabled={isResponded}
                      title={option.subtitle}
                    >
                      <span className="ckpt-staffing-option-kind">{option.kind}</span>
                      <span className="ckpt-staffing-option-name">{option.name}</span>
                    </button>
                  )
                })}
              </div>}
              <div className="ckpt-agent-picker">
                <label className="ckpt-agent-label" htmlFor={`staffing-agent-${meta.checkpoint_id}-${roleId}`}>
                  Execution Agent
                </label>
                <select
                  id={`staffing-agent-${meta.checkpoint_id}-${roleId}`}
                  className="ckpt-agent-select"
                  value={dynamicTeamCovered && !dynamicTeamBoundary
                    ? 'jiuwenswarm'
                    : (roleAgents[roleId] ?? role.selected_agent ?? role.default_agent ?? DEFAULT_ROLE_AGENT)}
                  onChange={event => setRoleAgents(current => ({ ...current, [roleId]: event.target.value as TaskPreferredAgent }))}
                  disabled={isResponded || Boolean(persistedExternalTeam) || (dynamicTeamCovered && !dynamicTeamBoundary)}
                >
                  {TASK_AGENT_OPTIONS.map(agent => (
                    <option key={agent} value={agent}>{TASK_AGENT_LABELS[agent]}</option>
                  ))}
                </select>
              </div>
            </div>
          )
        })}
      </div>

      {!isResponded && (
        <div className="ckpt-actions">
          {recommendAutoRecruit ? (
            <>
              <button className="ckpt-btn ckpt-btn-approve" onClick={handleAutoRecruit}>Auto Recruit</button>
              <button className="ckpt-btn ckpt-btn-feedback" onClick={handleApprove}>Approve Selections</button>
            </>
          ) : (
            <>
              <button className="ckpt-btn ckpt-btn-approve" onClick={handleApprove}>Approve Selections</button>
              {templates.length > 0 && <button className="ckpt-btn ckpt-btn-feedback" onClick={handleAutoRecruit}>Auto Recruit</button>}
            </>
          )}
        </div>
      )}
    </div>
  )
})
