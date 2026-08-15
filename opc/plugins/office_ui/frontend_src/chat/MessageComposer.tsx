import { useCallback, useEffect, useMemo, useRef, useState, type ReactElement } from 'react'
import {
  IconArrowRight,
  IconBuilding,
  IconCheck,
  IconClose,
  IconLock,
  IconPaperclip,
  IconSend,
  IconSparkles,
  IconStop,
  IconUserRound,
} from './SvgIcons'
import type { OutgoingAttachmentPayload } from '../types/chat'
import type { TaskPreferredAgent } from '../types/kanban'
import type { SavedOrgSummary } from '../types/visual'
import { getContextUsageMetrics } from '../lib/contextUsage'

const MAX_FILE_SIZE = 10 * 1024 * 1024
const MAX_TOTAL_SIZE = 20 * 1024 * 1024
const ACCEPTED_TYPES = 'image/*,video/mp4,video/mpeg,video/quicktime,video/webm,.mp4,.mpeg,.mpg,.mov,.webm,.txt,.md,.pdf,.csv,.json,.yaml,.yml,.py,.js,.ts,.tsx,.jsx,.html,.css,.java,.c,.cpp,.go,.rs,.rb,.sh,.xml,.toml,.docx,.xlsx,.pptx'

type AttachmentTransferState = 'reading' | 'ready' | 'error'
type ComposerExecMode = 'task' | 'company' | 'org' | 'custom'
type ComposerCompanyProfile = 'corporate' | 'custom'
type ComposerModeOption = 'task' | 'company'
type CompanyArchitectureOption = '' | 'corporate' | `org:${string}`

const TASK_AGENT_LABELS: Record<TaskPreferredAgent, string> = {
  native: 'OpenOPC Native',
  codex: 'Codex',
  claude_code: 'Claude Code',
  cursor: 'Cursor',
  opencode: 'OpenCode',
}

interface PendingAttachment {
  id: string
  file: File
  filename: string
  mime_type: string
  size_bytes: number
  preview_url: string
  base64_data?: string
  progress_percent: number
  transfer_state: AttachmentTransferState
  error?: string
}

