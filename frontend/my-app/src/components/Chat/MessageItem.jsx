import React from 'react'
import { extractMessageContent } from '../../utils/messageContent'

export default function MessageItem({ message }) {
  const { id, role = 'assistant' } = message
  const text = extractMessageContent(message.text ?? message.content ?? message.message ?? message)
  const label = role === 'user' ? 'You' : 'AI'

  return (
    <li className={`message-item ${role}`} data-id={id}>
      <div className="message-avatar" aria-hidden="true">{label}</div>
      <div className="message-bubble">
        <span className="message-role">{label}</span>
        <p className="message-text">{text}</p>
      </div>
    </li>
  )
}
