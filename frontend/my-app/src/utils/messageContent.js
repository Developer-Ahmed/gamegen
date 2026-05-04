const ASSISTANT_ROLE_HINTS = ['assistant', 'ai', 'bot', 'model']
const SKIPPED_KEYS = new Set([
  'id',
  'key',
  'name',
  'role',
  'type',
  'createdAt',
  'created_at',
  'additional_kwargs',
  'response_metadata',
  'usage_metadata',
  'tool_calls',
  'invalid_tool_calls',
])

function roleHint(value) {
  return String(value?.role || value?.type || value?.name || '').toLowerCase()
}

function contentToText(value) {
  if (value == null) return ''
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) {
    return value.map(contentToText).filter(Boolean).join('\n')
  }
  if (typeof value === 'object') {
    if (value.content != null) return contentToText(value.content)
    if (value.text != null) return contentToText(value.text)
    if (value.message != null) return contentToText(value.message)
  }
  return ''
}

function extractFromArray(values) {
  const reversed = [...values].reverse()
  const assistantMessage = reversed.find((item) => {
    const role = roleHint(item)
    return ASSISTANT_ROLE_HINTS.some((hint) => role.includes(hint)) && contentToText(item)
  })

  if (assistantMessage) return extractMessageContent(assistantMessage)

  for (const item of reversed) {
    const content = extractMessageContent(item)
    if (content) return content
  }

  return ''
}

export function extractMessageContent(payload) {
  if (payload == null) return ''
  if (typeof payload === 'string') return payload
  if (typeof payload === 'number' || typeof payload === 'boolean') return String(payload)
  if (Array.isArray(payload)) return extractFromArray(payload)

  if (typeof payload === 'object') {
    if (payload.messages != null) return extractMessageContent(payload.messages)

    const directContent = contentToText(payload.content)
      || contentToText(payload.text)
      || contentToText(payload.output)
      || contentToText(payload.response)
      || contentToText(payload.message)

    if (directContent) return directContent

    for (const [key, value] of Object.entries(payload).reverse()) {
      if (SKIPPED_KEYS.has(key)) continue
      const nestedContent = extractMessageContent(value)
      if (nestedContent) return nestedContent
    }
  }

  return ''
}

