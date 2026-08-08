import { useState, useCallback, useMemo } from 'react'
import type { Project } from '../types/kanban'
import { useI18n } from '../i18n'

function toProjectSlug(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-|-$/g, '')
}

interface ProjectSelectorProps {
  projects: Project[]
  activeId: string
  onSelect: (id: string) => void
  onCreate: (id: string, workplacePath?: string) => void
  onDelete?: (id: string) => void
  onSettings?: (id: string) => void
}

export function ProjectSelector({
  projects,
  activeId,
  onSelect,
  onCreate,
  onDelete,
  onSettings,
}: ProjectSelectorProps) {
  const { t } = useI18n()
  const [creating, setCreating] = useState(false)
  const [newName, setNewName] = useState('')
  const [newWorkplace, setNewWorkplace] = useState('')
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)

  const newSlug = useMemo(() => toProjectSlug(newName), [newName])

  const handleCreate = useCallback(() => {
    if (!newSlug) return
    onCreate(newSlug, newWorkplace.trim() || undefined)
    setNewName('')
    setNewWorkplace('')
    setCreating(false)
  }, [newSlug, newWorkplace, onCreate])

  const handleDelete = useCallback(() => {
    if (!confirmDelete || !onDelete) return
    onDelete(confirmDelete)
    setConfirmDelete(null)
  }, [confirmDelete, onDelete])

  return (
    <div className="project-selector" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <select
        className="theme-select"
        value={activeId}
        onChange={e => onSelect(e.target.value)}
        title={t('project.switch')}
        aria-label={t('project.switch')}
      >
        {projects.map(p => (
          <option key={p.id} value={p.id}>{p.name}</option>
        ))}
      </select>

      {creating ? (
        <div
          style={{
            position: 'fixed', inset: 0, zIndex: 9999,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            background: 'rgba(0,0,0,0.55)',
          }}
          onClick={e => { if (e.target === e.currentTarget) { setCreating(false); setNewName(''); setNewWorkplace('') } }}
        >
          <form
            onSubmit={e => { e.preventDefault(); handleCreate() }}
            onKeyDown={e => { if (e.key === 'Escape') { setCreating(false); setNewName(''); setNewWorkplace('') } }}
            style={{
              background: 'var(--bg-elevated)',
              border: '1px solid var(--border)',
              color: 'var(--text)',
              borderRadius: 12,
              padding: '24px 32px',
              minWidth: 420,
              maxWidth: 520,
              boxShadow: '0 8px 32px rgba(0,0,0,0.4)',
              display: 'flex',
              flexDirection: 'column',
              gap: 16,
            }}
          >
            <h3 style={{ margin: 0, fontSize: 15, fontWeight: 600, color: 'var(--text)' }}>
              {t('project.new')}
            </h3>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <label style={{ fontSize: 13, fontWeight: 500, color: 'var(--text)' }}>
                {t('project.nameLabel')}
              </label>
              <input
                autoFocus
                className="theme-select"
                value={newName}
                placeholder={t('project.placeholder')}
                onChange={e => setNewName(e.target.value)}
                style={{ width: '100%', boxSizing: 'border-box' }}
              />
              {newName.trim() && !newSlug && (
                <p style={{ margin: 0, fontSize: 11, color: '#f87171' }}>
                  {t('project.nameInvalid')}
                </p>
              )}
              {newSlug && newSlug !== newName.trim() && (
                <p style={{ margin: 0, fontSize: 11, color: 'var(--text-dim)' }}>
                  {t('project.slugPreview', { slug: newSlug })}
                </p>
              )}
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
              <label style={{ fontSize: 13, fontWeight: 500, color: 'var(--text)' }}>
                {t('project.workplace')}
                <span style={{ fontSize: 11, color: 'var(--text-dim)', marginLeft: 6 }}>
                  {t('project.workplaceOptional')}
                </span>
              </label>
              <input
                className="theme-select"
                value={newWorkplace}
                placeholder={t('project.workplacePlaceholder')}
                onChange={e => setNewWorkplace(e.target.value)}
                style={{ width: '100%', boxSizing: 'border-box', fontFamily: 'monospace', fontSize: 12 }}
              />
              <p style={{ margin: 0, fontSize: 11, color: 'var(--text-dim)' }}>
                {t('project.workplaceHint')}
              </p>
            </div>

            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button
                type="button"
                className="pill-btn"
                onClick={() => { setCreating(false); setNewName(''); setNewWorkplace('') }}
                style={{ fontSize: 12, padding: '4px 12px' }}
              >
                {t('common.cancel')}
              </button>
              <button
                type="submit"
                className="pill-btn"
                disabled={!newSlug}
                style={{ fontSize: 12, padding: '4px 14px', fontWeight: 600, opacity: newSlug ? 1 : 0.5 }}
              >
                {t('project.createButton')}
              </button>
            </div>
          </form>
        </div>
      ) : (
        <button
          className="pill-btn"
          onClick={() => setCreating(true)}
          title={t('project.new')}
          style={{ fontSize: 11, padding: '2px 8px' }}
        >
          +
        </button>
      )}

      {/* Settings button */}
      {onSettings && !creating && (
        <button
          className="pill-btn"
          onClick={() => onSettings(activeId)}
          title={t('project.settings')}
          style={{ fontSize: 11, padding: '2px 8px' }}
        >
          ⚙
        </button>
      )}

      {onDelete && activeId !== 'default' && !creating && !confirmDelete && (
        <button
          className="pill-btn"
          onClick={() => setConfirmDelete(activeId)}
          title={t('project.delete')}
          style={{ fontSize: 11, padding: '2px 8px', color: '#ef4444' }}
        >
          {t('project.deleteButton')}
        </button>
      )}

      {confirmDelete && (
        <div className="project-delete-confirm" style={{
          position: 'fixed', inset: 0, zIndex: 9999,
          display: 'flex', alignItems: 'center', justifyContent: 'center',
          background: 'rgba(0,0,0,0.5)',
        }}>
          <div style={{
            background: 'var(--bg-elevated)', borderRadius: 12, padding: '24px 32px',
            border: '1px solid var(--border)', color: 'var(--text)',
            maxWidth: 400, boxShadow: '0 8px 32px rgba(0,0,0,0.4)', textAlign: 'center',
          }}>
            <p style={{ margin: '0 0 8px', fontWeight: 600, fontSize: 15, color: 'var(--text)' }}>
              {t('project.confirmTitle', { name: confirmDelete })}
            </p>
            <p style={{ margin: '0 0 20px', fontSize: 13, color: 'var(--text-secondary)' }}>
              {t('project.confirmBody')}
            </p>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'center' }}>
              <button
                className="pill-btn"
                onClick={() => setConfirmDelete(null)}
                style={{ padding: '6px 18px', fontSize: 13 }}
              >
                {t('common.cancel')}
              </button>
              <button
                className="pill-btn"
                onClick={handleDelete}
                style={{ padding: '6px 18px', fontSize: 13, background: '#ef4444', color: '#fff' }}
              >
                {t('common.delete')}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
