import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { buildDynamicTeamCoverage, StaffingSelectionPanel } from './StaffingSelectionPanel'

const markup = renderToStaticMarkup(
  React.createElement(StaffingSelectionPanel, {
    meta: {
      checkpoint_type: 'company_staffing_selection',
      checkpoint_id: 'cp-staffing',
      company_profile: 'corporate',
      summary: 'Select staff manually, or run automatic recruitment.',
      staffing_roles: [
        {
          role_id: 'senior_engineer',
          role_label: 'Senior Engineer',
          default_selection: { kind: 'employee', id: 'senior-existing' },
          default_agent: 'codex',
          selected_agent: 'codex',
        },
      ],
      staffing_pool: {
        employees: [
          {
            employee_id: 'senior-existing',
            employee_name: 'Existing Engineer',
            role_id: 'senior_engineer',
            category: 'engineering',
          },
        ],
        templates: [
          {
            template_id: 'engineering-frontend-developer',
            template_name: 'Frontend Developer',
            category: 'engineering',
          },
        ],
      },
    },
    onReply: () => undefined,
    responded: false,
  }),
)

assert.match(markup, /Manual Staffing/)
assert.match(markup, /Existing Engineer/)
assert.match(markup, /Frontend Developer/)
assert.match(markup, /Approve/)
assert.match(markup, /Auto Recruit/)

const teamCoverage = buildDynamicTeamCoverage(
  [
    { role_id: 'cmo', role_label: 'CMO', reports_to: 'ceo' },
    { role_id: 'content_specialist', role_label: 'Content', reports_to: 'cmo' },
    { role_id: 'designer', role_label: 'Designer', reports_to: 'cmo' },
  ],
  { cmo: 'jiuwenswarm', content_specialist: 'codex', designer: 'claude_code' },
)
assert.equal(teamCoverage.boundaryByRole.get('content_specialist'), 'cmo')
assert.deepEqual(teamCoverage.coveredRoleIdsByBoundary.get('cmo'), ['cmo', 'content_specialist', 'designer'])

const here = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(here, 'StaffingSelectionPanel.tsx'), 'utf8')
assert.match(src, /staffing_action: action/, 'panel replies must send structured staffing_action metadata')
assert.match(src, /staffing_selections: selections/, 'panel replies must send structured staffing selections')
assert.match(src, /recruitment_agent: recruitmentAgent/, 'panel replies must send the selected recruiter agent')
assert.match(src, /hasSubmittedCheckpointMetadata/, 'responded staffing cards must detect persisted reply metadata')
assert.match(src, /setRoleAgents\(buildRoleAgentsFromMeta\(meta, roles\)\)/, 'responded staffing cards must sync displayed agent choices from reply metadata')

console.log('StaffingSelectionPanel.test.tsx: OK (manual staffing panel renders structured choices)')
