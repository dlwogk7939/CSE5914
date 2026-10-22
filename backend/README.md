# Backend

FastAPI sends each question to Gemini. Gemini requests SQL queries, the backend runs them
against the local `nba.db`, and query results/errors go back to Gemini until it returns an answer.
Uses Google's [OpenAI-compatible REST API](https://ai.google.dev/gemini-api/docs/openai#function-calling)
with the existing HTTPX dependency.

## Run

Run these commands from the repository root with Python 3.11 or newer:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
```

The backend automatically loads the repository-root `.env` at startup:

```dotenv
API_KEY=YOUR_GEMINI_API_KEY
```

The default model is `gemini-3.5-flash-lite`. Optionally set `GEMINI_MODEL` in `.env` to change it.
The frontend's Model dropdown offers Gemini 3.5 Flash-Lite, Gemini 3.1 Flash-Lite, and Gemini 3.8 Flash,
plus a custom `GEMINI_MODEL` default if configured. All choices use the same backend `API_KEY`.
The browser remembers the selected model and labels new answers with the model that generated them.
Google currently lists free-tier access for the three built-in models; actual access and quotas depend
on your Google project and billing tier. See [pricing](https://ai.google.dev/gemini-api/docs/pricing).
Existing environment variables take precedence over `.env`. The key stays on the backend;
`.env` is ignored by Git. Restart the backend after changing `.env`.

```sh
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

API documentation: `http://127.0.0.1:8000/docs`. `GET /health` checks that FastAPI is running;
it does not check Gemini access or quota.

## Ask

```sh
curl http://127.0.0.1:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"What did Nikola Jokic average in 2025-26?"}'
```

`GET /models` returns `{ "default": "...", "models": [{ "id": "...", "label": "..." }] }`.
It lists supported choices without contacting Google or exposing credentials; it does not verify quota.
`POST /ask` accepts an optional `model` from this list:
`{"question":"Who led in assists in 2025-26?","model":"gemini-3.8-flash"}`.
Omitting `model` preserves the configured backend default. Unknown models return HTTP 422.
The response is `{"answer":"...","model":"...","model_label":"..."}`.
Errors use FastAPI's `{"detail":"..."}` response. A failed model request does not silently switch models.
Questions must contain 1–4,000 characters after trimming whitespace. Each question is independent;
the frontend keeps its existing local history. Run `npm install` once, then `npm run dev` from
`my-app` to use the connected frontend. CORS allows Vite at localhost/127.0.0.1 port 5173.

## Limits and data

- SQLite opens read-only. The SQL authorizer permits reads from the five NBA tables and blocks
  writes, database attachment, PRAGMA statements, and extension loading. One statement per call.
- Maximum 100 returned rows per query, 5 seconds per query, 8 model calls and 8 SQL tool calls per
  question, and 120 seconds per Gemini call. SQL errors go back to the model for correction.
- The system prompt includes the actual schema and data limits: regular seasons 1993-94–2025-26,
  end-year season numbering, incomplete 1992-93 totals, skipped box scores, historical team ids,
  and missing plus/minus through 2007-08. The database is never modified.

## Test

```sh
python -m unittest discover -s backend -p 'test_*.py' -v
```

Tests use a temporary SQLite database, a fake API key, and simulated Gemini HTTP responses.
They do not call Gemini or modify `nba.db`. A live model check requires a working Gemini key,
network access, and available API quota.
