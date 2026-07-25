import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type {
  OrgInfoPayload,
  OrgCreateMemberInput,
  OrgSavedCreatePayload,
  SavedOrgSummary,
  TalentTemplate,
  EmployeeDetailPayload,
  ReorgProposalInfo,
  ArchitecturePreset,
  ArchitecturePresetDetail,
  HireTalentHandler,
} from '../types/visual'
import { TeamView } from './TeamView'
import { DelegationStrategyPanel } from './DelegationStrategyPanel'
import { ArchitectureMarketplace } from './ArchitectureMarketplace'
import { EmployeesMarketplace } from './EmployeesMarketplace'
import { ConfigImportExportPanel } from './ConfigImportExportPanel'
import { OrgCreateModal } from './OrgCreateModal'
import { getRuntimeOrgView } from '../lib/runtimeOrg'
import { useI18n } from '../i18n'
import './org.css'
import './team.css'
import './marketplace.css'
import './config.css'
import './structure.css'

/* ── Inline SVG icon data-URIs (no external CDN) ────────────────── */
const TAB_ICON = {
  team: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23888' d='M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z'/%3E%3C/svg%3E",
  runtime: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23888' d='M13 2.05v2.02c3.95.49 7 3.85 7 7.93 0 3.21-1.81 6-4.72 7.72L13 17v5h5l-1.22-1.22C19.91 19.07 22 15.76 22 12c0-5.18-3.95-9.45-9-9.95zM11 2.05C5.95 2.55 2 6.82 2 12c0 3.76 2.09 7.07 5.22 8.78L6 22h5V2.05z'/%3E%3C/svg%3E",
  architecture: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23888' d='M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z'/%3E%3C/svg%3E",
  employees: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23888' d='M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z'/%3E%3C/svg%3E",
}
const STAT_ICON = {
  agents: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23888' d='M20 9V7c0-1.1-.9-2-2-2h-3c0-1.66-1.34-3-3-3S9 3.34 9 5H6c-1.1 0-2 .9-2 2v2c-1.66 0-3 1.34-3 3s1.34 3 3 3v4c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2v-4c1.66 0 3-1.34 3-3s-1.34-3-3-3z'/%3E%3C/svg%3E",
  active: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%2322c55e' d='M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 14.5v-9l6 4.5-6 4.5z'/%3E%3C/svg%3E",
}
const SECTION_ICON = {
  packages: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23888' d='M20.54 5.23l-1.39-1.68C18.88 3.21 18.47 3 18 3H6c-.47 0-.88.21-1.16.55L3.46 5.23C3.17 5.57 3 6.02 3 6.5V19c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V6.5c0-.48-.17-.93-.46-1.27zM12 17.5L6.5 12H10v-2h4v2h3.5L12 17.5zM5.12 5l.81-1h12l.94 1H5.12z'/%3E%3C/svg%3E",
  channels: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23888' d='M1 9l2 2c4.97-4.97 13.03-4.97 18 0l2-2C16.93 2.93 7.08 2.93 1 9zm8 8l3 3 3-3c-1.65-1.66-4.34-1.66-6 0zm-4-4l2 2c2.76-2.76 7.24-2.76 10 0l2-2C15.14 9.14 8.87 9.14 5 13z'/%3E%3C/svg%3E",
  reorg: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23888' d='M6.99 11L3 15l3.99 4v-3H14v-2H6.99v-3zM21 9l-3.99-4v3H10v2h7.01v3L21 9z'/%3E%3C/svg%3E",
  importPkg: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath fill='%23888' d='M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z'/%3E%3C/svg%3E",
}

type SubTab = 'team' | 'runtime' | 'architecture' | 'employees'

function humanizeOrgName(value?: string | null): string {
  const normalized = String(value ?? '').trim()
  if (!normalized) return ''
  return normalized
    .replace(/^org[_-]/i, '')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, char => char.toUpperCase())
}

