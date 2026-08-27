import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { RecruitmentPanel } from './RecruitmentPanel'

const markup = renderToStaticMarkup(
  React.createElement(RecruitmentPanel, {
    meta: {
      checkpoint_type: 'company_recruitment_confirmation',
      checkpoint_id: 'cp-recruit',
      company_profile: 'corporate',
      summary: 'Company mode has a pending staffing decision before execution.',
      proposals: [
        {
          role_id: 'senior_engineer',
          status: 'proposed_hire',
          rationale: 'Selected the strongest backend option.',
          role_labels: ['Senior Engineer'],
          candidate: {
            template_id: 'engineering-backend-architect',
            template_name: 'Backend Architect',
            category: 'engineering',
            domains: ['backend', 'api'],
            proposed_name: 'Backend Architect',
            rationale: 'Strong API architecture fit.',
          },
          existing_employee_ids: [],
          default_agent: 'codex',
          selected_agent: 'codex',
        },
      ],
      recruitment_rationales: [
        {
          role_id: 'senior_engineer',
          role_label: 'Senior Engineer',
          status: 'proposed_hire',
          selection_label: 'Backend Architect',
          rationale: 'Strong API architecture fit.',
        },
      ],
      staffing_roles: [
        {
          role_id: 'senior_engineer',
          role_label: 'Senior Engineer',
          default_selection: { kind: 'template', id: 'engineering-backend-architect' },
          default_agent: 'codex',
          selected_agent: 'codex',
          same_role_employee_ids: [],
        },
      ],
      staffing_pool: {
        employees: [],
        templates: [
          {
            template_id: 'engineering-backend-architect',
            template_name: 'Backend Architect',
            category: 'engineering',
            domains: ['backend', 'api'],
          },
        ],
      },
      staffing_selections: {
        senior_engineer: { kind: 'template', id: 'engineering-backend-architect' },
      },
    },
    onReply: async () => true,
    responded: false,
  }),
)

assert.match(markup, /Recruitment Review/)
assert.match(markup, /Strong API architecture fit/)
assert.match(markup, /ckpt-staffing-grid/)
assert.match(markup, /Backend Architect/)
assert.match(markup, /Approve/)
assert.match(markup, /Send Feedback/)
assert.doesNotMatch(markup, /Deny/)

const source = readFileSync(new URL('./RecruitmentPanel.tsx', import.meta.url), 'utf8')
assert.match(source, /buildReplyMetadata\('approve'\)/)
assert.match(source, /buildReplyMetadata\('feedback'\)/)
assert.match(source, /recruitment_agent: recruitmentAgent/)
assert.match(source, /hasSubmittedCheckpointMetadata/, 'responded recruitment cards must detect persisted reply metadata')
assert.match(source, /setRoleAgents\(buildRoleAgentsFromMeta\(meta, roles\)\)/, 'responded recruitment cards must sync displayed agent choices from reply metadata')
assert.match(source, /if \(accepted\) setFeedback\(''\)/, 'feedback must only clear after the durable ACK')

console.log('RecruitmentPanel.test.tsx: OK (recruitment review uses staffing-style UI)')
