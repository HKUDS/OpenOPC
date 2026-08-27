import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { DeliveryFeedbackPanel } from './DeliveryFeedbackPanel'

const markup = renderToStaticMarkup(
  React.createElement(DeliveryFeedbackPanel, {
    meta: {
      checkpoint_type: 'company_delivery_feedback',
      checkpoint_id: 'cp-delivery',
      work_item_projection_title: 'CEO Delivery',
      feedback_scope: 'final',
      prompt: 'This final delivery is ready for review.\n\n- Inspect the build\n- Confirm acceptance\n\n```txt\nready\n```',
      options: [
        { id: 'approve', label: 'Fully Agree / 完全同意' },
        { id: 'ignore', label: 'Ignore / 忽略' },
        { id: 'feedback', label: 'Feedback / 反馈' },
      ],
      permission_requests: [{ id: 'perm-1' }],
    },
    onReply: async () => true,
    responded: false,
  }),
)

assert.match(markup, /CEO Delivery \(for self-evolution\)/)
assert.match(markup, /Fully Agree/)
assert.match(markup, /Ignore/)
assert.match(markup, /Feedback for self-evolution/)
assert.match(markup, /<li>Inspect the build<\/li>/)
assert.match(markup, /<code class="language-txt">/)
assert.match(markup, /<summary>Runtime State<\/summary>/)
assert.equal((markup.match(/class="ckpt-btn /g) ?? []).length, 3)

const here = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(join(here, 'DeliveryFeedbackPanel.tsx'), 'utf8')
assert.match(src, /metadata\.self_evolution_trigger = true/, 'delivery card replies must explicitly trigger self-evolution')
assert.match(src, /kind === 'approve' \|\| kind === 'feedback'/, 'ignore must not trigger self-evolution metadata')
assert.match(src, /buildReplyMetadata\('ignore'\)/, 'delivery card must send an explicit ignore checkpoint reply')
assert.match(src, /submitting = false/, 'delivery card actions must follow the shared ACK-correlated submit state')
assert.doesNotMatch(src, /submittingAction/, 'delivery card must not maintain a second submit state')
assert.match(src, /disabled=\{actionsDisabled\}/, 'delivery card must disable controls immediately after a card action')
assert.doesNotMatch(src, /ckpt-btn-deny/, 'delivery self-evolution card must not render a deny action')
assert.doesNotMatch(src, /localResponded|setLocalResponded/, 'panel must wait for server checkpoint metadata before showing responded state')

console.log('DeliveryFeedbackPanel.test.tsx: OK (markdown delivery review panel)')
