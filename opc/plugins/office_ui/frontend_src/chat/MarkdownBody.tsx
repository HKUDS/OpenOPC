import React, { useCallback, useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

import { IconCheck, IconCopy } from './SvgIcons'
import { restoreCollapsedMarkdown } from '../lib/markdownRepair'

const CODE_BLOCK_MAX_LINES = 30
const CODE_BLOCK_PEEK_LINES = 10

function CodeBlock({
  className,
  children,
  forceExpanded = false,
}: {
  className?: string
  children?: React.ReactNode
  forceExpanded?: boolean
}) {
  const [copied, setCopied] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const text = String(children).replace(/\n$/, '')
  const lang = className?.replace('language-', '') || ''
  const lines = text.split('\n')
  const needsTruncation = lines.length > CODE_BLOCK_MAX_LINES
  const omittedCount = needsTruncation ? lines.length - CODE_BLOCK_PEEK_LINES * 2 : 0

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }, [text])

  return (
    <div className="code-block-wrap">
      <div className="code-block-header">
        <span className="code-block-lang">{lang || 'code'}{needsTruncation ? ` (${lines.length} lines)` : ''}</span>
        <button className="code-block-copy" onClick={handleCopy}>
          {copied ? <><IconCheck /> <span>Copied</span></> : <><IconCopy /> <span>Copy</span></>}
        </button>
      </div>
      <pre><code className={className}>
        {needsTruncation && !forceExpanded && !expanded ? (
          <>
            {lines.slice(0, CODE_BLOCK_PEEK_LINES).join('\n') + '\n'}
            <span className="code-block-omitted" onClick={() => setExpanded(true)}>
              {'... +'}{omittedCount}{' lines (click to expand)'}
            </span>
            {'\n' + lines.slice(-CODE_BLOCK_PEEK_LINES).join('\n')}
          </>
        ) : (
          text
        )}
      </code></pre>
      {needsTruncation && !forceExpanded && expanded && (
        <button className="code-block-collapse-btn" onClick={() => setExpanded(false)}>
          Collapse ({lines.length} lines)
        </button>
      )}
    </div>
  )
}

const MSG_COLLAPSE_CHAR_THRESHOLD = 3000
const MSG_COLLAPSE_LINE_THRESHOLD = 60
const MSG_PREVIEW_CHARS = 800

function shouldCollapseContent(content: string): boolean {
  if (content.length > MSG_COLLAPSE_CHAR_THRESHOLD) return true
  let newlines = 0
  for (let i = 0; i < content.length; i++) {
    if (content[i] === '\n' && ++newlines >= MSG_COLLAPSE_LINE_THRESHOLD) return true
  }
  return false
}

function truncatePreview(content: string): string {
  const cut = content.lastIndexOf('\n', MSG_PREVIEW_CHARS)
  return content.slice(0, cut > MSG_PREVIEW_CHARS / 2 ? cut : MSG_PREVIEW_CHARS)
}

type MarkdownCollapseMode = 'auto' | 'never'

export const MarkdownBody = React.memo(function MarkdownBody({
  content,
  className = 'msg-content-agent',
  collapseMode = 'auto',
}: {
  content: string
  className?: string
  collapseMode?: MarkdownCollapseMode
}) {
  // Also repairs historical Jiuwen messages already persisted before the
  // adapter-side stream normalizer was added.
  const normalizedContent = restoreCollapsedMarkdown(content)
  const collapsible = collapseMode !== 'never' && shouldCollapseContent(normalizedContent)
  const [collapsed, setCollapsed] = useState(collapsible)

  useEffect(() => {
    setCollapsed(collapseMode !== 'never' && shouldCollapseContent(normalizedContent))
  }, [collapseMode, normalizedContent])

  const displayContent = collapsed ? truncatePreview(normalizedContent) : normalizedContent
  const lineCount = normalizedContent.split('\n').length
  const charCount = normalizedContent.length
  const mdComponents = {
    code({ className: codeClassName, children, ...props }: any) {
      const isBlock = codeClassName?.startsWith('language-')
      if (isBlock) {
        return (
          <CodeBlock
            className={codeClassName}
            forceExpanded={collapseMode === 'never'}
          >
            {children}
          </CodeBlock>
        )
      }
      return <code className={codeClassName} {...props}>{children}</code>
    },
  }

  return (
    <div className={className}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
        {displayContent}
      </ReactMarkdown>
      {collapsible && collapsed && (
        <button className="msg-collapse-toggle" onClick={() => setCollapsed(false)}>
          Show more ({lineCount} lines, {(charCount / 1000).toFixed(1)}k chars)
        </button>
      )}
      {collapsible && !collapsed && (
        <button className="msg-collapse-toggle" onClick={() => setCollapsed(true)}>
          Show less
        </button>
      )}
    </div>
  )
})
