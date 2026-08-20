import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { EscalationPanel } from './EscalationPanel'

const markup = renderToStaticMarkup(
  React.createElement(EscalationPanel, {
    meta: {
      checkpoint_type: 'company_work_item_gate',
      checkpoint_id: 'cp-gate',
      prompt: 'Gate Review\n\n- Confirm the artifact exists\n- Confirm tests passed\n\n```json\n{"ok": true}\n```',
      summary: 'Review the gate evidence.',
      options: [
        { id: 'approve', label: 'Approve' },
        { id: 'deny', label: 'Deny' },
      ],
      active_subagents: [{ id: 'sub-1' }],
      worktree_path: '/tmp/work',
    },
    onReply: () => undefined,
    responded: false,
  }),
)

assert.match(markup, /Gate Review/)
assert.match(markup, /<li>Confirm the artifact exists<\/li>/)
assert.match(markup, /<code class="language-json">/)
assert.match(markup, /<summary>Runtime State<\/summary>/)
assert.equal((markup.match(/<button/g) ?? []).length, 3)

const actionMarkup = renderToStaticMarkup(
  React.createElement(EscalationPanel, {
    meta: {
      checkpoint_type: 'action_permission',
      checkpoint_id: 'cp-action',
      prompt: 'Approve external network action?',
      options: [
        { id: 'approve_once', label: 'Approve once' },
        { id: 'deny', label: 'Deny' },
      ],
    },
    onReply: () => undefined,
    responded: false,
  }),
)
assert.match(actionMarkup, /Action Authorization/)
assert.match(actionMarkup, /action permission/)
assert.match(actionMarkup, /Approve external network action\?/)
assert.equal((actionMarkup.match(/<button/g) ?? []).length, 2)

const toolMarkup = renderToStaticMarkup(
  React.createElement(EscalationPanel, {
    meta: {
      checkpoint_type: 'tool_permission',
      checkpoint_id: 'cp-tool',
      prompt: 'Run exact command: npm test',
      options: [{ id: 'approve_once', label: 'Approve once' }],
    },
    onReply: () => undefined,
    responded: false,
  }),
)
assert.match(toolMarkup, /Tool Authorization/)
assert.match(toolMarkup, /<div class="ckpt-section-title">Request<\/div>/)
assert.match(toolMarkup, /Run exact command: npm test/)

const exactPayload = [
  'if True:',
  '    print(`<tag> literal`)',
  '',
  ...Array.from({ length: 450 }, (_, index) => `print(${index})`),
].join('\n')
const exactToolMarkup = renderToStaticMarkup(
  React.createElement(EscalationPanel, {
    meta: {
      checkpoint_type: 'tool_permission',
      checkpoint_id: 'cp-exact-tool',
      prompt: `  \nApprove exact Python payload?\n\n\`\`\`python\n${exactPayload}\n\`\`\`\n  `,
      options: [{ id: 'approve_once', label: 'Approve once' }],
    },
    onReply: () => undefined,
    responded: false,
  }),
)
assert.match(exactToolMarkup, /print\(0\)/)
assert.match(exactToolMarkup, /print\(449\)/)
assert.match(exactToolMarkup, /    print\(`&lt;tag&gt; literal`\)/)
assert.match(exactToolMarkup, /if True:\n    print/)
assert.match(exactToolMarkup, /<code>  \nApprove exact Python payload/)
assert.match(exactToolMarkup, /<\/code><\/pre>/)
assert.doesNotMatch(exactToolMarkup, /Show more|lines \(click to expand\)/)

const here = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(here, 'EscalationPanel.tsx'), 'utf8')
assert.doesNotMatch(src, /localResponded|setLocalResponded/, 'panel must wait for server checkpoint metadata before showing responded state')

console.log('EscalationPanel.test.tsx: OK (markdown gate panel)')
