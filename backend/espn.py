"""Resumable ESPN imports into the existing SQLite database; no model calls."""
import argparse
import fcntl
import json
import math
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

DB_PATH = Path(__file__).resolve().parents[1] / "nba.db"
SITE = "https://site.api.espn.com/apis/site/v2/sports"
CORE = "https://sports.core.api.espn.com/v2/sports"
SCOREBOARD_LIMIT = 500
LEAGUES = {
    "nba": ("basketball", "NBA"),
    "wnba": ("basketball", "WNBA"),
    "mens-college-basketball": ("basketball", "Men's college basketball"),
    "womens-college-basketball": ("basketball", "Women's college basketball"),
    "nfl": ("football", "NFL"),
    "college-football": ("football", "College football"),
    "mlb": ("baseball", "MLB"),
    "nhl": ("hockey", "NHL"),
    "eng.1": ("soccer", "English Premier League"),
    "usa.1": ("soccer", "MLS"),
    "esp.1": ("soccer", "La Liga"),
    "ger.1": ("soccer", "Bundesliga"),
    "ita.1": ("soccer", "Serie A"),
    "fra.1": ("soccer", "Ligue 1"),
    "pga": ("golf", "PGA Tour"),
    "atp": ("tennis", "ATP tennis and mixed doubles"),
    "wta": ("tennis", "WTA tennis"),
    "ufc": ("mma", "UFC"),
    "f1": ("racing", "Formula 1"),
}
TEAM_SPORTS = {"basketball", "football", "baseball", "hockey", "soccer"}
SPORTS = sorted({sport for sport, _ in LEAGUES.values()})
SUFFIXES = ("box_scores", "team_box_scores", "season_totals", "team_season_totals")
DATA_TABLES = {f"{sport}_{suffix}" for sport in SPORTS for suffix in SUFFIXES} | {
    "leagues", "league_teams", "league_players", "league_games", "stat_columns", "import_progress", "data_coverage",
}


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if str(value).endswith("s") and str(value)[:-1].isupper():
        return str(value).lower()
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(value))
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", value)
    value = re.sub(r"[^a-zA-Z0-9_]", "_", value).lower().strip("_")
    if not value or value[0].isdigit():
        value = "stat_" + value
    return value


def quote(value):
    return '"' + value.replace('"', '""') + '"'


def numeric(value):
    if value is None or str(value).strip() in ("", "--", "-", "N/A"):
        return None
    try:
        result = float(str(value).replace(",", "").rstrip("%"))
        return (int(result) if result.is_integer() else result) if math.isfinite(result) else None
    except ValueError:
        return str(value)


def metrics(entries):
    """Keep missing values NULL and split source-defined made/attempted pairs."""
    values, definitions = {}, {}
    for entry in entries:
        name = entry["name"]
        raw = entry.get("value", entry.get("displayValue"))
        pairs = [(name, raw)]
        for separator in ("/", "-"):
            names = name.split(separator)
            parts = str(raw).split(separator)
            if len(names) == len(parts) == 2 and all(isinstance(numeric(p), (int, float)) for p in parts):
                pairs = list(zip(names, parts))
                break
        if name == "fullInnings.partInnings" and re.fullmatch(r"\d+(?:\.[012])?", str(raw)):
            innings, _, outs = str(raw).partition(".")
            pairs = [("outsRecorded", int(innings) * 3 + int(outs or 0))]
        for key, value in pairs:
            column = identifier(key)
            values[column] = numeric(value)
            definitions[column] = (entry.get("displayName") or entry.get("label") or key,
                                   entry.get("description", ""), key)
            if isinstance(value, str) and re.fullmatch(r"\d+:\d{2}", value):
                minutes, seconds = map(int, value.split(":"))
                values[column + "_seconds"] = minutes * 60 + seconds
                definitions[column + "_seconds"] = (key + " in seconds", "Duration converted from M:SS", key)
    return values, definitions


def stat_groups(entries):
    """Team feeds may contain flat stats or named groups of stats."""
    flat = [entry for entry in entries if "stats" not in entry and "name" in entry]
    if flat:
        yield "total", flat
    for entry in entries:
        if isinstance(entry.get("stats"), list):
            yield entry.get("name", "total"), entry["stats"]


def grouped_metrics(data, namespace=True):
    values, definitions = {}, {}
    for group in data.get("splits", {}).get("categories", []):
        fields, info = metrics(group.get("stats", []))
        prefix = identifier(group.get("name", "total"))
        for field, value in fields.items():
            column = (field if not namespace or field == prefix or field.startswith(prefix + "_") else
                      prefix if prefix.endswith("_" + field) else prefix + "_" + field)
            values[column], definitions[column] = value, info[field]
    return values, definitions


def individual_competitions(league, event):
    if LEAGUES[league][0] != "tennis":
        return event.get("competitions", [])
    prefix = "mens-" if league == "atp" else "womens-"
    return [competition for group in event.get("groupings", [])
            if group.get("grouping", {}).get("slug", "").startswith(prefix)
            or (league == "atp" and group.get("grouping", {}).get("slug") == "mixed-doubles")
            for competition in group.get("competitions", [])]


def player_id(player, fallback=None):
    if player.get("id"):
        return str(player["id"])
    for link in player.get("links", []):
        match = re.search(r"/id/(\d+)(?:/|$)", link.get("href", ""))
        if match:
            return match[1]
    return fallback


