import React, { useCallback, useEffect, useMemo, useState } from 'react'
import type {
  CheckpointReplyMetadata,
  ChatMessageMeta,
  RecruitmentProposalEntry,
  StaffingEmployeeOption,
  StaffingRoleEntry,
  StaffingSelectionValue,
  StaffingTemplateOption,
} from '../types/chat'
import type { TaskPreferredAgent } from '../types/kanban'
import { RECRUITMENT_AGENT_OPTIONS, TASK_AGENT_LABELS, TASK_AGENT_OPTIONS } from '../lib/externalAgents'
import { buildDynamicTeamCoverage } from './StaffingSelectionPanel'

const DEFAULT_ROLE_AGENT: TaskPreferredAgent = 'codex'
const DEFAULT_RECRUITMENT_AGENT: TaskPreferredAgent = 'native'

type StaffingOption =
  | { kind: 'employee'; id: string; name: string; subtitle: string; category: string; searchText: string }
  | { kind: 'template'; id: string; name: string; subtitle: string; category: string; searchText: string }
  | { kind: 'fallback'; id: ''; name: string; subtitle: string; category: string; searchText: string }

interface RecruitmentPanelProps {
  meta: ChatMessageMeta
  onReply: (text: string, metadata?: CheckpointReplyMetadata) => Promise<boolean>
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
    const templateText = `${id} ${name} ${category} ${(template.domains ?? []).join(' ')} ${(template.tags ?? []).join(' ')}`.toLowerCase()
    const rank = roleText.split(/[^a-z0-9]+/).filter(token => token.length >= 3).reduce(
      (score, token) => score + (templateText.includes(token) ? 1 : 0),
      0,
    )
    return {
      kind: 'template',
      id,
      name,
      subtitle,
      category,
      searchText: templateText,
      rank,
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

function buildRolesFromProposals(proposals: RecruitmentProposalEntry[]): StaffingRoleEntry[] {
  return proposals.map((proposal) => {
    const roleId = String(proposal.role_id ?? '').trim()
    const existingId = String(proposal.existing_employee?.employee_id ?? '').trim()
    const templateId = String(proposal.candidate?.template_id ?? '').trim()
    const defaultSelection: StaffingSelectionValue = existingId
      ? { kind: 'employee', id: existingId }
      : templateId
        ? { kind: 'template', id: templateId }
        : { kind: 'fallback' }
    return {
      role_id: roleId,
      role_label: proposal.role_labels?.[0] ?? roleId,
      role_responsibility: '',
      default_selection: defaultSelection,
      same_role_employee_ids: proposal.existing_employee_ids ?? [],
      fallback_available: true,
      default_agent: proposal.default_agent ?? DEFAULT_ROLE_AGENT,
      selected_agent: proposal.selected_agent ?? proposal.default_agent ?? DEFAULT_ROLE_AGENT,
      default_source: 'recruitment',
    }
  }).filter(role => role.role_id)
}

function selectedRecruitmentName(
  proposal: RecruitmentProposalEntry | undefined,
  selected: StaffingOption,
): string {
  if (selected.kind === 'template' && proposal?.candidate?.template_id === selected.id) {
    return proposal.candidate.proposed_name || proposal.candidate.template_name || selected.name
  }
  if (selected.kind === 'employee' && proposal?.existing_employee?.employee_id === selected.id) {
    return proposal.existing_employee.employee_name || selected.name
  }
  return selected.name
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

function hasSubmittedCheckpointMetadata(meta: ChatMessageMeta): boolean {
  return Boolean(
    String(meta.checkpoint_response_message_id ?? '').trim()
    || String(meta.checkpoint_responded_at ?? '').trim()
    || String(meta.checkpoint_reply_kind ?? '').trim()
  )
}

export const RecruitmentPanel = React.memo(function RecruitmentPanel({
  meta, onReply, responded,
}: RecruitmentPanelProps) {
  const proposals = meta.proposals ?? []
  const proposalByRole = useMemo(() => {
    const next: Record<string, RecruitmentProposalEntry> = {}
    for (const proposal of proposals) {
      const roleId = String(proposal.role_id ?? '').trim()
      if (roleId) next[roleId] = proposal
    }
    return next
  }, [proposals])
  const roles = useMemo(
    () => (meta.staffing_roles?.length ? meta.staffing_roles : buildRolesFromProposals(proposals)),
    [meta.staffing_roles, proposals],
  )
  const employees = meta.staffing_pool?.employees ?? []
  const templates = meta.staffing_pool?.templates ?? []
  const rationales = meta.recruitment_rationales ?? []
  const optionsByRole = useMemo(() => {
    const next: Record<string, StaffingOption[]> = {}
    for (const role of roles) {
      const roleId = String(role.role_id ?? '').trim()
      if (roleId) next[roleId] = buildOptions(role, employees, templates)
    }
    return next
  }, [employees, roles, templates])
  const [queries, setQueries] = useState<Record<string, string>>({})
  const [feedback, setFeedback] = useState('')
  const [selections, setSelections] = useState<Record<string, StaffingSelectionValue>>(() => buildSelectionsFromMeta(meta, roles))
  const [roleAgents, setRoleAgents] = useState<Record<string, TaskPreferredAgent>>(() => buildRoleAgentsFromMeta(meta, roles))
  const [recruitmentAgent, setRecruitmentAgent] = useState<TaskPreferredAgent>(meta.recruitment_agent ?? DEFAULT_RECRUITMENT_AGENT)
  const isResponded = responded
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

  const buildReplyMetadata = useCallback((kind: NonNullable<CheckpointReplyMetadata['checkpoint_reply_kind']>): CheckpointReplyMetadata => {
    const checkpointId = String(meta.checkpoint_id ?? '').trim()
    if (!checkpointId) {
      throw new Error('Recruitment checkpoint reply requires checkpoint_id metadata.')
    }
    const checkpointType = String(meta.checkpoint_type ?? '').trim()
    return {
      response_to_checkpoint_id: checkpointId,
      response_to_checkpoint_type: checkpointType || 'company_recruitment_confirmation',
      checkpoint_reply_kind: kind,
      staffing_selections: selections,
      recruitment_agent: recruitmentAgent,
      recruitment_role_agents: roles.reduce<Record<string, TaskPreferredAgent>>((acc, role) => {
        const roleId = String(role.role_id ?? '').trim()
        if (!roleId) return acc
        acc[roleId] = roleAgents[roleId] ?? role.selected_agent ?? role.default_agent ?? DEFAULT_ROLE_AGENT
        return acc
      }, {}),
    }
  }, [meta.checkpoint_id, meta.checkpoint_type, recruitmentAgent, roleAgents, roles, selections])

  const handleApprove = useCallback(() => {
    if (isResponded) return
    void onReply('approve', buildReplyMetadata('approve'))
  }, [buildReplyMetadata, isResponded, onReply])

  const handleFeedback = useCallback(async () => {
    if (isResponded || !feedback.trim()) return
    const accepted = await onReply(feedback.trim(), buildReplyMetadata('feedback'))
    if (accepted) setFeedback('')
  }, [buildReplyMetadata, isResponded, feedback, onReply])

  return (
    <div className="ckpt-panel ckpt-recruitment">
      <div className="ckpt-header">
        <div className="ckpt-icon ckpt-icon-recruit">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="6" cy="5" r="3" />
            <path d="M2 14c0-2.2 1.8-4 4-4s4 1.8 4 4" />
            <path d="M12 5v4M10 7h4" />
          </svg>
        </div>
        <div className="ckpt-title">Recruitment Review</div>
        <span className="ckpt-badge ckpt-badge-profile">{meta.company_profile || 'corporate'}</span>
        {isResponded && <span className="ckpt-badge ckpt-badge-responded">Responded</span>}
      </div>

      {meta.summary && <div className="ckpt-summary">{meta.summary}</div>}

      <div className="ckpt-recruiter-agent">
        <label className="ckpt-agent-label" htmlFor={`recruitment-recruiter-agent-${meta.checkpoint_id}`}>
          Recruiter Agent
        </label>
        <select
          id={`recruitment-recruiter-agent-${meta.checkpoint_id}`}
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

      {rationales.length > 0 && (
        <div className="ckpt-recruitment-reasons">
          {rationales.map(item => (
            <div key={item.role_id} className="ckpt-recruitment-reason">
              <div className="ckpt-proposal-header">
                <span className="ckpt-role-name">{item.role_id}</span>
                {item.role_label && item.role_label !== item.role_id && <span className="ckpt-field-tag">{item.role_label}</span>}
                {item.selection_label && <span className="ckpt-badge ckpt-badge-template">{item.selection_label}</span>}
              </div>
              {item.rationale && <div className="ckpt-rationale">{item.rationale}</div>}
            </div>
          ))}
        </div>
      )}

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
          const proposal = proposalByRole[roleId]
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
                <div className="ckpt-cand-name">{selectedRecruitmentName(proposal, selected)}</div>
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
                <label className="ckpt-agent-label" htmlFor={`recruit-agent-${meta.checkpoint_id}-${roleId}`}>
                  Execution Agent
                </label>
                <select
                  id={`recruit-agent-${meta.checkpoint_id}-${roleId}`}
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
        <div className="ckpt-actions ckpt-actions-inline-feedback">
          <button className="ckpt-btn ckpt-btn-approve" onClick={handleApprove}>Approve</button>
          <textarea
            className="ckpt-feedback-input ckpt-feedback-inline-input"
            placeholder="Feedback to refine recruitment..."
            value={feedback}
            onChange={e => setFeedback(e.target.value)}
            rows={2}
          />
          <button className="ckpt-btn ckpt-btn-feedback" onClick={handleFeedback} disabled={!feedback.trim()}>
            Send Feedback
          </button>
        </div>
      )}
    </div>
  )
})
