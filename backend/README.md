# Backend

FastAPI sends each question to Gemini. Gemini requests SQL queries, the backend runs them
against the local `nba.db`, and query results/errors go back to Gemini until it returns an answer.
The same database now contains multiple sports, with shared league metadata and separate stat tables.
Uses Google's [OpenAI-compatible REST API](https://ai.google.dev/gemini-api/docs/openai#function-calling)
with the existing HTTPX dependency.

## Run

The database is stored with [Git LFS](https://docs.github.com/en/repositories/working-with-files/managing-large-files/installing-git-large-file-storage).
Install Git LFS (`brew install git-lfs` on macOS), then run from the repository root:

```sh
git lfs install --local
git lfs pull
```

Do this before starting the app or building its Docker image so `nba.db` contains the full database.

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
The response is `{"answer":"...","suggestions":["...","...","..."],"model":"...","model_label":"..."}`.
The same model response supplies three related follow-up questions. The provider receives a JSON schema;
the backend extracts Markdown answer text and a cleaned list of up to three suggestions separately.
Older plain-text responses remain compatible with an empty suggestions list.
Errors use FastAPI's `{"detail":"..."}` response. A failed model request does not silently switch models.
`POST /ask` also accepts an optional `league` ID from `/coverage` (for example, `nfl`).
The selected league becomes the model's question/follow-up context; unknown or unloaded leagues return HTTP 422.
Omitting `league` keeps the existing conversation behavior. `GET /recent-games?league=nfl&limit=8`
returns loaded results for that league; omitting the league preserves the original NBA cards.
NBA cards combine new imports with historical games, showing each game once with scores and its top scorer.
Questions must contain 1–4,000 characters after trimming whitespace. An optional `history` array contains previous `{ "question": "...", "answer": "..." }` turns in chronological order (up to 100 turns, 4,000 characters per question and 20,000 per answer). The backend forwards those turns before the new question; omitted history starts an independent conversation.
The frontend saves separate chats locally, restores the selected chat after refresh, and shows all questions and answers. Existing single-question records become separate chats. New chat starts with empty context. Each chat supports 100 questions; prior answers sent as context are capped at 20,000 characters, while the displayed answers remain intact. Run `npm install` once, then `npm run dev` from
`my-app` to use the connected frontend. CORS allows Vite at localhost/127.0.0.1 port 5173.

## Limits and data

- Gemini's SQLite connection opens read-only. The SQL authorizer permits approved sports/coverage tables and blocks
  writes, database attachment, PRAGMA statements, and extension loading. One statement per call.
- Maximum 100 returned rows per query, 5 seconds per query, 20 model calls and 20 SQL tool calls per
  question, and 120 seconds per Gemini call. SQL errors go back to the model for correction.
- The prompt includes the loaded schema, league/season/phase coverage, and source caveats.
  Gemini's SQL connection is read-only. The ESPN importer/updater writes new data.

## Multi-sport data

The included snapshot covers NFL and WNBA 2025/2026 and Premier League 2025-26/2026-27
with game box scores and available season totals. Current seasons include completed games through
the import date. Other feeds have partial coverage; older NBA history remains available.
Use `/coverage` and `data_coverage` to check the loaded window before making completeness claims.

Run from the repository root:

```sh
.venv/bin/python -u -m backend.espn --include-current --postseason --years 10
```

This imports the current season plus nine prior seasons for each configured league, including
playoffs/play-in and college tournaments. It stores player/team box scores and official ESPN
season totals where available; missing source data stays unavailable. It does not collect play-by-play.
Configured feeds cover NBA/WNBA, men's/women's college basketball, Division I college football
(FBS/FCS), NFL, MLB, NHL, six soccer leagues, PGA golf, ATP/WTA tennis, UFC, and Formula 1.
Individual sports use match/fight/race/tournament scorecards. Tennis includes singles/doubles;
mixed doubles are stored once under ATP, distinguished by category. ESPN sometimes supplies
scorecards without detailed match stats; `detailed_stats_available=0` records that limitation.
F1 season totals use official driver/constructor standings. UFC season totals are derived from
loaded fight box scores and clearly marked `derived_*`; unresolved results are not counted as losses.
Original NBA tables and their older history remain available. New data uses `league_*` metadata
and sport-specific tables such as `football_box_scores` and `basketball_season_totals`.
Join ids with `league_id`; filter `season_type` to keep regular and postseason stats separate.
Season-total columns retain category prefixes to distinguish overlapping stat names.

The import commits each game and checkpoints each day. Run the same command to resume.
One process owns the import lock; the default is six workers with a shared six-request/second cap.
Use `--leagues nfl mlb` to select leagues or `--days 3` to refresh recent games and season totals.
The server's automatic six-hour refresh and `POST /update` use the latest three days.
Set `AUTO_UPDATE=0` to disable automatic refresh while testing.

```sh
.venv/bin/python -m backend.espn --status
```

`GET /coverage` returns league counts. SQL views/tables `data_coverage`, `import_progress`, and
`stat_columns` provide per-season/phase coverage, progress/errors, and metric definitions.
A scan finishing does not establish complete source data: check missing games and unavailable totals.
Local import logs and database backups under `data_backups/` are ignored by Git.
For a new backup or deployment during an active import, use SQLite's backup API to create a
consistent snapshot. A live WAL-mode database must not be distributed by copying only `nba.db`.

## Test

```sh
python -m unittest discover -s backend -p 'test_*.py' -v
```

Tests use a temporary SQLite database, a fake API key, and simulated Gemini HTTP responses.
They do not call Gemini or modify `nba.db`. A live model check requires a working Gemini key,
network access, and available API quota.
