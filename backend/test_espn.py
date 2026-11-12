import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import espn


def event(game_id="g", season=2025, phase=2):
    return {"id": game_id, "date": "2025-02-02T01:00Z", "season": {"year": season, "type": phase},
            "status": {"type": {"completed": True}}, "competitions": [{"competitors": [
                {"homeAway": side, "team": {"id": team, "displayName": team}, "score": "80"}
                for side, team in (("home", "1"), ("away", "2"))]}]}


def summary(points="20", game_id="g", season=2025):
    return {"header": {"id": game_id, "season": {"year": season}}, "boxscore": {
        "teams": [{"team": {"id": team}, "statistics": [{"name": "points", "displayValue": "80"}]}
                  for team in ("1", "2")],
        "players": [{"team": {"id": team}, "statistics": [{"keys": ["points", "assists"], "athletes": [
            {"athlete": {"id": team, "displayName": "Player " + team}, "stats": [points, "--"]},
            {"athlete": {"id": "dnp"}, "didNotPlay": True, "stats": ["0", "0"]},
        ]}]} for team in ("1", "2")],
    }}


class ImportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.importer = espn.Importer(Path(directory.name) / "sports.db", workers=1)
        self.addCleanup(self.importer.close)
        self.store = self.importer.store
        self.phase = {"id": "2", "type": 2, "name": "Regular Season",
                      "startDate": "2025-02-01", "endDate": "2025-02-02"}

    def register(self, league="nba"):
        self.store.upsert("leagues", {"league_id": league},
                          {"sport": espn.LEAGUES[league][0], "name": league, "current_season": 2027})
        self.store.save_game(league, event(), self.phase, "2024-25")
        self.store.db.commit()

    def test_numeric_compounds_missing_and_durations(self):
        values, _ = espn.metrics([
            {"name": "fieldGoalsMade-fieldGoalsAttempted", "value": "0-4"},
            {"name": "completions/passingAttempts", "value": "16/25"},
            {"name": "assists", "value": "--"},
            {"name": "plusMinus", "value": "-12"},
            {"name": "timeOnIce", "value": "21:10"},
            {"name": "fullInnings.partInnings", "value": "5.2"},
        ])
        self.assertEqual(values["field_goals_made"], 0)
        self.assertEqual(values["field_goals_attempted"], 4)
        self.assertEqual(values["completions"], 16)
        self.assertEqual(values["passing_attempts"], 25)
        self.assertIsNone(values["assists"])
        self.assertEqual(values["plus_minus"], -12)
        self.assertEqual(values["time_on_ice_seconds"], 1270)
        self.assertEqual(values["outs_recorded"], 17)

    def test_league_ids_and_reimports_keep_rows_distinct(self):
        for league, points in (("nba", "20"), ("wnba", "30")):
            self.register(league)
            self.store.save_summary(league, "g", summary(points))
            self.store.save_summary(league, "g", summary(points))
        rows = self.store.db.execute("SELECT league_id,points,assists FROM basketball_box_scores ORDER BY league_id,player_id").fetchall()
        self.assertEqual(rows, [("nba", 20, None), ("nba", 20, None), ("wnba", 30, None), ("wnba", 30, None)])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM league_players WHERE player_id='dnp'").fetchone()[0], 0)

    def test_missing_one_team_is_reported_and_wrong_season_is_rejected(self):
        self.register()
        payload = summary()
        payload["boxscore"]["players"].pop()
        self.store.save_summary("nba", "g", payload)
        self.assertEqual(self.store.db.execute("SELECT stats_status FROM league_games").fetchone()[0], "missing_stats")
        with self.assertRaisesRegex(ValueError, "different game or season"):
            self.store.save_summary("nba", "g", summary(season=2026))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM basketball_box_scores").fetchone()[0], 1)

    def test_incomplete_refresh_keeps_previous_good_box_score(self):
        self.register()
        self.store.save_summary("nba", "g", summary())
        partial = summary("99")
        partial["boxscore"]["players"].pop()
        with self.assertRaisesRegex(ValueError, "previous box score retained"):
            self.store.save_summary("nba", "g", partial)
        self.assertEqual(self.store.db.execute("SELECT points FROM basketball_box_scores").fetchall(), [(20,), (20,)])

    def test_season_stat_categories_do_not_overwrite_each_other(self):
        data = {"season": {"$ref": "https://example/seasons/2025"},
                "seasonType": {"$ref": "https://example/types/2"}, "splits": {"categories": [
                    {"name": "passing", "stats": [{"name": "interceptions", "value": 11}]},
                    {"name": "defensiveInterceptions", "stats": [{"name": "interceptions", "value": 0}]},
                ]}}
        self.importer.save_totals("nfl", 2025, 2, "player", "p", data)
        row = self.store.db.execute("SELECT passing_interceptions,defensive_interceptions FROM football_season_totals").fetchone()
        self.assertEqual(row, (11, 0))
        with self.assertRaisesRegex(ValueError, "different season or phase"):
            self.importer.save_totals("nfl", 2024, 2, "player", "p", data)

    def test_current_season_window_uses_leagues_own_year(self):
        board = {"leagues": [{"season": {"year": 2027}}]}
        with patch.object(self.importer.feed, "get", return_value=board):
            self.assertEqual(self.importer.configure("nba", 10, True, True), 2027)
        self.assertEqual(self.store.db.execute("SELECT first_season,last_season,include_postseason FROM leagues").fetchone(), (2018, 2027, 1))

    def test_daily_discovery_handles_next_utc_day_and_resumes(self):
        self.register()
        calls = []
        def get(url, params=None):
            calls.append(url)
            return summary() if "/summary" in url else {"events": [event()]}
        with patch.object(self.importer, "season", return_value=({"displayName": "2024-25"}, [self.phase])), patch.object(self.importer.feed, "get", side_effect=get):
            self.importer.games("nba", 2025, True)
            first_count = len(calls)
            self.importer.games("nba", 2025, True)
        self.assertEqual(len(calls), first_count)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM league_games").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT stats_status FROM league_games").fetchone()[0], "complete")

    def test_failed_summary_is_retried_even_after_day_was_scanned(self):
        self.register()
        self.store.db.execute("UPDATE league_games SET stats_status='error'")
        for day in ("2025-02-01", "2025-02-02"):
            self.store.progress("nba", 2025, "day:" + day, "complete")
        with patch.object(self.importer, "season", return_value=({}, [self.phase])), patch.object(self.importer.feed, "get", return_value=summary()):
            self.importer.games("nba", 2025, True)
        self.assertEqual(self.store.db.execute("SELECT stats_status FROM league_games").fetchone()[0], "complete")

    def test_wrong_date_is_a_discovery_gap(self):
        self.register()
        wrong = event()
        wrong["date"] = "2026-01-01T00:00Z"
        with patch.object(self.importer, "season", return_value=({}, [self.phase])), patch.object(self.importer.feed, "get", return_value={"events": [wrong]}):
            self.importer.games("nba", 2025, True)
        self.assertEqual(self.store.db.execute("SELECT status FROM import_progress WHERE step='schedule'").fetchone()[0], "partial")

    def test_backfill_preserves_recent_names_and_historical_aliases(self):
        self.store.team("nba", {"id": "t", "displayName": "New Name"}, "2026-01-01")
        self.store.team("nba", {"id": "t", "displayName": "Old Name"}, "2017-01-01")
        name, aliases = self.store.db.execute("SELECT name,aliases FROM league_teams").fetchone()
        self.assertEqual(name, "New Name")
        self.assertIn("Old Name", aliases)

    def test_enabling_postseason_invalidates_regular_only_checkpoints(self):
        board = {"leagues": [{"season": {"year": 2027}}]}
        with patch.object(self.importer.feed, "get", return_value=board):
            self.importer.configure("nba", 10, True, False)
            self.store.progress("nba", 2025, "day:2025-02-01", "complete")
            self.importer.configure("nba", 10, True, True)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM import_progress WHERE step LIKE 'day:%'").fetchone()[0], 0)

    def test_refresh_does_not_redownload_an_old_finished_season(self):
        self.register()
        with patch.object(self.importer, "season", return_value=({}, [self.phase])), patch.object(self.importer.feed, "get") as get:
            self.importer.games("nba", 2025, True, days=3)
        get.assert_not_called()

    def test_scoreboard_limit_change_invalidates_truncated_scan(self):
        self.store.upsert("leagues", {"league_id": "nfl"}, {"sport": "football", "name": "NFL",
            "include_postseason": 1, "discovery_group": None, "scoreboard_limit": 1000})
        self.store.progress("nfl", 2025, "day:2025-02-01", "complete")
        with patch.object(self.importer.feed, "get", return_value={"leagues": [{"season": {"year": 2026}}]}):
            self.importer.configure("nfl", 10, True, True)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM import_progress WHERE step LIKE 'day:%'").fetchone()[0], 0)

    def test_regular_playoff_and_playin_phases_are_separate(self):
        self.register()
        phases = [{**self.phase, "id": str(n), "type": n, "name": label}
                  for n, label in ((1, "Preseason"), (2, "Regular Season"), (3, "Postseason"), (5, "Play-In Season"))]
        def get(url, params=None):
            return summary(game_id=url.split("event=")[-1]) if "/summary" in url else {
                "events": [event(str(n), phase=n) for n in (1, 2, 3, 5)]}
        with patch.object(self.importer, "season", return_value=({}, phases)), patch.object(self.importer.feed, "get", side_effect=get):
            self.importer.games("nba", 2025, True)
        rows = self.store.db.execute("SELECT game_id,season_type FROM league_games WHERE game_id!='g' ORDER BY season_type").fetchall()
        self.assertEqual(rows, [("2", 2), ("3", 3), ("5", 5)])

    def test_tennis_filters_gender_and_keeps_doubles_members(self):
        def pair(key, members, winner):
            return {"id": key, "type": "team", "winner": winner, "roster": {"displayName": key,
                "athletes": [{"displayName": str(p), "links": [{"href": f"https://www.espn.com/tennis/player/_/id/{p}/name"}]} for p in members]}}
        competition = {"id": "d", "date": "2025-02-02T00:00Z", "type": {"slug": "mens-doubles"},
                       "status": {"type": {"completed": True}},
                       "competitors": [pair("11-22", (11, 22), True), pair("33-44", (33, 44), False)]}
        tournament = {"id": "t-2025", "name": "Tournament", "date": "2025-02-01T00:00Z", "season": {"year": 2025, "type": 2},
                      "groupings": [{"grouping": {"slug": "mens-doubles"}, "competitions": [competition]},
                                    {"grouping": {"slug": "womens-singles"}, "competitions": [{"id": "w"}]}]}
        self.assertEqual([c["id"] for c in espn.individual_competitions("atp", tournament)], ["d"])
        self.assertEqual([c["id"] for c in espn.individual_competitions("wta", tournament)], ["w"])
        with patch.object(self.importer.feed, "get", return_value={"statsSource": {"state": "none"}, "competitors": []}):
            self.importer.individual("atp", tournament, self.phase, "2025")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM tennis_box_scores").fetchone()[0], 4)
        self.assertEqual(self.store.db.execute("SELECT sum(winner) FROM tennis_team_box_scores").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT stats_status FROM league_games").fetchone()[0], "complete")

    def test_golf_scorecard_keeps_strokes_and_nulls_empty_breakdowns(self):
        competition = {"id": "open", "date": "2025-07-17T00:00Z", "status": {"type": {"completed": True}},
                       "competitors": [{"id": "9478", "type": "athlete", "athlete": {"displayName": "Golfer"},
                                        "score": "-17", "linescores": [{"value": n} for n in (68, 64, 67, 68)]}]}
        tournament = {"id": "open", "name": "The Open", "date": competition["date"],
                      "season": {"year": 2025, "type": 2}, "competitions": [competition]}
        fields = {"regScore": 267, "scoreToPar": -17, "cupPoints": 750, "birdies": 0, "eagles": 0, "pars": 0, "bogeys": 0}
        data = {"splits": {"categories": [{"name": "general", "stats": [{"name": k, "value": v} for k, v in fields.items()]}]}}
        with patch.object(self.importer.feed, "get", return_value=data) as get:
            self.importer.individual("pga", tournament, self.phase, "2025")
            self.importer.individual("pga", tournament, self.phase, "2025")
        self.assertEqual(get.call_count, 1)
        self.assertEqual(self.store.db.execute("SELECT recorded_strokes,score_to_par,cup_points,birdies FROM golf_box_scores").fetchone(), (267, -17, 750, None))

    def test_ufc_unknown_result_is_not_counted_as_a_loss(self):
        competition = {"id": "fight", "date": "2025-02-02T00:00Z", "status": {"type": {"completed": True}},
                       "competitors": [{"id": str(p), "type": "athlete", "winner": False,
                                        "athlete": {"displayName": str(p)}} for p in (1, 2)]}
        card = {"id": "card", "date": competition["date"], "name": "Card", "season": {"year": 2025, "type": 2}}
        self.store.save_individual("ufc", card, competition, self.phase, "2025")
        self.importer.mma_totals("ufc", 2025, [self.phase])
        rows = self.store.db.execute("SELECT derived_fights,derived_losses,derived_unresolved_results FROM mma_season_totals").fetchall()
        self.assertEqual(rows, [(1, None, 1), (1, None, 1)])

    def test_f1_uses_official_driver_and_constructor_standings(self):
        phase = {**self.phase, "standings": {"$ref": "https://feed/index"}}
        replies = {
            "https://feed/index": {"items": [{"$ref": "https://feed/drivers"}, {"$ref": "https://feed/constructors"}]},
            "https://feed/drivers": {"displayName": "Driver Standings", "standings": [{"athlete": {"$ref": "https://feed/p"},
                "records": [{"name": "overall", "stats": [{"name": "championshipPts", "value": 423}]}]}]},
            "https://feed/constructors": {"displayName": "Constructor Standings", "standings": [{"manufacturer": {"$ref": "https://feed/c"},
                "records": [{"name": "overall", "stats": [{"name": "points", "value": 833}]}]}]},
            "https://feed/p": {"id": "p", "displayName": "Driver"},
            "https://feed/c": {"id": "c", "displayName": "Constructor"},
        }
        with patch.object(self.importer.feed, "get", side_effect=lambda url: replies[url]):
            self.importer.racing_totals("f1", 2025, [phase])
        self.assertEqual(self.store.db.execute("SELECT overall_championship_pts,totals_source FROM racing_season_totals").fetchone(), (423, "ESPN Driver Standings"))
        self.assertEqual(self.store.db.execute("SELECT overall_points FROM racing_team_season_totals").fetchone(), (833,))

    def test_racing_zero_lap_times_are_missing(self):
        competition = {"id": "race", "date": "2025-07-06T14:00Z", "type": {"abbreviation": "Race"},
                       "status": {"type": {"completed": True}}, "competitors": [
                           {"id": "5579", "type": "athlete", "winner": True, "athlete": {"displayName": "Driver"}}]}
        event_data = {"id": "gp", "name": "Grand Prix", "date": competition["date"],
                      "season": {"year": 2025, "type": 2}, "competitions": [competition]}
        data = {"splits": {"categories": [{"name": "general", "stats": [
            {"name": "totalTime", "value": 5835735}, {"name": "qual1TimeMS", "value": 0},
        ]}]}}
        with patch.object(self.importer.feed, "get", return_value=data):
            self.importer.individual("f1", event_data, self.phase, "2025")
        self.assertEqual(self.store.db.execute("SELECT total_time_ms,qual1_time_ms FROM racing_box_scores").fetchone(), (5835735, None))


if __name__ == "__main__":
    unittest.main()
