/**
 * RoleTable — Tanstack-table bulk editor for roles.
 *
 * Columns:
 *   - checkbox          (multi-select)
 *   - icon              (click to open IconPicker — deferred; currently read-only)
 *   - name              (inline editable via double-click)
 *   - role_id           (monospace, immutable)
 *   - reports_to        (dropdown)
 *   - tools             (count, clickable to open popover)
 *   - agent             (select)
 *   - employees         (count)
 *   - actions           (⋯ menu: Delete)
 *
 * Row click → selects the row in StructureEditor (opens Inspector).
 * Multi-select → bulk-edit bar at top ("Change agent for N roles", etc.)
 */
import { useMemo, useState, type ChangeEvent } from 'react'
import {
  useReactTable, getCoreRowModel, getSortedRowModel, flexRender,
  type ColumnDef, type SortingState,
} from '@tanstack/react-table'
import type { OrgRole, OrgEmployee } from '../types/visual'
import { TASK_AGENT_OPTIONS, executionAgentLabel } from '../lib/externalAgents'
import { resolveRoleIcon } from './roleIcons'

const EXTERNAL_AGENTS = TASK_AGENT_OPTIONS.filter(agent => agent !== 'native')

interface RoleTableProps {
  roles: OrgRole[]
  employees: OrgEmployee[]
  selectedIds: string[]
  onSelectRow: (id: string) => void
  onUpdateRole: (roleId: string, updates: {
    name?: string
    reports_to?: string
    preferred_external_agent?: string | null
  }) => void
  onDeleteRole: (roleId: string) => void
  readOnly?: boolean
}

interface TableRow {
  role_id: string
  name: string
  icon: string | null
  reports_to: string
  toolCount: number
  agent: string | null
  employeeCount: number
  coveredByExternalTeam: boolean
  externalTeamBoundary: boolean
  externalTeamBoundaryRoleId: string | null
}

/* ── RoleTable ───────────────────────────────────────────────── */

