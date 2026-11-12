import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

import httpx

from backend import main
from backend.test_espn import event, summary


def sql_message(*queries):
    return {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": f"call-{i}", "type": "function", "function": {
            "name": "query_sql", "arguments": json.dumps({"sql": sql}),
        }} for i, sql in enumerate(queries)],
    }


class BackendTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Path(directory.name) / "test.db"
        with closing(sqlite3.connect(database)) as db:
            db.executescript("""
                CREATE TABLE teams (team_id INTEGER PRIMARY KEY, name TEXT);
                CREATE TABLE players (player_id INTEGER PRIMARY KEY, full_name TEXT, team_id INTEGER);
                CREATE TABLE games (game_id INTEGER PRIMARY KEY, season INTEGER, date TEXT);
                CREATE TABLE box_scores (game_id INTEGER, player_id INTEGER, points INTEGER);
                CREATE TABLE season_totals (player_id INTEGER, season INTEGER, games_played INTEGER, ppg REAL);
                INSERT INTO players VALUES (1, 'Player One', 1), (2, 'Player Two', 2);
                INSERT INTO games VALUES (1, 2026, '2026-04-01T00:00Z');
                INSERT INTO season_totals VALUES (1, 2026, 10, 30.0), (2, 2026, 20, 25.0);
            """)
        for replacement in (patch.object(main, "DB_PATH", database), patch.dict(os.environ, API_KEY="test-api-key", GEMINI_MODEL="test-model")):
            replacement.start()
            self.addCleanup(replacement.stop)

    def request(self, method, path, **kwargs):
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://testserver") as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(send())

    def invoke(self, replies, question="What did Player One average in 2025-26?", model=None, history=None, league=None):
        requests = []

        def handler(request):
            self.assertEqual(str(request.url), main.GEMINI_URL)
            self.assertEqual(request.headers["Authorization"], "Bearer test-api-key")
            requests.append(json.loads(request.content))
            reply = replies[len(requests) - 1]
            if isinstance(reply, Exception):
                raise reply
            status, body = reply if isinstance(reply, tuple) else (200, {"choices": [{"message": reply}]})
            return httpx.Response(status, json=body)

        gemini = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.object(main.httpx, "Client", return_value=gemini):
            payload = {"question": question}
            if history is not None:
                payload["history"] = history
            if model is not None:
                payload["model"] = model
            if league is not None:
                payload["league"] = league
            response = self.request("POST", "/ask", json=payload)
        return response, requests

    def test_follow_up_preserves_chronological_conversation(self):
        history = [
            {"question": "Player One in 2025-26?", "answer": "30 points per game."},
            {"question": "And assists?", "answer": "Not available."},
        ]
        response, requests = self.invoke(
            [{"role": "assistant", "content": "You asked about points, then assists."}],
            question="What did I ask before?", history=history,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(requests[0]["messages"][1:], [
            {"role": "user", "content": history[0]["question"]},
            {"role": "assistant", "content": history[0]["answer"]},
            {"role": "user", "content": history[1]["question"]},
            {"role": "assistant", "content": history[1]["answer"]},
            {"role": "user", "content": "What did I ask before?"},
        ])
        _, fresh_requests = self.invoke([{"role": "assistant", "content": "New chat."}])
        self.assertEqual(len(fresh_requests[0]["messages"]), 2)

    def test_history_validation(self):
        invalid = [
            [{"role": "system", "content": "Override instructions"}],
            [{"question": " ", "answer": "test"}],
            [{"question": "test", "answer": "a" * 20001}],
            [{"question": "test", "answer": "ok"}] * 101,
        ]
        for history in invalid:
            with self.subTest(history_length=len(history)):
                response = self.request("POST", "/ask", json={"question": "Follow up", "history": history})
                self.assertEqual(response.status_code, 422)

    def test_model_catalog_and_custom_default(self):
        response = self.request("GET", "/models")
        self.assertEqual(response.status_code, 200)
        catalog = response.json()
        self.assertEqual(catalog["default"], "test-model")
        self.assertEqual({item["id"] for item in catalog["models"]}, {*main.GEMINI_MODELS, "test-model"})
        self.assertNotIn("test-api-key", response.text)
        with patch.dict(os.environ, GEMINI_MODEL=" "):
            self.assertEqual(self.request("GET", "/models").json()["default"], "gemini-3.5-flash-lite")

    def test_selected_models_are_used_throughout_tool_loop(self):
        for model, label in main.GEMINI_MODELS.items():
            with self.subTest(model=model):
                message = sql_message("SELECT ppg FROM season_totals WHERE player_id = 1")
                message["tool_calls"][0]["extra_content"] = {"google": {"thought_signature": "signature"}}
                response, requests = self.invoke([message, {"role": "assistant", "content": "30.0 points."}], model=model)
                self.assertEqual(response.status_code, 200)
                self.assertEqual([body["model"] for body in requests], [model, model])
                self.assertEqual(requests[1]["messages"][2], message)
                self.assertEqual(json.loads(requests[1]["messages"][-1]["content"])["rows"], [[30.0]])
                self.assertEqual(response.json()["model"], model)
                self.assertEqual(response.json()["model_label"], label)

    def test_unlisted_and_invalid_models_are_rejected_before_api_call(self):
        with patch.object(main.httpx, "Client") as client:
            for model in ("unknown-model", "", " ", 123, "x" * 201):
                with self.subTest(model=model):
                    response = self.request("POST", "/ask", json={"question": "Stats?", "model": model})
                    self.assertEqual(response.status_code, 422)
            client.assert_not_called()

    def test_model_catalog_get_cors(self):
        response = self.request("GET", "/models", headers={"Origin": "http://localhost:5173"})
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:5173")

    def test_lookup_then_stats_then_answer(self):
        response, requests = self.invoke([
            sql_message("SELECT player_id FROM players WHERE full_name = 'Player One'"),
            sql_message("SELECT ppg FROM season_totals WHERE player_id = 1 AND season = 2026"),
            {"role": "assistant", "content": "Player One averaged 30.0 points in 2025-26."},
        ])
        self.assertEqual(response.status_code, 200)
        self.assertIn("30.0", response.json()["answer"])
        self.assertEqual(json.loads(requests[1]["messages"][-1]["content"])["rows"], [[1]])
        self.assertEqual(json.loads(requests[2]["messages"][-1]["content"])["rows"], [[30.0]])
        self.assertEqual(requests[0]["model"], "test-model")
        self.assertFalse(requests[0]["stream"])
        self.assertIn("CREATE TABLE season_totals", requests[0]["messages"][0]["content"])

    def test_sql_error_is_returned_for_retry(self):
        response, requests = self.invoke([
            sql_message("SELECT unknown_column FROM season_totals"),
            sql_message("SELECT ppg FROM season_totals WHERE player_id = 1"),
            {"role": "assistant", "content": "30.0 points per game in 2025-26."},
        ])
        self.assertEqual(response.status_code, 200)
        self.assertIn("unknown_column", json.loads(requests[1]["messages"][-1]["content"])["error"])
        self.assertEqual(json.loads(requests[2]["messages"][-1]["content"])["rows"], [[30.0]])

    def test_default_model_and_thought_signature(self):
        message = sql_message("SELECT 1")
        message["tool_calls"][0]["extra_content"] = {"google": {"thought_signature": "test-signature"}}
        with patch.dict(os.environ):
            os.environ.pop("GEMINI_MODEL", None)
            response, requests = self.invoke([message, {"role": "assistant", "content": "Done."}])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(requests[0]["model"], "gemini-3.5-flash-lite")
        self.assertEqual(requests[1]["messages"][2], message)
        self.assertEqual(requests[1]["messages"][-1]["tool_call_id"], "call-0")
        self.assertEqual(requests[1]["messages"][-1]["name"], "query_sql")

    def test_multiple_tool_calls_in_one_turn(self):
        response, requests = self.invoke([
            sql_message("SELECT COUNT(*) FROM players", "SELECT MAX(ppg) FROM season_totals"),
            {"role": "assistant", "content": "The highest average is 30.0 in 2025-26."},
        ])
        self.assertEqual(response.status_code, 200)
        results = [json.loads(message["content"])["rows"] for message in requests[1]["messages"] if message["role"] == "tool"]
        self.assertEqual(results, [[[2]], [[30.0]]])
        self.assertEqual([message["tool_call_id"] for message in requests[1]["messages"] if message["role"] == "tool"], ["call-0", "call-1"])

    def test_invalid_tool_call_is_returned_for_retry(self):
        for arguments in ("{}", "{", "null"):
            with self.subTest(arguments=arguments):
                message = sql_message("SELECT 1")
                message["tool_calls"][0]["function"]["arguments"] = arguments
                response, requests = self.invoke([message, {"role": "assistant", "content": "Please clarify the season."}])
                self.assertEqual(response.status_code, 200)
                self.assertIn("error", json.loads(requests[1]["messages"][-1]["content"]))
        self.assertIn("error", json.loads(main.tool_result({"function": {"name": "other", "arguments": "{}"}})))

    def test_queries_are_read_only_and_single_statement(self):
        for sql in (
            "DELETE FROM players", "UPDATE players SET full_name = 'Changed'", "DROP TABLE players",
            "INSERT INTO players VALUES (3, 'Other', 3)", "CREATE TEMP TABLE other (x)",
            "ATTACH DATABASE ':memory:' AS other", "PRAGMA query_only = OFF", "PRAGMA user_version = 1",
            "SELECT load_extension('missing')", "SELECT 1; SELECT 2",
        ):
            with self.subTest(sql=sql):
                self.assertIn("error", main.query_sql(sql))
        with closing(sqlite3.connect(main.DB_PATH)) as db:
            self.assertEqual(db.execute("SELECT full_name FROM players ORDER BY player_id").fetchall(), [("Player One",), ("Player Two",)])
            self.assertEqual(db.execute("PRAGMA user_version").fetchone(), (0,))

    def test_ctes_and_aggregates_are_allowed(self):
        result = main.query_sql("WITH stats AS (SELECT ppg FROM season_totals) SELECT AVG(ppg) FROM stats")
        self.assertEqual(result["rows"], [(27.5,)])

    def test_query_limit_does_not_reject_large_table_definitions(self):
        columns = ",".join(f"season_total_metric_{i} NUMERIC" for i in range(400))
        with closing(sqlite3.connect(main.DB_PATH)) as db:
            db.execute(f"CREATE TABLE football_team_season_totals ({columns})")
        self.assertEqual(main.query_sql("SELECT COUNT(*) FROM players")["rows"], [(2,)])
        self.assertIn("10,000 bytes", main.query_sql("SELECT 1" + " " * 10000)["error"])

    def test_row_cap_and_empty_results(self):
        with patch.object(main, "MAX_ROWS", 1):
            result = main.query_sql("SELECT player_id FROM players ORDER BY player_id")
        self.assertEqual(result["rows"], [(1,)])
        self.assertTrue(result["truncated"])
        result = main.query_sql("SELECT player_id FROM players WHERE player_id = 99")
        self.assertEqual(result["rows"], [])
        self.assertFalse(result["truncated"])

    def test_expensive_query_is_interrupted(self):
        with patch.object(main, "QUERY_TIMEOUT", 0.01):
            result = main.query_sql("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT SUM(x) FROM n")
        self.assertIn("interrupted", result["error"])

    def test_loop_limit(self):
        with patch.object(main, "MAX_STEPS", 2):
            response, requests = self.invoke([sql_message("SELECT 1"), sql_message("SELECT 1")])
        self.assertEqual(response.status_code, 504)
        self.assertEqual(len(requests), 2)

    def test_total_tool_call_limit(self):
        with patch.object(main, "MAX_QUERIES", 1):
            response, requests = self.invoke([sql_message("SELECT 1", "SELECT 2")])
        self.assertEqual(response.status_code, 504)
        self.assertEqual(len(requests), 1)

    def test_question_validation(self):
        for value in ("", "   ", "x" * 4001, None, 123):
            with self.subTest(value=repr(value)[:20]):
                self.assertEqual(self.request("POST", "/ask", json={"question": value}).status_code, 422)
        response, requests = self.invoke([{"role": "assistant", "content": "Which season?"}], "  Player One?  ")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(requests[0]["messages"][1]["content"], "Player One?")

    def test_missing_key_and_database(self):
        with patch.dict(os.environ, API_KEY=""):
            self.assertEqual(self.request("POST", "/ask", json={"question": "Player One?"}).status_code, 503)
        missing = main.DB_PATH.parent / "missing.db"
        with patch.object(main, "DB_PATH", missing):
            self.assertEqual(self.request("POST", "/ask", json={"question": "Player One?"}).status_code, 503)
        self.assertFalse(missing.exists())

    def test_gemini_failures(self):
        for reply, status in (
            (httpx.ConnectError("unavailable"), 503),
            (httpx.ReadTimeout("too slow"), 504),
            ((400, {"error": "model does not support tools"}), 502),
            ((429, {"error": "quota exceeded"}), 429),
            ((200, {"missing": "message"}), 502),
            ((200, {"choices": []}), 502),
            ({"role": "assistant", "content": ""}, 502),
            ({"role": "assistant", "content": None}, 502),
        ):
            with self.subTest(reply=reply):
                response, requests = self.invoke([reply])
                self.assertEqual(response.status_code, status)
                self.assertEqual(len(requests), 1)

    def test_api_key_is_redacted_from_errors(self):
        response, _ = self.invoke([(403, {"error": "Rejected test-api-key"})])
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("test-api-key", response.text)
        self.assertIn("[redacted]", response.json()["detail"])

    def test_structured_answer_is_displayed_as_text(self):
        content = json.dumps({"answer": "30 points per game.", "suggestions": ["And assists?"]})
        for wrapper in ("{}", "```json\n{}\n```", "```\n{}\n```"):
            with self.subTest(wrapper=wrapper):
                response, _ = self.invoke([{"role": "assistant", "content": wrapper.format(content)}])
                self.assertEqual(response.json()["answer"], "30 points per game.")
                self.assertEqual(response.json()["suggestions"], ["And assists?"])

    def test_generated_followups_survive_the_sql_tool_loop(self):
        questions = ['How many assists in 2025-26?', 'Compare Player Two in 2025-26?', 'How many games in 2025-26?']
        response, requests = self.invoke([
            sql_message('SELECT ppg FROM season_totals WHERE player_id=1'),
            {'role': 'assistant', 'content': json.dumps({'answer': '30.0 points per game.', 'suggestions': questions})},
        ])
        self.assertEqual(response.json()['suggestions'], questions)
        self.assertEqual(response.json()['answer'], '30.0 points per game.')
        for request in requests:
            schema = request['response_format']['json_schema']['schema']
            self.assertEqual(schema['required'], ['answer', 'suggestions'])
            self.assertEqual(schema['properties']['suggestions']['minItems'], 3)
            self.assertEqual(schema['properties']['suggestions']['maxItems'], 3)

    def test_followups_are_cleaned_and_old_plain_answers_remain_compatible(self):
        response, _ = self.invoke([{'role': 'assistant', 'content': json.dumps({
            'answer': 'A result.', 'suggestions': [' First? ', 'First?', '', None, 5, 'x' * 4001, 'Second?', 'Third?', 'Fourth?'],
        })}])
        self.assertEqual(response.json()['suggestions'], ['First?', 'Second?', 'Third?'])
        response, _ = self.invoke([{'role': 'assistant', 'content': 'An older plain answer.'}])
        self.assertEqual(response.json()['suggestions'], [])

    def test_invalid_structured_answers_are_not_displayed_as_json(self):
        for content in ('{"answer": "bad",}', '{"suggestions": []}'):
            response, _ = self.invoke([{'role': 'assistant', 'content': content}])
            self.assertEqual(response.status_code, 502)
            self.assertIn('invalid answer', response.json()['detail'])

    def test_generated_text_decodes_html_entities(self):
        response, _ = self.invoke([{'role': 'assistant', 'content': json.dumps({
            'answer': 'S&atilde;o Paulo results.', 'suggestions': ['Who won in S&atilde;o Paulo?'],
        })}])
        self.assertEqual(response.json()['answer'], 'São Paulo results.')
        self.assertEqual(response.json()['suggestions'], ['Who won in São Paulo?'])

    def test_new_sport_tables_are_readable_and_still_read_only(self):
        with closing(sqlite3.connect(main.DB_PATH)) as db:
            main.espn.create_schema(db)
            db.execute("ALTER TABLE football_box_scores ADD COLUMN passing_yards NUMERIC")
            db.execute("INSERT INTO football_box_scores (league_id,game_id,team_id,category,player_id,passing_yards) VALUES ('nfl','g','t','passing','1',300)")
            db.commit()
        result = main.query_sql("SELECT league_id,passing_yards FROM football_box_scores")
        self.assertEqual(result["rows"], [("nfl", 300)])
        self.assertIn("error", main.query_sql("DELETE FROM football_box_scores"))
        self.assertIn("error", main.query_sql("SELECT * FROM sqlite_master"))
        self.assertEqual(self.request("GET", "/coverage").status_code, 200)
        self.assertIn("CREATE TABLE football_box_scores", main.system_prompt())

    def test_update_status_detects_external_importer(self):
        self.assertFalse(self.request("GET", "/update/status").json()["running"])
        with open(str(main.DB_PATH) + ".import.lock", "w") as lock:
            main.espn.fcntl.flock(lock, main.espn.fcntl.LOCK_EX | main.espn.fcntl.LOCK_NB)
            self.assertTrue(self.request("GET", "/update/status").json()["running"])
        self.assertFalse(self.request("GET", "/update/status").json()["running"])

    def seed_league_choices(self):
        store = main.espn.Store(main.DB_PATH)
        with closing(store.db) as db:
            for league in ('nfl', 'college-football', 'f1'):
                sport, name = main.espn.LEAGUES[league]
                store.upsert('leagues', {'league_id': league}, {'sport': sport, 'name': name})
                store.upsert('league_games', {'league_id': league, 'game_id': 'shared'}, {
                    'date': '2026-11-08T17:00Z', 'season': 2026, 'season_type': 2, 'stats_status': 'complete',
                    'home_team_id': '1', 'away_team_id': '2', 'home_score': 23, 'away_score': 6,
                    'event_name': 'Test Grand Prix', 'competition_type': 'race',
                })
                for key in ('1', '2'):
                    store.upsert('league_teams', {'league_id': league, 'team_id': key}, {'name': f'{name} team {key}', 'abbreviation': key})
            store.player('f1', {'id': '1', 'displayName': 'Driver One'})
            store.upsert('racing_box_scores', {'league_id': 'f1', 'game_id': 'shared', 'player_id': '1', 'team_id': '', 'category': 'race'}, {'winner': 1, 'display_order': 1})
            db.commit()

    def test_selected_league_is_supplied_to_the_model(self):
        self.seed_league_choices()
        response, requests = self.invoke([{'role': 'assistant', 'content': 'An NFL answer.'}], league='nfl')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Selected league: NFL (league_id=nfl)', requests[0]['messages'][0]['content'])

    def test_unknown_and_unloaded_leagues_are_rejected_before_model_call(self):
        self.seed_league_choices()
        with patch.object(main.httpx, 'Client') as client:
            for league in ('unknown', 'mlb'):
                self.assertEqual(self.request('POST', '/ask', json={'question': 'Who won?', 'league': league}).status_code, 422)
                self.assertEqual(self.request('GET', '/recent-games', params={'league': league}).status_code, 422)
            client.assert_not_called()

    def test_recent_results_keep_identical_ids_in_other_leagues_separate(self):
        self.seed_league_choices()
        game = self.request('GET', '/recent-games', params={'league': 'nfl'}).json()['games'][0]
        self.assertEqual(game['league_id'], 'nfl')
        self.assertEqual(game['home']['name'], 'NFL team 1')
        self.assertEqual(game['away']['score'], 6)

    def seed_nba_cards(self):
        with closing(sqlite3.connect(main.DB_PATH)) as db:
            db.executescript("""
                ALTER TABLE teams ADD COLUMN abbreviation TEXT;
                ALTER TABLE games ADD COLUMN home_team_id INTEGER;
                ALTER TABLE games ADD COLUMN away_team_id INTEGER;
                ALTER TABLE box_scores ADD COLUMN team_id INTEGER;
                ALTER TABLE box_scores ADD COLUMN rebounds INTEGER;
                ALTER TABLE box_scores ADD COLUMN assists INTEGER;
                INSERT INTO teams VALUES (1, 'Home', 'HOM'), (2, 'Away', 'AWY');
                UPDATE games SET home_team_id=1, away_team_id=2;
                INSERT INTO games VALUES (2, 2026, '2026-03-30T00:00Z', 1, 2);
                INSERT INTO box_scores VALUES
                    (1, 1, 40, 1, 8, 6), (1, 2, 30, 2, 5, 4),
                    (2, 1, 25, 1, 8, 6), (2, 2, 20, 2, 5, 4);
            """)

    def test_nba_cards_work_with_legacy_and_unpopulated_sport_tables(self):
        self.seed_nba_cards()
        original = self.request('GET', '/recent-games').json()['games']
        self.assertEqual([game['game_id'] for game in original], [1, 2])
        self.assertEqual(original[0]['home'], {'name': 'Home', 'abbreviation': 'HOM', 'score': 40})
        self.assertEqual(original[0]['top_performer'], {
            'name': 'Player One', 'team': 'HOM', 'points': 40, 'rebounds': 8, 'assists': 6,
        })
        with closing(sqlite3.connect(main.DB_PATH)) as db:
            main.espn.create_schema(db)
        self.assertEqual(self.request('GET', '/recent-games', params={'league': 'nba'}).json()['games'], original)

    def test_new_nba_imports_refresh_cards_without_duplicates_or_other_leagues(self):
        self.seed_nba_cards()
        store = main.espn.Store(main.DB_PATH)
        self.addCleanup(store.db.close)

        def import_game(league, game_id, day, points, phase=2):
            game = event(str(game_id), season=2026, phase=phase)
            game['date'] = f'2026-04-{day:02d}T00:00Z'
            for team, side, score in zip(game['competitions'][0]['competitors'], ('home', 'away'), (80, 70)):
                team['team'].update(displayName=f'{league} {side}', abbreviation=side.upper())
                team['score'] = str(score)
            store.save_game(league, game, {'id': str(phase), 'name': 'Playoffs' if phase == 3 else 'Regular Season'}, '2025-26')
            payload = summary(game_id=str(game_id), season=2026)
            for index, block in enumerate(payload['boxscore']['players']):
                group = block['statistics'][0]
                group['keys'] = ['points', 'rebounds', 'assists']
                group['athletes'][0]['athlete']['displayName'] = f'{league} player {index + 1}'
                group['athletes'][0]['stats'] = [str(points if index == 0 else 20), '10', '7']
            store.save_summary(league, str(game_id), payload)

        import_game('nba', 1, 1, 50)
        import_game('nba', 3, 2, 60, phase=3)
        import_game('wnba', 3, 3, 99)
        store.save_game('nba', event('4', season=2026), {'id': '2', 'name': 'Regular Season'}, '2025-26')
        store.db.execute("UPDATE league_games SET date='2026-04-04T00:00Z' WHERE league_id='nba' AND game_id='4'")
        store.db.commit()
        response = self.request('GET', '/recent-games', params={'league': 'nba', 'limit': 3})
        self.assertEqual(response.status_code, 200)
        games = response.json()['games']
        self.assertEqual([game['game_id'] for game in games], [3, 1, 2])
        self.assertEqual(games[0]['home'], {'name': 'nba home', 'abbreviation': 'HOME', 'score': 80})
        self.assertEqual(games[0]['away']['score'], 70)
        self.assertEqual(games[0]['top_performer'], {
            'name': 'nba player 1', 'team': 'HOME', 'points': 60, 'rebounds': 10, 'assists': 7,
        })
        self.assertEqual(games[1]['top_performer']['points'], 50)
        self.assertEqual(self.request('GET', '/recent-games', params={'limit': 2}).json()['games'], games[:2])
        self.assertIsNone(store.db.execute('SELECT 1 FROM games WHERE game_id=3').fetchone())
        import_game('nba', 3, 2, 65, phase=3)
        self.assertEqual(self.request('GET', '/recent-games').json()['games'][0]['top_performer']['points'], 65)

    def test_individual_recent_results_include_the_event_and_winner(self):
        self.seed_league_choices()
        game = self.request('GET', '/recent-games', params={'league': 'f1'}).json()['games'][0]
        self.assertEqual(game['kind'], 'event')
        self.assertEqual(game['event_name'], 'Test Grand Prix')
        self.assertEqual(game['participants'], [{'id': '1', 'name': 'Driver One', 'winner': 1}])

    def test_vite_cors(self):
        response = self.request("OPTIONS", "/ask", headers={
            "Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://localhost:5173")
        response = self.request("OPTIONS", "/ask", headers={
            "Origin": "https://example.com", "Access-Control-Request-Method": "POST",
        })
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
