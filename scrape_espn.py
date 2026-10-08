"""Scrape NBA box scores & season totals from ESPN's API (1992-93 through 2025-26) into nba.db."""
import sqlite3
import time

import requests

SEASONS = range(1994, 2027)  # ESPN season-year: 1994 = 1993-94 season ... 2026 = 2025-26 season
# NOTE: season=1993 is NOT usable here - ESPN's schedule endpoint aliases it to season 1994's
# games (confirmed across multiple teams). 1992 and earlier return only 2-4 sparse games per team
# (no full schedule), so 1993-94 is the earliest season with a complete schedule via this endpoint.
BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/seasons"
DELAY = 0.03
DB_PATH = "nba.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    team_id INTEGER PRIMARY KEY,
    name TEXT,
    abbreviation TEXT
);
CREATE TABLE IF NOT EXISTS players (
    player_id INTEGER PRIMARY KEY,
    full_name TEXT,
    position TEXT,
    team_id INTEGER REFERENCES teams(team_id)
);
CREATE TABLE IF NOT EXISTS games (
    game_id INTEGER PRIMARY KEY,
    date TEXT,
    season INTEGER,
    home_team_id INTEGER REFERENCES teams(team_id),
    away_team_id INTEGER REFERENCES teams(team_id)
);
CREATE TABLE IF NOT EXISTS box_scores (
    box_score_id INTEGER PRIMARY KEY,
    game_id INTEGER REFERENCES games(game_id),
    player_id INTEGER REFERENCES players(player_id),
    team_id INTEGER REFERENCES teams(team_id),
    minutes INTEGER, points INTEGER, rebounds INTEGER, assists INTEGER,
    steals INTEGER, blocks INTEGER, turnovers INTEGER, fouls INTEGER, plus_minus INTEGER,
    fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER, ftm INTEGER, fta INTEGER,
    UNIQUE(game_id, player_id)
);
CREATE TABLE IF NOT EXISTS season_totals (
    season_total_id INTEGER PRIMARY KEY,
    player_id INTEGER REFERENCES players(player_id),
    season INTEGER,
    games_played INTEGER,
    points INTEGER, rebounds INTEGER, assists INTEGER, steals INTEGER, blocks INTEGER,
    turnovers INTEGER, fouls INTEGER, fgm INTEGER, fga INTEGER, fg3m INTEGER, fg3a INTEGER,
    ftm INTEGER, fta INTEGER,
    ppg REAL, rpg REAL, apg REAL, spg REAL, bpg REAL, tpg REAL, mpg REAL,
    UNIQUE(player_id, season)
);
"""


def get(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            time.sleep(DELAY)
            return r.json()
        except requests.exceptions.RequestException:
            if attempt == retries - 1:
                raise
            time.sleep(2)


def split_made_att(s):
    made, att = s.split("-")
    return int(made), int(att)


def scrape_teams(conn):
    data = get(f"{BASE}/teams")
    teams = [t["team"] for t in data["sports"][0]["leagues"][0]["teams"]]
    conn.executemany("INSERT OR IGNORE INTO teams (team_id, name, abbreviation) VALUES (?, ?, ?)",
                      [(int(t["id"]), t["displayName"], t["abbreviation"]) for t in teams])
    conn.commit()
    return [t["id"] for t in teams]


def scrape_season_games(team_ids, season):
    games = {}
    for team_id in team_ids:
        data = get(f"{BASE}/teams/{team_id}/schedule", params={"season": season})
        for e in data.get("events", []):
            if not e["competitions"][0]["status"]["type"]["completed"]:
                continue
            comp = e["competitions"][0]
            home = next(c for c in comp["competitors"] if c["homeAway"] == "home")
            away = next(c for c in comp["competitors"] if c["homeAway"] == "away")
            games[int(e["id"])] = (int(e["id"]), e["date"], season, int(home["team"]["id"]), int(away["team"]["id"]))
    return games


def scrape_box_score(conn, game_id):
    data = get(f"{BASE}/summary", params={"event": game_id})
    for team_block in data["boxscore"]["players"]:
        team_id = int(team_block["team"]["id"])
        stat_group = team_block["statistics"][0]
        labels = stat_group["labels"]
        for a in stat_group["athletes"]:
            if a.get("didNotPlay") or not a.get("stats") or "id" not in a["athlete"]:
                continue
            stats = dict(zip(labels, a["stats"]))
            athlete = a["athlete"]
            player_id = int(athlete["id"])
            try:
                fgm, fga = split_made_att(stats["FG"])
                fg3m, fg3a = split_made_att(stats["3PT"])
                ftm, fta = split_made_att(stats["FT"])
                row = (game_id, player_id, team_id, int(float(stats["MIN"])), int(stats["PTS"]), int(stats["REB"]),
                       int(stats["AST"]), int(stats["STL"]), int(stats["BLK"]), int(stats["TO"]), int(stats["PF"]),
                       int(stats["+/-"].replace("+", "")), fgm, fga, fg3m, fg3a, ftm, fta)
            except (ValueError, KeyError) as ex:
                print(f"  skipping player {player_id} in game {game_id}: bad stats {stats} ({ex})")
                continue
            conn.execute("""INSERT INTO players (player_id, full_name, position, team_id) VALUES (?, ?, ?, ?)
                            ON CONFLICT(player_id) DO UPDATE SET full_name=excluded.full_name,
                            position=excluded.position, team_id=excluded.team_id""",
                         (player_id, athlete["displayName"], athlete["position"]["abbreviation"], team_id))
            conn.execute("""INSERT OR IGNORE INTO box_scores (game_id, player_id, team_id, minutes, points,
                            rebounds, assists, steals, blocks, turnovers, fouls, plus_minus,
                            fgm, fga, fg3m, fg3a, ftm, fta)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", row)
    conn.commit()