def create_schema(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS leagues (
            league_id TEXT PRIMARY KEY, sport TEXT NOT NULL, name TEXT NOT NULL,
            current_season INTEGER, first_season INTEGER, last_season INTEGER,
            include_postseason INTEGER, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS league_teams (
            league_id TEXT, team_id TEXT, name TEXT, abbreviation TEXT,
            aliases TEXT, source_date TEXT,
            PRIMARY KEY (league_id, team_id)
        );
        CREATE TABLE IF NOT EXISTS league_players (
            league_id TEXT, player_id TEXT, full_name TEXT, position TEXT,
            aliases TEXT, source_date TEXT,
            PRIMARY KEY (league_id, player_id)
        );
        CREATE TABLE IF NOT EXISTS league_games (
            league_id TEXT, game_id TEXT, date TEXT, season INTEGER, season_type INTEGER,
            season_label TEXT, phase_name TEXT, home_team_id TEXT, away_team_id TEXT,
            home_score NUMERIC, away_score NUMERIC, stats_status TEXT NOT NULL DEFAULT 'pending',
            stats_error TEXT, updated_at TEXT, PRIMARY KEY (league_id, game_id)
        );
        CREATE INDEX IF NOT EXISTS league_games_season ON league_games (league_id, season, season_type, date);
        CREATE TABLE IF NOT EXISTS stat_columns (
            table_name TEXT, column_name TEXT, label TEXT, description TEXT, source_name TEXT,
            PRIMARY KEY (table_name, column_name)
        );
        CREATE TABLE IF NOT EXISTS import_progress (
            league_id TEXT, season INTEGER, step TEXT, status TEXT, records INTEGER,
            message TEXT, updated_at TEXT, PRIMARY KEY (league_id, season, step)
        );
    """)
    additions = {"leagues": {"discovery_group": "INTEGER", "scoreboard_limit": "INTEGER"}, "league_games": {
        "event_id": "TEXT", "event_name": "TEXT", "competition_type": "TEXT", "round_name": "TEXT",
        "home_player_id": "TEXT", "away_player_id": "TEXT", "participant_count": "INTEGER"}}
    for table, fields in additions.items():
        present = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for field, kind in fields.items():
            if field not in present:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {field} {kind}")
    for sport in SPORTS:
        for suffix in SUFFIXES:
            entity = "team_id" if suffix.startswith("team_") else "player_id"
            if suffix.endswith("season_totals"):
                fields = f"league_id TEXT, season INTEGER, season_type INTEGER, {entity} TEXT"
                keys = f"league_id, season, season_type, {entity}"
            else:
                fields = "league_id TEXT, game_id TEXT, team_id TEXT, category TEXT"
                keys = "league_id, game_id, team_id, category"
                if entity == "player_id":
                    fields += ", player_id TEXT"
                    keys += ", player_id"
            db.execute(f"CREATE TABLE IF NOT EXISTS {sport}_{suffix} ({fields}, PRIMARY KEY ({keys}))")
            period = "season,season_type" if suffix.endswith("season_totals") else "game_id"
            db.execute(f"CREATE INDEX IF NOT EXISTS {sport}_{suffix}_entity ON {sport}_{suffix} (league_id,{entity},{period})")
    db.execute("""CREATE VIEW IF NOT EXISTS data_coverage AS
        SELECT l.league_id,l.name,l.sport,g.season,g.season_label,g.season_type,g.phase_name,
            count(*) AS games,sum(g.stats_status='complete') AS complete_games,
            sum(g.stats_status='legacy') AS legacy_games,
            sum(g.stats_status NOT IN ('complete','legacy')) AS missing_games,
            min(g.date) AS first_game,max(g.date) AS last_game,
            coalesce((SELECT p.status FROM import_progress p WHERE p.league_id=g.league_id
                AND p.season=g.season AND p.step='schedule'),'not_scanned') AS schedule_status
        FROM league_games g JOIN leagues l USING (league_id)
        GROUP BY l.league_id,g.season,g.season_type""")
    db.commit()


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        create_schema(self.db)
        self.columns = {}

    def upsert(self, table, keys, values, definitions=None):
        if table not in self.columns:
            self.columns[table] = {row[1] for row in self.db.execute(f"PRAGMA table_info({quote(table)})")}
        columns = self.columns[table]
        values = {key: value for key, value in values.items() if key not in keys}
        for column in values:
            if column not in columns:
                self.db.execute(f"ALTER TABLE {quote(table)} ADD COLUMN {quote(column)} NUMERIC")
                columns.add(column)
        for column, info in (definitions or {}).items():
            self.db.execute("INSERT OR IGNORE INTO stat_columns VALUES (?, ?, ?, ?, ?)", (table, column, *info))
        row = {**keys, **values}
        fields = ", ".join(map(quote, row))
        updates = ", ".join(f"{quote(key)}=excluded.{quote(key)}" for key in values)
        conflict = "DO UPDATE SET " + updates if updates else "DO NOTHING"
        self.db.execute(f"INSERT INTO {quote(table)} ({fields}) VALUES ({','.join('?' for _ in row)}) "
                        f"ON CONFLICT ({','.join(map(quote, keys))}) {conflict}", tuple(row.values()))

    def entity(self, table, keys, name_field, values, source_date):
        where = " AND ".join(f"{quote(key)}=?" for key in keys)
        old = self.db.execute(f"SELECT {name_field},aliases,source_date FROM {table} WHERE {where}", tuple(keys.values())).fetchone()
        names = {values.get(name_field)}
        if old:
            names.update((old[0], *(old[1] or "").split(" | ")))
        values = {key: value for key, value in values.items() if value is not None}
        if old and old[2] and source_date and old[2] > source_date:
            values = {}
        elif source_date:
            values["source_date"] = source_date
        values["aliases"] = " | ".join(sorted(name for name in names if name))
        self.upsert(table, keys, values)

    def team(self, league, team, source_date=None):
        if team.get("id"):
            self.entity("league_teams", {"league_id": league, "team_id": str(team["id"])}, "name",
                        {"name": team.get("displayName") or team.get("name"), "abbreviation": team.get("abbreviation")}, source_date)

    def player(self, league, player, source_date=None):
        if player.get("id"):
            self.entity("league_players", {"league_id": league, "player_id": str(player["id"])}, "full_name",
                        {"full_name": player.get("displayName") or player.get("fullName"),
                         "position": (player.get("position") or {}).get("abbreviation")}, source_date)

    def progress(self, league, season, step, status, records=0, message=None):
        self.upsert("import_progress", {"league_id": league, "season": season, "step": step},
                    {"status": status, "records": records, "message": message, "updated_at": now()})
        self.db.commit()

    def save_game(self, league, event, phase, label):
        competitors = event["competitions"][0]["competitors"]
        teams = {c["homeAway"]: c for c in competitors}
        for competitor in competitors:
            self.team(league, competitor["team"], event["date"])
        self.upsert("league_games", {"league_id": league, "game_id": str(event["id"])}, {
            "date": event["date"], "season": int(event["season"]["year"]),
            "season_type": int(phase["id"]), "season_label": label, "phase_name": phase["name"],
            "home_team_id": str(teams["home"]["team"]["id"]), "away_team_id": str(teams["away"]["team"]["id"]),
            "home_score": numeric(teams["home"].get("score")), "away_score": numeric(teams["away"].get("score")),
            "updated_at": now(),
        })

    def save_summary(self, league, event_id, summary):
        sport = LEAGUES[league][0]
        player_count, team_count = 0, 0
        header = summary.get("header", {})
        game = self.db.execute("SELECT season,date,stats_status FROM league_games WHERE league_id=? AND game_id=?", (league, event_id)).fetchone()
        source_date = game[1] if game else None
        if str(header.get("id", event_id)) != str(event_id) or (header.get("season", {}).get("year") and game
                and int(header["season"]["year"]) != game[0]):
            raise ValueError("Summary returned a different game or season")
        with self.db:
            for suffix in ("box_scores", "team_box_scores"):
                self.db.execute(f"DELETE FROM {sport}_{suffix} WHERE league_id=? AND game_id=?", (league, event_id))
            for block in summary.get("boxscore", {}).get("teams", []):
                self.team(league, block["team"], source_date)
                for category, entries in stat_groups(block.get("statistics", [])):
                    values, definitions = metrics(entries)
                    if values:
                        self.upsert(f"{sport}_team_box_scores", {"league_id": league, "game_id": event_id,
                            "team_id": str(block["team"]["id"]), "category": category}, values, definitions)
                        team_count += 1
            for block in summary.get("boxscore", {}).get("players", []):
                self.team(league, block["team"], source_date)
                for group in block.get("statistics", []):
                    category = group.get("type") or group.get("name") or "total"
                    if sport == "hockey" and category in ("forwards", "defenses"):
                        category = "skaters"
                    for entry in group.get("athletes", []):
                        if entry.get("didNotPlay") or not entry.get("stats"):
                            continue
                        player = entry.get("athlete", {})
                        if not player.get("id"):
                            continue
                        self.player(league, player, source_date)
                        fields = [{"name": key, "value": value} for key, value in zip(group.get("keys", []), entry["stats"])]
                        values, definitions = metrics(fields)
                        if values:
                            self.upsert(f"{sport}_box_scores", {"league_id": league, "game_id": event_id,
                                "team_id": str(block["team"]["id"]), "category": category,
                                "player_id": str(player["id"])}, values, definitions)
                            player_count += 1
            for block in summary.get("rosters", []) if sport == "soccer" else []:
                self.team(league, block["team"], source_date)
                for entry in block.get("roster", []):
                    self.player(league, entry["athlete"], source_date)
                    entries = entry.get("stats", [])
                    appearances = next((s.get("value") for s in entries if s.get("name") == "appearances"), None)
                    if not entries or appearances == 0:
                        continue
                    values, definitions = metrics(entries)
                    self.upsert(f"{sport}_box_scores", {"league_id": league, "game_id": event_id,
                        "team_id": str(block["team"]["id"]), "category": "total",
                        "player_id": str(entry["athlete"]["id"])}, values, definitions)
                    player_count += 1
            player_teams = self.db.execute(f"SELECT count(DISTINCT team_id) FROM {sport}_box_scores WHERE league_id=? AND game_id=?", (league, event_id)).fetchone()[0]
            team_teams = self.db.execute(f"SELECT count(DISTINCT team_id) FROM {sport}_team_box_scores WHERE league_id=? AND game_id=?", (league, event_id)).fetchone()[0]
            status = "complete" if player_teams == team_teams == 2 else "missing_stats"
            if status != "complete" and game and game[2] == "complete":
                self.columns.clear()
                raise ValueError("Incomplete source refresh; previous box score retained")
            self.db.execute("UPDATE league_games SET stats_status=?, stats_error=?, updated_at=? WHERE league_id=? AND game_id=?",
                            (status, None if status == "complete" else f"player rows={player_count}, team rows={team_count}", now(), league, event_id))
        return player_count, team_count

    def save_individual(self, league, event, competition, phase, label):
        sport = LEAGUES[league][0]
        game_id = str(competition["id"])
        when = competition.get("date") or event["date"]
        category = identifier((competition.get("type") or {}).get("slug") or
                              (competition.get("type") or {}).get("abbreviation") or "total")
        competitors = competition.get("competitors", [])
        round_info = competition.get("round", {})
        round_name = (round_info.get("displayName") or round_info.get("text") or str(round_info.get("number", ""))) if isinstance(round_info, dict) else str(round_info)
        values = {"date": when, "season": int(event["season"]["year"]), "season_type": int(phase["id"]),
                  "season_label": label, "phase_name": phase["name"], "event_id": str(event["id"]),
                  "event_name": event.get("name"), "competition_type": category, "round_name": round_name,
                  "participant_count": len(competitors), "stats_status": "pending", "updated_at": now()}
        for competitor in competitors:
            side = competitor.get("homeAway")
            if side in ("home", "away"):
                key = side + ("_team_id" if competitor.get("type") == "team" else "_player_id")
                values[key] = str(competitor["id"])
        self.upsert("league_games", {"league_id": league, "game_id": game_id}, values)
        for competitor in competitors:
            team_id = str(competitor["id"]) if competitor.get("type") == "team" else ""
            members = competitor.get("roster", {}).get("athletes", []) if team_id else [competitor.get("athlete", {})]
            if team_id:
                self.team(league, {"id": team_id, "displayName": competitor.get("roster", {}).get("displayName")}, when)
            winner = competitor.get("winner")
            result = "win" if winner is True else "loss" if any(other.get("winner") is True for other in competitors) else None
            stats = {"winner": int(winner) if isinstance(winner, bool) else None, "result": result,
                     "display_order": competitor.get("order"),
                     "result_status": competition.get("status", {}).get("type", {}).get("description"),
                     "status_name": competition.get("status", {}).get("type", {}).get("name"),
                     "detailed_stats_available": None}
            if "score" in competitor:
                stats["score_to_par" if sport == "golf" else "score"] = 0 if sport == "golf" and competitor["score"] == "E" else numeric(competitor["score"])
            for index, line in enumerate(competitor.get("linescores", []), 1):
                if sport in ("golf", "tennis"):
                    field = f"round_{index}_strokes" if sport == "golf" else f"set_{index}_games"
                    stats[field] = numeric(line.get("value"))
            if sport == "golf":
                rounds = [v for k, v in stats.items() if k.endswith("_strokes") and isinstance(v, (int, float))]
                stats.update(recorded_strokes=sum(rounds) if rounds else None, rounds_recorded=len(rounds))
            if sport == "mma":
                status = competition.get("status", {})
                stats.update(finish_round=status.get("period"), finish_clock=status.get("displayClock"),
                             finish_clock_seconds=numeric(status.get("clock")))
            definitions = {field: (field, "ESPN scoreboard/scorecard value", field) for field in stats}
            if team_id:
                self.upsert(f"{sport}_team_box_scores", {"league_id": league, "game_id": game_id,
                    "team_id": team_id, "category": category}, stats, definitions)
            for member in members:
                key = player_id(member, str(competitor["id"]) if not team_id else None)
                if not key:
                    continue
                self.player(league, {**member, "id": key}, when)
                self.upsert(f"{sport}_box_scores", {"league_id": league, "game_id": game_id,
                    "team_id": team_id, "category": category, "player_id": key}, stats, definitions)
        self.db.commit()
        return game_id

    def mark_individual(self, league, game_id, error=None):
        self.db.execute("UPDATE league_games SET stats_status=?,stats_error=?,updated_at=? WHERE league_id=? AND game_id=?",
                        ("missing_stats" if error else "complete", str(error)[:1000] if error else None, now(), league, game_id))
        self.db.commit()


def seed_nba(store):
    """Register the untouched legacy NBA data; new imports use the sport tables."""
    db = store.db
    if not {"teams", "players", "games", "box_scores", "season_totals"} <= {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        return
    if db.execute("SELECT 1 FROM import_progress WHERE league_id='nba' AND step='legacy'").fetchone():
        return
    with db:
        db.execute("INSERT OR IGNORE INTO leagues (league_id,sport,name) VALUES ('nba','basketball','NBA')")
        db.execute("""INSERT OR IGNORE INTO league_teams (league_id,team_id,name,abbreviation,aliases,source_date)
            SELECT 'nba',CAST(team_id AS TEXT),name,abbreviation,name,(SELECT max(date) FROM games) FROM teams""")
        db.execute("""INSERT OR IGNORE INTO league_players (league_id,player_id,full_name,position,aliases,source_date)
            SELECT 'nba',CAST(player_id AS TEXT),full_name,position,full_name,(SELECT max(date) FROM games) FROM players""")
        db.execute("""INSERT OR IGNORE INTO league_games
            (league_id,game_id,date,season,season_type,season_label,phase_name,home_team_id,away_team_id,stats_status)
            SELECT 'nba',CAST(game_id AS TEXT),date,season,2,
                printf('%d-%02d',season-1,season%100),'Regular Season',CAST(home_team_id AS TEXT),
                CAST(away_team_id AS TEXT),'legacy' FROM games""")
    store.progress("nba", 0, "legacy", "complete", db.execute("SELECT count(*) FROM games").fetchone()[0])


class Feed:
    def __init__(self, rate=6):
        self.client = httpx.Client(timeout=30, trust_env=False, follow_redirects=True)
        self.rate, self.next_request = rate, 0
        self.lock = threading.Lock()

    def get(self, url, params=None):
        url = url.replace("http://", "https://", 1)
        for attempt in range(4):
            with self.lock:
                time.sleep(max(0, self.next_request - time.monotonic()))
                self.next_request = time.monotonic() + 1 / self.rate
            try:
                response = self.client.get(url, params=params)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as error:
                status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
                if attempt == 3 or (status and status < 500 and status != 429):
                    raise
                time.sleep(min(30, 2 ** (attempt + 1)))

    def result(self, item):
        key, url = item
        try:
            return key, self.get(url), None
        except (httpx.HTTPError, ValueError) as error:
            return key, None, str(error)


class Importer:
    def __init__(self, path=DB_PATH, workers=6, rate=6):
        self.store = Store(path)
        self.feed = Feed(rate)
        self.pool = ThreadPoolExecutor(max_workers=workers)
        self.metadata = {}

    def close(self):
        self.pool.shutdown(wait=True)
        self.feed.client.close()
        self.store.db.close()

    def configure(self, league, years, include_current, postseason):
        sport, name = LEAGUES[league]
        board = self.feed.get(f"{SITE}/{sport}/{league}/scoreboard", {"limit": 1})
        current = int(board["leagues"][0]["season"]["year"])
        last = current if include_current else current - 1
        group = 50 if "college-basketball" in league else 90 if league == "college-football" else None
        previous = self.store.db.execute("SELECT include_postseason,discovery_group,scoreboard_limit FROM leagues WHERE league_id=?", (league,)).fetchone()
        if previous and (previous[0] != int(postseason) or previous[1] != group or previous[2] != SCOREBOARD_LIMIT):
            self.store.db.execute("DELETE FROM import_progress WHERE league_id=? AND (step LIKE 'day:%' OR step='schedule')", (league,))
        self.store.upsert("leagues", {"league_id": league}, {"sport": sport, "name": name,
            "current_season": current, "first_season": last - years + 1, "last_season": last,
            "include_postseason": int(postseason), "discovery_group": group,
            "scoreboard_limit": SCOREBOARD_LIMIT, "updated_at": now()})
        self.store.db.commit()
        return last

    def season(self, league, year):
        key = (league, year)
        if key not in self.metadata:
            sport = LEAGUES[league][0]
            data = self.feed.get(f"{CORE}/{sport}/leagues/{league}/seasons/{year}")
            if int(data["year"]) != year:
                raise ValueError("ESPN returned a different season")
            phases = data.get("types", {}).get("items", [])
            if not phases:
                phases = self.feed.get(f"{CORE}/{sport}/leagues/{league}/seasons/{year}/types", {"limit": 100})["items"]
            phases = [phase if "id" in phase else self.feed.get(phase["$ref"]) for phase in phases]
            self.metadata[key] = data, phases
        return self.metadata[key]

    def save_totals(self, league, year, phase_id, entity, entity_id, data, source="ESPN season statistics"):
        for key, marker in (("season", f"/seasons/{year}"), ("seasonType", f"/types/{phase_id}")):
            reference = data.get(key, {}).get("$ref", "").split("?")[0].rstrip("/")
            if reference and not reference.endswith(marker):
                raise ValueError("Season statistics returned a different season or phase")
        values, definitions = grouped_metrics(data)
        if not values:
            raise ValueError("No season statistics in ESPN response")
        suffix = "season_totals" if entity == "player" else "team_season_totals"
        table = f"{LEAGUES[league][0]}_{suffix}"
        self.store.upsert(table, {"league_id": league, "season": year, "season_type": phase_id,
                          entity + "_id": entity_id}, {**values, "totals_source": source, "updated_at": now()}, definitions)
        self.store.db.commit()

    def individual(self, league, event, phase, label):
        sport, db = LEAGUES[league][0], self.store.db
        prepared, jobs, errors = {}, [], {}
        for competition in individual_competitions(league, event):
            if not competition.get("status", {}).get("type", {}).get("completed"):
                continue
            key = str(competition["id"])
            previous = db.execute("SELECT stats_status FROM league_games WHERE league_id=? AND game_id=?", (league, key)).fetchone()
            when = date.fromisoformat((competition.get("date") or event["date"])[:10])
            if previous and previous[0] == "complete" and when < datetime.now(timezone.utc).date() - timedelta(days=3):
                continue
            self.store.save_individual(league, event, competition, phase, label)
            prepared[key] = competition
            base = f"{CORE}/{sport}/leagues/{league}/events/{event['id']}/competitions/{key}"
            if sport == "tennis":
                jobs.append(((key, None), base))
            else:
                jobs.extend(((key, str(c["id"])), f"{base}/competitors/{c['id']}/statistics")
                            for c in competition.get("competitors", []))
        if sport == "tennis":
            stat_jobs = []
            for (key, _), data, error in self.pool.map(self.feed.result, jobs):
                if error:
                    errors[key] = error
                    continue
                for competitor in data.get("competitors", []):
                    reference = competitor.get("statistics", {}).get("$ref")
                    if reference:
                        stat_jobs.append(((key, str(competitor["id"])), reference))
                if not any(job[0][0] == key for job in stat_jobs):
                    for table in ("tennis_box_scores", "tennis_team_box_scores"):
                        if "detailed_stats_available" in self.store.columns.get(table, set()):
                            db.execute(f"UPDATE {table} SET detailed_stats_available=0 WHERE league_id=? AND game_id=?", (league, key))
                    if data.get("statsSource", {}).get("state") != "none":
                        errors[key] = "No detailed stats reference; scorecard retained"
                db.commit()
            jobs = stat_jobs
        for (key, competitor_id), data, error in self.pool.map(self.feed.result, jobs):
            if error:
                errors[key] = error
                continue
            try:
                values, definitions = grouped_metrics(data, namespace=sport == "tennis")
                if not values:
                    raise ValueError("No detailed competitor statistics")
                values["detailed_stats_available"] = 1
                if sport == "golf" and values.get("reg_score", 0) and all(values.get(k) == 0 for k in ("birdies", "eagles", "pars", "bogeys")):
                    unavailable = {"drive_dist_avg", "drive_accuracy_pct", "gir", "gir_poss", "putts_gir_avg",
                        "sand_saves", "sand_saves_poss", "eagles", "birdies", "pars", "bogeys", "doubles",
                        "double_bogeys_and_worse", "triple_bogeys_and_worse", "penalties"}
                    values.update({k: None for k in unavailable if k in values})
                if sport == "racing" and "total_time" in values:
                    values["total_time_ms"] = values.pop("total_time")
                    definitions["total_time_ms"] = ("Race time in milliseconds", "ESPN totalTime, milliseconds", "totalTime")
                if sport == "racing":
                    values.update({k: None for k, v in values.items() if k.endswith("_time_ms") and v == 0})
                table = f"{sport}_box_scores"
                rows = db.execute(f"SELECT player_id,team_id,category FROM {table} WHERE league_id=? AND game_id=? AND (player_id=? OR team_id=?)",
                                  (league, key, competitor_id, competitor_id)).fetchall()
                paired = any(team for _, team, _ in rows)
                if paired:
                    self.store.upsert(f"{sport}_team_box_scores", {"league_id": league, "game_id": key,
                        "team_id": competitor_id, "category": rows[0][2]}, values, definitions)
                else:
                    for player, team, category in rows:
                        self.store.upsert(table, {"league_id": league, "game_id": key, "team_id": team,
                            "category": category, "player_id": player}, values, definitions)
                db.commit()
            except (ValueError, KeyError, TypeError, AttributeError, sqlite3.Error) as exc:
                db.rollback()
                self.store.columns.clear()
                errors[key] = str(exc)
        for key, competition in prepared.items():
            actual = db.execute(f"SELECT count(DISTINCT coalesce(nullif(team_id,''),player_id)) FROM {sport}_box_scores WHERE league_id=? AND game_id=?", (league, key)).fetchone()[0]
            error = errors.get(key) or ("Missing competitor identities" if actual != len(competition.get("competitors", [])) else None)
            self.store.mark_individual(league, key, error)
        return len(prepared), len(errors)

    def racing_totals(self, league, year, phases):
        for phase in phases:
            reference = phase.get("standings", {}).get("$ref")
            if not reference:
                continue
            index = self.feed.get(reference)
            count = 0
            for item in index.get("items", []):
                standings = self.feed.get(item["$ref"])
                for entry in standings.get("standings", []):
                    entity = "player" if "athlete" in entry else "team"
                    profile_ref = entry.get("athlete", entry.get("manufacturer", {})).get("$ref")
                    if not profile_ref:
                        continue
                    profile = self.feed.get(profile_ref)
                    key = str(profile["id"])
                    if entity == "player":
                        self.store.player(league, profile, str(year))
                    else:
                        self.store.team(league, profile, str(year))
                    data = {"splits": {"categories": entry.get("records", [])}}
                    self.save_totals(league, year, int(phase["id"]), entity, key, data,
                                     "ESPN " + standings.get("displayName", "standings"))
                    count += 1
            self.store.progress(league, year, f"standings:{phase['id']}", "complete", count)

    def mma_totals(self, league, year, phases):
        db = self.store.db
        columns = {row[1] for row in db.execute("PRAGMA table_info(mma_box_scores)")}
        if "result" not in columns:
            self.store.progress(league, year, "derived_totals", "partial", 0, "No fight results available")
            return
        additive = sorted(c for c in columns if c.endswith(("_attempted", "_landed")) or c in ("knock_downs", "submission_attempts", "takedowns_slams"))
        selections = ["count(DISTINCT b.game_id)", "sum(b.result='win')", "sum(b.result='loss')", "sum(b.result IS NULL)"]
        selections += [f"sum(b.{quote(c)})" for c in additive]
        names = ["derived_fights", "derived_wins", "derived_losses", "derived_unresolved_results"] + ["derived_" + c for c in additive]
        for phase in phases:
            rows = db.execute(f"SELECT b.player_id,{','.join(selections)} FROM mma_box_scores b JOIN league_games g ON b.league_id=g.league_id AND b.game_id=g.game_id WHERE g.league_id=? AND g.season=? AND g.season_type=? GROUP BY b.player_id", (league, year, int(phase["id"]))).fetchall()
            for row in rows:
                values = dict(zip(names, row[1:]))
                values.update(totals_source="Derived from loaded ESPN fight box scores; check coverage", updated_at=now())
                self.store.upsert("mma_season_totals", {"league_id": league, "season": year,
                    "season_type": int(phase["id"]), "player_id": row[0]}, values)
            db.commit()
            self.store.progress(league, year, f"derived_totals:{phase['id']}", "complete", len(rows))

    def totals(self, league, year, phases, recent_days=None):
        db, sport = self.store.db, LEAGUES[league][0]
        if sport == "racing":
            return self.racing_totals(league, year, phases)
        if sport == "mma":
            return self.mma_totals(league, year, phases)
        current = db.execute("SELECT current_season FROM leagues WHERE league_id=?", (league,)).fetchone()[0]
        for phase in phases:
            phase_id = int(phase["id"])
            for entity in (("player", "team") if sport in TEAM_SPORTS else ("player",)):
                suffix = "season_totals" if entity == "player" else "team_season_totals"
                table = f"{sport}_{suffix}"
                if entity == "player":
                    ids = {row[0] for row in db.execute(f"""SELECT DISTINCT b.player_id FROM {sport}_box_scores b
                        JOIN league_games g ON b.league_id=g.league_id AND b.game_id=g.game_id
                        WHERE g.league_id=? AND g.season=? AND g.season_type=?""", (league, year, phase_id))}
                    if sport == "tennis":
                        prefix = "mens_%" if league == "atp" else "womens_%"
                        ids = {row[0] for row in db.execute(f"SELECT DISTINCT b.player_id FROM tennis_box_scores b JOIN league_games g ON b.league_id=g.league_id AND b.game_id=g.game_id WHERE g.league_id=? AND g.season=? AND g.season_type=? AND b.category LIKE ?", (league, year, phase_id, prefix))}
                else:
                    ids = {team for row in db.execute("SELECT home_team_id,away_team_id FROM league_games WHERE league_id=? AND season=? AND season_type=?", (league, year, phase_id)) for team in row if team}
                existing = {row[0] for row in db.execute(f"SELECT {entity}_id FROM {table} WHERE league_id=? AND season=? AND season_type=?", (league, year, phase_id))}
                if recent_days:
                    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=recent_days)).isoformat()
                    if entity == "player":
                        recent = {row[0] for row in db.execute(f"SELECT DISTINCT b.player_id FROM {sport}_box_scores b JOIN league_games g ON b.league_id=g.league_id AND b.game_id=g.game_id WHERE g.league_id=? AND g.season=? AND g.season_type=? AND g.date>=?", (league, year, phase_id, cutoff))}
                    else:
                        recent = {team for row in db.execute("SELECT home_team_id,away_team_id FROM league_games WHERE league_id=? AND season=? AND season_type=? AND date>=?", (league, year, phase_id, cutoff)) for team in row if team}
                    ids = (ids - existing) | (ids & recent)
                elif year != current:
                    ids -= existing
                collection = "athletes" if entity == "player" else "teams"
                urls = [(key, f"{CORE}/{sport}/leagues/{league}/seasons/{year}/types/{phase_id}/{collection}/{key}/statistics") for key in sorted(ids)]
                saved, missing = 0, 0
                for key, data, error in self.pool.map(self.feed.result, urls):
                    try:
                        if error:
                            raise ValueError(error)
                        self.save_totals(league, year, phase_id, entity, key, data)
                        saved += 1
                    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
                        missing += 1
                        if missing <= 3:
                            print(f"{league} {year} {entity} {key}: {exc}", flush=True)
                    if (saved + missing) % 100 == 0:
                        self.store.progress(league, year, f"{entity}_totals:{phase_id}", "running", saved,
                                            f"attempted={saved+missing}/{len(ids)} unavailable={missing}")
                self.store.progress(league, year, f"{entity}_totals:{phase_id}", "partial" if missing else "complete", saved,
                                    f"requested={len(ids)} unavailable={missing}")
                print(f"{league} {year} phase={phase_id}: {entity} totals saved={saved}, unavailable={missing}", flush=True)

    def summaries(self, league, event_ids):
        sport = LEAGUES[league][0]
        jobs = [(key, f"{SITE}/{sport}/{league}/summary?event={key}") for key in event_ids]
        saved, missing = 0, 0
        for key, summary, error in self.pool.map(self.feed.result, jobs):
            try:
                if error:
                    raise ValueError(error)
                players, teams = self.store.save_summary(league, key, summary)
                saved += int(bool(players and teams))
            except (ValueError, KeyError, TypeError, AttributeError, IndexError, sqlite3.Error) as exc:
                self.store.db.rollback()
                self.store.columns.clear()
                self.store.db.execute("UPDATE league_games SET stats_status='error',stats_error=?,updated_at=? WHERE league_id=? AND game_id=?",
                                      (str(exc)[:1000], now(), league, key))
                self.store.db.commit()
                missing += 1
                print(f"{league} game {key}: {exc}", flush=True)
        return saved, missing

    def games(self, league, year, postseason, days=None, latest_available=False):
        sport = LEAGUES[league][0]
        metadata, all_phases = self.season(league, year)
        regular = 1 if sport == "soccer" else 2
        phases = [p for p in all_phases if int(p["id"]) == regular or (postseason and
                  not any(word in p.get("name", "").lower() for word in ("preseason", "pre-season", "off season", "off-season", "all-star", "all star", "pro bowl")))]
        if not phases:
            raise ValueError("No requested season phases available")
        phase_map = {int(key): p for p in phases for key in (p["id"], p.get("type", p["id"]))}
        today = datetime.now(timezone.utc).date()
        start = min(date.fromisoformat(p["startDate"][:10]) for p in phases)
        end = min(today, max(date.fromisoformat(p["endDate"][:10]) for p in phases))
        if days:
            start = max(start, (end if latest_available else today) - timedelta(days=days - 1))
        elif sport in TEAM_SPORTS:
            retry_ids = [row[0] for row in self.store.db.execute("SELECT game_id FROM league_games WHERE league_id=? AND season=? AND stats_status IN ('error','missing_stats')", (league, year))]
            self.summaries(league, retry_ids)
        retry_days = set()
        if sport not in TEAM_SPORTS:
            for row in self.store.db.execute("SELECT date FROM league_games WHERE league_id=? AND season=? AND stats_status IN ('error','missing_stats','pending')", (league, year)):
                retry_day = date.fromisoformat(row[0][:10])
                retry_days.update((retry_day, retry_day - timedelta(days=1)))
        done = {row[0] for row in self.store.db.execute("SELECT step FROM import_progress WHERE league_id=? AND season=? AND status='complete'", (league, year))}
        seen, failures = 0, 0
        day = start
        while day <= end:
            step = "day:" + day.isoformat()
            if step in done and day < today - timedelta(days=3) and day not in retry_days:
                day += timedelta(days=1)
                continue
            params = {"dates": f"{day:%Y%m%d}", "limit": SCOREBOARD_LIMIT}
            if "college-basketball" in league or league == "college-football":
                params["groups"] = 50 if "college-basketball" in league else 90
            try:
                board = self.feed.get(f"{SITE}/{sport}/{league}/scoreboard", params)
                events = board.get("events")
                if not isinstance(events, list):
                    raise ValueError("ESPN response has no event list")
                if len(events) >= SCOREBOARD_LIMIT:
                    raise ValueError("Scoreboard reached its event limit; coverage needs pagination")
                pending, daily_count, individual_saved, individual_errors = [], 0, 0, 0
                for event in events:
                    event_day = date.fromisoformat(event["date"][:10])
                    if sport in TEAM_SPORTS and abs((event_day - day).days) > 2:
                        raise ValueError("Scoreboard returned events outside the requested dates")
                    event_season = event.get("season", {})
                    phase = phase_map.get(int(event_season.get("type", -1)))
                    if int(event_season.get("year", -1)) != year or not phase:
                        continue
                    if sport not in TEAM_SPORTS:
                        saved, missing = self.individual(league, event, phase, metadata.get("displayName", str(year)))
                        individual_saved += saved
                        individual_errors += missing
                        daily_count += saved
                        continue
                    competition = event.get("competitions", [{}])[0]
                    status = event.get("status", competition.get("status", {}))
                    if not status.get("type", {}).get("completed"):
                        continue
                    self.store.save_game(league, event, phase, metadata.get("displayName", str(year)))
                    key = str(event["id"])
                    state = self.store.db.execute("SELECT stats_status FROM league_games WHERE league_id=? AND game_id=?", (league, key)).fetchone()[0]
                    if state != "complete" or day >= today - timedelta(days=3):
                        pending.append(key)
                    daily_count += 1
                self.store.db.commit()
                saved, missing = self.summaries(league, list(dict.fromkeys(pending)))
                saved += individual_saved
                missing += individual_errors
                seen += daily_count
                self.store.progress(league, year, step, "complete", daily_count,
                                    f"summaries_saved={saved} errors={missing}")
                if pending or individual_saved:
                    print(f"{league} {year} {day}: games={daily_count}, fetched={saved}, errors={missing}", flush=True)
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, IndexError, sqlite3.Error) as exc:
                self.store.db.rollback()
                self.store.progress(league, year, step, "error", 0, str(exc)[:1000])
                failures += 1
                print(f"{league} {year} {day}: discovery error: {exc}", flush=True)
            day += timedelta(days=1)
        if not days:
            self.store.progress(league, year, "schedule", "partial" if failures else "complete", seen,
                                f"through={end} failed_days={failures}")
        return phases


def coverage(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='leagues'").fetchone():
        return []
    cursor = db.execute("""SELECT l.league_id,l.name,l.sport,l.first_season,l.last_season,
        count(g.game_id) AS games,coalesce(sum(g.stats_status='complete'),0) AS complete_games,
        coalesce(sum(g.stats_status='legacy'),0) AS legacy_games,
        coalesce(sum(g.stats_status NOT IN ('complete','legacy')),0) AS missing_games,max(g.date) AS latest_game
        FROM leagues l LEFT JOIN league_games g USING (league_id) GROUP BY l.league_id ORDER BY l.sport,l.name""")
    return [dict(zip([column[0] for column in cursor.description], row)) for row in cursor]


def import_running(path=DB_PATH):
    try:
        with open(str(path) + ".import.lock") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except FileNotFoundError:
        pass
    return False


def run_import(path=DB_PATH, leagues=None, years=10, include_current=True, postseason=True, days=None, workers=6, rate=6):
    path = Path(path).resolve()
    with open(str(path) + ".import.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already running"}
        importer = Importer(path, workers, rate)
        errors = []
        try:
            seed_nba(importer.store)
            selected = list(LEAGUES) if leagues is None else leagues
            last_seasons = {}
            for league in selected:
                try:
                    last_seasons[league] = importer.configure(league, years, include_current, postseason)
                except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
                    errors.append(f"{league}: {exc}")
                    print(errors[-1], flush=True)
            # Seed every league first so the app can use multiple sports during the backfill.
            if not days:
                for league, year in last_seasons.items():
                    try:
                        importer.games(league, year, postseason, days=7, latest_available=True)
                    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
                        print(f"{league} seed: {exc}", flush=True)
            for offset in range(1 if days else years):
                for league, last in last_seasons.items():
                    year = last - offset
                    try:
                        print(f"Importing {league} {year}", flush=True)
                        phases = importer.games(league, year, postseason, days)
                        importer.totals(league, year, phases, recent_days=days)
                    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError, IndexError, sqlite3.Error) as exc:
                        importer.store.db.rollback()
                        importer.store.columns.clear()
                        importer.store.progress(league, year, "season", "error", 0, str(exc)[:1000])
                        errors.append(f"{league} {year}: {exc}")
                        print(errors[-1], flush=True)
            gaps = {"missing_game_stats": importer.store.db.execute("SELECT count(*) FROM league_games WHERE stats_status NOT IN ('complete','legacy')").fetchone()[0],
                    "incomplete_steps": importer.store.db.execute("SELECT count(*) FROM import_progress WHERE status IN ('partial','error','running')").fetchone()[0]}
            result = {"status": "finished_with_gaps" if errors or any(gaps.values()) else "finished", "errors": errors,
                      "gaps": gaps, "leagues": coverage(importer.store.db)}
            print(json.dumps(result), flush=True)
            return result
        finally:
            importer.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--leagues", nargs="+", choices=list(LEAGUES))
    parser.add_argument("--years", type=int, default=10)
    parser.add_argument("--days", type=int, help="Refresh only the latest N days of each selected season")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--rate", type=float, default=6, help="Maximum ESPN requests per second across all workers")
    parser.add_argument("--status", action="store_true")
    seasons = parser.add_mutually_exclusive_group()
    seasons.add_argument("--include-current", action="store_true")
    seasons.add_argument("--completed-only", action="store_true")
    phases = parser.add_mutually_exclusive_group()
    phases.add_argument("--postseason", action="store_true")
    phases.add_argument("--regular-only", action="store_true")
    args = parser.parse_args()
    if args.status:
        with closing(sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            print(json.dumps(coverage(db), indent=2))
    elif not (args.include_current or args.completed_only) or not (args.postseason or args.regular_only):
        parser.error("Choose --include-current/--completed-only and --postseason/--regular-only explicitly")
    elif args.years < 1 or args.workers < 1 or args.rate <= 0 or (args.days is not None and args.days < 1):
        parser.error("years, workers, rate and days must be positive")
    else:
        run_import(args.db, args.leagues, args.years, args.include_current, args.postseason, args.days, args.workers, args.rate)
