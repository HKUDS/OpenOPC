import { useState, useEffect, useCallback } from 'react'
import { useI18n } from '../i18n'

export interface ProjectWorkplaceInfo {
  project_id: string
  workplace_path: string
  source: 'default' | 'env' | 'manual'
  exists: boolean
}

interface ProjectSettingsProps {
  projectId: string
  onSave: (projectId: string, workplacePath: string) => void
  onReset: (projectId: string) => void
  onClose: () => void
  workplaceInfo?: ProjectWorkplaceInfo | null
}

export function ProjectSettings({
  projectId,
  onSave,
  onReset,
  onClose,
  workplaceInfo,
}: ProjectSettingsProps) {
  const { t } = useI18n()
  const [path, setPath] = useState('')
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (workplaceInfo && workplaceInfo.source === 'manual') {
      setPath(workplaceInfo.workplace_path)
    } else {
      setPath('')
    }
  }, [workplaceInfo])

  const handleSave = useCallback(() => {
    setSaving(true)
    onSave(projectId, path.trim())
    setSaving(false)
    onClose()
  }, [projectId, path, onSave, onClose])

  const handleReset = useCallback(() => {
    onReset(projectId)
    setPath('')
    onClose()
  }, [projectId, onReset, onClose])

  const sourceLabel = workplaceInfo
    ? t(`project.workplaceSource.${workplaceInfo.source}` as any) ?? workplaceInfo.source
    : ''

  return (
    <div
      style={{
        position: 'fixed', inset: 0, zIndex: 9999,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        background: 'rgba(0,0,0,0.55)',
      }}
      onClick={e => { if (e.target === e.currentTarget) onClose() }}
    >
      <div style={{
        background: 'var(--bg-elevated)',
        border: '1px solid var(--border)',
        color: 'var(--text)',
        borderRadius: 12,
        padding: '28px 32px',
        minWidth: 420,
        maxWidth: 560,
        boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
        display: 'flex',
        flexDirection: 'column',
        gap: 16,
      }}>
        <h3 style={{ margin: 0, fontSize: 15, fontWeight: 600, color: 'var(--text)' }}>
          {t('project.settings')} — <span style={{ color: 'var(--text-secondary)' }}>{projectId}</span>
        </h3>

        {/* Current resolved path info */}
        {workplaceInfo && (
          <div style={{
            background: 'var(--bg-secondary)',
            border: '1px solid var(--border)',
            borderRadius: 8,
            padding: '10px 14px',
            fontSize: 12,
            display: 'flex',
            flexDirection: 'column',
            gap: 4,
          }}>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <span style={{ color: 'var(--text-dim)' }}>Current:</span>
              <code style={{ fontSize: 11, wordBreak: 'break-all', color: 'var(--text)' }}>{workplaceInfo.workplace_path}</code>
            </div>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
              <span style={{ color: 'var(--text-dim)' }}>Source:</span>
              <span style={{
                fontSize: 11,
                padding: '1px 6px',
                borderRadius: 4,
                color: workplaceInfo.source === 'manual' ? 'var(--accent)' : 'var(--text-secondary)',
                background: workplaceInfo.source === 'manual'
                  ? 'var(--accent-soft)'
                  : 'transparent',
                border: '1px solid var(--border)',
              }}>{sourceLabel}</span>
              {!workplaceInfo.exists && (
                <span style={{ color: '#f87171', fontSize: 11 }}>⚠ path not found</span>
              )}
            </div>
          </div>
        )}

        {/* Custom path input */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <label style={{ fontSize: 13, fontWeight: 500, color: 'var(--text)' }}>
            {t('project.workplace')}
            <span style={{ fontSize: 11, color: 'var(--text-dim)', marginLeft: 6 }}>
              {t('project.workplaceOptional')}
            </span>
          </label>
          <input
            className="theme-select"
            type="text"
            value={path}
            onChange={e => setPath(e.target.value)}
            placeholder={t('project.workplacePlaceholder')}
            style={{ width: '100%', boxSizing: 'border-box', fontFamily: 'monospace', fontSize: 12 }}
            autoFocus
          />
          <p style={{ margin: 0, fontSize: 11, color: 'var(--text-dim)' }}>
            {t('project.workplaceHint')}
          </p>
        </div>

        {/* Action buttons */}
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', flexWrap: 'wrap' }}>
          {workplaceInfo?.source === 'manual' && (
            <button
              className="pill-btn"
              onClick={handleReset}
              style={{ fontSize: 12, padding: '4px 12px', color: '#f87171' }}
            >
              {t('project.workplaceReset')}
            </button>
          )}
          <button
            className="pill-btn"
            onClick={onClose}
            style={{ fontSize: 12, padding: '4px 12px' }}
          >
            {t('common.cancel')}
          </button>
          <button
            className="pill-btn"
            onClick={handleSave}
            disabled={saving}
            style={{ fontSize: 12, padding: '4px 14px', fontWeight: 600 }}
          >
            {t('project.workplaceSave')}
          </button>
        </div>
      </div>
    </div>
  )
}
