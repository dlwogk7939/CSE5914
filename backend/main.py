import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Annotated

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, StringConstraints

DB_PATH = Path(__file__).resolve().parents[1] / "nba.db"
load_dotenv(DB_PATH.parent / ".env")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODELS = {
    "gemini-3.8-flash": "Gemini 3.8 Flash",
    "gemini-3.5-flash-lite": "Gemini 3.5 Flash-Lite",
    "gemini-3.1-flash-lite": "Gemini 3.1 Flash-Lite"
}
MAX_STEPS, MAX_QUERIES, MAX_ROWS = 8, 8, 100
QUERY_TIMEOUT, GEMINI_TIMEOUT = 5, 120
TABLES = {"teams", "players", "games", "box_scores", "season_totals"}

app = FastAPI(title="AskNBA backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class Question(BaseModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None


class Answer(BaseModel):
    answer: str
    model: str
    model_label: str


@app.get("/models")
def models():
    default = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.5-flash-lite"
    # Preserve an administrator's configured default alongside the curated choices.
    choices = {**GEMINI_MODELS}
    choices.setdefault(default, default)
    return {"default": default, "models": [{"id": key, "label": label} for key, label in choices.items()]}


TOOLS = [{
    "type": "function",
    "function": {
        "name": "query_sql",
        "description": "Run one read-only SQLite SELECT or WITH query. Returns columns, rows, and truncated, or an error. At most 100 rows; aggregate or filter larger results.",
        "parameters": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
}]


def connect():
    return sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True, timeout=QUERY_TIMEOUT)


def system_prompt():
    with closing(connect()) as db:
        schema = "\n".join(row[0] for row in db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ))
    return f"""You answer NBA statistics questions using the query_sql tool and this SQLite schema:
{schema}

Use database results for statistical claims. Never invent statistics or treat missing data as zero.
You can make multiple queries. Correct SQL errors and retry. If results are truncated, filter,
order, or aggregate in SQL before making claims about the complete result. Use only one statement
per tool call. Ask the user to clarify an ambiguous player, season, or metric.

Data covers NBA regular seasons 1993-94 through 2025-26, not playoffs or live/current statistics.
season is the END year: 2026 means 2025-26. season_totals also has an incomplete 1992-93 subset
(season=1993); do not use that subset for league rankings or complete-season coverage claims.
Use season_totals for season stats and games played. Its rows combine teams when a player was traded.
Some box score rows were skipped during ingestion; game-level aggregates may be incomplete.
players.team_id is the team from the last processed game, not a reliable current-team field.
Use box_scores.team_id for the team a player represented in a particular game.
box_scores.plus_minus is unavailable through season=2008; those zeroes represent missing data.
Do not claim all-time records outside the available coverage. Resolve player names to player_id
with a lookup query before querying their stats. Keep the final answer concise and name the season.
"""


def authorize(action, table, function, database, trigger):
    allowed = (
        action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE)
        # SQLite sometimes omits the database name for reads that do not access a column.
        or (action == sqlite3.SQLITE_READ and table in TABLES and database in ("main", None))
        or (action == sqlite3.SQLITE_FUNCTION and str(function).lower() != "load_extension")
    )
    return sqlite3.SQLITE_OK if allowed else sqlite3.SQLITE_DENY


def query_sql(sql):
    try:
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("sql must be a nonempty string.")
        with closing(connect()) as db:
            db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 10000)
            db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 100000)
            db.set_authorizer(authorize)
            deadline = time.monotonic() + QUERY_TIMEOUT
            db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            cursor = db.execute(sql)
            if cursor.description is None:
                raise ValueError("The query must return rows.")
            rows = cursor.fetchmany(MAX_ROWS + 1)
            return {
                "columns": [column[0] for column in cursor.description],
                "rows": rows[:MAX_ROWS],
                "truncated": len(rows) > MAX_ROWS,
            }
    except (sqlite3.Error, ValueError) as error:
        return {"error": str(error)}


def tool_result(call):
    try:
        function = call["function"]
        if function["name"] != "query_sql":
            raise ValueError("The only available tool is query_sql.")
        result = query_sql(json.loads(function["arguments"])["sql"])
        return json.dumps(result)
    except (KeyError, TypeError, ValueError) as error:
        return json.dumps({"error": f"Invalid tool call: {error}"})


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ask", response_model=Answer)
def ask(request: Question):
    catalog = models()
    model = request.model if request.model is not None else catalog["default"]
    selected = next((item for item in catalog["models"] if item["id"] == model), None)
    if selected is None:
        raise HTTPException(422, "Choose a supported Gemini model from /models.")
    api_key = os.environ.get("API_KEY", "").strip()
    if not api_key:
        raise HTTPException(503, "Set API_KEY in the repository's .env file to your Gemini API key.")
    try:
        messages = [
            {"role": "system", "content": system_prompt()},
            {"role": "user", "content": request.question},
        ]
        queries = 0
        with httpx.Client(timeout=GEMINI_TIMEOUT, trust_env=False) as client:
            for _ in range(MAX_STEPS):
                response = client.post(GEMINI_URL, headers={"Authorization": f"Bearer {api_key}"}, json={
                    "model": model, "messages": messages, "tools": TOOLS, "stream": False,
                })
                response.raise_for_status()
                message = response.json()["choices"][0]["message"]
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    raise ValueError("Expected an assistant message.")
                messages.append(message)
                calls = message.get("tool_calls") or []
                if not isinstance(calls, list):
                    raise ValueError("Expected a list of tool calls.")
                if not calls:
                    answer = message.get("content", "").strip()
                    if not answer:
                        raise ValueError("The model returned an empty answer.")
                    return {"answer": answer, "model": model, "model_label": selected["label"]}
                for call in calls:
                    queries += 1
                    if queries > MAX_QUERIES:
                        raise HTTPException(504, "The model exceeded the SQL query limit.")
                    messages.append({
                        "role": "tool", "name": call["function"]["name"],
                        "tool_call_id": call["id"], "content": tool_result(call),
                    })
        raise HTTPException(504, "The model exceeded the query loop step limit.")
    except httpx.TimeoutException as error:
        raise HTTPException(504, "Gemini timed out.") from error
    except httpx.HTTPStatusError as error:
        detail = error.response.text.replace(api_key, "[redacted]")[:500]
        raise HTTPException(429 if error.response.status_code == 429 else 502, f"Gemini rejected the request: {detail}") from error
    except httpx.RequestError as error:
        raise HTTPException(503, "Cannot reach the Gemini API.") from error
    except sqlite3.Error as error:
        raise HTTPException(503, f"Cannot read nba.db: {error}") from error
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as error:
        raise HTTPException(502, f"Invalid Gemini response: {error}") from error
