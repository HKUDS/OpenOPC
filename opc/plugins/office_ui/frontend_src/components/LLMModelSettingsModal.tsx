import { useState, useEffect } from 'react'

export interface LLMSettingsData {
  provider: string
  default_model: string
  api_base: string
  api_key: string
  has_api_key?: boolean
  is_local: boolean
  context_window?: number
}

interface LLMModelSettingsModalProps {
  isOpen: boolean
  initialConfig?: LLMSettingsData | null
  onClose: () => void
  onSave?: (settings: LLMSettingsData) => Promise<boolean> | boolean
}

const PROVIDER_OPTIONS = [
  { value: 'ollama', label: 'Ollama (Local Node)', defaultBase: 'http://localhost:11434', defaultModel: 'ollama/llama3.3', isLocal: true },
  { value: 'vllm', label: 'vLLM Engine (Local)', defaultBase: 'http://localhost:8000/v1', defaultModel: 'vllm/meta-llama-3.1-8b-instruct', isLocal: true },
  { value: 'lmstudio', label: 'LM Studio (Local Server)', defaultBase: 'http://localhost:1234/v1', defaultModel: 'openai/deepseek-r1-distill-qwen-14b', isLocal: true },
  { value: 'localai', label: 'LocalAI / Llama.cpp (Local)', defaultBase: 'http://localhost:8080/v1', defaultModel: 'openai/starcoder2-15b', isLocal: true },
  { value: 'openai', label: 'OpenAI (Cloud)', defaultBase: '', defaultModel: 'openai/gpt-4o', isLocal: false },
  { value: 'anthropic', label: 'Anthropic Claude (Cloud)', defaultBase: '', defaultModel: 'anthropic/claude-sonnet-4-20250514', isLocal: false },
  { value: 'openrouter', label: 'OpenRouter AI (Cloud)', defaultBase: '', defaultModel: 'openrouter/auto', isLocal: false },
  { value: 'custom', label: 'Custom Self-Hosted / Proxy', defaultBase: 'http://localhost:8000/v1', defaultModel: 'openai/custom-model', isLocal: true },
] as const

const POPULAR_MODELS: Record<string, string[]> = {
  ollama: ['ollama/llama3.3', 'ollama/qwen2.5-coder', 'ollama/deepseek-r1:14b', 'ollama/mistral-nemo'],
  vllm: ['vllm/meta-llama-3.1-8b-instruct', 'vllm/qwen2.5-72b-instruct', 'vllm/mistral-7b-instruct-v0.3'],
  lmstudio: ['openai/deepseek-r1-distill-qwen-14b', 'openai/llama-3.3-70b-instruct'],
  localai: ['openai/starcoder2-15b', 'openai/llama-3-8b-instruct'],
  openai: ['openai/gpt-4o', 'openai/gpt-4o-mini', 'openai/o3-mini'],
  anthropic: ['anthropic/claude-sonnet-4-20250514', 'anthropic/claude-3-5-haiku-20241022'],
  openrouter: ['openrouter/auto', 'openrouter/anthropic/claude-3.5-sonnet'],
  custom: [],
}

