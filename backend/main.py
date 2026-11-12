import json
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Annotated

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, StringConstraints

from backend import espn

DB_PATH = Path(__file__).resolve().parents[1] / "nba.db"
load_dotenv(DB_PATH.parent / ".env")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODELS = {
    "gemini-3.8-flash": "Gemini 3.8 Flash",
    "gemini-3.5-flash-lite": "Gemini 3.5 Flash-Lite",
    "gemini-3.1-flash-lite": "Gemini 3.1 Flash-Lite"
}
MAX_STEPS, MAX_QUERIES, MAX_ROWS = 20, 20, 100
QUERY_TIMEOUT, GEMINI_TIMEOUT = 5, 120
LEGACY_TABLES = {"teams", "players", "games", "box_scores", "season_totals"}
TABLES = LEGACY_TABLES | espn.DATA_TABLES

# ESPN updater settings
UPDATE_INTERVAL_HOURS = float(os.environ.get("UPDATE_INTERVAL_HOURS", "6"))
AUTO_UPDATE = os.environ.get("AUTO_UPDATE", "1") != "0"


@asynccontextmanager
async def lifespan(app):
    # Runs once when the server starts: launch the ESPN updater in a background thread.
    # Everything after "yield" runs when the server shuts down.
    stop = threading.Event()
    if AUTO_UPDATE:
        threading.Thread(target=update_loop, args=(stop,), daemon=True).start()
    yield
    stop.set()


