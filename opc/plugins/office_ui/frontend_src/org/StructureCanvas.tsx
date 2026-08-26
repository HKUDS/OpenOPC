import { useMemo, useCallback, useState, useEffect, useRef, useImperativeHandle, forwardRef } from 'react'
import { ReactFlow, Background, Controls, MiniMap, ReactFlowProvider, applyNodeChanges } from '@xyflow/react'
import type { Node, Edge, NodeChange } from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import type { OrgRole, OrgEmployee } from '../types/visual'
import { StructureCanvasNode, type StructureCanvasNodeData } from './StructureCanvasNode'
import { computeDagreLayout } from './dagreLayout'

const nodeTypes = { roleNode: StructureCanvasNode }

export interface StructureCanvasHandle {
  /** Re-run dagre and animate nodes to tidy positions. */
  autoLayout: () => void
}

interface StructureCanvasProps {
  roles: OrgRole[]
  employees: OrgEmployee[]
  /**
   * role_id -> recruited names for the currently selected session. When
   * provided it takes precedence over the global `employees` for the node
   * subtitle, so the canvas reflects the selected session's hires. Null/absent
   * -> fall back to the global org employees.
   */
  sessionRecruitmentByRole?: Record<string, string[]> | null
  selectedRoleId: string | null
  onSelectRole: (roleId: string | null) => void
  onReparent: (roleId: string, newParentId: string) => void
  readOnly?: boolean
}

/**
 * Constrained canvas for org structure.
 * Positions are managed internally. See D1 + D3 in the plan doc:
 *  - dagre runs on mount, on role add/delete, and on explicit autoLayout() calls.
 *  - Role-field updates do NOT reflow the graph.
 *  - Reparenting reflows (a new parent->child edge would leave the graph stale).
 */
export const StructureCanvas = forwardRef<StructureCanvasHandle, StructureCanvasProps>(function StructureCanvas(props, ref) {
  return (
    <ReactFlowProvider>
      <StructureCanvasInner {...props} forwardedRef={ref} />
    </ReactFlowProvider>
  )
})