SEASON_STAT_KEYS = ["gamesPlayed", "points", "rebounds", "assists", "steals", "blocks", "turnovers", "fouls",
                    "fieldGoalsMade", "fieldGoalsAttempted", "threePointFieldGoalsMade",
                    "threePointFieldGoalsAttempted", "freeThrowsMade", "freeThrowsAttempted",
                    "avgPoints", "avgRebounds", "avgAssists", "avgSteals", "avgBlocks", "avgTurnovers", "avgMinutes"]


def scrape_season_total(conn, player_id, season):
    try:
        data = get(f"{CORE}/{season}/types/2/athletes/{player_id}/statistics")
    except requests.exceptions.RequestException:
        return
    flat = {}
    for cat in data["splits"]["categories"]:
        for s in cat["stats"]:
            flat[s["name"]] = s["value"]
    if "gamesPlayed" not in flat:
        return
    vals = [flat.get(k, 0) for k in SEASON_STAT_KEYS]
    conn.execute("""INSERT OR IGNORE INTO season_totals (player_id, season, games_played, points, rebounds,
                    assists, steals, blocks, turnovers, fouls, fgm, fga, fg3m, fg3a, ftm, fta,
                    ppg, rpg, apg, spg, bpg, tpg, mpg)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (player_id, season, *vals))
    conn.commit()


if __name__ == "__main__":
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    team_ids = scrape_teams(conn)

    done_games = {row[0] for row in conn.execute("SELECT DISTINCT game_id FROM box_scores")}
    done_totals = {(row[0], row[1]) for row in conn.execute("SELECT player_id, season FROM season_totals")}

    for season in SEASONS:
        print(f"=== season {season} ===")
        games = scrape_season_games(team_ids, season)
        conn.executemany("""INSERT OR IGNORE INTO games (game_id, date, season, home_team_id, away_team_id)
                             VALUES (?, ?, ?, ?, ?)""", games.values())
        conn.commit()

        game_ids = [gid for gid in games if gid not in done_games]
        for i, game_id in enumerate(game_ids):
            print(f"box score {i+1}/{len(game_ids)}")
            try:
                scrape_box_score(conn, game_id)
            except requests.exceptions.RequestException as ex:
                print(f"  skipping game {game_id}: {ex}")
                continue
            done_games.add(game_id)

        players_this_season = {row[0] for row in conn.execute(
            "SELECT DISTINCT bs.player_id FROM box_scores bs JOIN games g ON bs.game_id = g.game_id "
            "WHERE g.season = ?", (season,))}
        todo = [p for p in players_this_season if (p, season) not in done_totals]
        for i, player_id in enumerate(todo):
            print(f"season total {i+1}/{len(todo)}")
            scrape_season_total(conn, player_id, season)
            done_totals.add((player_id, season))

    conn.close()
