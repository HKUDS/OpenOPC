/**
 * StructureEditor — top-level wrapper for Team sub-tab's role editor.
 *
 * Owns:
 *   - view mode (canvas | table)
 *   - selection state (which role is open in Inspector)
 *   - reparent handler (proxies to onUpdateRole)
 *   - keyboard shortcuts: Escape (close Inspector), Delete (delete selected
 *     role in Canvas mode), F (fit view to graph), ⌘D (duplicate selected)
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { OrgRole, OrgEmployee, SavedOrgSummary } from '../types/visual'
import { OrgVersionSwitcher } from './OrgVersionSwitcher'
import { RoleInspector, type RoleUpdatePatch } from './RoleInspector'
import { RoleTable } from './RoleTable'
import { StructureCanvas, type StructureCanvasHandle } from './StructureCanvas'

interface StructureEditorProps {
  roles: OrgRole[]
  employees: OrgEmployee[]
  /** role_id -> recruited names for the selected session (canvas display only). */
  sessionRecruitmentByRole?: Record<string, string[]> | null
  isCustomMode?: boolean
  organizationId?: string
  onAddRole: (
    roleId: string,
    name: string,
    responsibility: string,
    reportsTo: string,
    icon?: string | null,
  ) => void
  onUpdateRole: (roleId: string, updates: RoleUpdatePatch) => void
  onDeleteRole: (roleId: string) => void
  onBindExternalTeam?: (data: { boundary_role_id: string; organization_id?: string; scope: 'subtree' }) => void
  onUnbindExternalTeam?: (data: { binding_id?: string; boundary_role_id?: string; organization_id?: string }) => void
  // Saved org architectures — render a version-switcher pill in the toolbar
  savedOrgsList?: SavedOrgSummary[] | null
  activeSavedOrg?: string | null
  currentOrgVersion?: number
  versionAtLoad?: number | null
  onSavedOrgsList?: () => void
  onSavedOrgSaveAs?: (name: string, overwrite: boolean) => void
  onSavedOrgLoad?: (name: string) => void
  onSavedOrgDelete?: (name: string) => void
}

type EditorView = 'canvas' | 'table'

