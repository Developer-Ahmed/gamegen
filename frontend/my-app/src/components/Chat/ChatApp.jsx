import React from 'react'
import MessageList from './MessageList'
import Composer from './Composer'
import useChat from '../../hooks/useChat'
import './chat.css'

export default function ChatApp() {
  const { messages, sendMessage, isSending, error } = useChat()

  return (
    <main className="chat-root" role="main">
      <aside className="chat-sidebar" aria-label="Conversations">
        <h2 className="sr-only">Conversations</h2>
        <div className="sidebar-brand">
          <span className="brand-mark" aria-hidden="true">G</span>
          <div>
            <p className="brand-name">GameGen</p>
            <span className="brand-subtitle">Workspace</span>
          </div>
        </div>
        <button className="conversation-button active" type="button">
          <span>Current chat</span>
          <small>{messages.length} messages</small>
        </button>
      </aside>

      <section className="chat-panel" aria-label="Chat panel">
        <header className="chat-header">
          <div>
            <span className="chat-kicker">AI Assistant</span>
            <h1>Game builder</h1>
          </div>
          <span className="status-pill">
            <span className="status-dot" aria-hidden="true" />
            Ready
          </span>
        </header>

        <MessageList messages={messages} />

        <footer className="chat-composer">
          <Composer onSend={sendMessage} disabled={isSending} />
          {error && <div className="chat-error" role="alert">{error}</div>}
        </footer>
      </section>
    </main>
  )
}