app = FastAPI(title="AskNBA backend", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


class ConversationTurn(BaseModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    answer: Annotated[str, StringConstraints(max_length=20000)]


class Question(BaseModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    history: list[ConversationTurn] = Field(default_factory=list, max_length=100)
    model: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    league: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None


class GeneratedAnswer(BaseModel):
    answer: str
    suggestions: list[str] = Field(min_length=3, max_length=3)


class Answer(BaseModel):
    answer: str
    suggestions: list[str] = Field(default_factory=list)
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


def season_label(season):
    return f"{season - 1}-{str(season)[2:]}"


def loaded_league(league, datasets):
    item = next((item for item in datasets if item["league_id"] == league), None)
    if not item or not (item["complete_games"] or item["legacy_games"]):
        raise HTTPException(422, "Choose a league with loaded data.")
    return item


def system_prompt(league=None):
    with closing(connect()) as db:
        schema = "\n".join(row[1] for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type IN ('table','view') ORDER BY name"
        ) if row[0] in TABLES and row[1])
        datasets = espn.coverage(db)
        first_season, last_season, last_date = db.execute("SELECT min(season),max(season),max(date) FROM games").fetchone()
    selection = loaded_league(league, datasets) if league else None
    context = (f"Selected league: {selection['name']} (league_id={league}). Use this league for questions "
               "and follow-ups. If a question conflicts with this selection, ask the user to clarify."
               if selection else "No league is selected; use the league named in the question or conversation.")
    response_shape = json.dumps({"answer": "A concise answer in Markdown, naming the league and season.",
                                 "suggestions": ["Related question 1?", "Related question 2?", "Related question 3?"]})
    return f"""You answer sports statistics questions using query_sql and this SQLite schema:
{schema}

{context}

Loaded league summary (partial imports are possible):
{json.dumps(datasets)}
Legacy NBA data covers seasons {first_season} through {last_season}, with games through {last_date}.

Use previous turns for follow-up context; verify statistics with query_sql. When asked about
previous questions, list the actual questions chronologically. Clarify ambiguous players,
leagues, seasons, or metrics. Never invent statistics or treat NULL/missing data as zero.
Filter by league_id and join on BOTH league_id and the entity/game id. ESPN ids are scoped by league.
College coverage uses Division I scoreboards; football includes FBS and FCS. Opponent records
can include other divisions, so do not assume an athlete's division without supporting data.
The original teams/players/games/box_scores/season_totals tables are NBA-only historical data.
Legacy NBA season totals are available independently of the new import checkpoints.
Prefer the sport-specific tables for newly imported data; use legacy NBA tables when needed
for older coverage. Do not combine overlapping legacy and new rows as separate games.

Before league-wide rankings or historical records, query data_coverage and import_progress.
Only claim the coverage actually loaded. A schedule_status of complete means the selected
season dates were scanned through the import timestamp; missing_games still indicates gaps.
Current seasons remain partial. Never describe coverage as all-time without checking its extent.
Use league_games.season_label and phase_name to interpret seasons. NBA/college basketball/NHL
use the season END year; NFL/WNBA/MLB/MLS use calendar years; European soccer uses the START year.
Always distinguish regular seasons, playoffs/play-in, and tournaments. Default to regular-season
stats unless the question requests otherwise. Use season_type and phase_name to filter phases.
Regular-season type is 1 for soccer and 2 for the other configured sports; postseason phases are separate.
Game timestamps are UTC; label or convert dates explicitly rather than implying a local date.

Each sport has player/team box-score and season-total tables. Player box scores may have
multiple category rows per game (e.g. football passing/rushing or baseball batting/pitching).
Filter category when needed; count DISTINCT game_id/player_id rather than counting category rows.
Season-total columns include their source category prefix to prevent collisions. Query
stat_columns for column definitions. Use ESPN season totals for season statistics and rates.
Do not sum percentages, averages, year-to-date fields, or season-to-date values embedded in game
box scores. MLB outs_recorded is an integer count of outs, not decimal innings. Duration columns
ending _seconds are seconds. Other duration fields may be M:SS strings. Check units before converting.

Individual-sport games are matches, fights, tournaments, or race sessions. Use event_name,
competition_type, round_name, and the sport's box scores. Racing total_time_ms is milliseconds;
filter race sessions separately from practice/qualifying. Golf display_order is source display
order, not an authoritative tied finishing position. Golf round_N_strokes are scorecard strokes.
Cuts/withdrawals can have fewer golf rounds; do not rank partial stroke totals as full tournaments.
Tennis atp includes men's and mixed-doubles match cards; wta includes women's matches.
Filter category for singles/doubles/mixed/qualifying. Doubles player rows share the match result
and set scores; pair-level detailed stats belong in team_box_scores. Do not attribute pair aces
to each individual. detailed_stats_available=0 means ESPN supplied scorecards without detailed
match stats. Unavailable golf breakdowns are NULL, not zero. F1 season totals come from ESPN
driver/constructor standings. UFC derived_* season columns aggregate only loaded fights;
check coverage and unresolved results, and describe them as derived rather than official totals.
Current-season source totals may lag recent game results; disclose such a discrepancy.

Legacy NBA caveats: season_totals has an incomplete 1992-93 subset (season=1993), unsuitable
for full-league comparisons. Some box scores were skipped. players.team_id is a last-processed
team, not a current-team field; use box_scores.team_id for historical membership. Legacy NBA
plus_minus is missing through season=2008; those zeroes do not establish actual plus/minus.

Use only one read-only statement per tool call. Correct SQL errors and retry. If a result is
truncated, filter/order/aggregate in SQL before making claims about the whole result.
After answering, suggest three concise follow-up questions related to the answer and selected league.
Use loaded data and coverage to suggest answerable questions. Include player/team names and seasons
where needed, and avoid repeating questions already asked. Do not answer the follow-ups yet.
When coverage is partial, focus on loaded games/events instead of season-wide or all-time rankings.
Return JSON in this format: {response_shape}
The answer string can contain Markdown tables or lists. Do not wrap the JSON in a code block.
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
        if len(sql.encode("utf-8")) > 10000:
            raise ValueError("SQL must not exceed 10,000 bytes.")
        with closing(connect()) as db:
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


update_lock = threading.Lock()
update_status = {"running": False, "last_run": None, "last_result": None}


def run_update():
    if not update_lock.acquire(blocking=False):
        return {"status": "already running"}
    update_status["running"] = True
    try:
        result = espn.run_import(DB_PATH, days=3)
        update_status["last_result"] = result
        return result
    finally:
        update_status.update(running=False, last_run=datetime.now(timezone.utc).isoformat())
        update_lock.release()


def update_loop(stop):
    while not stop.is_set():
        run_update()
        stop.wait(UPDATE_INTERVAL_HOURS * 3600)


@app.get("/coverage")
def data_coverage():
    try:
        with closing(connect()) as db:
            return {"leagues": espn.coverage(db)}
    except sqlite3.Error as error:
        raise HTTPException(503, f"Cannot read database: {error}") from error


@app.get("/")
def home():
    return {"message": "AskNBA backend is running.", "try": ["/health", "/docs", "/update/status"]}


@app.get("/health")
def health():
    try:
        with closing(connect()) as db:
            found = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )}
            last_date = db.execute("SELECT max(date) FROM games").fetchone()[0]
            if "league_games" in found:
                last_date = db.execute("SELECT max(date) FROM league_games").fetchone()[0] or last_date
    except sqlite3.Error as error:
        raise HTTPException(503, f"Cannot read nba.db at {DB_PATH}: {error}") from error
    return {
        "status": "ok",
        "database": str(DB_PATH),
        "tables": sorted(found & TABLES),
        "missing_tables": sorted(LEGACY_TABLES - found),
        "latest_game": last_date,
    }


RECENT_GAMES_SQL = """
WITH recent AS (
    SELECT game_id, date, season, home_team_id, away_team_id
    FROM games
    WHERE game_id IN (SELECT game_id FROM box_scores)
    ORDER BY date DESC, game_id DESC
    LIMIT ?
),
scores AS (
    -- games has no score columns, so each team's score is the sum of its players' points
    SELECT b.game_id, b.team_id, sum(b.points) AS points
    FROM box_scores b JOIN recent r ON r.game_id = b.game_id
    GROUP BY b.game_id, b.team_id
),
leaders AS (
    SELECT b.game_id, p.full_name, t.abbreviation AS team, b.points, b.rebounds, b.assists,
           row_number() OVER (PARTITION BY b.game_id
                              ORDER BY b.points DESC, b.rebounds + b.assists DESC) AS rank
    FROM box_scores b
    JOIN recent r ON r.game_id = b.game_id
    JOIN players p ON p.player_id = b.player_id
    LEFT JOIN teams t ON t.team_id = b.team_id
)
SELECT r.game_id, r.date, r.season,
       ht.name, ht.abbreviation, hs.points,
       at.name, at.abbreviation, aws.points,
       l.full_name, l.team, l.points, l.rebounds, l.assists
FROM recent r
LEFT JOIN teams ht ON ht.team_id = r.home_team_id
LEFT JOIN teams at ON at.team_id = r.away_team_id
LEFT JOIN scores hs ON hs.game_id = r.game_id AND hs.team_id = r.home_team_id
LEFT JOIN scores aws ON aws.game_id = r.game_id AND aws.team_id = r.away_team_id
LEFT JOIN leaders l ON l.game_id = r.game_id AND l.rank = 1
ORDER BY r.date DESC, r.game_id DESC
"""


NBA_RECENT_GAMES_SQL = """
WITH recent AS (
    SELECT * FROM league_games
    WHERE league_id='nba' AND stats_status='complete'
    ORDER BY date DESC, game_id DESC LIMIT ?
),
leaders AS (
    SELECT b.game_id, p.full_name, t.abbreviation AS team, b.points, b.rebounds, b.assists,
           row_number() OVER (PARTITION BY b.game_id
                              ORDER BY b.points DESC, b.rebounds + b.assists DESC) AS rank
    FROM basketball_box_scores b
    JOIN recent r ON r.game_id=b.game_id AND r.league_id=b.league_id
    LEFT JOIN league_players p ON p.league_id=b.league_id AND p.player_id=b.player_id
    LEFT JOIN league_teams t ON t.league_id=b.league_id AND t.team_id=b.team_id
    WHERE b.league_id='nba' AND b.category='total'
)
SELECT CAST(r.game_id AS INTEGER), r.date, r.season,
       ht.name, ht.abbreviation, r.home_score,
       at.name, at.abbreviation, r.away_score,
       l.full_name, l.team, l.points, l.rebounds, l.assists
FROM recent r
LEFT JOIN league_teams ht ON ht.league_id=r.league_id AND ht.team_id=r.home_team_id
LEFT JOIN league_teams at ON at.league_id=r.league_id AND at.team_id=r.away_team_id
LEFT JOIN leaders l ON l.game_id=r.game_id AND l.rank=1
ORDER BY r.date DESC, r.game_id DESC
"""


@app.get("/recent-games")
def recent_games(limit: int = Query(8, ge=1, le=30), league: str | None = None):
    """Loaded results for a league; NBA merges new imports with its historical cards."""
    try:
        with closing(connect()) as db:
            if league and league != "nba":
                info = loaded_league(league, espn.coverage(db))
                sport = info["sport"]
                if sport not in espn.SPORTS:
                    raise HTTPException(422, "Unsupported sport.")
                db.row_factory = sqlite3.Row
                games = [dict(row) for row in db.execute("""SELECT * FROM league_games
                    WHERE league_id=? AND stats_status='complete' ORDER BY date DESC,game_id DESC LIMIT ?""", (league, limit))]
                if sport in espn.TEAM_SPORTS:
                    teams = {row["team_id"]: dict(row) for row in db.execute(
                        "SELECT team_id,name,abbreviation FROM league_teams WHERE league_id=?", (league,))}
                    for game in games:
                        game["kind"] = "team"
                        for side in ("home", "away"):
                            game[side] = {**teams.get(game[side + "_team_id"], {}), "score": game[side + "_score"]}
                else:
                    identity = "coalesce(nullif(b.team_id,''),b.player_id)" if sport == "tennis" else "b.player_id"
                    for game in games:
                        game["kind"] = "match" if sport in ("tennis", "mma") else "event"
                        game["participants"] = [dict(row) for row in db.execute(f"""SELECT {identity} AS id,
                            coalesce(max(t.name),group_concat(DISTINCT p.full_name)) AS name,max(b.winner) AS winner
                            FROM {sport}_box_scores b
                            LEFT JOIN league_players p ON p.league_id=b.league_id AND p.player_id=b.player_id
                            LEFT JOIN league_teams t ON t.league_id=b.league_id AND t.team_id=b.team_id
                            WHERE b.league_id=? AND b.game_id=? AND (?='match' OR b.winner=1) GROUP BY {identity}
                            ORDER BY max(b.winner) DESC,min(b.display_order),id""", (league, game["game_id"], game["kind"]))]
                return {"games": games}
            rows = db.execute(RECENT_GAMES_SQL, (limit,)).fetchall()
            columns = {row[1] for row in db.execute("PRAGMA table_info(basketball_box_scores)")}
            if {"points", "rebounds", "assists"} <= columns:
                rows += db.execute(NBA_RECENT_GAMES_SQL, (limit,)).fetchall()
    except sqlite3.Error as error:
        raise HTTPException(503, f"Cannot read nba.db: {error}") from error
    games = {}
    for (game_id, game_date, season, home_name, home_abbr, home_pts, away_name, away_abbr, away_pts,
         leader_name, leader_team, leader_pts, leader_reb, leader_ast) in rows:
        games[game_id] = {
            "game_id": game_id,
            "date": game_date,
            "season": season,
            "home": {"name": home_name, "abbreviation": home_abbr, "score": home_pts},
            "away": {"name": away_name, "abbreviation": away_abbr, "score": away_pts},
            "top_performer": leader_name and {
                "name": leader_name, "team": leader_team,
                "points": leader_pts, "rebounds": leader_reb, "assists": leader_ast,
            },
        }
    return {"games": sorted(games.values(), key=lambda game: (game["date"], game["game_id"]), reverse=True)[:limit]}


@app.get("/update/status")
def get_update_status():
    return {**update_status, "running": update_status["running"] or espn.import_running(DB_PATH)}


@app.post("/update")
def trigger_update():
    return run_update()


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
            {"role": "system", "content": system_prompt(request.league)},
        ]
        for turn in request.history:
            messages.append({"role": "user", "content": turn.question})
            messages.append({"role": "assistant", "content": turn.answer})
        messages.append({"role": "user", "content": request.question})
        queries = 0
        with httpx.Client(timeout=GEMINI_TIMEOUT, trust_env=False) as client:
            for _ in range(MAX_STEPS):
                response = client.post(GEMINI_URL, headers={"Authorization": f"Bearer {api_key}"}, json={
                    "model": model, "messages": messages, "tools": TOOLS, "stream": False,
                    "response_format": {"type": "json_schema", "json_schema": {
                        "name": "sports_answer", "schema": GeneratedAnswer.model_json_schema(),
                    }},
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
                    suggestions = []
                    if not answer:
                        raise ValueError("The model returned an empty answer.")
                    structured_text = answer.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                    if structured_text.startswith("{"):
                        try:
                            structured = json.loads(structured_text)
                            if not isinstance(structured, dict) or not isinstance(structured.get("answer"), str):
                                raise ValueError("The model returned an invalid answer. Please try again.")
                            answer = structured["answer"].strip()
                            raw = structured.get("suggestions", [])
                            if isinstance(raw, list):
                                raw = [unescape(q).strip() for q in raw if isinstance(q, str)]
                                suggestions = list(dict.fromkeys(q for q in raw if 0 < len(q) <= 4000))[:3]
                        except json.JSONDecodeError:
                            raise ValueError("The model returned an invalid answer. Please try again.") from None
                    answer = unescape(answer).strip()
                    if not answer:
                        raise ValueError("The model returned an empty answer.")
                    return {"answer": answer, "suggestions": suggestions, "model": model, "model_label": selected["label"]}
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
