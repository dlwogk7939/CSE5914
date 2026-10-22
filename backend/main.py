import json
import os
import sqlite3
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager, closing
from datetime import date, datetime, timedelta, timezone
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
MAX_STEPS, MAX_QUERIES, MAX_ROWS = 8, 8, 100
QUERY_TIMEOUT, GEMINI_TIMEOUT = 5, 120
TABLES = {"teams", "players", "games", "box_scores", "season_totals"}

# ESPN updater settings
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
REGULAR_SEASON = 2  # ESPN season type: 1 preseason, 2 regular season, 3 playoffs, 5 play-in
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


class Question(BaseModel):
    question: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]


class Answer(BaseModel):
    answer: str


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


def system_prompt():
    with closing(connect()) as db:
        schema = "\n".join(row[0] for row in db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ))
        last_season, last_date = db.execute("SELECT max(season), max(date) FROM games").fetchone()
    return f"""You answer NBA statistics questions using the query_sql tool and this SQLite schema:
{schema}

Use database results for statistical claims. Never invent statistics or treat missing data as zero.
You can make multiple queries. Correct SQL errors and retry. If results are truncated, filter,
order, or aggregate in SQL before making claims about the complete result. Use only one statement
per tool call. Ask the user to clarify an ambiguous player, season, or metric.

Data covers NBA regular seasons 1993-94 through {season_label(last_season)}, with games through
{last_date[:10]} (UTC dates). It does not include playoffs or games still in progress.
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


# ---------------------------------------------------------------------------
# ESPN updater
#
# nba.db was built from ESPN data (game_id, player_id and team_id are ESPN IDs),
# so new ESPN games line up with the existing rows. Each run only:
#   1. Asks ESPN's scoreboard, one game day at a time, for completed regular-season
#      games from 2 days before the newest game in nba.db up to today (a few requests).
#   2. Downloads box scores only for games that are not in nba.db yet.
#   3. Recalculates season_totals only for players who appeared in those games.
# Runs automatically every UPDATE_INTERVAL_HOURS, or on demand with POST /update.
# ---------------------------------------------------------------------------

update_lock = threading.Lock()
update_status = {"running": False, "last_run": None, "last_result": None}

TOTAL_COLUMNS = ["points", "rebounds", "assists", "steals", "blocks", "turnovers", "fouls",
                 "fgm", "fga", "fg3m", "fg3a", "ftm", "fta"]
AVERAGE_COLUMNS = {"ppg": "points", "rpg": "rebounds", "apg": "assists", "spg": "steals",
                   "bpg": "blocks", "tpg": "turnovers", "mpg": "minutes"}


def parse_int(value):
    try:
        return int(str(value).replace("+", ""))
    except (TypeError, ValueError):
        return None  # ESPN uses "--" for missing values


def parse_made_attempted(value):
    made, _, attempted = str(value).partition("-")
    return parse_int(made), parse_int(attempted)


def espn_get(client, path, params):
    response = client.get(f"{ESPN_BASE}/{path}", params=params)
    response.raise_for_status()
    return response.json()


def scoreboard(client, day):
    return espn_get(client, "scoreboard", {"dates": f"{day:%Y%m%d}"})


def find_new_games(client, db):
    """Completed regular-season ESPN games that nba.db does not have yet, oldest first."""
    latest = db.execute("SELECT max(date) FROM games").fetchone()[0]
    if latest:
        start = (datetime.fromisoformat(latest.replace("Z", "+00:00")) - timedelta(days=2)).date()
    else:
        start = date(1993, 11, 1)
    end = datetime.now(timezone.utc).date()
    known = {row[0] for row in db.execute("SELECT game_id FROM games")}

    # Each scoreboard response lists every day with games in that season ("calendar"), so we
    # only request days that actually had games instead of every date (skips the offseason).
    boards = {day: scoreboard(client, day) for day in {start, end}}
    game_days = set()
    for board in boards.values():
        for league in board.get("leagues", []):
            for entry in league.get("calendar", []):
                if isinstance(entry, str):
                    game_days.add(entry[:10])
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    if game_days:  # if ESPN ever drops the calendar, fall back to checking every day
        days = [day for day in days if day.isoformat() in game_days or day in boards]

    new_games = {}
    for day in days:
        board = boards.get(day) or scoreboard(client, day)
        for event in board.get("events", []):
            if (event.get("season", {}).get("type") == REGULAR_SEASON
                    and event.get("status", {}).get("type", {}).get("completed")
                    and int(event["id"]) not in known):
                new_games[event["id"]] = event
    return sorted(new_games.values(), key=lambda event: event["date"])


def box_score_rows(summary):
    """Turn an ESPN game summary into (player, box score) rows. Skips players who did not play."""
    rows = []
    for team_block in summary.get("boxscore", {}).get("players", []):
        team_id = int(team_block["team"]["id"])
        for group in team_block.get("statistics", [])[:1]:
            keys = group.get("keys") or []
            for entry in group.get("athletes", []):
                if entry.get("didNotPlay") or not entry.get("stats"):
                    continue
                stats = dict(zip(keys, entry["stats"]))
                athlete = entry["athlete"]
                fgm, fga = parse_made_attempted(stats.get("fieldGoalsMade-fieldGoalsAttempted"))
                fg3m, fg3a = parse_made_attempted(stats.get("threePointFieldGoalsMade-threePointFieldGoalsAttempted"))
                ftm, fta = parse_made_attempted(stats.get("freeThrowsMade-freeThrowsAttempted"))
                rows.append({
                    "player_id": int(athlete["id"]),
                    "full_name": athlete.get("displayName"),
                    "position": (athlete.get("position") or {}).get("abbreviation"),
                    "team_id": team_id,
                    "minutes": parse_int(stats.get("minutes")),
                    "points": parse_int(stats.get("points")),
                    "rebounds": parse_int(stats.get("rebounds")),
                    "assists": parse_int(stats.get("assists")),
                    "steals": parse_int(stats.get("steals")),
                    "blocks": parse_int(stats.get("blocks")),
                    "turnovers": parse_int(stats.get("turnovers")),
                    "fouls": parse_int(stats.get("fouls")),
                    "plus_minus": parse_int(stats.get("plusMinus")),
                    "fgm": fgm, "fga": fga, "fg3m": fg3m, "fg3a": fg3a, "ftm": ftm, "fta": fta,
                })
    return rows


def add_game(db, client, event):
    """Insert one new game with its box score. Returns the player IDs that appeared, or an
    empty set if ESPN has no box score yet (the game is then retried on the next run)."""
    rows = box_score_rows(espn_get(client, "summary", {"event": event["id"]}))
    if not rows:
        return set()
    teams = {c["homeAway"]: c["team"] for c in event["competitions"][0]["competitors"]}
    with db:  # one transaction per game: either all of it is saved or none of it
        for team in teams.values():
            db.execute("INSERT OR IGNORE INTO teams (team_id, name, abbreviation) VALUES (?, ?, ?)",
                       (int(team["id"]), team.get("displayName"), team.get("abbreviation")))
        db.execute("INSERT INTO games (game_id, date, season, home_team_id, away_team_id) VALUES (?, ?, ?, ?, ?)",
                   (int(event["id"]), event["date"], int(event["season"]["year"]),
                    int(teams["home"]["id"]), int(teams["away"]["id"])))
        for row in rows:
            db.execute(
                """INSERT INTO players (player_id, full_name, position, team_id) VALUES (?, ?, ?, ?)
                   ON CONFLICT(player_id) DO UPDATE SET
                     full_name = coalesce(excluded.full_name, full_name),
                     position = coalesce(excluded.position, position),
                     team_id = excluded.team_id""",
                (row["player_id"], row["full_name"], row["position"], row["team_id"]))
            db.execute(
                """INSERT OR IGNORE INTO box_scores (game_id, player_id, team_id, minutes, points, rebounds,
                     assists, steals, blocks, turnovers, fouls, plus_minus, fgm, fga, fg3m, fg3a, ftm, fta)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (int(event["id"]), row["player_id"], row["team_id"], row["minutes"], row["points"],
                 row["rebounds"], row["assists"], row["steals"], row["blocks"], row["turnovers"],
                 row["fouls"], row["plus_minus"], row["fgm"], row["fga"], row["fg3m"], row["fg3a"],
                 row["ftm"], row["fta"]))
    return {row["player_id"] for row in rows}


