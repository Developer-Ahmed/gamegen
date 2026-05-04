import React, { useState, useRef, useEffect } from 'react'

export default function Composer({ onSend, disabled }) {
  const [value, setValue] = useState('')
  const textareaRef = useRef(null)

  useEffect(() => {
    if (!textareaRef.current) return
    textareaRef.current.style.height = 'auto'
    textareaRef.current.style.height = textareaRef.current.scrollHeight + 'px'
  }, [value])

  const handleSubmit = (e) => {
    e.preventDefault()
    const trimmed = value.trim()
    if (!trimmed || disabled) return
    onSend(trimmed)
    setValue('')
  }

  return (
    <form className="composer" onSubmit={handleSubmit}>
      <label htmlFor="chat-input" className="sr-only">Message</label>
      <textarea
        id="chat-input"
        ref={textareaRef}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="Ask me anything..."
        rows={1}
        disabled={disabled}
        aria-disabled={disabled}
        maxLength={2000}
      />
      <div className="composer-actions">
        <button type="submit" className="send-btn" disabled={disabled} aria-disabled={disabled}>
          Send
        </button>
      </div>
    </form>
  )
}