export function RoleTable({
  roles, employees, selectedIds, onSelectRow,
  onUpdateRole, onDeleteRole, readOnly,
}: RoleTableProps) {
  const [sorting, setSorting] = useState<SortingState>([])
  const [rowSelection, setRowSelection] = useState<Record<string, boolean>>({})
  const [editingCell, setEditingCell] = useState<{ rowId: string; col: 'name' } | null>(null)
  const [nameBuffer, setNameBuffer] = useState('')

  const data: TableRow[] = useMemo(() => {
    const countByRole = new Map<string, number>()
    for (const e of employees) {
      const roleIds = e.role_ids?.length ? e.role_ids : [e.role_id]
      for (const roleId of roleIds) {
        if (roleId) countByRole.set(roleId, (countByRole.get(roleId) ?? 0) + 1)
      }
    }
    return roles.map(r => ({
      role_id: r.role_id,
      name: r.name,
      icon: r.icon ?? null,
      reports_to: r.reports_to,
      toolCount: r.tools?.length ?? 0,
      agent: r.preferred_external_agent ?? null,
      employeeCount: countByRole.get(r.role_id) ?? 0,
      coveredByExternalTeam: Boolean(r.covered_by_external_team),
      externalTeamBoundary: Boolean(r.external_team_boundary),
      externalTeamBoundaryRoleId: r.external_team_boundary_role_id ?? null,
    }))
  }, [roles, employees])

  const reportsToOptions = useMemo(
    () => [{ id: 'owner', name: 'Owner' }, ...roles.map(r => ({ id: r.role_id, name: r.name }))],
    [roles],
  )

  const commitName = (rowId: string) => {
    const trimmed = nameBuffer.trim()
    if (trimmed && trimmed !== roles.find(r => r.role_id === rowId)?.name) {
      onUpdateRole(rowId, { name: trimmed })
    }
    setEditingCell(null)
    setNameBuffer('')
  }

  const columns: ColumnDef<TableRow>[] = useMemo(() => [
    {
      id: 'select',
      header: ({ table }) => (
        <input
          type="checkbox"
          checked={table.getIsAllRowsSelected()}
          ref={el => { if (el) el.indeterminate = table.getIsSomeRowsSelected() && !table.getIsAllRowsSelected() }}
          onChange={table.getToggleAllRowsSelectedHandler()}
          disabled={readOnly}
          aria-label="Select all"
        />
      ),
      cell: ({ row }) => (
        <input
          type="checkbox"
          checked={row.getIsSelected()}
          onChange={row.getToggleSelectedHandler()}
          disabled={readOnly}
          aria-label={`Select ${row.original.role_id}`}
          onClick={e => e.stopPropagation()}
        />
      ),
      size: 32,
      enableSorting: false,
    },
    {
      id: 'icon',
      header: '',
      cell: ({ row }) => (
        <img src={resolveRoleIcon(row.original.icon)} alt="" className="rt-cell-icon" />
      ),
      size: 32,
      enableSorting: false,
    },
    {
      id: 'name',
      header: 'Name',
      accessorKey: 'name',
      cell: ({ row }) => {
        const r = row.original
        const isEditing = editingCell?.rowId === r.role_id && editingCell.col === 'name'
        if (isEditing) {
          return (
            <input
              autoFocus
              className="rt-inline-input"
              value={nameBuffer}
              onChange={e => setNameBuffer(e.target.value)}
              onBlur={() => commitName(r.role_id)}
              onKeyDown={e => {
                if (e.key === 'Enter') commitName(r.role_id)
                else if (e.key === 'Escape') { setEditingCell(null); setNameBuffer('') }
              }}
              onClick={e => e.stopPropagation()}
            />
          )
        }
        return (
          <span
            className="rt-cell-name"
            onDoubleClick={e => {
              if (readOnly) return
              e.stopPropagation()
              setNameBuffer(r.name)
              setEditingCell({ rowId: r.role_id, col: 'name' })
            }}
            title="Double-click to edit"
          >
            {r.name}
          </span>
        )
      },
    },
    {
      id: 'role_id',
      header: 'ID',
      accessorKey: 'role_id',
      cell: ({ row }) => <code className="rt-cell-id">{row.original.role_id}</code>,
    },
    {
      id: 'reports_to',
      header: 'Reports to',
      accessorKey: 'reports_to',
      cell: ({ row }) => {
        const r = row.original
        if (r.coveredByExternalTeam) {
          return (
            <span className="rt-team-agent" title={r.externalTeamBoundary
              ? 'This role and its descendants execute as one opaque team.'
              : `Covered by the team at ${r.externalTeamBoundaryRoleId ?? 'the boundary role'}`}>
              {r.externalTeamBoundary ? 'JiuwenSwarm Team' : `Covered by ${r.externalTeamBoundaryRoleId ?? 'team'}`}
            </span>
          )
        }
        return (
          <select
            className="rt-cell-select"
            value={r.reports_to}
            onChange={(e: ChangeEvent<HTMLSelectElement>) => onUpdateRole(r.role_id, { reports_to: e.target.value })}
            disabled={readOnly}
            onClick={e => e.stopPropagation()}
          >
            {reportsToOptions.filter(o => o.id !== r.role_id).map(o => (
              <option key={o.id} value={o.id}>{o.name}</option>
            ))}
          </select>
        )
      },
    },
    {
      id: 'toolCount',
      header: 'Tools',
      accessorKey: 'toolCount',
      cell: ({ row }) => (
        <span className="rt-cell-count">{row.original.toolCount}</span>
      ),
      size: 64,
    },
    {
      id: 'agent',
      header: 'Agent',
      accessorKey: 'agent',
      cell: ({ row }) => {
        const r = row.original
        return (
          <select
            className="rt-cell-select"
            value={r.agent ?? ''}
            onChange={(e: ChangeEvent<HTMLSelectElement>) => onUpdateRole(r.role_id, { preferred_external_agent: e.target.value || null })}
            disabled={readOnly}
            onClick={e => e.stopPropagation()}
          >
            <option value="">(auto)</option>
            {EXTERNAL_AGENTS.map(a => <option key={a} value={a}>{executionAgentLabel(a)}</option>)}
          </select>
        )
      },
    },
    {
      id: 'employeeCount',
      header: 'People',
      accessorKey: 'employeeCount',
      cell: ({ row }) => <span className="rt-cell-count">{row.original.employeeCount}</span>,
      size: 64,
    },
    {
      id: 'actions',
      header: '',
      cell: ({ row }) => (
        <button
          className="rt-cell-action"
          onClick={e => {
            e.stopPropagation()
            if (readOnly) return
            if (confirm(`Delete role "${row.original.name}"?`)) onDeleteRole(row.original.role_id)
          }}
          disabled={readOnly}
          title="Delete"
        >✕</button>
      ),
      size: 40,
      enableSorting: false,
    },
  ], [editingCell, nameBuffer, readOnly, reportsToOptions, onUpdateRole, onDeleteRole])

  const table = useReactTable({
    data,
    columns,
    state: { sorting, rowSelection },
    onSortingChange: setSorting,
    onRowSelectionChange: setRowSelection,
    enableRowSelection: true,
    getRowId: (row) => row.role_id,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  })

  const selectedCount = Object.values(rowSelection).filter(Boolean).length
  const selectedRoleIds = Object.keys(rowSelection).filter(id => rowSelection[id])

  /* ── Bulk actions ────────────────────────────────────────── */
  const bulkSetAgent = (agent: string | null) => {
    if (readOnly) return
    selectedRoleIds.forEach(id => onUpdateRole(id, { preferred_external_agent: agent }))
    setRowSelection({})
  }
  const bulkDelete = () => {
    if (readOnly) return
    if (!confirm(`Delete ${selectedCount} roles? This cannot be undone.`)) return
    selectedRoleIds.forEach(id => onDeleteRole(id))
    setRowSelection({})
  }

  return (
    <div className="rt-container">
      {selectedCount > 0 && !readOnly && (
        <div className="rt-bulk-bar">
          <span className="rt-bulk-count">{selectedCount} selected</span>
          <select
            className="rt-bulk-select"
            defaultValue=""
            onChange={e => { const v = e.target.value; if (v) bulkSetAgent(v === '__auto__' ? null : v) }}
          >
            <option value="" disabled>Change agent…</option>
            <option value="__auto__">(auto)</option>
            {EXTERNAL_AGENTS.map(a => <option key={a} value={a}>{executionAgentLabel(a)}</option>)}
          </select>
          <button className="btn btn-danger btn-sm" onClick={bulkDelete}>Delete selected</button>
          <button className="btn btn-ghost btn-sm" onClick={() => setRowSelection({})}>Clear</button>
        </div>
      )}

      <div className="rt-table-wrap">
        <table className="rt-table">
          <thead>
            {table.getHeaderGroups().map(hg => (
              <tr key={hg.id}>
                {hg.headers.map(h => (
                  <th
                    key={h.id}
                    style={{ width: h.getSize() }}
                    className={h.column.getCanSort() ? 'rt-th-sortable' : ''}
                    onClick={h.column.getToggleSortingHandler()}
                  >
                    {flexRender(h.column.columnDef.header, h.getContext())}
                    {h.column.getIsSorted() === 'asc' && <span className="rt-sort-caret"> ▲</span>}
                    {h.column.getIsSorted() === 'desc' && <span className="rt-sort-caret"> ▼</span>}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.length === 0 && (
              <tr><td colSpan={9} className="rt-empty">No roles. Add one via the "+ Add role" button.</td></tr>
            )}
            {table.getRowModel().rows.map(row => {
              const isSelected = selectedIds.includes(row.original.role_id)
              return (
                <tr
                  key={row.id}
                  className={`rt-row${row.getIsSelected() ? ' rt-row-checked' : ''}${isSelected ? ' rt-row-active' : ''}`}
                  onClick={() => onSelectRow(row.original.role_id)}
                >
                  {row.getVisibleCells().map(cell => (
                    <td key={cell.id} style={{ width: cell.column.getSize() }}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  ))}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