export function LLMModelSettingsModal({ isOpen, initialConfig, onClose, onSave }: LLMModelSettingsModalProps) {
  const [provider, setProvider] = useState<string>('ollama')
  const [defaultModel, setDefaultModel] = useState<string>('ollama/llama3.3')
  const [apiBase, setApiBase] = useState<string>('http://localhost:11434')
  const [apiKey, setApiKey] = useState<string>('')
  const [isLocal, setIsLocal] = useState<boolean>(true)
  const [contextWindow, setContextWindow] = useState<number>(0)
  const [statusMessage, setStatusMessage] = useState<{ text: string; isError: boolean } | null>(null)
  const [isSubmitting, setIsSubmitting] = useState<boolean>(false)

  useEffect(() => {
    if (isOpen) {
      if (initialConfig) {
        setProvider(initialConfig.provider || 'ollama')
        setDefaultModel(initialConfig.default_model || 'ollama/llama3.3')
        setApiBase(initialConfig.api_base || '')
        setApiKey(initialConfig.api_key || '')
        setIsLocal(initialConfig.is_local ?? true)
        setContextWindow(initialConfig.context_window || 0)
      } else {
        setProvider('ollama')
        setDefaultModel('ollama/llama3.3')
        setApiBase('http://localhost:11434')
        setApiKey('')
        setIsLocal(true)
        setContextWindow(0)
      }
      setStatusMessage(null)
      setIsSubmitting(false)
    }
  }, [isOpen, initialConfig])

  if (!isOpen) return null

  const handleProviderChange = (newProvider: string) => {
    setProvider(newProvider)
    const preset = PROVIDER_OPTIONS.find((p) => p.value === newProvider)
    if (preset) {
      setIsLocal(preset.isLocal)
      setApiBase(preset.defaultBase || '')
      if (preset.defaultModel) setDefaultModel(preset.defaultModel)
    }
  }

  const handleSave = async () => {
    if (!defaultModel.trim()) {
      setStatusMessage({ text: 'Model identifier is required.', isError: true })
      return
    }

    setIsSubmitting(true)
    setStatusMessage(null)

    const settings: LLMSettingsData = {
      provider,
      default_model: defaultModel.trim(),
      api_base: apiBase.trim(),
      api_key: apiKey.trim(),
      is_local: isLocal,
      context_window: Number(contextWindow) || 0,
    }

    try {
      if (onSave) {
        const success = await onSave(settings)
        if (success !== false) {
          setStatusMessage({ text: '✓ LLM Settings Saved & Applied to Backend!', isError: false })
          setTimeout(() => {
            onClose()
          }, 800)
        } else {
          setStatusMessage({ text: 'Failed to update backend configuration.', isError: true })
        }
      } else {
        setStatusMessage({ text: 'No save handler registered.', isError: true })
      }
    } catch (err: any) {
      setStatusMessage({ text: err?.message || 'Error saving settings.', isError: true })
    } finally {
      setIsSubmitting(false)
    }
  }

  return (
    <div className="dev-overlay" style={{ maxWidth: '540px', width: '92vw', zIndex: 9999 }}>
      <div className="dev-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span style={{ fontSize: '16px' }}>🤖</span>
          <span className="dev-title">LLM Model & Provider Settings</span>
        </div>
        <button className="icon-btn" onClick={onClose} aria-label="Close settings">
          ✕
        </button>
      </div>

      <div style={{ padding: '16px 20px', display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {/* Provider Selection */}
        <div className="dev-group">
          <label className="dev-label" style={{ marginBottom: '6px' }}>
            LLM Provider / Execution Mode
          </label>
          <select
            className="inline-select"
            style={{ width: '100%', padding: '8px 12px', fontSize: '13px', borderRadius: 'var(--radius-sm)' }}
            value={provider}
            onChange={(e) => handleProviderChange(e.target.value)}
          >
            {PROVIDER_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>

        {/* Local Node Status Indicator */}
        {isLocal ? (
          <div
            style={{
              padding: '10px 14px',
              borderRadius: 'var(--radius-sm)',
              background: 'rgba(52, 211, 153, 0.12)',
              border: '1px solid rgba(52, 211, 153, 0.3)',
              color: 'var(--green)',
              fontSize: '12px',
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
            }}
          >
            <span style={{ fontSize: '14px' }}>🟢</span>
            <div>
              <strong>Keyless Local Model Active</strong>
              <div style={{ fontSize: '11px', opacity: 0.9 }}>
                No remote API key required. Requests connect directly to your local endpoint. Note: Model must support tool/function calling.
              </div>
            </div>
          </div>
        ) : (
          <div
            style={{
              padding: '10px 14px',
              borderRadius: 'var(--radius-sm)',
              background: 'rgba(99, 102, 241, 0.12)',
              border: '1px solid rgba(99, 102, 241, 0.3)',
              color: 'var(--accent)',
              fontSize: '12px',
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
            }}
          >
            <span style={{ fontSize: '14px' }}>☁️</span>
            <div>
              <strong>Cloud Provider Active</strong>
              <div style={{ fontSize: '11px', opacity: 0.9 }}>
                Requires an API key configured below or in your system environment.
              </div>
            </div>
          </div>
        )}

        {/* Model Selection & Suggestions */}
        <div className="dev-group">
          <label className="dev-label" style={{ marginBottom: '6px' }}>
            Model Identifier (`default_model`)
          </label>
          <input
            style={{
              width: '100%',
              padding: '8px 12px',
              fontSize: '13px',
              borderRadius: 'var(--radius-sm)',
              background: 'var(--surface)',
              border: '1px solid var(--border)',
              color: 'var(--text)',
              outline: 'none',
            }}
            value={defaultModel}
            onChange={(e) => setDefaultModel(e.target.value)}
            placeholder="e.g. ollama/llama3.3 or openai/gpt-4o"
          />
          {POPULAR_MODELS[provider] && POPULAR_MODELS[provider].length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginTop: '8px' }}>
              {POPULAR_MODELS[provider].map((m) => (
                <button
                  key={m}
                  type="button"
                  className={`pill-btn ${defaultModel === m ? 'active' : ''}`}
                  onClick={() => setDefaultModel(m)}
                  style={{ fontSize: '11px', padding: '3px 9px' }}
                >
                  {m.replace(/^[^/]+\//, '')}
                </button>
              ))}
            </div>
          )}
        </div>

        {/* API Base URL */}
        <div className="dev-group">
          <label className="dev-label" style={{ marginBottom: '6px' }}>
            API Base / Local Host URL (`api_base`)
          </label>
          <input
            style={{
              width: '100%',
              padding: '8px 12px',
              fontSize: '13px',
              borderRadius: 'var(--radius-sm)',
              background: 'var(--surface)',
              border: '1px solid var(--border)',
              color: 'var(--text)',
              outline: 'none',
            }}
            value={apiBase}
            onChange={(e) => setApiBase(e.target.value)}
            placeholder={isLocal ? 'e.g. http://localhost:11434 or http://localhost:8000/v1' : 'Leave empty for standard cloud endpoint'}
          />
        </div>

        {/* API Key */}
        <div className="dev-group">
          <label className="dev-label" style={{ marginBottom: '6px' }}>
            API Key {isLocal ? '(Optional for Local Nodes)' : '(Required for Cloud Models)'}
          </label>
          <input
            type="password"
            style={{
              width: '100%',
              padding: '8px 12px',
              fontSize: '13px',
              borderRadius: 'var(--radius-sm)',
              background: 'var(--surface)',
              border: '1px solid var(--border)',
              color: 'var(--text)',
              outline: 'none',
            }}
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={isLocal ? 'Not required for local nodes (leave blank)' : 'sk-...'}
          />
        </div>

        {/* Context Window & Advanced Options */}
        <div className="dev-group" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
          <div>
            <label className="dev-label" style={{ marginBottom: '6px' }}>
              Explicit `is_local` Flag
            </label>
            <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', fontSize: '13px', marginTop: '6px' }}>
              <input
                type="checkbox"
                checked={isLocal}
                onChange={(e) => setIsLocal(e.target.checked)}
              />
              Force Local Execution
            </label>
          </div>
          <div>
            <label className="dev-label" style={{ marginBottom: '6px' }}>
              Context Window Override (Tokens)
            </label>
            <input
              type="number"
              style={{
                width: '100%',
                padding: '6px 10px',
                fontSize: '13px',
                borderRadius: 'var(--radius-sm)',
                background: 'var(--surface)',
                border: '1px solid var(--border)',
                color: 'var(--text)',
                outline: 'none',
              }}
              value={contextWindow || ''}
              onChange={(e) => setContextWindow(Number(e.target.value))}
              placeholder="0 (Auto-detect)"
            />
          </div>
        </div>

        {/* Action Buttons */}
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: '12px' }}>
          {statusMessage ? (
            <span style={{ color: statusMessage.isError ? 'var(--red, #ef4444)' : 'var(--green, #10b981)', fontSize: '13px', fontWeight: 600 }}>
              {statusMessage.text}
            </span>
          ) : (
            <span style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>
              Config will be persisted atomically to backend `.opc/config/llm_config.yaml`.
            </span>
          )}
          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              type="button"
              className="pill-btn"
              onClick={onClose}
              disabled={isSubmitting}
              style={{ padding: '6px 14px', fontSize: '13px' }}
            >
              Cancel
            </button>
            <button
              type="button"
              className="pill-btn active"
              onClick={handleSave}
              disabled={isSubmitting}
              style={{ padding: '6px 16px', fontSize: '13px', fontWeight: 600 }}
            >
              {isSubmitting ? 'Saving...' : 'Save & Apply Settings'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
