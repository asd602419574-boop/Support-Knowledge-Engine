from __future__ import annotations

import copy
import inspect
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from support_knowledge_engine import create_app
from support_knowledge_engine.db import connect_database
from support_knowledge_engine.demo_data import seed_demo_data
from support_knowledge_engine import repository
from support_knowledge_engine.repository import retrieve_with_context, search_with_context
from support_knowledge_engine.retrieval_tool import (
    DEFAULT_RETRIEVAL_DEADLINE_S,
    ERROR_INVALID_REQUEST,
    ERROR_RETRIEVAL_FAILURE,
    ERROR_RETRIEVAL_TIMEOUT,
    MAX_RESULTS,
    MAX_RETRIEVAL_DEADLINE_S,
    MAX_SNIPPET_CHARS,
    TOOL_NAME,
    TOOL_VERSION,
    execute_retrieval_tool,
)
from support_knowledge_engine.search_telemetry import (
    TELEMETRY_FAILED,
    TELEMETRY_RECORDED,
    TELEMETRY_TIMEOUT,
    TelemetryPayload,
    emit_search_telemetry,
    insert_search_log,
)
from tests.helpers import SAMPLE_DIR


_CORE_FIELDS = (
    "original_query",
    "normalized_query",
    "retrieval_query",
    "applied_rules",
    "recognized_products",
    "match_state",
    "match_state_label",
    "risk_messages",
    "results",
)
_UI_FIELDS = set(_CORE_FIELDS) | {"elapsed_ms"}
_LOG_COLUMNS = {
    "id",
    "original_query",
    "normalized_query",
    "applied_rules",
    "recognized_products",
    "match_state",
    "result_count",
    "elapsed_ms",
    "created_at",
}
_KNOWLEDGE_SQL = {
    "documents": "SELECT * FROM documents ORDER BY id",
    "pages": "SELECT id, document_id, page_number, content FROM pages ORDER BY id",
    "page_fts": """SELECT document_id, page_number, content
                   FROM page_fts ORDER BY document_id, page_number""",
    "products": "SELECT * FROM products ORDER BY id",
    "product_aliases": "SELECT * FROM product_aliases ORDER BY id",
    "document_field_values": "SELECT * FROM document_field_values ORDER BY id",
    "audit_log": "SELECT * FROM audit_log ORDER BY id",
}


