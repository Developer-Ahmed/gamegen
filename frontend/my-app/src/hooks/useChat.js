import { useState, useEffect, useCallback } from 'react'
import { v4 as uuidv4 } from 'uuid'
import { extractMessageContent } from '../utils/messageContent'

// Minimal client-side chat hook with safe defaults and cancellation
export default function useChat() {
  const [messages, setMessages] = useState([])
  const [isSending, setIsSending] = useState(false)
  const [error, setError] = useState(null)

  useEffect(() => {
    // load session messages from localStorage safely
    try {
      const raw = window.localStorage.getItem('chat:messages')
      if (raw) setMessages(JSON.parse(raw))
    } catch (e) {
      console.error('Failed to load messages', e)
    }
  }, [])

  useEffect(() => {
    try {
      window.localStorage.setItem('chat:messages', JSON.stringify(messages))
    } catch (e) {
      console.error('Failed to save messages', e)
    }
  }, [messages])

  const sendMessage = useCallback(async (text) => {
    setIsSending(true)
    setError(null)
    const userMessage = { id: uuidv4(), role: 'user', text, createdAt: Date.now() }
    setMessages((m) => [...m, userMessage])

    // call backend API that proxies to an LLM. Keep backend approach secure (no API keys in client).
    const controller = new AbortController()
    try {
      const resp = await fetch('http://localhost:8000/chat/send/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, thread_id: 'default' }),
        signal: controller.signal,
      })

      if (!resp.ok) {
        const payload = await resp.text()
        throw new Error(payload || 'Failed to fetch response')
      }

      const contentType = resp.headers.get('content-type') || ''
      const payload = contentType.includes('application/json')
        ? await resp.json()
        : await resp.text()
      const assistantText = extractMessageContent(payload) || 'No response content received.'

      setMessages((prev) => [
        ...prev,
        { id: uuidv4(), role: 'assistant', text: assistantText, createdAt: Date.now() },
      ])
    } catch (e) {
      if (e.name === 'AbortError') {
        console.info('Chat request aborted')
      } else {
        console.error(e)
        setError('Unable to get response')
      }
    } finally {
      setIsSending(false)
    }

    return () => controller.abort()
  }, [])

  return { messages, sendMessage, isSending, error }
}
