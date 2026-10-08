# Local backend

FastAPI sends each question to local Ollama. Ollama requests SQL queries, the backend runs them
against the existing `nba.db`, and query results/errors go back to Ollama until it returns an answer.
Uses Ollama's [tool-calling API](https://docs.ollama.com/capabilities/tool-calling).

## Run

Run these commands from the repository root with Python 3.11 or newer:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
```

Start your installed Ollama in another terminal with `ollama serve` (or its desktop app).
The backend connects to `http://localhost:11434`. Choose an installed model that supports tools;
`ollama list` shows installed names. Set its exact name below:

```sh
export OLLAMA_MODEL="YOUR_INSTALLED_MODEL"
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

API documentation: `http://127.0.0.1:8000/docs`. `GET /health` checks that FastAPI is running;
it does not check Ollama or model readiness.

## Ask

```sh
curl http://127.0.0.1:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"What did Nikola Jokic average in 2025-26?"}'
```

The response is `{"answer":"..."}`. Errors use FastAPI's `{"detail":"..."}` response.
Questions must contain 1–4,000 characters after trimming whitespace. Each question is independent;
the frontend keeps its existing local history. The frontend's placeholder response still needs
to be connected to `POST /ask`. CORS allows the existing Vite app at localhost/127.0.0.1 port 5173.

## Limits and data

- SQLite opens read-only. The SQL authorizer permits reads from the five NBA tables and blocks
  writes, database attachment, PRAGMA statements, and extension loading. One statement per call.
- Maximum 100 returned rows per query, 5 seconds per query, 8 model calls and 8 SQL tool calls per
  question, and 120 seconds per Ollama call. SQL errors go back to the model for correction.
- The system prompt includes the actual schema and data limits: regular seasons 1993-94–2025-26,
  end-year season numbering, incomplete 1992-93 totals, skipped box scores, historical team ids,
  and missing plus/minus through 2007-08. The database is never modified.

## Test

```sh
python -m unittest discover -s backend -p 'test_*.py' -v
```

Tests use a temporary SQLite database and simulated Ollama HTTP responses. They do not require
Ollama or modify `nba.db`. A live model check also requires your local Ollama and chosen model.
