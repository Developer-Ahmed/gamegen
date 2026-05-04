import React from 'react'
import MessageItem from './MessageItem'

export default function MessageList({ messages = [] }) {
  return (
    <ul className="message-list" role="log" aria-live="polite">
      {messages.length === 0 && (
        <li className="empty-chat">
          <span className="empty-chat-mark" aria-hidden="true">G</span>
          <h2>Ready when you are.</h2>
        </li>
      )}
      {messages.map((m) => (
        <MessageItem key={m.id} message={m} />
      ))}
    </ul>
  )
}