export function StructureEditor({
  roles, employees, sessionRecruitmentByRole, isCustomMode, organizationId,
  onAddRole, onUpdateRole, onDeleteRole, onBindExternalTeam, onUnbindExternalTeam,
  savedOrgsList, activeSavedOrg, currentOrgVersion, versionAtLoad,
  onSavedOrgsList, onSavedOrgSaveAs, onSavedOrgLoad, onSavedOrgDelete,
}: StructureEditorProps) {
  const [view, setView] = useState<EditorView>('canvas')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const canvasRef = useRef<StructureCanvasHandle>(null)

  const selectedRole = useMemo(
    () => roles.find(r => r.role_id === selectedId) ?? null,
    [roles, selectedId],
  )

  /** Drag-to-reparent comes from StructureCanvas and turns into a normal role update. */
  const handleReparent = useCallback((roleId: string, newParentId: string) => {
    if (!isCustomMode) return
    onUpdateRole(roleId, { reports_to: newParentId })
  }, [onUpdateRole, isCustomMode])

  /** Toolbar "Auto-layout" — triggers dagre reflow via forwardRef on Canvas. */
  const handleAutoLayout = useCallback(() => {
    canvasRef.current?.autoLayout()
  }, [])

  /**
   * Toolbar "+ Add role" — generates a unique placeholder ID + default label,
   * then selects the new role so Inspector opens ready-to-edit.
   */
  const handleAddRoleQuick = useCallback(() => {
    if (!isCustomMode) return
    const existingIds = new Set(roles.map(r => r.role_id))
    let id = 'new_role'
    let suffix = 1
    while (existingIds.has(id)) { suffix += 1; id = `new_role_${suffix}` }
    onAddRole(id, 'New Role', '', 'owner', null)
    setSelectedId(id)
  }, [roles, onAddRole, isCustomMode])

  /** Duplicate the currently-selected role (⌘D). */
  const handleDuplicateSelected = useCallback(() => {
    if (!isCustomMode || !selectedRole) return
    const existingIds = new Set(roles.map(r => r.role_id))
    let id = `${selectedRole.role_id}_copy`
    let suffix = 1
    while (existingIds.has(id)) { suffix += 1; id = `${selectedRole.role_id}_copy_${suffix}` }
    onAddRole(id, `${selectedRole.name} (copy)`, selectedRole.responsibility, selectedRole.reports_to, selectedRole.icon)
    setSelectedId(id)
  }, [roles, onAddRole, selectedRole, isCustomMode])

  /**
   * Keyboard shortcuts (scoped to StructureEditor via a wrapper ref +
   * document-level listener that first checks whether the event originated
   * from inside the editor). Skips when user is typing in a form field.
   */
  const rootRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      // Don't swallow shortcuts while user types in form fields.
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT' || t.isContentEditable)) return
      // Only react if the editor is on screen (event path touches our root).
      if (rootRef.current && !rootRef.current.contains(t)) return

      if (e.key === 'Escape' && selectedId) {
        setSelectedId(null)
        e.preventDefault()
        return
      }
      if (e.key === 'Delete' && selectedId && isCustomMode) {
        onDeleteRole(selectedId)
        setSelectedId(null)
        e.preventDefault()
        return
      }
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'd' && selectedId && isCustomMode) {
        handleDuplicateSelected()
        e.preventDefault()
        return
      }
      if (e.key.toLowerCase() === 'f' && view === 'canvas') {
        handleAutoLayout()
        e.preventDefault()
        return
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [selectedId, isCustomMode, view, onDeleteRole, handleAutoLayout, handleDuplicateSelected])

  return (
    <div className="se-container" ref={rootRef}>
      <div className="se-toolbar">
        <div className="se-view-switcher" role="tablist" aria-label="Editor view">
          <button
            role="tab"
            aria-selected={view === 'canvas'}
            className={`se-view-btn${view === 'canvas' ? ' is-active' : ''}`}
            onClick={() => setView('canvas')}
          >Canvas</button>
          <button
            role="tab"
            aria-selected={view === 'table'}
            className={`se-view-btn${view === 'table' ? ' is-active' : ''}`}
            onClick={() => setView('table')}
          >Table</button>
        </div>
        <div className={`se-toolbar-actions${isCustomMode ? '' : ' se-toolbar-actions--readonly'}`}>
          {isCustomMode ? (
            <div className="se-saved-org-control">
              <span className="se-toolbar-label">Saved org</span>
              <OrgVersionSwitcher
                savedOrgs={savedOrgsList ?? null}
                activeName={activeSavedOrg ?? null}
                isDirty={versionAtLoad != null && (currentOrgVersion ?? 0) !== versionAtLoad}
                onRefresh={onSavedOrgsList ?? (() => {})}
                onSaveAs={onSavedOrgSaveAs ?? (() => {})}
                onLoad={onSavedOrgLoad ?? (() => {})}
                onDelete={onSavedOrgDelete ?? (() => {})}
              />
            </div>
          ) : (
            <span className="se-readonly-pill">Read-only corporate</span>
          )}
          <div className="se-toolbar-divider" aria-hidden />
          {view === 'canvas' && (
            <button className="btn btn-ghost btn-sm" onClick={handleAutoLayout}>
              Auto-layout
            </button>
          )}
          {isCustomMode && (
            <button className="btn btn-primary btn-sm" onClick={handleAddRoleQuick}>
              + Add role
            </button>
          )}
        </div>
      </div>

      <div className="se-body">
        {view === 'canvas' ? (
          <StructureCanvas
            ref={canvasRef}
            roles={roles}
            employees={employees}
            sessionRecruitmentByRole={sessionRecruitmentByRole}
            selectedRoleId={selectedId}
            onSelectRole={setSelectedId}
            onReparent={handleReparent}
            readOnly={!isCustomMode}
          />
        ) : (
          <RoleTable
            roles={roles}
            employees={employees}
            selectedIds={selectedId ? [selectedId] : []}
            onSelectRow={(id) => setSelectedId(id)}
            onUpdateRole={onUpdateRole}
            onDeleteRole={onDeleteRole}
            readOnly={!isCustomMode}
          />
        )}

        {selectedRole && (
          <RoleInspector
            role={selectedRole}
            allRoles={roles}
            employees={employees}
            readOnly={!isCustomMode}
            teamBindingEditable
            organizationId={organizationId}
            onUpdateRole={onUpdateRole}
            onBindExternalTeam={onBindExternalTeam}
            onUnbindExternalTeam={onUnbindExternalTeam}
            onDeleteRole={(id) => { onDeleteRole(id); setSelectedId(null) }}
            onClose={() => setSelectedId(null)}
          />
        )}
      </div>
    </div>
  )
}