interface OrgTabProps {
  data: OrgInfoPayload | null
  /** role_id -> recruited names for the selected session (canvas display only). */
  sessionRecruitmentByRole?: Record<string, string[]> | null
  talents: TalentTemplate[]
  employeeDetail: EmployeeDetailPayload | null
  reorgProposals: ReorgProposalInfo[]
  isCustomMode?: boolean
  onRequestData: () => void
  onRequestTalents: () => void
  onRequestEmployeeDetail: (employeeId: string) => void
  onHireTalent: HireTalentHandler
  hiringTemplateId?: string | null
  onImportEmployee?: (employeeId: string) => void
  onRequestReorgList: () => void
  onReorgDecide: (proposalId: string, approved: boolean, notes?: string) => void
  // Market
  onMarketExport?: (data: { package_id: string; name: string; description: string; version: string }) => void
  onMarketInstall?: (path: string, strategy: string) => void
  onMarketUninstall?: (packageId: string) => void
  // Architecture gallery
  marketPresets?: ArchitecturePreset[]
  marketPreviewData?: ArchitecturePresetDetail | null
  onMarketBrowse?: () => void
  onMarketPreview?: (presetId: string) => void
  onMarketApplyPreset?: (presetId: string, strategy: string) => void
  onMarketClearPreview?: () => void
  // Config import/export
  onConfigExport?: () => void
  onConfigImport?: (yaml: string, dryRun: boolean) => void
  configExportYaml?: string | null
  configImportPreview?: { roles_added: number; roles_removed: number; employees_changed: number } | null
  configImportError?: string | null
  // Saved org architectures (named snapshots) — rendered in the Team tab toolbar
  onSavedOrgsList?: () => void
  onSavedOrgSaveAs?: (name: string, overwrite: boolean) => void
  onSavedOrgCreate?: (organizationName: string, members: OrgCreateMemberInput[]) => void
  onSavedOrgLoad?: (name: string) => void
  onSavedOrgDelete?: (name: string) => void
  savedOrgsList?: SavedOrgSummary[] | null
  activeSavedOrg?: string | null
  activeSavedOrgVersionAtLoad?: number | null
  orgCreatePending?: boolean
  orgCreateResult?: (OrgSavedCreatePayload & { nonce: number }) | null
  onSelectCorporate?: () => void
  // Org editing
  onAddRole?: (roleId: string, name: string, responsibility: string, reportsTo: string, icon?: string | null) => void
  onBulkAddRoles?: (roles: Array<{ role_id: string; name: string; responsibility: string; reports_to: string }>) => void
  onUpdateRole?: (roleId: string, updates: { name?: string; responsibility?: string; reports_to?: string; can_spawn?: string[]; icon?: string | null; execution_strategy?: string; preferred_external_agent?: string | null; prompt_refs?: string[] }) => void
  onDeleteRole?: (roleId: string) => void
  onUpdateOrgStrategy?: (data: { final_decider_role_id?: string | null }) => void
  onUpdateRuntimePolicy?: (policy: Record<string, any>) => void
  onResetArchitecture?: () => void
}

