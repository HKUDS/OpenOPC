import { memo } from 'react'
import { Handle, Position } from '@xyflow/react'
import { resolveRoleIcon } from './roleIcons'

export interface StructureCanvasNodeData {
  [key: string]: unknown
  roleId: string
  name: string
  responsibility: string
  icon: string | null
  employeeCount: number
  /** Names of the actual recruited (non-placeholder) people staffed on this role. */
  employeeNames: string[]
  isOwner: boolean
  isSelected: boolean
  isDropTarget: boolean
  externalTeamLabel?: string
  externalTeamBoundary?: boolean
}

/**
 * Canvas node — single-accent refined card.
 * Outer <div> always carries .oc-canvas-node (plus optional state
 * modifiers) — this is the E2E anchor. Icon is rendered via CSS
 * `mask-image` so its tint can track the active theme's --accent;
 * the card reads consistently under Paper, OpenOPC, etc.
 */
export const StructureCanvasNode = memo(function StructureCanvasNode({ data }: { data: StructureCanvasNodeData }) {
  const stateClass = [
    'oc-canvas-node',
    data.isOwner && 'is-owner',
    data.isSelected && 'is-selected',
    data.isDropTarget && 'is-drop-target',
    data.externalTeamBoundary && 'is-external-team-boundary',
  ].filter(Boolean).join(' ')

  const iconSrc = resolveRoleIcon(data.icon)

  return (
    <div className={stateClass}>
      <Handle type="target" position={Position.Top} className="oc-canvas-handle" />
      <div className="oc-canvas-node-row">
        <div className="oc-canvas-node-chip">
          <span
            className="oc-canvas-node-chip-icon"
            style={{
              WebkitMaskImage: `url("${iconSrc}")`,
              maskImage: `url("${iconSrc}")`,
            }}
            aria-hidden
          />
        </div>
        <div className="oc-canvas-node-text">
          <span className="oc-canvas-node-name">{data.name}</span>
          {data.externalTeamLabel && (
            <span className="oc-canvas-node-team" title={data.externalTeamLabel}>
              {data.externalTeamBoundary ? 'JiuwenSwarm Team' : data.externalTeamLabel}
            </span>
          )}
          <div className="oc-canvas-node-meta">
            {data.employeeNames.length > 0 ? (
              <span
                className="oc-canvas-node-person"
                title={data.employeeNames.join(', ')}
              >
                {data.employeeNames[0]}
                {data.employeeNames.length > 1 ? ` +${data.employeeNames.length - 1}` : ''}
              </span>
            ) : (
              <span className="oc-canvas-node-id">{data.roleId}</span>
            )}
            {data.employeeCount > 0 && (
              <span className="oc-canvas-node-badge">{data.employeeCount}</span>
            )}
          </div>
        </div>
      </div>
      <Handle type="source" position={Position.Bottom} className="oc-canvas-handle" />
    </div>
  )
})
