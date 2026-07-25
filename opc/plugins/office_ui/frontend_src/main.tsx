import React from 'react'
import { createRoot } from 'react-dom/client'
import App from './App'
import { I18nProvider } from './i18n'
import './index.css'

class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null; info: string }
> {
  state = { error: null as Error | null, info: '' }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    console.error('[ErrorBoundary] React crashed:', error, errorInfo)
    this.setState({ info: errorInfo.componentStack ?? '' })
  }

  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 32, color: '#ff6b6b', background: '#1a1a2e', minHeight: '100vh', fontFamily: 'monospace' }}>
          <h2>UI Error — React crashed</h2>
          <pre style={{ whiteSpace: 'pre-wrap', fontSize: 13 }}>
            {this.state.error.message}
          </pre>
          <details open style={{ marginTop: 12, fontSize: 12, color: '#ccc' }}>
            <summary>Stack trace</summary>
            <pre style={{ whiteSpace: 'pre-wrap' }}>{this.state.error.stack}</pre>
          </details>
          {this.state.info && (
            <details style={{ marginTop: 12, fontSize: 12, color: '#888' }}>
              <summary>Component stack</summary>
              <pre style={{ whiteSpace: 'pre-wrap' }}>{this.state.info}</pre>
            </details>
          )}
          <button
            onClick={() => { this.setState({ error: null, info: '' }) }}
            style={{ marginTop: 16, padding: '8px 16px', background: '#6366f1', color: '#fff', border: 'none', borderRadius: 6, cursor: 'pointer' }}
          >
            Try to recover
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

// Global error handler for uncaught JS errors
window.addEventListener('error', (e) => {
  console.error('[Global] Uncaught error:', e.error ?? e.message)
})
window.addEventListener('unhandledrejection', (e) => {
  console.error('[Global] Unhandled promise rejection:', e.reason)
})

const root = document.getElementById('root')
if (!root) {
  document.body.innerHTML = '<h1 style="color:red">Root element not found</h1>'
  throw new Error('Root element #root not found')
}

try {
  createRoot(root).render(
    <ErrorBoundary>
      <I18nProvider>
        <App />
      </I18nProvider>
    </ErrorBoundary>
  )
} catch (err) {
  console.error('React render error:', err)
  root.innerHTML = `<h1 style="color:red">React Error: ${err}</h1>`
}