def update_season_totals(db, season, player_ids):
    """Recalculate season_totals from box_scores, only for the given players in one season."""
    ids = sorted(player_ids)
    sums = ", ".join(f"sum(b.{c})" for c in TOTAL_COLUMNS)
    averages = ", ".join(f"1.0 * sum(b.{c}) / count(*)" for c in AVERAGE_COLUMNS.values())
    columns = TOTAL_COLUMNS + list(AVERAGE_COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in ["games_played"] + columns)
    with db:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            db.execute(
                f"""INSERT INTO season_totals (player_id, season, games_played, {", ".join(columns)})
                    SELECT b.player_id, g.season, count(*), {sums}, {averages}
                    FROM box_scores b JOIN games g ON g.game_id = b.game_id
                    WHERE g.season = ? AND b.player_id IN ({", ".join("?" * len(chunk))})
                    GROUP BY b.player_id
                    ON CONFLICT(player_id, season) DO UPDATE SET {updates}""",
                [season, *chunk])


def run_update():
    """Add any new games from ESPN. Safe to call repeatedly: it skips games already saved."""
    if not update_lock.acquire(blocking=False):
        return {"status": "already running"}
    update_status["running"] = True
    started = time.monotonic()
    result = {"status": "ok", "games_added": 0, "players_updated": 0, "errors": []}
    try:
        touched = defaultdict(set)
        with closing(sqlite3.connect(DB_PATH, timeout=30)) as db, httpx.Client(timeout=30) as client:
            for event in find_new_games(client, db):
                try:
                    players = add_game(db, client, event)
                except (httpx.HTTPError, sqlite3.Error, KeyError, TypeError, ValueError) as error:
                    result["errors"].append(f"game {event.get('id')}: {error}")
                    continue
                if players:
                    result["games_added"] += 1
                    touched[int(event["season"]["year"])].update(players)
                time.sleep(0.25)  # be polite to ESPN
            for season, players in touched.items():
                update_season_totals(db, season, players)
                result["players_updated"] += len(players)
    except (httpx.HTTPError, sqlite3.Error, KeyError, TypeError, ValueError) as error:
        result.update(status="error", error=str(error))
    finally:
        result["seconds"] = round(time.monotonic() - started, 1)
        update_status.update(running=False, last_run=datetime.now(timezone.utc).isoformat(), last_result=result)
        update_lock.release()
    print(f"ESPN update: {result}")
    return result


def update_loop(stop):
    """The 6-hour schedule: update now, wait UPDATE_INTERVAL_HOURS, repeat until shutdown."""
    while not stop.is_set():
        run_update()
        stop.wait(UPDATE_INTERVAL_HOURS * 3600)


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
    except sqlite3.Error as error:
        raise HTTPException(503, f"Cannot read nba.db at {DB_PATH}: {error}") from error
    return {
        "status": "ok",
        "database": str(DB_PATH),
        "tables": sorted(found & TABLES),
        "missing_tables": sorted(TABLES - found),
        "latest_game": last_date,
    }


@app.get("/update/status")
def get_update_status():
    return update_status


@app.post("/update")
def trigger_update():
    return run_update()


@app.post("/ask", response_model=Answer)
def ask(request: Question):
    api_key = os.environ.get("API_KEY", "").strip()
    if not api_key:
        raise HTTPException(503, "Set API_KEY in the repository's .env file to your Gemini API key.")
    model = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")
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
                    return {"answer": answer}
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