def _knowledge_snapshot(connection: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    return {
        name: [tuple(row) for row in connection.execute(sql)]
        for name, sql in _KNOWLEDGE_SQL.items()
    }


def _request(query: str, **extra: object) -> dict[str, object]:
    payload: dict[str, object] = {"request_schema_version": "1", "query": query}
    payload.update(extra)
    return payload


_SLOW_RETRIEVAL_SQL = (
    "WITH RECURSIVE c(x) AS ("
    "SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 500000000"
    ") SELECT max(x) FROM c"
)
_SENSITIVE_QUERY = "云台漂移 ada@example.com +1-415-555-0199 SN-9F3K2LQ8P1"


class RetrievalToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._seed_directory = tempfile.TemporaryDirectory()
        cls._seed_database = Path(cls._seed_directory.name) / "seed.db"
        seed_demo_data(cls._seed_database, SAMPLE_DIR)
        with connect_database(cls._seed_database) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._seed_directory.cleanup()

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.database = Path(self._temporary.name) / "g2.db"
        shutil.copy(self._seed_database, self.database)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def test_tool_matches_shared_core_and_search_with_context(self) -> None:
        queries = (
            "ACM2 gimbal home sensor",
            "legacy horizon drift reset",
            "quantum toaster error Z-999",
            "Service Handbook",
            "",
        )
        with connect_database(self.database) as connection:
            product_id = str(connection.execute("SELECT id FROM products ORDER BY id LIMIT 1").fetchone()["id"])
            log_count = connection.execute("SELECT COUNT(*) FROM search_logs").fetchone()[0]
            for query in queries:
                self._assert_same_path(connection, query)
            self._assert_same_path(connection, "calibration", product_id=product_id, status="effective")
            later_count = connection.execute("SELECT COUNT(*) FROM search_logs").fetchone()[0]
        self.assertEqual(later_count - log_count, 2 * (len(queries) + 1))

    def test_limits_apply_without_second_ranking(self) -> None:
        with connect_database(self.database) as connection:
            direct = retrieve_with_context(connection, "Service Handbook")
            self.assertGreater(len(direct["results"]), 1)
            self.assertTrue(any(len(str(row["snippet"])) > 8 for row in direct["results"]))
            with patch(
                "support_knowledge_engine.repository.retrieve_with_context",
                wraps=retrieve_with_context,
            ) as spy:
                limited = execute_retrieval_tool(
                    connection,
                    _request("Service Handbook", max_results=1, max_snippet_chars=8),
                )
            self.assertEqual(spy.call_count, 1)
        self.assertTrue(limited["ok"])
        self.assertIsNone(limited["error"])
        self.assertEqual(limited["result"]["match_state"], direct["match_state"])
        self.assertEqual(len(limited["result"]["results"]), 1)
        first = limited["result"]["results"][0]
        self.assertEqual(first["id"], direct["results"][0]["id"])
        self.assertEqual(first["page_number"], direct["results"][0]["page_number"])
        self.assertEqual(first["snippet"], str(direct["results"][0]["snippet"])[:8])
        self.assertLessEqual(len(first["snippet"]), 8)
        for field in ("id", "filename", "page_number", "status", "canonical_product_name", "snippet"):
            self.assertIn(field, first)

    def test_invalid_request_does_not_retrieve(self) -> None:
        cases = (
            [],
            {"request_schema_version": "2", "query": "sensor"},
            {"request_schema_version": "1"},
            {"request_schema_version": "1", "query": None},
            {"request_schema_version": "1", "query": "x" * 2001},
            {"request_schema_version": "1", "query": "sensor", "product_id": "abc"},
            {"request_schema_version": "1", "query": "sensor", "association": "all"},
            {"request_schema_version": "1", "query": "sensor", "max_results": 0},
            {"request_schema_version": "1", "query": "sensor", "max_results": MAX_RESULTS + 1},
            {"request_schema_version": "1", "query": "sensor", "max_results": True},
            {"request_schema_version": "1", "query": "sensor", "max_snippet_chars": 0},
            {"request_schema_version": "1", "query": "sensor", "max_snippet_chars": MAX_SNIPPET_CHARS + 1},
            {"request_schema_version": "1", "query": "sensor", "evidence_id": "g3"},
        )
        with connect_database(self.database) as connection:
            before = _knowledge_snapshot(connection)
            logs_before = connection.execute("SELECT COUNT(*) FROM search_logs").fetchone()[0]
            for case in cases:
                with patch(
                    "support_knowledge_engine.repository.retrieve_with_context",
                    wraps=retrieve_with_context,
                ) as spy:
                    response = execute_retrieval_tool(connection, case)
                self.assertEqual(spy.call_count, 0, case)
                self._assert_rejected(response, ERROR_INVALID_REQUEST)
            response = execute_retrieval_tool(connection, _request("sensor"), telemetry_deadline_s=0)
            self._assert_rejected(response, ERROR_INVALID_REQUEST)
            response = execute_retrieval_tool(connection, _request("sensor"), retrieval_deadline_s=-1)
            self._assert_rejected(response, ERROR_INVALID_REQUEST)
            after = _knowledge_snapshot(connection)
            logs_after = connection.execute("SELECT COUNT(*) FROM search_logs").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(logs_before, logs_after)

    def test_retrieval_failure_is_not_telemetry(self) -> None:
        with connect_database(self.database) as connection:
            logs_before = connection.execute("SELECT COUNT(*) FROM search_logs").fetchone()[0]
            before = _knowledge_snapshot(connection)
            with patch(
                "support_knowledge_engine.repository.retrieve_with_context",
                side_effect=RuntimeError("compute failed"),
            ) as spy:
                response = execute_retrieval_tool(connection, _request("sensor"))
            after = _knowledge_snapshot(connection)
            logs_after = connection.execute("SELECT COUNT(*) FROM search_logs").fetchone()[0]
        self.assertEqual(spy.call_count, 1)
        self._assert_rejected(response, ERROR_RETRIEVAL_FAILURE)
        self.assertNotIn("telemetry", response["error"]["type"])
        self.assertEqual(before, after)
        self.assertEqual(logs_before, logs_after)

    def test_retrieval_deadline_interrupts_blocking_sql(self) -> None:
        deadline_default = inspect.signature(execute_retrieval_tool).parameters["retrieval_deadline_s"].default
        self.assertEqual(deadline_default, DEFAULT_RETRIEVAL_DEADLINE_S)
        self.assertGreater(DEFAULT_RETRIEVAL_DEADLINE_S, 0)
        self.assertLessEqual(DEFAULT_RETRIEVAL_DEADLINE_S, MAX_RETRIEVAL_DEADLINE_S)

        def slow_retrieval(connection: sqlite3.Connection, *args: object, **kwargs: object) -> dict[str, object]:
            del args, kwargs
            connection.execute(_SLOW_RETRIEVAL_SQL).fetchone()
            raise AssertionError("blocking retrieval finished instead of being interrupted")

        sink_calls: list[TelemetryPayload] = []

        def sink(payload: TelemetryPayload) -> None:
            sink_calls.append(payload)

        with connect_database(self.database) as connection:
            logs_before = connection.execute("SELECT COUNT(*) FROM search_logs").fetchone()[0]
            with patch(
                "support_knowledge_engine.repository.retrieve_with_context",
                side_effect=slow_retrieval,
            ) as spy:
                started = time.monotonic()
                response = execute_retrieval_tool(
                    connection,
                    _request("sensor"),
                    retrieval_deadline_s=0.25,
                    telemetry_sink=sink,
                )
                elapsed = time.monotonic() - started
            connection.execute("SELECT 1").fetchone()
            logs_after = connection.execute("SELECT COUNT(*) FROM search_logs").fetchone()[0]
        self.assertEqual(spy.call_count, 1)
        self.assertLess(elapsed, 2.0)
        self._assert_rejected(response, ERROR_RETRIEVAL_TIMEOUT)
        self.assertIsNone(response["telemetry"])
        self.assertEqual(sink_calls, [])
        self.assertEqual(logs_before, logs_after)

    def test_missing_search_logs_returns_one_retrieval(self) -> None:
        with connect_database(self.database) as connection:
            before = _knowledge_snapshot(connection)
            direct = retrieve_with_context(connection, "ACM2 gimbal home sensor")
            connection.execute("DROP TABLE search_logs")
            with patch(
                "support_knowledge_engine.repository.retrieve_with_context",
                wraps=retrieve_with_context,
            ) as tool_spy:
                response = execute_retrieval_tool(connection, _request("ACM2 gimbal home sensor"))
            with patch(
                "support_knowledge_engine.repository.retrieve_with_context",
                wraps=retrieve_with_context,
            ) as context_spy:
                context = search_with_context(connection, "ACM2 gimbal home sensor")
            after = _knowledge_snapshot(connection)
        self.assertEqual(tool_spy.call_count, 1)
        self.assertEqual(context_spy.call_count, 1)
        self.assertEqual(response["ok"], True)
        self.assertIsNone(response["error"])
        self.assertEqual(response["telemetry"]["status"], TELEMETRY_FAILED)
        self._assert_same_retrieval(direct, response["result"])
        self._assert_same_retrieval(direct, context)
        self.assertEqual(set(context), _UI_FIELDS)
        self.assertEqual(before, after)

    def test_telemetry_exception_keeps_result_and_one_retrieval(self) -> None:
        def explode(payload: TelemetryPayload) -> None:
            del payload
            raise RuntimeError("telemetry exploded")

        with connect_database(self.database) as connection:
            before = _knowledge_snapshot(connection)
            direct = retrieve_with_context(connection, "ACM2 gimbal home sensor")
            logs_before = connection.execute("SELECT COUNT(*) FROM search_logs").fetchone()[0]
            with patch(
                "support_knowledge_engine.repository.retrieve_with_context",
                wraps=retrieve_with_context,
            ) as spy:
                response = execute_retrieval_tool(
                    connection,
                    _request("ACM2 gimbal home sensor"),
                    telemetry_sink=explode,
                )
            logs_after = connection.execute("SELECT COUNT(*) FROM search_logs").fetchone()[0]
            after = _knowledge_snapshot(connection)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(response["ok"], True)
        self.assertIsNone(response["error"])
        self.assertEqual(response["telemetry"]["status"], TELEMETRY_FAILED)
        self._assert_same_retrieval(direct, response["result"])
        self.assertEqual(logs_before, logs_after)
        self.assertEqual(before, after)

    def test_locked_telemetry_returns_bounded_result(self) -> None:
        def locked_sink(payload: TelemetryPayload) -> None:
            holder = sqlite3.connect(self.database)
            writer = sqlite3.connect(self.database, timeout=0.2)
            try:
                holder.execute("BEGIN EXCLUSIVE")
                insert_search_log(writer, payload)
                writer.commit()
            finally:
                writer.close()
                holder.close()

        with connect_database(self.database) as connection:
            before = _knowledge_snapshot(connection)
            direct = retrieve_with_context(connection, "ACM2 gimbal home sensor")
            with patch(
                "support_knowledge_engine.repository.retrieve_with_context",
                wraps=retrieve_with_context,
            ) as spy:
                started = time.monotonic()
                response = execute_retrieval_tool(
                    connection,
                    _request("ACM2 gimbal home sensor"),
                    telemetry_sink=locked_sink,
                    telemetry_deadline_s=2.0,
                )
                elapsed = time.monotonic() - started
            after = _knowledge_snapshot(connection)
        self.assertEqual(spy.call_count, 1)
        self.assertLess(elapsed, 2.5)
        self.assertEqual(response["ok"], True)
        self.assertIsNone(response["error"])
        self.assertIn(response["telemetry"]["status"], {TELEMETRY_FAILED, TELEMETRY_TIMEOUT})
        self._assert_same_retrieval(direct, response["result"])
        self.assertEqual(before, after)

    def test_blocking_telemetry_returns_within_bound(self) -> None:
        release = threading.Event()

        late = {"ran": False}

        def blocking_sink(payload: TelemetryPayload) -> None:
            del payload
            release.wait(30)
            late["ran"] = True

        try:
            with connect_database(self.database) as connection:
                before = _knowledge_snapshot(connection)
                direct = retrieve_with_context(connection, "ACM2 gimbal home sensor")
                with patch(
                    "support_knowledge_engine.repository.retrieve_with_context",
                    wraps=retrieve_with_context,
                ) as spy:
                    started = time.monotonic()
                    response = execute_retrieval_tool(
                        connection,
                        _request("ACM2 gimbal home sensor"),
                        telemetry_sink=blocking_sink,
                        telemetry_deadline_s=0.25,
                    )
                    elapsed = time.monotonic() - started
                connection.execute("SELECT 1").fetchone()
                after = _knowledge_snapshot(connection)
                frozen_response = copy.deepcopy(response)
            self.assertEqual(spy.call_count, 1)
            self.assertLess(elapsed, 2.0)
            self.assertEqual(response["ok"], True)
            self.assertIsNone(response["error"])
            self.assertEqual(response["telemetry"]["status"], TELEMETRY_TIMEOUT)
            self._assert_same_retrieval(direct, response["result"])
            self.assertEqual(before, after)
            self.assertFalse(late["ran"])
        finally:
            release.set()
        deadline = time.monotonic() + 1.0
        while not late["ran"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(late["ran"])
        self.assertEqual(response, frozen_response)

    def test_default_sink_lock_is_bounded_without_another_retrieval(self) -> None:
        with connect_database(self.database) as connection:
            with patch(
                "support_knowledge_engine.repository.retrieve_with_context",
                wraps=retrieve_with_context,
            ) as spy:
                result = repository.retrieve_with_context(connection, "ACM2 gimbal home sensor")
                connection.execute("PRAGMA busy_timeout = 200")
                holder = sqlite3.connect(self.database)
                holder.execute("BEGIN EXCLUSIVE")
                started = time.monotonic()
                try:
                    report = emit_search_telemetry(connection, result, deadline_s=0.2)
                finally:
                    holder.close()
                elapsed = time.monotonic() - started
            self.assertEqual(spy.call_count, 1)
            preserved = retrieve_with_context(connection, "ACM2 gimbal home sensor")
        self.assertLess(elapsed, 2.0)
        self.assertIn(report.status, {TELEMETRY_FAILED, TELEMETRY_TIMEOUT})
        self._assert_same_retrieval(result, preserved)

    def test_success_is_read_only_and_logs_exclude_page_text(self) -> None:
        with connect_database(self.database) as connection:
            before = _knowledge_snapshot(connection)
            tables_before = self._table_names(connection)
            response = execute_retrieval_tool(connection, _request("ACM2 gimbal home sensor"))
            after = _knowledge_snapshot(connection)
            tables_after = self._table_names(connection)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(search_logs)")}
            log = connection.execute("SELECT * FROM search_logs ORDER BY id DESC LIMIT 1").fetchone()
            schema_version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        self.assertEqual(response["ok"], True)
        self.assertEqual(response["telemetry"]["status"], TELEMETRY_RECORDED)
        self.assertEqual(response["tool_name"], TOOL_NAME)
        self.assertEqual(response["tool_version"], TOOL_VERSION)
        self.assertEqual(before, after)
        self.assertEqual(tables_before, tables_after)
        self.assertEqual(columns, _LOG_COLUMNS)
        self.assertEqual(schema_version, 5)
        stored = " ".join(str(log[name]) for name in log.keys())
        self.assertNotIn("ACM2 gimbal home sensor", stored)
        self.assertEqual(response["result"]["original_query"], "ACM2 gimbal home sensor")
        for row in response["result"]["results"]:
            snippet = str(row["snippet"])
            if len(snippet) > 24:
                self.assertNotIn(snippet, stored)

    def test_class_c_log_omits_raw_sensitive_query(self) -> None:
        with connect_database(self.database) as connection:
            response = execute_retrieval_tool(connection, _request(_SENSITIVE_QUERY))
            log = connection.execute("SELECT * FROM search_logs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(response["ok"], True)
        self.assertEqual(response["result"]["original_query"], _SENSITIVE_QUERY)
        self.assertNotEqual(response["result"]["normalized_query"], "")
        stored = " ".join(str(log[name]) for name in log.keys())
        for secret in (
            _SENSITIVE_QUERY,
            str(response["result"]["normalized_query"]),
            "ada@example.com",
            "+1-415-555-0199",
            "415-555-0199",
            "1-415-555-0199",
            "SN-9F3K2LQ8P1",
        ):
            self.assertNotIn(secret, stored)

    def test_custom_sink_consumes_payload_without_caller_connection(self) -> None:
        seen: list[TelemetryPayload] = []

        def sink(payload: TelemetryPayload) -> None:
            seen.append(payload)

        with connect_database(self.database) as connection:
            direct = retrieve_with_context(connection, "ACM2 gimbal home sensor")
            with patch(
                "support_knowledge_engine.repository.retrieve_with_context",
                wraps=retrieve_with_context,
            ) as spy:
                response = execute_retrieval_tool(
                    connection,
                    _request("ACM2 gimbal home sensor"),
                    telemetry_sink=sink,
                )
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(response["telemetry"]["status"], TELEMETRY_RECORDED)
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], TelemetryPayload)
        self.assertEqual(seen[0].match_state, direct["match_state"])
        self.assertEqual(seen[0].result_count, len(direct["results"]))
        self.assertNotIn("ACM2 gimbal home sensor", seen[0].query_representation)
        self.assertNotIn(str(direct["normalized_query"]), seen[0].normalized_representation)
        self._assert_same_retrieval(direct, response["result"])

    def test_flask_query_uses_shared_core_once(self) -> None:
        with connect_database(self.database) as connection:
            direct = search_with_context(connection, "ACM2 gimbal home sensor")
        app = create_app({"DATABASE": str(self.database), "TESTING": True})
        with patch(
            "support_knowledge_engine.repository.retrieve_with_context",
            wraps=retrieve_with_context,
        ) as spy:
            response = app.test_client().get("/?q=ACM2+gimbal+home+sensor")
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("高可信匹配", body)
        self.assertIn(str(direct["results"][0]["filename"]), body)
        self.assertEqual(direct["match_state"], "high_confidence")

    def _assert_same_path(
        self,
        connection: sqlite3.Connection,
        query: str,
        product_id: str = "",
        status: str = "",
    ) -> None:
        context = search_with_context(
            connection,
            query,
            status=status,
            product_id=product_id,
        )
        with patch(
            "support_knowledge_engine.repository.retrieve_with_context",
            wraps=retrieve_with_context,
        ) as spy:
            response = execute_retrieval_tool(
                connection,
                _request(query, product_id=product_id, status=status),
            )
        self.assertEqual(spy.call_count, 1, query)
        self.assertEqual(set(context), _UI_FIELDS)
        self.assertTrue(response["ok"], query)
        self.assertIsNone(response["error"], query)
        self.assertEqual(response["telemetry"]["status"], TELEMETRY_RECORDED, query)
        self._assert_same_retrieval(context, response["result"])

    def _assert_same_retrieval(self, left: dict[str, object], right: dict[str, object]) -> None:
        for field in _CORE_FIELDS:
            self.assertEqual(left[field], right[field], field)
        self.assertIsInstance(left["elapsed_ms"], float)
        self.assertIsInstance(right["elapsed_ms"], float)

    def _assert_rejected(self, response: dict[str, object], error_type: str) -> None:
        self.assertFalse(response["ok"])
        self.assertIsNone(response["result"])
        self.assertIsNone(response["telemetry"])
        self.assertEqual(response["error"]["type"], error_type)
        self.assertEqual(response["tool_name"], TOOL_NAME)
        self.assertEqual(response["tool_version"], TOOL_VERSION)

    def _table_names(self, connection: sqlite3.Connection) -> set[str]:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


if __name__ == "__main__":
    unittest.main()