export function OrgTab({
  data, sessionRecruitmentByRole, talents, employeeDetail, reorgProposals, isCustomMode,
  onRequestData, onRequestTalents, onRequestEmployeeDetail,
  onHireTalent, hiringTemplateId, onImportEmployee, onRequestReorgList, onReorgDecide,
  onMarketExport, onMarketInstall, onMarketUninstall,
  marketPresets, marketPreviewData, onMarketBrowse, onMarketPreview, onMarketApplyPreset, onMarketClearPreview,
  onAddRole, onBulkAddRoles, onUpdateRole, onDeleteRole, onUpdateOrgStrategy,
  onUpdateRuntimePolicy, onResetArchitecture,
  onConfigExport, onConfigImport, configExportYaml, configImportPreview, configImportError,
  onSavedOrgsList, onSavedOrgSaveAs, onSavedOrgCreate, onSavedOrgLoad, onSavedOrgDelete, savedOrgsList,
  activeSavedOrg, activeSavedOrgVersionAtLoad, orgCreatePending, orgCreateResult, onSelectCorporate,
}: OrgTabProps) {
  const { t } = useI18n()
  const [activeTab, setActiveTab] = useState<SubTab>('team')
  const [createOpen, setCreateOpen] = useState(false)

  const switchTab = useCallback((tab: SubTab) => {
    setActiveTab(tab)
  }, [])
  const [applyingPresetId, setApplyingPresetId] = useState<string | null>(null)
  const versionAtApply = useRef<number>(-1) // track org_version when apply starts
  const [toast, setToast] = useState<{ msg: string; type: 'info' | 'warn' } | null>(null)
  const toastTimer = useRef<ReturnType<typeof setTimeout>>(null)

  const showToast = useCallback((msg: string, type: 'info' | 'warn' = 'info') => {
    setToast({ msg, type })
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToast(null), 3000)
  }, [])

  useEffect(() => () => {
    if (toastTimer.current) clearTimeout(toastTimer.current)
  }, [])

  const onRequestDataRef = useRef(onRequestData)
  const onRequestTalentsRef = useRef(onRequestTalents)
  const onRequestReorgListRef = useRef(onRequestReorgList)
  const onMarketBrowseRef = useRef(onMarketBrowse)
  const onSavedOrgsListRef = useRef(onSavedOrgsList)
  onRequestDataRef.current = onRequestData
  onRequestTalentsRef.current = onRequestTalents
  onRequestReorgListRef.current = onRequestReorgList
  onMarketBrowseRef.current = onMarketBrowse
  onSavedOrgsListRef.current = onSavedOrgsList

  useEffect(() => {
    onRequestDataRef.current()
    onRequestTalentsRef.current()
    onRequestReorgListRef.current()
    onMarketBrowseRef.current?.()
    onSavedOrgsListRef.current?.()
  }, [])

  useEffect(() => {
    if (!orgCreateResult || !orgCreateResult.ok) return
    setCreateOpen(false)
    setActiveTab('team')
    showToast(t('org.createdToast', { name: orgCreateResult.organization_name || orgCreateResult.name }))
  }, [orgCreateResult, showToast, t])

  // In org mode: show only user-owned roles. In company mode: show all roles read-only.
  const allRoles = data?.roles ?? []
  const displayRoles = useMemo(() => isCustomMode ? allRoles.filter(r => !r.is_builtin) : allRoles, [allRoles, isCustomMode])
  const displayEmployees = useMemo(() => data?.employees ?? [], [data?.employees])
  const runtimeView = useMemo(() => getRuntimeOrgView(data), [data])
  const activeAgents = useMemo(() => displayEmployees.filter(e => e.linked_agent_id), [displayEmployees])
  const configuredOrgName = data?.organization_name?.trim()
  const activeOrgLabel = configuredOrgName || humanizeOrgName(activeSavedOrg) || (isCustomMode ? t('org.customOrg') : t('org.corporateCompany'))
  const activeOrgId = (isCustomMode ? (data?.organization_id || activeSavedOrg) : 'corporate') || ''
  const architectureKindLabel = isCustomMode ? t('org.kind.saved') : t('org.kind.corporate')
  const architectureStateLabel = isCustomMode
    ? activeSavedOrg ? t('org.state.editableSaved') : t('org.state.editableDraft')
    : t('org.state.readOnly')
  const runtimeStateLabel = runtimeView.frontier.status || runtimeView.projectRun?.status || runtimeView.projectRun?.lifecycle_status || t('common.ready')

  // Roles that already have at least one non-placeholder employee.
  const filledRoleIds = useMemo(
    () => {
      const ids = new Set<string>()
      for (const employee of displayEmployees) {
        if (employee.is_default_employee) continue
        const roleIds = employee.role_ids?.length ? employee.role_ids : [employee.role_id]
        for (const roleId of roleIds) {
          if (roleId) ids.add(roleId)
        }
      }
      return ids
    },
    [displayEmployees],
  )
  const vacantRoles = useMemo(() => displayRoles.filter(r => !filledRoleIds.has(r.role_id)), [displayRoles, filledRoleIds])

  const installedIds = useMemo(() => new Set((data?.installed_packages ?? []).map(p => p.package_id)), [data?.installed_packages])

  // When applying a preset, wait for org_version to change (every config.save() increments it)
  const orgVersion = data?.org_version ?? 0
  useEffect(() => {
    if (applyingPresetId && versionAtApply.current >= 0 && orgVersion !== versionAtApply.current) {
      setApplyingPresetId(null)
      versionAtApply.current = -1
      setActiveTab('team')
      showToast(t('org.architectureApplied'))
    }
  }, [orgVersion, applyingPresetId, showToast, t])

  const handleApplyPreset = (presetId: string, strategy: string) => {
    versionAtApply.current = orgVersion // snapshot current version
    setApplyingPresetId(presetId)
    onMarketApplyPreset?.(presetId, strategy)
  }

  const handleOrgSelection = (value: string) => {
    if (value === 'corporate') {
      onSelectCorporate?.()
      return
    }
    if (value.startsWith('org:')) {
      const orgName = value.slice(4)
      if (orgName) onSavedOrgLoad?.(orgName)
    }
  }

  if (!data) {
    return <div className="org-tab"><div className="org-loading">{t('org.loading')}</div></div>
  }

  const installedPackages = data.installed_packages ?? []
  const savedOrgOptions = savedOrgsList ?? []
  const selectedOrgValue = isCustomMode && activeSavedOrg ? `org:${activeSavedOrg}` : 'corporate'

  return (
    <div className="org-tab">
      <div className={`org-header${isCustomMode ? ' org-header--custom' : ' org-header--corporate'}`}>
        <div className="org-header-main">
          <div className="org-eyebrow">
            <span>{t('org.company')}</span>
            <span className="org-eyebrow-separator">/</span>
            <span>{architectureKindLabel}</span>
          </div>
          <div className="org-title-row">
            <h2 className="org-title">{activeOrgLabel}</h2>
            <span className="org-version-badge">v{data.org_version}</span>
            <span className={`org-state-badge${isCustomMode ? ' org-state-badge--editable' : ' org-state-badge--readonly'}`}>
              {architectureStateLabel}
            </span>
          </div>
          <div className="org-header-meta">
            <span className="org-meta-pill">{data.company_profile || (isCustomMode ? 'custom' : 'corporate')}</span>
            {activeOrgId && <code className="org-meta-code">{activeOrgId}</code>}
            <span className="org-meta-pill org-meta-pill--runtime">{runtimeStateLabel}</span>
            <span className="org-meta-pill org-meta-pill--saved">{t('common.autoSaved')}</span>
          </div>
        </div>

        <div className="org-control-panel">
          <label className="org-switcher">
            <span className="org-switcher-label">{t('org.organization')}</span>
            <span className="org-switcher-select-wrap">
              <select
                className="org-switcher-select"
                value={selectedOrgValue}
                onChange={e => handleOrgSelection(e.target.value)}
                onFocus={() => onSavedOrgsList?.()}
                onPointerDown={() => onSavedOrgsList?.()}
                aria-label={t('org.organization')}
              >
                <option value="corporate">{t('org.corporate')}</option>
                {savedOrgOptions.map(org => (
                  <option key={org.name} value={`org:${org.name}`}>
                    {(org.organization_name || org.name).trim() || org.name}
                  </option>
                ))}
              </select>
            </span>
          </label>
          <button type="button" className="org-create-trigger" onClick={() => setCreateOpen(true)}>
            <span className="org-create-trigger-icon" aria-hidden>+</span>
            {t('org.newOrganization')}
          </button>
        </div>

        <div className="org-stats-strip">
          <span className="org-stat">
            <img src={STAT_ICON.agents} alt="" className="org-stat-icon" />
            <b>{displayRoles.length}</b> {t('org.roles')}
          </span>
          <span className="org-stat">
            <img src={TAB_ICON.employees} alt="" className="org-stat-icon" />
            <b>{displayEmployees.length}</b> {t('org.employees')}
          </span>
          <span className="org-stat">
            <img src={TAB_ICON.runtime} alt="" className="org-stat-icon" />
            <b>{runtimeView.runtimeTeams.length}</b> {t('org.runtimeTeams')}
          </span>
          <span className="org-stat">
            <img src={STAT_ICON.active} alt="" className="org-stat-icon" />
            <b>{activeAgents.length}</b> {t('common.active')}
          </span>
        </div>
      </div>

      <div className="org-subtabs">
        {([
          { id: 'team' as SubTab, icon: TAB_ICON.team, label: t('org.tab.team'), count: displayRoles.length },
          { id: 'runtime' as SubTab, icon: TAB_ICON.runtime, label: t('org.tab.runtime'), count: runtimeView.runtimeTeams.length },
          { id: 'architecture' as SubTab, icon: TAB_ICON.architecture, label: t('org.tab.architecture'), count: marketPresets?.length ?? 0 },
          { id: 'employees' as SubTab, icon: TAB_ICON.employees, label: t('org.tab.employees'), count: talents.length },
        ]).map(tab => (
          <button key={tab.id}
            className={`org-subtab${activeTab === tab.id ? ' org-subtab--active' : ''}`}
            onClick={() => switchTab(tab.id)}>
            <img src={tab.icon} alt="" className="org-subtab-icon" />
            <span className="org-subtab-label">{tab.label}</span>
            <span className="org-subtab-count">{tab.count}</span>
          </button>
        ))}
      </div>

      {/* ── Tab content ─────────────────────────────────── */}
      {/* Toast notification */}
      {toast && (
        <div className={`org-toast org-toast--${toast.type}`} onClick={() => setToast(null)}>
          {toast.msg}
        </div>
      )}

      <div className="org-tab-content">

        {/* Team tab */}
        {activeTab === 'team' && (
          <TeamView
            roles={displayRoles}
            employees={displayEmployees}
            sessionRecruitmentByRole={sessionRecruitmentByRole}
            isCustomMode={isCustomMode}
            onAddRole={onAddRole ?? (() => {})}
            onBulkAddRoles={onBulkAddRoles}
            onUpdateRole={onUpdateRole ?? (() => {})}
            onDeleteRole={onDeleteRole ?? (() => {})}
            onExport={onMarketExport ?? (() => {})}
            onImportEmployee={onImportEmployee}
            onResetArchitecture={onResetArchitecture}
            onSwitchToTab={(target) => setActiveTab(target)}
            savedOrgsList={savedOrgsList}
            activeSavedOrg={activeSavedOrg ?? null}
            currentOrgVersion={data?.org_version ?? 0}
            versionAtLoad={activeSavedOrgVersionAtLoad ?? null}
            onSavedOrgsList={onSavedOrgsList}
            onSavedOrgSaveAs={onSavedOrgSaveAs}
            onSavedOrgLoad={onSavedOrgLoad}
            onSavedOrgDelete={onSavedOrgDelete}
          />
        )}

        {/* Runtime tab */}
        {activeTab === 'runtime' && (
          <DelegationStrategyPanel
            roles={displayRoles}
            runtimeTeams={runtimeView.runtimeTeams}
            runtimeSeats={runtimeView.runtimeSeats}
            workItems={runtimeView.workItems}
            frontier={runtimeView.frontier}
            companyProfile={data.company_profile}
            runtimePolicy={data.runtime_policy}
            finalDeciderRoleId={data.final_decider_role_id}
            topLevelRoleIds={data.top_level_role_ids}
            readOnly={!isCustomMode}
            onUpdateOrgStrategy={onUpdateOrgStrategy}
            onUpdateRuntimePolicy={onUpdateRuntimePolicy}
          />
        )}

        {/* Architecture tab */}
        {activeTab === 'architecture' && (
          <>
            <ArchitectureMarketplace
              presets={marketPresets ?? []}
              installedIds={installedIds}
              previewData={marketPreviewData ?? null}
              applyingPresetId={applyingPresetId}
              readOnly={!isCustomMode}
              onPreview={onMarketPreview ?? (() => {})}
              onApplyPreset={handleApplyPreset}
              onClearPreview={onMarketClearPreview ?? (() => {})}
              installedPackages={installedPackages}
              channels={data.channels}
              reorgProposals={reorgProposals}
              isCustomMode={!!isCustomMode}
              onReorgDecide={onReorgDecide}
              onMarketInstall={(p, s) => onMarketInstall?.(p, s)}
              onMarketUninstall={(id) => onMarketUninstall?.(id)}
            />
            {isCustomMode && (
              <ConfigImportExportPanel
                onExport={onConfigExport ?? (() => {})}
                onImport={onConfigImport ?? (() => {})}
                configExportYaml={configExportYaml ?? null}
                importPreview={configImportPreview ?? null}
                importError={configImportError ?? null}
              />
            )}
          </>
        )}

        {/* Employees tab */}
        {activeTab === 'employees' && (
          <EmployeesMarketplace
            templates={talents}
            vacantRoles={vacantRoles}
            hiringTemplateId={hiringTemplateId ?? null}
            readOnly={!isCustomMode}
            onHireTalent={onHireTalent}
          />
        )}
      </div>
      <OrgCreateModal
        open={createOpen}
        pending={orgCreatePending}
        result={orgCreateResult ?? null}
        onClose={() => setCreateOpen(false)}
        onCreate={(organizationName, members) => onSavedOrgCreate?.(organizationName, members)}
      />
    </div>
  )
}