function StructureCanvasInner({ roles, employees, sessionRecruitmentByRole, selectedRoleId, onSelectRole, onReparent, readOnly, forwardedRef }: StructureCanvasProps & { forwardedRef: React.ForwardedRef<StructureCanvasHandle> }) {
  const employeesByRole = useMemo(() => {
    const m = new Map<string, OrgEmployee[]>()
    for (const e of employees) {
      const roleIds = e.role_ids?.length ? e.role_ids : [e.role_id]
      for (const roleId of roleIds) {
        if (!roleId) continue
        const arr = m.get(roleId) ?? []
        arr.push(e)
        m.set(roleId, arr)
      }
    }
    return m
  }, [employees])

  // Names of the actually recruited people per role.
  //  - When the selected session carries a recruitment map, it is authoritative
  //    (a role absent from it is unstaffed *for that session*).
  //  - Otherwise fall back to the global org employees, excluding placeholder/
  //    default employees (which carry the role name itself, not a real hire).
  const recruitedNamesByRole = useCallback(
    (roleId: string): string[] => {
      if (sessionRecruitmentByRole) return sessionRecruitmentByRole[roleId] ?? []
      return (employeesByRole.get(roleId) ?? [])
        .filter(e => !e.is_default_employee)
        .map(e => e.name)
        .filter(Boolean)
    },
    [employeesByRole, sessionRecruitmentByRole],
  )

  // Layout invalidation key. Incrementing -> dagre re-runs.
  const [layoutVersion, setLayoutVersion] = useState(0)

  // Re-layout automatically when the set of role IDs changes (add/delete).
  const roleIdsKey = useMemo(() => roles.map(r => r.role_id).sort().join('|'), [roles])
  const prevRoleIdsKeyRef = useRef(roleIdsKey)
  useEffect(() => {
    if (prevRoleIdsKeyRef.current !== roleIdsKey) {
      prevRoleIdsKeyRef.current = roleIdsKey
      setLayoutVersion(v => v + 1)
    }
  }, [roleIdsKey])

  // Expose imperative autoLayout() to parent so the "Auto-layout" button can trigger it.
  useImperativeHandle(forwardedRef, () => ({
    autoLayout: () => setLayoutVersion(v => v + 1),
  }), [])

  const [dropTargetId, setDropTargetId] = useState<string | null>(null)

  // Free-form drag positions: a node the user has dragged sticks where it was
  // dropped (purely visual — never changes the org structure). Cleared whenever
  // dagre re-runs (autoLayout / role add-delete / reparent) so an explicit
  // "Auto-layout" tidies everything back into the hierarchy.
  const [manualPositions, setManualPositions] = useState<Record<string, { x: number; y: number }>>({})
  useEffect(() => { setManualPositions({}) }, [layoutVersion])

  // Compute layout once per layoutVersion bump (NOT on every roles update).
  const laidOut = useMemo(() => {
    const ownerNode: Node<StructureCanvasNodeData> = {
      id: 'owner',
      type: 'roleNode',
      position: { x: 0, y: 0 },
      draggable: false,
      data: {
        roleId: 'owner', name: 'You (Owner)', responsibility: '',
        icon: null, employeeCount: 0, employeeNames: [],
        isOwner: true, isSelected: false, isDropTarget: false,
        externalTeamLabel: undefined, externalTeamBoundary: false,
      },
    }
    const roleNodes: Node<StructureCanvasNodeData>[] = roles.map(r => ({
      id: r.role_id,
      type: 'roleNode',
      position: { x: 0, y: 0 },
      // Always draggable: in editable mode a drop onto another node reparents;
      // otherwise the drag is a free visual reposition (no structural change).
      draggable: true,
      data: {
        roleId: r.role_id, name: r.name, responsibility: r.responsibility,
        icon: r.icon ?? null, employeeCount: recruitedNamesByRole(r.role_id).length,
        employeeNames: recruitedNamesByRole(r.role_id),
        isOwner: false, isSelected: false, isDropTarget: false,
        externalTeamLabel: r.covered_by_external_team
          ? (r.external_team_boundary ? 'JiuwenSwarm Team' : `via ${r.external_team_boundary_role_id ?? 'team boundary'}`)
          : undefined,
        externalTeamBoundary: !!r.external_team_boundary,
      },
    }))
    const all = [ownerNode, ...roleNodes]
    const edges: Edge[] = roles.map(r => ({
      id: `e-${r.reports_to}-${r.role_id}`,
      source: r.reports_to,
      target: r.role_id,
      type: 'smoothstep',
    }))
    const positioned = computeDagreLayout(all, edges)
    return { nodes: positioned, edges }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- layoutVersion is the explicit reflow trigger
  }, [layoutVersion, readOnly])

  // Live nodes: start from laid-out positions; overlay current role data
  // (name/icon/etc.) without re-running dagre.
  const liveNodes = useMemo(() => {
    return laidOut.nodes.map(n => {
      if (n.id === 'owner') return n
      const role = roles.find(r => r.role_id === n.id)
      if (!role) return n  // node for a deleted role -- invariant: layoutVersion will bump and this clears
      const manual = manualPositions[n.id]
      return {
        ...n,
        position: manual ?? n.position,
        draggable: true,
        data: {
          ...n.data,
          name: role.name,
          responsibility: role.responsibility,
          icon: role.icon ?? null,
          employeeCount: recruitedNamesByRole(role.role_id).length,
          employeeNames: recruitedNamesByRole(role.role_id),
          isSelected: role.role_id === selectedRoleId,
          isDropTarget: role.role_id === dropTargetId,
          externalTeamLabel: role.covered_by_external_team
            ? (role.external_team_boundary ? 'JiuwenSwarm Team' : `via ${role.external_team_boundary_role_id ?? 'team boundary'}`)
            : undefined,
          externalTeamBoundary: !!role.external_team_boundary,
        },
      }
    })
  }, [laidOut.nodes, roles, employeesByRole, recruitedNamesByRole, selectedRoleId, dropTargetId, readOnly, manualPositions])

  const [stateNodes, setStateNodes] = useState(liveNodes)
  useEffect(() => { setStateNodes(liveNodes) }, [liveNodes])

  const handleNodesChange = useCallback((changes: NodeChange[]) => {
    setStateNodes(curr => applyNodeChanges(changes, curr))
  }, [])

  const handleNodeDrag = useCallback((_evt: any, node: Node) => {
    // Reparent drop-target highlighting only applies in editable mode. In
    // read-only mode the drag is a pure visual reposition -- no target.
    if (readOnly) return
    // Bounding-box hit test for drop target
    const W = 220, H = 80
    const pt = { x: node.position.x + W / 2, y: node.position.y + H / 2 }
    const hit = stateNodes.find(n => {
      if (n.id === node.id) return false
      return pt.x >= n.position.x && pt.x <= n.position.x + W &&
             pt.y >= n.position.y && pt.y <= n.position.y + H
    })
    setDropTargetId(hit?.id ?? null)
  }, [stateNodes, readOnly])

  const handleNodeDragStop = useCallback((_evt: any, node: Node) => {
    const target = dropTargetId
    setDropTargetId(null)
    // Remember the dropped position so the node stays put (visual only).
    const keepPosition = () =>
      setManualPositions(prev => ({ ...prev, [node.id]: { x: node.position.x, y: node.position.y } }))
    // No valid reparent (read-only, no/own target, or a cycle) -> free reposition.
    if (readOnly || !target || target === node.id || isDescendant(roles, node.id, target)) {
      keepPosition()
      return
    }
    onReparent(node.id, target)
    // A reparent changes the edge structure -- reflow (also clears manual positions).
    setLayoutVersion(v => v + 1)
  }, [dropTargetId, roles, readOnly, onReparent])

  const handleNodeClick = useCallback((_evt: any, node: Node) => {
    onSelectRole(node.id === 'owner' ? null : node.id)
  }, [onSelectRole])

  // @xyflow/react v12's `.react-flow` CSS class does NOT set height/width.
  // Wrap in a sized div so the canvas has a definite box to render into —
  // this is the library's documented integration pattern for v12.
  return (
    <div style={{ width: '100%', height: '100%' }}>
      <ReactFlow
        nodes={stateNodes}
        edges={laidOut.edges}
        nodeTypes={nodeTypes}
        onNodesChange={handleNodesChange}
        onNodeDrag={handleNodeDrag}
        onNodeDragStop={handleNodeDragStop}
        onNodeClick={handleNodeClick}
        fitView
        fitViewOptions={{ padding: 0.32, minZoom: 0.45, maxZoom: 1.2 }}
        nodesConnectable={false}
        edgesFocusable={false}
        style={{ width: '100%', height: '100%' }}
      >
        <Background gap={28} size={1} color="rgba(240, 237, 232, 0.10)" />
        <Controls showInteractive={false} />
        <MiniMap pannable nodeStrokeWidth={0} maskColor="rgba(12, 17, 27, 0.6)" />
      </ReactFlow>
    </div>
  )
}

// Note: no ReactFlow `proOptions.hideAttribution` used -- @xyflow/react v12 is MIT
// with the attribution fully removed at the library level, so no workaround needed.

/** Helper: is `candidateDescendantId` a descendant of `rootId`? */
function isDescendant(roles: OrgRole[], rootId: string, candidateDescendantId: string): boolean {
  const children = roles.filter(r => r.reports_to === rootId).map(r => r.role_id)
  for (const c of children) {
    if (c === candidateDescendantId) return true
    if (isDescendant(roles, c, candidateDescendantId)) return true
  }
  return false
}
