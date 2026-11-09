# GameGen Backend

The generation agent uses Groq's OpenAI-compatible API with
`openai/gpt-oss-120b`. Configure it in `backend/.env`:

```dotenv
GROQ_API_KEY=your_key
# Optional conservative defaults for an 8,000 token/minute quota:
GROQ_TOKENS_PER_MINUTE=8000
GROQ_TOKEN_BUDGET=7600
GROQ_CONTEXT_WINDOW=7600
GROQ_MAX_OUTPUT_TOKENS=1200
```

Start the backend with `poetry run start_dev` from `backend/`.
