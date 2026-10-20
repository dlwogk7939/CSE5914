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
                CREATE TABLE games (game_id INTEGER PRIMARY KEY, season INTEGER);
                CREATE TABLE box_scores (game_id INTEGER, player_id INTEGER, points INTEGER);
                CREATE TABLE season_totals (player_id INTEGER, season INTEGER, games_played INTEGER, ppg REAL);
                INSERT INTO players VALUES (1, 'Player One', 1), (2, 'Player Two', 2);
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

    def invoke(self, replies, question="What did Player One average in 2025-26?"):
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
            response = self.request("POST", "/ask", json={"question": question})
        return response, requests

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
