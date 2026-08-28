import React, { useEffect, useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'

import type { ChatMessage } from '../types/chat'
import type { Session } from '../types/kanban'
import { ContextPanel } from '../workspace/ContextPanel'
import { selectMultiViewSessions } from '../workspace/WorkspacePage'
import '../index.css'
import '../chat/chat.css'
import '../workspace/workspace.css'

declare global {
  interface Window {
    __contextPanelFixtureReady?: boolean
    __contextPanelFixture?: {
      toggleMultiView: () => void
      selectSession: (taskId: string) => void
    }
  }
}

function makeSession(index: number): Session {
  const taskId = `task-${index + 1}`
  return {
    projectId: 'fixture-project',
    taskId,
    channelId: `session:${taskId}`,
    title: `Chat ${index + 1}`,
    status: index % 3 === 0 ? 'running' : 'done',
    columnId: index % 3 === 0 ? 'in-progress' : 'done',
    assigneeIds: [],
    priority: null,
    tags: [],
    progressLog: [],
    createdAt: index + 1,
    updatedAt: index + 1,
    messageCount: index % 2 === 0 ? 1 : 0,
    mode: 'primary',
  }
}

const sessions = Array.from({ length: 20 }, (_, index) => makeSession(index))
const messagesByTask = Object.fromEntries(sessions.map((session, index): [string, ChatMessage[]] => [
  session.taskId,
  index % 2 === 0
    ? [{
      id: `message-${index + 1}`,
      channelId: session.channelId,
      sender: 'assistant',
      senderName: 'OPC',
      content: `Durable transcript for ${session.title}`,
      timestamp: index + 1,
      mentions: [],
      metadata: {},
    }]
    : [],
]))

function Fixture() {
  const [activeTaskId, setActiveTaskId] = useState(sessions[0].taskId)
  const [multiSessionView, setMultiSessionView] = useState(false)
  const activeSession = sessions.find(session => session.taskId === activeTaskId) ?? sessions[0]
  const multiViewSessions = useMemo(
    () => selectMultiViewSessions(sessions, activeTaskId),
    [activeTaskId],
  )
  const multiSessionMessages = useMemo(
    () => Object.fromEntries(multiViewSessions.map(session => [session.taskId, messagesByTask[session.taskId]])),
    [multiViewSessions],
  )

  useEffect(() => {
    window.__contextPanelFixture = {
      toggleMultiView: () => setMultiSessionView(value => !value),
      selectSession: setActiveTaskId,
    }
    window.__contextPanelFixtureReady = true
    return () => {
      delete window.__contextPanelFixture
      delete window.__contextPanelFixtureReady
    }
  }, [])

  return (
    <main style={{ display: 'flex', width: '100vw', height: '100vh', background: 'var(--bg)' }}>
      <ContextPanel
        panelState="open"
        width={1100}
        onResizeMouseDown={() => {}}
        isResizing={false}
        activeView={{ kind: 'session', taskId: activeSession.taskId }}
        activeSession={activeSession}
        activeTask={null}
        linkedTaskSession={null}
        linkedTaskSessionMessages={[]}
        childDetailSession={null}
        messages={messagesByTask[activeSession.taskId]}
        childDetailMessages={[]}
        allSessions={sessions}
        openSessions={sessions}
        multiViewSessions={multiViewSessions}
        multiSessionMessages={multiSessionMessages}
        agents={[]}
        childSessions={[]}
        taskPreferredAgent="native"
        nativeApprovalDefault="auto"
        channelId={activeSession.channelId}
        channelName={activeSession.title}
        secretaryChannelId="secretary:fixture-project"
        multiSessionView={multiSessionView}
        panelTab="chat"
        onPanelTabChange={() => {}}
        onTitleChange={() => {}}
        onSelectSessionTab={setActiveTaskId}
        onToggleMultiSessionView={() => setMultiSessionView(value => !value)}
        onCollapse={() => {}}
        onExpand={() => {}}
        onMaximize={() => {}}
        onComposerSend={() => {}}
        onInteractionReply={async () => ({
          ok: true,
          accepted: true,
          checkpoint_id: 'fixture',
          checkpoint_type: 'fixture',
          client_request_id: 'fixture',
        })}
        onWorkItemClick={() => {}}
        onMarkRead={() => {}}
      />
    </main>
  )
}

const root = document.getElementById('root')
if (!root) throw new Error('fixture root is missing')
createRoot(root).render(<Fixture />)
