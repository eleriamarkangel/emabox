# Emabox AI API

Emabox is a small Python API that accepts prompts from multiple applications, selects a specialist, calls Gemini during local development or GPT-6 Luna in production, and returns a response. Applications own their conversation databases. Each request may include whichever earlier messages the application wants the model to see; Emabox never retrieves or stores a conversation.

## Quick start (Windows PowerShell)

Requires Python 3.11 or newer. Create a virtual environment and install the service:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
Copy-Item .env.example .env
```

Edit `.env` with a real Gemini API key and a unique random key for each calling application. Keep `.env` out of Git. A new Gemini project may need a current Flash model; the default is `gemini-3.8-flash`. If your account has access to the model in your example, set `EMABOX_GEMINI_MODEL=gemini-2.5-flash`.

Start the local server:

```powershell
.\.venv\Scripts\python.exe -m uvicorn emabox.main:app --env-file .env --reload
```

Open `http://127.0.0.1:8000/docs` for the interactive request schema. `GET /health` needs no key. Other requests require the `X-API-Key` header. **Enter an application key from `EMABOX_API_CLIENTS` here, not your Gemini or OpenAI provider key.** The provider key stays inside Emabox and is used only for outbound model calls. Use HTTPS when the service is deployed.

## Request contract

`POST /v1/responses` accepts:

```json
{
  "prompt": "Today felt overwhelming.",
  "goal": "Respond to my journal entry",
  "messages": [
    {"role": "user", "content": "Yesterday I felt anxious."},
    {"role": "assistant", "content": "That sounds exhausting."}
  ],
  "stream": false
}
```

`prompt` is required. `goal`, `messages`, `specialist`, and `stream` are optional. `messages` holds only the earlier turns or other context the application chooses to send, in order. Each entry must have a `user` or `assistant` role. Omit it for a one-shot request. `specialist` can explicitly select `journal`, `it`, `social`, or `generic`; otherwise Emabox routes by `goal`, then the application's configured default, then the current prompt. The selected specialist is returned to the caller.

The ordinary response is JSON:

```json
{
  "request_id": "a UUID",
  "specialist": "journal",
  "provider": "gemini",
  "model": "gemini-3.8-flash",
  "response": "That sounds like a heavy day.",
  "usage": {"input_tokens": 123, "output_tokens": 38}
}
```

With `"stream": true`, the response is a Server-Sent Events stream: one `meta` event, zero or more `delta` events, and a final `done` event with usage. A provider failure after streaming starts sends an `error` event, because HTTP status cannot be changed after the stream begins.

### Example call

```powershell
$body = '{"prompt":"Help me investigate a Python production error","stream":false}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/responses `
  -Headers @{"X-API-Key"="your-long-random-app-key"} `
  -ContentType application/json -Body $body
```

## Configuration

- `EMABOX_PROVIDER`: `gemini` or `openai`. Set `openai` in production for GPT-6 Luna.
- `GEMINI_API_KEY`, `OPENAI_API_KEY`: provider credentials. Only the selected provider's key is required.
- `EMABOX_GEMINI_MODEL`, `EMABOX_OPENAI_MODEL`: model IDs. OpenAI defaults to `gpt-6-luna`.
- `EMABOX_API_CLIENTS`: JSON mapping application IDs to `key` and optional `default_specialist`. Keys must be at least 16 characters. Rotate them in your deployment secret store.
- `EMABOX_DATABASE_URL`: SQLAlchemy URL. Defaults to `sqlite:///./emabox.db`; use `postgresql+psycopg://user:password@host:5432/emabox` in the cloud.
- `EMABOX_LOG_RETENTION_DAYS`: defaults to 40.

The generic specialist uses provider token counting to reject inputs above 1,200 tokens, including its instructions, and caps generation at 256 output tokens. Other specialists cap output at 1,200 tokens. The endpoint also limits message count and request text size. Routing uses simple goal and keyword rules, so callers can set `specialist` when they need a guaranteed choice. No extra model call is spent on routing.

The audit database stores request ID, application ID, specialist, provider, model, status, latency, token usage, and timestamp. It never stores prompts, earlier messages, generated responses, or provider keys. Rows older than 40 days are removed on startup and daily while the service runs. If the service is stopped, deletion resumes on its next startup. OpenAI calls use `store=False` for the Responses API.

For production, set the provider and database URL in your deployment environment, supply secrets through your cloud secret manager, and run a single process initially. Add database migrations, per-app rate limits, and monitoring before exposing this gateway broadly.

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Tests use a fake model provider, so they need no Gemini or OpenAI credentials. Live provider integration requires keys and is intentionally separate from the offline suite.

## Provider documentation

- [Gemini Python SDK](https://googleapis.github.io/python-genai/)
- [Gemini model catalog](https://ai.google.dev/gemini-api/docs/models)
- [OpenAI Responses API](https://developers.openai.com/api/docs/guides/text)
- [OpenAI streaming](https://developers.openai.com/api/docs/guides/streaming-responses)
- [OpenAI GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)