function readFileAsBase64(file: File, onProgress?: (progress: number) => void): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onprogress = (event) => {
      if (!event.lengthComputable) return
      onProgress?.(Math.round((event.loaded / event.total) * 100))
    }
    reader.onload = () => {
      const result = reader.result as string
      onProgress?.(100)
      resolve(result.split(',')[1] || '')
    }
    reader.onerror = reject
    reader.readAsDataURL(file)
  })
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)}KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)}MB`
}

function attachmentBadgeLabel(mime: string, filename: string): string {
  const extension = filename.includes('.') ? filename.split('.').pop()?.toUpperCase() ?? '' : ''
  if (mime.startsWith('image/')) return 'IMG'
  if (mime.startsWith('video/')) return 'VID'
  if (mime === 'application/pdf') return 'PDF'
  if (mime.includes('wordprocessingml')) return 'DOC'
  if (mime.includes('spreadsheetml') || extension === 'CSV') return 'XLS'
  if (mime.includes('presentationml')) return 'PPT'
  if (mime.includes('json')) return 'JSON'
  if (mime.includes('yaml') || extension === 'YML' || extension === 'YAML') return 'YAML'
  if (mime.startsWith('text/')) return 'TXT'
  if (['PY', 'TS', 'TSX', 'JS', 'JSX', 'GO', 'RS', 'RB', 'JAVA', 'C', 'CPP', 'HTML', 'CSS', 'SH'].includes(extension)) return extension
  return extension || 'FILE'
}

function attachmentToneClass(mime: string, filename: string): string {
  const label = attachmentBadgeLabel(mime, filename)
  if (label === 'IMG') return 'image'
  if (label === 'VID') return 'video'
  if (label === 'PDF') return 'pdf'
  if (label === 'DOC' || label === 'XLS' || label === 'PPT') return 'office'
  if (label === 'JSON' || label === 'YAML') return 'data'
  if (label === 'TXT') return 'text'
  if (['PY', 'TS', 'TSX', 'JS', 'JSX', 'GO', 'RS', 'RB', 'JAVA', 'C', 'CPP', 'HTML', 'CSS', 'SH'].includes(label)) return 'code'
  return 'generic'
}

function AttachmentProgressRing({
  progress,
  state,
  error,
}: {
  progress: number
  state: AttachmentTransferState
  error?: string
}) {
  const radius = 11
  const circumference = 2 * Math.PI * radius
  const normalized = error ? 100 : state === 'ready' ? 100 : Math.max(0, Math.min(progress, 100))
  const dashOffset = circumference * (1 - normalized / 100)

  return (
    <span
      className={`attachment-progress-ring${error ? ' error' : state === 'ready' ? ' ready' : ''}`}
      aria-label={error ? 'Attachment preparation failed' : state === 'ready' ? 'Attachment ready to send' : `Preparing attachment ${normalized}%`}
      role="img"
    >
      <svg viewBox="0 0 28 28" aria-hidden="true">
        <circle className="attachment-progress-track" cx="14" cy="14" r={radius} />
        <circle
          className="attachment-progress-value"
          cx="14"
          cy="14"
          r={radius}
          strokeDasharray={circumference}
          strokeDashoffset={dashOffset}
        />
      </svg>
      <span className="attachment-progress-center">
        {error ? '!' : state === 'ready' ? <IconCheck /> : normalized}
      </span>
    </span>
  )
}

function ContextRing({
  usedPct,
  usedTokens,
  windowTokens,
}: {
  usedPct: number
  usedTokens?: number
  windowTokens?: number
}) {
  const radius = 11
  const circumference = 2 * Math.PI * radius
  const clamped = Math.max(0, Math.min(usedPct, 100))
  const dashOffset = circumference * (1 - clamped / 100)
  const isLow = clamped >= 80
  const isCritical = clamped >= 90
  const usageLabel = typeof usedTokens === 'number' && typeof windowTokens === 'number'
    ? `${clamped}% used (${usedTokens.toLocaleString()}/${windowTokens.toLocaleString()})`
    : `${clamped}% used`

  return (
    <span
      className={`composer-context-ring${isLow ? ' low' : ''}${isCritical ? ' critical' : ''}`}
      title={`Context window: ${usageLabel}`}
      aria-label={`Context window ${usageLabel}`}
      role="img"
    >
      <svg viewBox="0 0 28 28" aria-hidden="true">
        <circle className="composer-context-ring-track" cx="14" cy="14" r={radius} />
        <circle
          className="composer-context-ring-value"
          cx="14"
          cy="14"
          r={radius}
          strokeDasharray={circumference}
          strokeDashoffset={dashOffset}
        />
      </svg>
      <span className="composer-context-ring-label">{clamped}</span>
    </span>
  )
}

interface MessageComposerProps {
  disabled?: boolean
  placeholder?: string
  channelId?: string
  execMode?: string
  companyProfile?: string
  taskPreferredAgent?: TaskPreferredAgent
  agentStatus?: string
  currentTool?: string
  displayTool?: string
  activeAgentCount?: number
  runtimeControlState?: 'running' | 'suspending' | 'suspended' | 'resuming' | 'idle'
  canStop?: boolean
  autoFocus?: boolean
  contextTokens?: number
  contextWindow?: number
  contextRemainingPct?: number
  savedOrgs?: SavedOrgSummary[] | null
  activeSavedOrg?: string | null
  selectedOrgId?: string | null
  /**
   * When true the mode/agent pickers freeze into a read-only chip: the chat
   * has committed to its current execution identity (i.e. messages have been
   * sent) and the identity is no longer changeable from this composer. The
   * chip exposes a hover hint pointing users at "start a new chat" instead.
   */
  lockedMode?: boolean
  onSend: (content: string, attachments?: OutgoingAttachmentPayload[]) => void
  onModeChange?: (mode: ComposerExecMode, profile?: ComposerCompanyProfile, orgId?: string) => void
  onTaskAgentChange?: (preferredAgent: TaskPreferredAgent) => void
  onSavedOrgsRefresh?: () => void
  onSavedOrgLoad?: (name: string) => void
  onStop?: () => void
  /**
   * Spawn a brand-new chat in the requested mode, preserving the user inside
   * the same project. Wired from the locked-mode chip popover so users can
   * "continue in a different mode" without having to find the global new-chat
   * button. When omitted, the popover degrades gracefully to text-only.
   */
  onContinueInNewChat?: (mode: ComposerExecMode, profile?: ComposerCompanyProfile, orgId?: string) => void
}

interface ModeAlternative {
  key: string
  mode: ComposerExecMode
  profile?: ComposerCompanyProfile
  orgId?: string
  label: string
  description: string
  icon: ReactElement
}



function savedOrgLabel(org: SavedOrgSummary): string {
  return org.organization_name?.trim() || org.name
}

export function MessageComposer({
  disabled,
  placeholder,
  channelId,
  execMode,
  companyProfile,
  taskPreferredAgent = 'native',
  agentStatus,
  currentTool,
  displayTool,
  activeAgentCount,
  runtimeControlState,
  canStop,
  autoFocus = true,
  contextTokens,
  contextWindow,
  contextRemainingPct,
  savedOrgs,
  activeSavedOrg,
  selectedOrgId,
  lockedMode = false,
  onSend,
  onModeChange,
  onTaskAgentChange,
  onSavedOrgsRefresh,
  onSavedOrgLoad,
  onStop,
  onContinueInNewChat,
}: MessageComposerProps) {
  const [text, setText] = useState('')
  const [focused, setFocused] = useState(false)
  const [pending, setPending] = useState<PendingAttachment[]>([])
  const [lightbox, setLightbox] = useState<string | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const isStopping = runtimeControlState === 'suspending'
  const isRuntimeActive = runtimeControlState === 'running'
    || runtimeControlState === 'suspending'
    || runtimeControlState === 'resuming'
  const isSuspended = runtimeControlState === 'suspended'
  const hasRuntimeControlState = runtimeControlState != null
  const isWorking = isRuntimeActive || (!hasRuntimeControlState && agentStatus != null && agentStatus !== 'idle')
  const stopEnabled = (canStop ?? true) && !isStopping && !isSuspended
  const contextUsage = useMemo(
    () => getContextUsageMetrics({ contextTokens, contextWindow, contextRemainingPct }),
    [contextRemainingPct, contextTokens, contextWindow],
  )

  useEffect(() => {
    const ta = textareaRef.current
    if (!ta) return
    ta.style.height = 'auto'
    ta.style.height = `${Math.min(ta.scrollHeight, 200)}px`
  }, [text])

  useEffect(() => {
    setText('')
    setPending(prev => {
      prev.forEach(attachment => {
        if (attachment.preview_url) URL.revokeObjectURL(attachment.preview_url)
      })
      return []
    })
    if (!disabled && autoFocus) setTimeout(() => textareaRef.current?.focus(), 50)
  }, [channelId, disabled, autoFocus])

  const updateAttachment = useCallback((id: string, updater: (attachment: PendingAttachment) => PendingAttachment) => {
    setPending(prev => prev.map(attachment => attachment.id === id ? updater(attachment) : attachment))
  }, [])

  const prepareAttachment = useCallback(async (attachmentId: string, file: File) => {
    try {
      const base64 = await readFileAsBase64(file, (progress) => {
        updateAttachment(attachmentId, (attachment) => ({
          ...attachment,
          progress_percent: progress,
          transfer_state: 'reading',
        }))
      })
      updateAttachment(attachmentId, (attachment) => ({
        ...attachment,
        base64_data: base64,
        progress_percent: 100,
        transfer_state: 'ready',
      }))
    } catch {
      updateAttachment(attachmentId, (attachment) => ({
        ...attachment,
        error: 'Failed to prepare file',
        progress_percent: 0,
        transfer_state: 'error',
      }))
    }
  }, [updateAttachment])

  const addFiles = useCallback((files: FileList | File[]) => {
    const arr = Array.from(files)
    let runningTotal = pending.reduce((sum, attachment) => sum + attachment.size_bytes, 0)

    const newPending: PendingAttachment[] = arr.map(file => {
      let error: string | undefined
      if (file.size > MAX_FILE_SIZE) error = `Too large (${formatSize(file.size)})`
      else if (runningTotal + file.size > MAX_TOTAL_SIZE) error = 'Total size exceeds 20MB'
      else runningTotal += file.size

      return {
        id: crypto.randomUUID(),
        file,
        filename: file.name || 'upload',
        mime_type: file.type || 'application/octet-stream',
        size_bytes: file.size,
        preview_url: file.type.startsWith('image/') ? URL.createObjectURL(file) : '',
        progress_percent: 0,
        transfer_state: error ? 'error' : 'reading',
        error,
      }
    })

    setPending(prev => [...prev, ...newPending])
    newPending
      .filter(attachment => !attachment.error)
      .forEach(attachment => { void prepareAttachment(attachment.id, attachment.file) })
  }, [pending, prepareAttachment])

  const removeAttachment = useCallback((id: string) => {
    setPending(prev => {
      const item = prev.find(attachment => attachment.id === id)
      if (item?.preview_url) URL.revokeObjectURL(item.preview_url)
      return prev.filter(attachment => attachment.id !== id)
    })
  }, [])

  const handleSend = useCallback(() => {
    // Not gated on isWorking: the backend already queues/routes mid-turn sends (mirrors CLI /queue).
    if (disabled) return
    const content = text.trim()
    const preparing = pending.filter(attachment => !attachment.error && attachment.transfer_state === 'reading')
    const ready = pending.filter(attachment => !attachment.error && !!attachment.base64_data)
    if (preparing.length > 0) return
    if (!content && ready.length === 0) return

    const attachments = ready.map(attachment => ({
      filename: attachment.filename,
      data: attachment.base64_data!,
      mime_type: attachment.mime_type,
    }))

    onSend(content || 'Sent with attachments', attachments.length ? attachments : undefined)
    setText('')
    pending.forEach(attachment => {
      if (attachment.preview_url) URL.revokeObjectURL(attachment.preview_url)
    })
    setPending([])
  }, [disabled, text, pending, onSend])

  const handlePaste = useCallback((event: React.ClipboardEvent) => {
    const files = event.clipboardData?.files
    if (files && files.length > 0) {
      event.preventDefault()
      addFiles(files)
    }
  }, [addFiles])

  const normalizedCompanyProfile = String(companyProfile ?? '').trim().toLowerCase()
  const normalizedExecMode: ComposerExecMode = execMode === 'company'
    ? 'company'
    : execMode === 'org' || execMode === 'custom' || normalizedCompanyProfile === 'custom'
      ? 'org'
      : 'task'
  const savedOrgOptions = useMemo(
    () => (savedOrgs ?? []).filter(org => !!org.name && org.name !== 'corporate'),
    [savedOrgs],
  )
  const activeSavedOrgOption = activeSavedOrg
    ? savedOrgOptions.find(org => org.name === activeSavedOrg)
    : undefined
  const selectedOrgOption = selectedOrgId
    ? savedOrgOptions.find(org => org.name === selectedOrgId)
    : undefined
  const activeSavedOrgLabel = activeSavedOrgOption
    ? savedOrgLabel(activeSavedOrgOption)
    : activeSavedOrg || ''
  const selectedOrgLabel = selectedOrgOption
    ? savedOrgLabel(selectedOrgOption)
    : selectedOrgId || activeSavedOrgLabel
  const selectedOrgValue = selectedOrgOption?.name || selectedOrgId || activeSavedOrgOption?.name || ''
  const selectedModeOption: ComposerModeOption = normalizedExecMode === 'task' ? 'task' : 'company'
  const selectedCompanyArchitecture: CompanyArchitectureOption = normalizedExecMode === 'org'
    ? (selectedOrgValue ? `org:${selectedOrgValue}` : '')
    : 'corporate'
  const companyArchitectureLabel = normalizedExecMode === 'org'
    ? selectedOrgLabel
      ? `Company / ${selectedOrgLabel}`
      : 'Company / Saved org'
    : 'Company / Corporate'
  const modeLabel = selectedModeOption === 'task' ? 'Task' : companyArchitectureLabel
  const showModePicker = !!execMode && !!onModeChange
  const showTaskAgentPicker = normalizedExecMode === 'task' && !!onTaskAgentChange

  // Build the list of "Continue in a new chat" alternatives, excluding the
  // mode the current chat is already locked to.  We surface up to three options
  // so the popover stays compact; the order is stable so users build muscle
  // memory for it.
  const continueAlternatives: ModeAlternative[] = useMemo(() => {
    const currentKey = normalizedExecMode === 'task'
      ? 'task'
      : normalizedExecMode === 'org'
        ? `org:${selectedOrgValue || 'selected'}`
        : 'company:corporate'
    const continueOrgName = selectedOrgValue || activeSavedOrgOption?.name || ''
    const continueOrgLabel = selectedOrgLabel || activeSavedOrgLabel || continueOrgName
    const all: ModeAlternative[] = [
      {
        key: 'task',
        mode: 'task',
        label: 'Task',
        description: 'A single agent handles the request',
        icon: <IconUserRound />,
      },
      {
        key: 'company:corporate',
        mode: 'company',
        profile: 'corporate',
        label: 'Company / Corporate',
        description: 'A team of roles collaborates',
        icon: <IconBuilding />,
      },
    ]
    if (continueOrgName) {
      all.push({
        key: `org:${continueOrgName}`,
        mode: 'org',
        profile: 'custom',
        orgId: continueOrgName,
        label: `Company / ${continueOrgLabel}`,
        description: 'A saved company architecture collaborates',
        icon: <IconSparkles />,
      })
    }
    return all.filter(option => option.key !== currentKey)
  }, [activeSavedOrgLabel, activeSavedOrgOption?.name, normalizedExecMode, selectedOrgLabel, selectedOrgValue])
  const preparingAttachmentCount = pending.filter(attachment => !attachment.error && attachment.transfer_state === 'reading').length
  const readyAttachmentCount = pending.filter(attachment => !attachment.error && attachment.transfer_state === 'ready').length
  const visibleTool = displayTool || currentTool

  const statusText = (() => {
    if (!isWorking) return null
    if (isStopping) return 'Stopping...'
    if (activeAgentCount && activeAgentCount > 1) return `${activeAgentCount} agents working`
    if (visibleTool) return `Running ${visibleTool}`
    if (agentStatus === 'reflecting') return 'Thinking...'
    return 'Working...'
  })()

  return (
    <>
      <div className={`msg-composer${focused ? ' focused' : ''}${isWorking ? ' working' : ''}`}>
        {isWorking && statusText && (
          <div className="composer-status">
            <div className="composer-status-indicator" />
            <span className="composer-status-text">{statusText}</span>
            <button className="composer-stop-btn" onClick={onStop} title="Stop" disabled={!stopEnabled || !onStop}>
              <IconStop />
              <span>{isStopping ? 'Stopping...' : 'Stop'}</span>
            </button>
          </div>
        )}

        {pending.length > 0 && (
          <div className="composer-attachments">
            {pending.map(attachment => (
              <div key={attachment.id} className={`attachment-chip${attachment.error ? ' error' : ''}`}>
                {attachment.preview_url ? (
                  <img
                    className="attachment-thumb"
                    src={attachment.preview_url}
                    alt={attachment.filename}
                    onClick={() => setLightbox(attachment.preview_url)}
                  />
                ) : (
                  <span className={`attachment-file-icon tone-${attachmentToneClass(attachment.mime_type, attachment.filename)}`}>
                    {attachmentBadgeLabel(attachment.mime_type, attachment.filename)}
                  </span>
                )}
                <span className="attachment-chip-info">
                  <span className="attachment-chip-name">{attachment.filename}</span>
                  <span className="attachment-chip-size">
                      {attachment.error
                      ? attachment.error
                      : attachment.transfer_state === 'reading'
                        ? `Preparing ${attachment.progress_percent}%`
                        : `${formatSize(attachment.size_bytes)} - Ready`}
                  </span>
                </span>
                <AttachmentProgressRing
                  progress={attachment.progress_percent}
                  state={attachment.transfer_state}
                  error={attachment.error}
                />
                <button
                  className="attachment-chip-remove"
                  onClick={() => removeAttachment(attachment.id)}
                  title="Remove"
                >
                  <IconClose />
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="composer-input-area">
          <textarea
            ref={textareaRef}
            value={text}
            onChange={event => setText(event.target.value)}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            onPaste={handlePaste}
            onKeyDown={event => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                handleSend()
              }
            }}
            placeholder={placeholder ?? 'Message...'}
            rows={1}
            disabled={disabled}
          />

          <div className="composer-bottom">
            <button
              className="composer-attach-btn"
              onClick={() => fileInputRef.current?.click()}
              title="Attach files"
              disabled={disabled}
            >
              <IconPaperclip />
            </button>
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={ACCEPTED_TYPES}
              style={{ display: 'none' }}
              onChange={event => {
                if (event.target.files) addFiles(event.target.files)
                event.target.value = ''
              }}
            />
            {(showModePicker || execMode) && (
              <div className="composer-config-group" data-locked={lockedMode ? 'true' : undefined}>
                {showModePicker && !lockedMode ? (
                  <label
                    className="composer-mode-inline"
                    data-kind="mode"
                    title="Execution mode for this chat and new work started from it"
                  >
                    <span className="composer-mode-inline-label">Mode</span>
                    <span className="composer-mode-select-wrap">
                      <select
                        className="composer-mode-select"
                        value={selectedModeOption}
                        onChange={(event) => {
                          const value = event.target.value as ComposerModeOption
                          if (value === 'task') {
                            onModeChange?.('task')
                            return
                          }
                          onModeChange?.('company', 'corporate')
                        }}
                        onFocus={() => onSavedOrgsRefresh?.()}
                        onPointerDown={() => onSavedOrgsRefresh?.()}
                        disabled={disabled}
                        aria-label="Execution mode"
                      >
                        <option value="task">Task</option>
                        <option value="company">Company</option>
                      </select>
                    </span>
                  </label>
                ) : showModePicker && lockedMode ? (
                  <div
                    className="composer-mode-chip"
                    data-kind="mode"
                    tabIndex={0}
                    role="group"
                    aria-label={`Mode locked to ${modeLabel}.${onContinueInNewChat ? ' Use the menu to start a new chat in a different mode.' : ' Start a new chat to use a different mode.'}`}
                  >
                    <span className="composer-mode-chip-icon" aria-hidden="true">
                      <IconLock />
                    </span>
                    <span className="composer-mode-chip-label">{modeLabel}</span>
                    <div className="composer-mode-chip-popover" role="dialog" aria-label="Mode info">
                      <div className="composer-mode-chip-popover-title">
                        Mode is fixed for this chat
                      </div>
                      <div className="composer-mode-chip-popover-body">
                        Once the first message is sent, this chat is committed to{' '}
                        <strong>{modeLabel}</strong>.
                      </div>
                      {onContinueInNewChat && continueAlternatives.length > 0 && (
                        <>
                          <div className="composer-mode-chip-popover-divider" aria-hidden="true" />
                          <div className="composer-mode-chip-popover-action-title">
                            Continue in a new chat
                          </div>
                          <div className="composer-mode-chip-popover-actions">
                            {continueAlternatives.map(alt => (
                              <button
                                key={alt.key}
                                type="button"
                                className="composer-mode-chip-popover-action"
                                onClick={() => onContinueInNewChat(alt.mode, alt.profile, alt.orgId)}
                                aria-label={`Start a new chat in ${alt.label}`}
                              >
                                <span className="composer-mode-chip-popover-action-icon" aria-hidden="true">
                                  {alt.icon}
                                </span>
                                <span className="composer-mode-chip-popover-action-text">
                                  <span className="composer-mode-chip-popover-action-label">
                                    {alt.label}
                                  </span>
                                  <span className="composer-mode-chip-popover-action-desc">
                                    {alt.description}
                                  </span>
                                </span>
                                <span className="composer-mode-chip-popover-action-arrow" aria-hidden="true">
                                  <IconArrowRight />
                                </span>
                              </button>
                            ))}
                          </div>
                        </>
                      )}
                    </div>
                  </div>
                ) : (
                  <span className="composer-mode" data-kind="mode">{modeLabel}</span>
                )}
                {selectedModeOption === 'company' && showModePicker && !lockedMode && (
                  <>
                    <span className="composer-config-divider" aria-hidden="true" />
                    <label
                      className="composer-mode-inline"
                      data-kind="org"
                      title="Company architecture for this chat"
                    >
                      <span className="composer-mode-inline-label">Company</span>
                      <span className="composer-mode-select-wrap">
                        <select
                          className="composer-mode-select"
                          value={selectedCompanyArchitecture}
                          onChange={(event) => {
                            const value = event.target.value as CompanyArchitectureOption
                            if (value === 'corporate') {
                              onModeChange?.('company', 'corporate')
                              return
                            }
                            if (value.startsWith('org:')) {
                              const orgName = value.slice(4)
                              if (orgName) onModeChange?.('org', 'custom', orgName)
                            }
                          }}
                          onFocus={() => onSavedOrgsRefresh?.()}
                          onPointerDown={() => onSavedOrgsRefresh?.()}
                          disabled={disabled}
                          aria-label="Company architecture"
                        >
                          <option value="corporate">Corporate</option>
                          {!selectedCompanyArchitecture && (
                            <option value="" disabled>Select saved org</option>
                          )}
                          {selectedCompanyArchitecture
                            && selectedCompanyArchitecture !== 'corporate'
                            && !savedOrgOptions.some(org => `org:${org.name}` === selectedCompanyArchitecture) && (
                              <option value={selectedCompanyArchitecture}>{selectedOrgLabel || selectedOrgValue}</option>
                            )}
                          {savedOrgOptions.length === 0 ? (
                            <option value="" disabled>No saved orgs</option>
                          ) : savedOrgOptions.map(org => (
                            <option key={org.name} value={`org:${org.name}`}>
                              {savedOrgLabel(org)}
                            </option>
                          ))}
                        </select>
                      </span>
                    </label>
                  </>
                )}
                {normalizedExecMode === 'task' && (
                  <>
                    <span className="composer-config-divider" aria-hidden="true" />
                    {showTaskAgentPicker && !lockedMode ? (
                      <label
                        className="composer-mode-inline"
                        data-kind="agent"
                        title="Execution agent for this task-mode chat"
                      >
                        <span className="composer-mode-inline-label">Agent</span>
                        <span className="composer-mode-select-wrap">
                          <select
                            className="composer-mode-select"
                            value={taskPreferredAgent}
                            onChange={(event) => onTaskAgentChange?.(event.target.value as TaskPreferredAgent)}
                            disabled={disabled}
                            aria-label="Task mode agent"
                          >
                            {Object.entries(TASK_AGENT_LABELS).map(([value, label]) => (
                              <option key={value} value={value}>{label}</option>
                            ))}
                          </select>
                        </span>
                      </label>
                    ) : showTaskAgentPicker && lockedMode ? (
                      <span
                        className="composer-mode-chip"
                        data-kind="agent"
                        tabIndex={0}
                        role="status"
                        aria-label={`Agent locked to ${TASK_AGENT_LABELS[taskPreferredAgent]}. Start a new chat to use a different agent.`}
                      >
                        <span className="composer-mode-chip-icon" aria-hidden="true">
                          <IconLock />
                        </span>
                        <span className="composer-mode-chip-label">
                          {TASK_AGENT_LABELS[taskPreferredAgent]}
                        </span>
                        <span className="composer-mode-chip-popover" role="tooltip">
                          <span className="composer-mode-chip-popover-title">
                            Agent is fixed for this chat
                          </span>
                          <span className="composer-mode-chip-popover-body">
                            The execution agent is committed once the chat starts. Start a new
                            chat to switch agents.
                          </span>
                        </span>
                      </span>
                    ) : (
                      <span className="composer-mode" data-kind="agent">{TASK_AGENT_LABELS[taskPreferredAgent]}</span>
                    )}
                  </>
                )}
              </div>
            )}
            <span className="composer-hint">
              {preparingAttachmentCount > 0
                ? `Preparing ${preparingAttachmentCount} attachment${preparingAttachmentCount > 1 ? 's' : ''}`
                : readyAttachmentCount > 0
                  ? `${readyAttachmentCount} attachment${readyAttachmentCount > 1 ? 's' : ''} ready`
                  : 'Shift+Enter for new line'}
            </span>
            {typeof contextUsage.usedPct === 'number' && (
              <ContextRing
                usedPct={contextUsage.usedPct}
                usedTokens={contextUsage.usedTokens}
                windowTokens={contextUsage.windowTokens}
              />
            )}
            <button
              className="composer-send-btn"
              onClick={handleSend}
              // Deliberately not gated on isWorking — see handleSend's note.
              disabled={disabled || preparingAttachmentCount > 0 || (!text.trim() && readyAttachmentCount === 0)}
              title={isWorking ? 'Send — will queue behind the current turn' : undefined}
            >
              <IconSend />
            </button>
          </div>
        </div>
      </div>

      {lightbox && (
        <div className="lightbox-overlay" onClick={() => setLightbox(null)}>
          <img className="lightbox-img" src={lightbox} alt="Preview" />
        </div>
      )}
    </>
  )
}
