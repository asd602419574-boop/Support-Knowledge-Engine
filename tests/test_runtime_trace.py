from __future__ import annotations

import ast
import hashlib
import inspect
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from support_knowledge_engine.backup import create_backup, restore_backup, verify_backup
from support_knowledge_engine.db import connect_database, init_database
from support_knowledge_engine.evidence import (
    EvidenceDecision,
    PACKET_SCHEMA_VERSION,
    decide_evidence,
)
from support_knowledge_engine.governance import add_product_alias, create_product
from support_knowledge_engine.migrations import (
    MIGRATION_004_DATA_LOSS,
    MIGRATIONS,
    _migration_004_runtime_trace,
    current_schema_version,
)
from support_knowledge_engine.repository import retrieve_with_context
from support_knowledge_engine.retrieval_tool import execute_retrieval_tool
from support_knowledge_engine.runtime import (
    RUNTIME_REQUEST_SCHEMA_VERSION,
    RuntimeCompositionError,
    RuntimeFailure,
    RuntimeRequest,
    RuntimeResult,
    run_runtime,
)
from support_knowledge_engine.trace import (
    REDACTION_REFUSAL,
    REDACTION_REFUSAL_MESSAGE,
    TRACE_NOT_DURABLE,
    TRACE_NOT_DURABLE_MESSAGE,
    TRACE_WRITE_FAILURE,
    TRACE_WRITE_FAILURE_MESSAGE,
    TracePersistenceError,
    commit_class_c_trace,
    execute_traced_runtime,
    project_class_c_record,
)
from tests.helpers import PROJECT_ROOT


PHRASE = "calibration beacon zz-17"
ORIGINAL_PAGE = f"{PHRASE} service step"
QUERY = f"ACM2 {PHRASE}"
SECRET_EMAIL = "ada@example.com"
SECRET_PHONE = "+1-415-555-0199"
SECRET_SN = "SN-9F3K2LQ8P1"
SECRETS = (SECRET_EMAIL, SECRET_PHONE, SECRET_SN)
SENSITIVE_PAGE = f"{ORIGINAL_PAGE} {SECRET_EMAIL} {SECRET_PHONE} {SECRET_SN}"
_SLOW_RETRIEVAL_SQL = (
    "WITH RECURSIVE c(x) AS ("
    "SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 500000000"
    ") SELECT max(x) FROM c"
)
_KNOWLEDGE_SQL = {
    "documents": "SELECT * FROM documents ORDER BY id",
    "pages": "SELECT id, document_id, page_number, content FROM pages ORDER BY id",
    "page_fts": """SELECT document_id, page_number, content
                   FROM page_fts ORDER BY document_id, page_number""",
    "products": "SELECT * FROM products ORDER BY id",
    "product_aliases": "SELECT * FROM product_aliases ORDER BY id",
    "document_field_values": "SELECT * FROM document_field_values ORDER BY id",
    "audit_log": "SELECT * FROM audit_log ORDER BY id",
    "search_logs": "SELECT * FROM search_logs ORDER BY id",
}
_MIGRATION_NAMES = (
    (1, "phase 1 baseline"),
    (2, "knowledge governance and lifecycle"),
    (3, "controlled corpus acquisition and search observability"),
    (4, "runtime trace"),
)


class TraceContractTests(unittest.TestCase):
    def test_trace_boundary_has_no_model_or_second_retrieval(self) -> None:
        source = (PROJECT_ROOT / "support_knowledge_engine" / "trace.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: list[str] = []
        calls: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module.split(".")[0])
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    calls.append(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    calls.append(node.func.attr)
        self.assertNotIn("retrieval_tool", imported)
        for name in ("openai", "anthropic", "xai", "grok", "httpx", "aiohttp", "flask"):
            self.assertNotIn(name, imported)
        self.assertEqual(calls.count("run_runtime"), 1)
        for name in (
            "execute_retrieval_tool",
            "retrieve_with_context",
            "decide_evidence",
            "capture_evidence_packet",
            "normalize_query",
            "insert_search_log",
        ):
            self.assertNotIn(name, calls)
        for token in (
            "knowledge_store_retrieval",
            "TOOL_NAME",
            "TOOL_VERSION",
            "search_logs",
            "case_id",
            "openai",
            "anthropic",
            "xai",
            "grok",
            "httpx",
            "aiohttp",
            "FROM pages",
            "FROM documents",
            "max_steps",
            "budget_exhausted",
            "provider_error",
            "UPDATE ",
            "DELETE ",
        ):
            self.assertNotIn(token, source, token)
        self.assertEqual(source.count("INSERT INTO runtime_traces"), 1)
        for relative in ("support_knowledge_engine/routes.py", "support_knowledge_engine/__init__.py"):
            text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("execute_traced_runtime", text)
            self.assertNotIn("runtime_traces", text)


class TraceBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "g5.db"
        init_database(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_supported_trace_matches_packet_evidence_ids(self) -> None:
        self._write_case()
        execution, row, tool, core, decide, runtime_spy = self._run(_request())
        self._assert_counts(tool, core, decide, runtime_spy, tool_count=1, core_count=1, decide_count=1)
        packet = execution.runtime.packet
        decision = execution.runtime.decision
        assert packet is not None and decision is not None
        self.assertEqual(row["termination_reason"], "supported")
        self.assertEqual(row["tool_name"], packet.retrieval_tool_name)
        self.assertEqual(row["tool_name"], packet.evidence[0].retrieval_tool_name)
        self.assertEqual(row["tool_version"], packet.retrieval_tool_version)
        self.assertEqual(row["request_schema_version"], packet.request.request_schema_version)
        self.assertEqual(row["response_schema_version"], packet.retrieval_response_schema_version)
        self.assertEqual(json.loads(row["evidence_ids"]), list(decision.evidence_ids))
        self.assertEqual(
            json.loads(row["evidence_ids"]),
            [item.evidence_id for item in packet.evidence],
        )
        with connect_database(self.database) as connection:
            names = {
                item[0]
                for item in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        self.assertIn("runtime_traces", names)
        self.assertNotIn("cases", names)
        self.assertFalse(any(name.startswith("case_") for name in names))

    def test_abstain_trace_records_termination(self) -> None:
        self._write_case(firmware_range="1.2.3")
        execution, row, tool, core, decide, runtime_spy = self._run(_request(firmware_version="1.2.3"))
        self._assert_counts(tool, core, decide, runtime_spy, tool_count=1, core_count=1, decide_count=1)
        self.assertEqual(row["termination_reason"], "abstain")
        assert execution.runtime.packet is not None and execution.runtime.decision is not None
        self.assertEqual(execution.runtime.packet.retrieval_state, "high_confidence")
        self.assertNotEqual(row["termination_reason"], "supported")
        self.assertTrue(row["tool_name"])

    def test_conflict_trace_records_termination(self) -> None:
        with connect_database(self.database) as connection:
            product_id = _product(connection, "AeroCam Mini 2", "ACM2")
            _document(connection, product_id, "current.pdf", ORIGINAL_PAGE, status="effective")
            _document(connection, product_id, "old.pdf", ORIGINAL_PAGE, status="superseded")
        execution, row, tool, core, decide, runtime_spy = self._run(_request())
        self._assert_counts(tool, core, decide, runtime_spy, tool_count=1, core_count=1, decide_count=1)
        self.assertEqual(row["termination_reason"], "conflict")
        decision = json.loads(row["decision_json"])
        assert execution.runtime.decision is not None and execution.runtime.packet is not None
        self.assertEqual(decision["reason_codes"], list(execution.runtime.decision.reason_codes))
        self.assertEqual(
            json.loads(row["evidence_ids"]),
            [item.evidence_id for item in execution.runtime.packet.evidence],
        )

    def test_retrieval_failure_trace_is_not_abstain(self) -> None:
        self._write_case()
        execution, row, tool, core, decide, runtime_spy = self._run(
            _request(),
            core_side_effect=RuntimeError("compute failed"),
        )
        self._assert_counts(tool, core, decide, runtime_spy, tool_count=1, core_count=1, decide_count=0)
        self.assertFalse(execution.runtime.ok)
        self.assertIsNone(execution.runtime.packet)
        self.assertIsNone(execution.runtime.decision)
        self.assertEqual(row["termination_reason"], "retrieval_failure")
        self.assertNotEqual(row["termination_reason"], "abstain")
        self.assertIsNone(row["tool_name"])
        self.assertIsNone(row["tool_version"])
        self.assertIsNone(row["request_schema_version"])
        self.assertIsNone(row["response_schema_version"])

    def test_timeout_trace_records_termination(self) -> None:
        self._write_case()

        def slow(connection, *args, **kwargs):
            del args, kwargs
            connection.execute(_SLOW_RETRIEVAL_SQL).fetchone()
            raise AssertionError("blocking retrieval finished instead of being interrupted")

        started = time.monotonic()
        execution, row, tool, core, decide, runtime_spy = self._run(
            _request(),
            deadline=0.25,
            core_side_effect=slow,
        )
        self.assertLess(time.monotonic() - started, 2.0)
        self._assert_counts(tool, core, decide, runtime_spy, tool_count=1, core_count=1, decide_count=0)
        self.assertEqual(row["termination_reason"], "retrieval_timeout")
        self.assertNotEqual(row["termination_reason"], "abstain")
        self.assertIsNone(execution.runtime.decision)
        self.assertGreaterEqual(row["latency_ms"], 0.0)
        self.assertLess(row["latency_ms"], 2000.0)

    def test_invalid_request_trace_does_not_call_core(self) -> None:
        self._write_case()
        execution, row, tool, core, decide, runtime_spy = self._run(_request(product_id="abc"))
        self._assert_counts(tool, core, decide, runtime_spy, tool_count=1, core_count=0, decide_count=0)
        self.assertEqual(row["termination_reason"], "invalid_request")
        self.assertIsNone(execution.runtime.packet)
        self.assertIsNone(execution.runtime.decision)
        self.assertIsNone(row["tool_name"])
        self.assertIsNone(row["tool_version"])

    def test_non_string_firmware_trace_does_not_call_tool(self) -> None:
        self._write_case()
        request = replace(_request(), firmware_version=1)  # type: ignore[arg-type]
        execution, row, tool, core, decide, runtime_spy = self._run(request)
        self._assert_counts(tool, core, decide, runtime_spy, tool_count=0, core_count=0, decide_count=0)
        self.assertEqual(row["termination_reason"], "invalid_request")
        self.assertIsNone(execution.runtime.packet)
        self.assertIsNone(execution.runtime.decision)
        self.assertIsNone(row["tool_name"])
        payload = json.loads(row["input_json"])
        self.assertIsNone(payload["firmware_version"])
        self.assertNotIn("1", payload["firmware_version"] or "")

    def test_source_index_mismatch_trace(self) -> None:
        self._write_case()
        divergent = "完全不同但非空的页面"
        with connect_database(self.database) as connection:
            connection.execute("UPDATE pages SET content = ?", (divergent,))
        execution, row, tool, core, decide, runtime_spy = self._run(_request())
        self._assert_counts(tool, core, decide, runtime_spy, tool_count=1, core_count=1, decide_count=0)
        self.assertEqual(row["termination_reason"], "source_index_mismatch")
        self.assertIsNone(execution.runtime.packet)
        self.assertIsNone(execution.runtime.decision)
        self.assertNotEqual(row["termination_reason"], "abstain")
        self.assertNotIn(divergent, _stored(row))

    def test_sensitive_fixture_is_not_stored_raw(self) -> None:
        self._write_case(content=SENSITIVE_PAGE)
        execution, row, tool, core, decide, runtime_spy = self._run(
            _request(firmware_version=SECRET_PHONE)
        )
        self._assert_counts(tool, core, decide, runtime_spy, tool_count=1, core_count=1, decide_count=1)
        packet = execution.runtime.packet
        assert packet is not None and execution.runtime.decision is not None
        visible = packet.evidence[0].supporting_original_text
        for secret in SECRETS:
            self.assertIn(secret, visible)
        self.assertEqual(packet.request.firmware_version, SECRET_PHONE)
        stored = _stored(row)
        for secret in (*SECRETS, QUERY, SENSITIVE_PAGE, visible):
            self.assertNotIn(secret, stored)
        self.assertIn("redacted:", row["input_json"])
        self.assertEqual(
            json.loads(row["evidence_ids"]),
            [item.evidence_id for item in packet.evidence],
        )
        with connect_database(self.database) as connection:
            logs = " ".join(
                " ".join(str(value) for value in log)
                for log in connection.execute("SELECT * FROM search_logs")
            )
        for secret in (*SECRETS, QUERY, SENSITIVE_PAGE):
            self.assertNotIn(secret, logs)

    def test_exception_message_secrets_are_markers(self) -> None:
        message = f"failed {SECRET_EMAIL} {SECRET_PHONE} {SECRET_SN}"
        result = RuntimeResult(
            ok=False,
            runtime_version="1",
            runtime_response_schema_version="1",
            request=RuntimeRequest(request_schema_version="1", query=QUERY),
            packet=None,
            decision=None,
            error=RuntimeFailure(type="retrieval_failure", message=message),
        )
        record = project_class_c_record(
            result,
            1.25,
            run_id="ab" * 16,
            created_at="2026-01-01T00:00:00+00:00",
        )
        with connect_database(self.database) as connection:
            commit_class_c_trace(connection, record)
            row = connection.execute("SELECT * FROM runtime_traces").fetchone()
        self.assertIsNotNone(row)
        assert row is not None
        stored = _stored(row)
        for secret in (*SECRETS, message, QUERY):
            self.assertNotIn(secret, stored)
        decision = json.loads(row["decision_json"])
        self.assertTrue(decision["error"]["message"].startswith("redacted:"))
        self.assertEqual(row["termination_reason"], "retrieval_failure")
        self.assertAlmostEqual(row["latency_ms"], 1.25)

    def test_update_and_delete_are_rejected(self) -> None:
        self._write_case()
        _execution, row, *_rest = self._run(_request())
        with connect_database(self.database) as connection:
            stored = connection.execute("SELECT * FROM runtime_traces").fetchone()
            assert stored is not None
            columns = list(stored.keys())
            values = tuple(stored[name] for name in columns)
            for statement in (
                "UPDATE runtime_traces SET latency_ms = latency_ms",
                "DELETE FROM runtime_traces",
            ):
                with self.assertRaises(sqlite3.IntegrityError) as caught:
                    connection.execute(statement)
                self.assertIn("runtime_traces is append-only", str(caught.exception))
                connection.rollback()
            placeholders = ", ".join("?" for _ in columns)
            names = ", ".join(columns)
            with self.assertRaises(sqlite3.IntegrityError) as caught:
                connection.execute(
                    f"INSERT OR REPLACE INTO runtime_traces ({names}) VALUES ({placeholders})",
                    values,
                )
            self.assertIn("runtime_traces is append-only", str(caught.exception))
            connection.rollback()
            remaining = connection.execute("SELECT run_id FROM runtime_traces").fetchall()
        self.assertEqual([item["run_id"] for item in remaining], [row["run_id"]])

    def test_trace_write_failure_keeps_runtime_decision(self) -> None:
        self._write_case()
        with connect_database(self.database) as connection:
            connection.execute(
                """CREATE TRIGGER runtime_traces_block_insert
                   BEFORE INSERT ON runtime_traces
                   BEGIN
                       SELECT RAISE(ABORT, 'blocked');
                   END"""
            )
            connection.commit()
            execution = execute_traced_runtime(connection, _request())
            count = connection.execute("SELECT COUNT(*) FROM runtime_traces").fetchone()[0]
        self.assertFalse(execution.trace_ok)
        self.assertIsNone(execution.run_id)
        self.assertIsNotNone(execution.error)
        assert execution.error is not None
        self.assertEqual(execution.error.type, TRACE_WRITE_FAILURE)
        self.assertEqual(execution.error.message, TRACE_WRITE_FAILURE_MESSAGE)
        self.assertNotIn("blocked", execution.error.message)
        self.assertTrue(execution.runtime.ok)
        assert execution.runtime.decision is not None
        self.assertEqual(execution.runtime.decision.decision_type, "supported")
        self.assertNotEqual(execution.runtime.decision.decision_type, "abstain")
        self.assertEqual(count, 0)

    def test_caller_transaction_is_not_reported_as_durable(self) -> None:
        self._write_case()
        with connect_database(self.database) as connection:
            connection.execute("BEGIN")
            execution = execute_traced_runtime(connection, _request())
            self.assertTrue(connection.in_transaction)
            self.assertFalse(execution.trace_ok)
            self.assertIsNone(execution.run_id)
            assert execution.error is not None
            self.assertEqual(execution.error.type, TRACE_NOT_DURABLE)
            self.assertEqual(execution.error.message, TRACE_NOT_DURABLE_MESSAGE)
            self.assertTrue(execution.runtime.ok)
            assert execution.runtime.decision is not None
            self.assertEqual(execution.runtime.decision.decision_type, "supported")
            self.assertNotEqual(execution.runtime.decision.decision_type, "abstain")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM runtime_traces").fetchone()[0],
                0,
            )
            connection.rollback()
            self.assertFalse(connection.in_transaction)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM runtime_traces").fetchone()[0],
                0,
            )
        with connect_database(self.database) as connection:
            connection.execute("BEGIN")
            execution = execute_traced_runtime(connection, _request())
            self.assertFalse(execution.trace_ok)
            self.assertIsNone(execution.run_id)
            connection.commit()
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM runtime_traces").fetchone()[0],
                0,
            )
        execution, row, *_rest = self._run(_request())
        self.assertTrue(execution.trace_ok)
        self.assertEqual(row["run_id"], execution.run_id)

    def test_unverified_free_input_is_not_stored_raw(self) -> None:
        product_id = str(self._write_case())
        serial = "9F3K2LQ8"
        ticket = "WO-9912"
        short_ticket = "T12345"
        mixed = "A1B2C3D4"
        request = _request(
            product_id=product_id,
            product_series=serial,
            document_type=short_ticket,
            status=mixed,
            association=ticket,
        )
        with connect_database(self.database) as connection:
            execution = execute_traced_runtime(connection, request)
            row = connection.execute("SELECT * FROM runtime_traces").fetchone()
        self.assertTrue(execution.trace_ok, execution.error)
        runtime_request = execution.runtime.request
        assert isinstance(runtime_request, RuntimeRequest)
        self.assertEqual(runtime_request.product_series, serial)
        self.assertEqual(runtime_request.document_type, short_ticket)
        self.assertEqual(runtime_request.status, mixed)
        self.assertEqual(runtime_request.association, ticket)
        self.assertEqual(runtime_request.product_id, product_id)
        assert execution.runtime.error is not None
        self.assertEqual(execution.runtime.error.type, "invalid_request")
        self.assertIsNone(execution.runtime.decision)
        assert row is not None
        stored = _stored(row)
        for secret in (serial, ticket, short_ticket, mixed):
            self.assertNotIn(secret, stored)
        payload = json.loads(row["input_json"])
        for field in ("product_series", "document_type", "status", "association", "product_id"):
            self.assertTrue(str(payload[field]).startswith("redacted:"), payload[field])
        self.assertNotEqual(payload["product_id"], product_id)
        trusted, trusted_row, *_rest = self._run(
            _request(product_id=product_id, status="effective", association="linked")
        )
        trusted_payload = json.loads(trusted_row["input_json"])
        self.assertEqual(trusted_payload["status"], "effective")
        self.assertEqual(trusted_payload["association"], "linked")
        self.assertTrue(str(trusted_payload["product_id"]).startswith("redacted:"))
        self.assertNotEqual(trusted_payload["product_id"], product_id)
        self.assertEqual(trusted_payload["product_series"], "")
        self.assertEqual(trusted_payload["document_type"], "")
        packet = trusted.runtime.packet
        assert packet is not None and packet.evidence
        self.assertTrue(packet.evidence[0].evidence_id.startswith("ev1-"))
        self.assertEqual(
            json.loads(trusted_row["evidence_ids"]),
            [item.evidence_id for item in packet.evidence],
        )

    def test_unverified_numeric_product_id_is_not_stored_raw(self) -> None:
        real_id = str(self._write_case())
        phone_like = "13800138000"
        leaked = _request(product_id=phone_like)
        with connect_database(self.database) as connection:
            execution = execute_traced_runtime(connection, leaked)
            row = connection.execute("SELECT * FROM runtime_traces").fetchone()
        self.assertTrue(execution.trace_ok, execution.error)
        self.assertIsNone(execution.error)
        runtime_request = execution.runtime.request
        assert isinstance(runtime_request, RuntimeRequest)
        self.assertEqual(runtime_request.product_id, phone_like)
        assert execution.runtime.decision is not None
        self.assertEqual(execution.runtime.decision.decision_type, "abstain")
        self.assertIsNone(execution.runtime.error)
        assert row is not None
        self.assertEqual(row["termination_reason"], "abstain")
        self.assertNotIn(phone_like, _stored(row))
        leaked_payload = json.loads(row["input_json"])
        self.assertTrue(str(leaked_payload["product_id"]).startswith("redacted:"))
        self.assertNotIn(phone_like, leaked_payload["product_id"])

        real_request = _request(product_id=real_id)
        with connect_database(self.database) as connection:
            direct = run_runtime(connection, real_request)
            traced = execute_traced_runtime(connection, real_request)
            traced_row = connection.execute(
                "SELECT * FROM runtime_traces WHERE run_id = ?",
                (traced.run_id,),
            ).fetchone()
        self.assertTrue(direct.ok)
        self.assertTrue(traced.trace_ok, traced.error)
        self.assertIsNone(traced.error)
        assert traced.runtime.decision is not None and direct.decision is not None
        self.assertEqual(traced.runtime.request.product_id, real_id)
        self.assertEqual(traced.runtime.decision.decision_type, direct.decision.decision_type)
        self.assertEqual(traced.runtime.decision.decision_type, "supported")
        self.assertEqual(
            list(traced.runtime.decision.evidence_ids),
            list(direct.decision.evidence_ids),
        )
        assert traced_row is not None
        self.assertEqual(
            json.loads(traced_row["evidence_ids"]),
            list(direct.decision.evidence_ids),
        )
        self.assertTrue(str(direct.decision.evidence_ids[0]).startswith("ev1-"))
        traced_payload = json.loads(traced_row["input_json"])
        self.assertTrue(str(traced_payload["product_id"]).startswith("redacted:"))
        self.assertNotEqual(traced_payload["product_id"], real_id)

    def test_unsafe_payload_is_not_inserted(self) -> None:
        self._write_case()
        with connect_database(self.database) as connection:
            result = run_runtime(connection, _request())
            bad_decision = replace(result.decision, decision_type="maybe")  # type: ignore[arg-type]
            with self.assertRaises(TracePersistenceError) as caught:
                project_class_c_record(replace(result, decision=bad_decision), 0.0)
            self.assertEqual(caught.exception.error_type, REDACTION_REFUSAL)
            self.assertEqual(str(caught.exception), REDACTION_REFUSAL_MESSAGE)
            good = project_class_c_record(
                result,
                0.0,
                run_id="cd" * 16,
                created_at="2026-01-02T00:00:00+00:00",
            )
            commit_class_c_trace(connection, good)
            unsafe = (
                replace(good, input_payload={**good.input_payload, "query": SECRET_EMAIL}),
                replace(
                    good,
                    output_payload={
                        **good.output_payload,
                        "evidence": [{"note": SECRET_EMAIL}],
                    },
                ),
                replace(
                    good,
                    output_payload={
                        **good.output_payload,
                        "evidence": [
                            {
                                **dict(good.output_payload["evidence"][0]),  # type: ignore[index]
                                "supporting_original_text": "redacted:0123456789ab:chars=1",
                            }
                        ],
                    },
                ),
                replace(good, termination_reason="maybe"),
            )
            for record in unsafe:
                with self.assertRaises(TracePersistenceError) as caught:
                    commit_class_c_trace(connection, record)
                self.assertEqual(caught.exception.error_type, REDACTION_REFUSAL)
                self.assertNotIn(SECRET_EMAIL, str(caught.exception))
                self.assertNotIn("supporting_original_text", str(caught.exception))
            rows = list(connection.execute("SELECT * FROM runtime_traces"))
        self.assertEqual(len(rows), 1)
        stored = _stored(rows[0])
        self.assertNotIn(SECRET_EMAIL, stored)
        self.assertNotIn(QUERY, stored)

    def test_g4_runtime_does_not_write_trace(self) -> None:
        self._write_case()
        with connect_database(self.database) as connection:
            before = _knowledge(connection)
            before_count = connection.execute("SELECT COUNT(*) FROM runtime_traces").fetchone()[0]
            with _spies({"wraps": retrieve_with_context}) as (tool, core, decide, runtime_spy):
                result = run_runtime(connection, _request())
            after = _knowledge(connection)
            after_count = connection.execute("SELECT COUNT(*) FROM runtime_traces").fetchone()[0]
        self.assertTrue(result.ok)
        self.assertEqual(before, after)
        self.assertEqual(before_count, 0)
        self.assertEqual(after_count, 0)
        self.assertEqual(runtime_spy.call_count, 0)
        self.assertEqual((tool.call_count, core.call_count, decide.call_count), (1, 1, 1))

    def test_composition_error_is_not_persisted(self) -> None:
        self._write_case()
        foreign = EvidenceDecision(
            decision_type="supported",
            reason_codes=(),
            retrieval_state="high_confidence",
            evidence_ids=("ev1-" + ("ab" * 32),),
            packet_schema_version=PACKET_SCHEMA_VERSION,
        )
        with connect_database(self.database) as connection:
            with _spies({"wraps": retrieve_with_context}) as (tool, core, decide, runtime_spy):
                decide.side_effect = lambda _packet: foreign
                with self.assertRaises(RuntimeCompositionError):
                    execute_traced_runtime(connection, _request())
            count = connection.execute("SELECT COUNT(*) FROM runtime_traces").fetchone()[0]
        self.assertEqual(count, 0)
        self.assertEqual(runtime_spy.call_count, 1)
        self.assertEqual((tool.call_count, core.call_count, decide.call_count), (1, 1, 1))

    def _write_case(self, *, firmware_range: str = "", content: str = ORIGINAL_PAGE) -> int:
        with connect_database(self.database) as connection:
            product_id = _product(connection, "AeroCam Mini 2", "ACM2")
            _document(
                connection,
                product_id,
                "handbook.pdf",
                content,
                firmware_range=firmware_range,
            )
        return product_id

    def _run(self, request, *, deadline=None, core_side_effect=None):
        core_kwargs = (
            {"side_effect": core_side_effect}
            if core_side_effect is not None
            else {"wraps": retrieve_with_context}
        )
        with connect_database(self.database) as connection:
            before = _knowledge(connection)
            before_count = connection.execute("SELECT COUNT(*) FROM runtime_traces").fetchone()[0]
            with _spies(core_kwargs) as (tool, core, decide, runtime_spy):
                arguments = {}
                if deadline is not None:
                    arguments["retrieval_deadline_s"] = deadline
                execution = execute_traced_runtime(connection, request, **arguments)
            after = _knowledge(connection)
            rows = list(connection.execute("SELECT * FROM runtime_traces ORDER BY id"))
        self.assertEqual(before, after)
        self.assertTrue(execution.trace_ok, execution.error)
        self.assertIsNone(execution.error)
        self.assertEqual(len(rows), before_count + 1)
        row = rows[-1]
        self.assertEqual(row["run_id"], execution.run_id)
        self._assert_row(row, execution)
        return execution, row, tool, core, decide, runtime_spy

    def _assert_counts(self, tool, core, decide, runtime_spy, *, tool_count, core_count, decide_count) -> None:
        self.assertEqual(
            (runtime_spy.call_count, tool.call_count, core.call_count, decide.call_count),
            (1, tool_count, core_count, decide_count),
        )
        self.assertLessEqual(tool_count, 1)
        self.assertLessEqual(core_count, 1)
        self.assertLessEqual(decide_count, 1)

    def _assert_row(self, row, execution) -> None:
        runtime = execution.runtime
        self.assertEqual(row["step_id"], "1")
        self.assertEqual(row["trace_schema_version"], "1")
        self.assertGreaterEqual(row["latency_ms"], 0.0)
        self.assertEqual(row["runtime_version"], runtime.runtime_version)
        self.assertEqual(row["runtime_response_schema_version"], runtime.runtime_response_schema_version)
        stored = _stored(row)
        self.assertNotIn("supporting_original_text", stored)
        self.assertNotIn("decision_visible_representation", stored)
        self.assertNotIn("original_query", stored)
        self.assertNotIn("answer_text", stored)
        request = runtime.request
        if isinstance(request, RuntimeRequest):
            self.assertEqual(row["runtime_request_schema_version"], request.request_schema_version)
            payload = json.loads(row["input_json"])
            self.assertTrue(str(payload["query"]).startswith("redacted:"))
            if len(request.query) >= 12:
                self.assertNotIn(request.query, stored)
            if isinstance(request.firmware_version, str) and len(request.firmware_version) >= 4:
                self.assertNotIn(request.firmware_version, stored)
        else:
            self.assertIsNone(row["runtime_request_schema_version"])
            self.assertIsNone(json.loads(row["input_json"])["query"])
        packet = runtime.packet
        decision_payload = json.loads(row["decision_json"])
        evidence_ids = json.loads(row["evidence_ids"])
        if packet is None:
            self.assertIsNone(row["tool_name"])
            self.assertIsNone(row["tool_version"])
            self.assertIsNone(row["request_schema_version"])
            self.assertIsNone(row["response_schema_version"])
        else:
            self.assertEqual(row["tool_name"], packet.retrieval_tool_name)
            self.assertEqual(row["tool_version"], packet.retrieval_tool_version)
            self.assertEqual(row["request_schema_version"], packet.request.request_schema_version)
            self.assertEqual(row["response_schema_version"], packet.retrieval_response_schema_version)
            for item in packet.evidence:
                for value in (
                    item.supporting_original_text,
                    item.decision_visible_representation,
                    item.filename,
                    item.source_url,
                    item.source_locator,
                    item.canonical_product_name,
                    item.firmware_range,
                ):
                    if isinstance(value, str) and len(value) >= 12:
                        self.assertNotIn(value, stored)
        if runtime.decision is None:
            self.assertIsNotNone(runtime.error)
            assert runtime.error is not None
            self.assertEqual(row["termination_reason"], runtime.error.type)
            self.assertNotEqual(row["termination_reason"], "abstain")
            self.assertEqual(evidence_ids, [])
            self.assertIsNone(decision_payload["decision_type"])
            self.assertTrue(str(decision_payload["error"]["message"]).startswith("redacted:"))
            self.assertNotIn(runtime.error.message, stored)
        else:
            self.assertEqual(row["termination_reason"], runtime.decision.decision_type)
            self.assertEqual(evidence_ids, list(runtime.decision.evidence_ids))
            self.assertEqual(decision_payload["reason_codes"], list(runtime.decision.reason_codes))
            self.assertEqual(decision_payload["evidence_ids"], list(runtime.decision.evidence_ids))
            known = [item.evidence_id for item in packet.evidence] if packet is not None else []
            self.assertTrue(all(item in known for item in evidence_ids))


class TraceMigrationTests(unittest.TestCase):
    def test_forward_migration_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "g5.db"
            init_database(database)
            init_database(database)
            with connect_database(database) as connection:
                self.assertEqual(
                    [(version, name) for version, name, _fn in MIGRATIONS],
                    list(_MIGRATION_NAMES),
                )
                versions = [
                    row["version"]
                    for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
                ]
                self.assertEqual(versions, [1, 2, 3, 4])
                _migration_004_runtime_trace(connection)
                _migration_004_runtime_trace(connection)
                triggers = [
                    row["name"]
                    for row in connection.execute(
                        """SELECT name FROM sqlite_master
                           WHERE type = 'trigger' AND tbl_name = 'runtime_traces'
                           ORDER BY name"""
                    )
                ]
                table_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runtime_traces'"
                ).fetchone()["sql"]
                execution = execute_traced_runtime(connection, {"query": QUERY})
                self.assertTrue(execution.trace_ok)
                run_id = execution.run_id
            source = inspect.getsource(_migration_004_runtime_trace)
            for token in (
                "UPDATE runtime_traces SET",
                "DELETE FROM runtime_traces",
                "DROP ",
                "executescript",
                "case_id",
            ):
                self.assertNotIn(token, source, token)
            self.assertEqual(
                triggers,
                [
                    "runtime_traces_no_delete",
                    "runtime_traces_no_replace",
                    "runtime_traces_no_update",
                ],
            )
            for reason in (
                "supported",
                "abstain",
                "conflict",
                "invalid_request",
                "retrieval_failure",
                "retrieval_timeout",
                "source_index_mismatch",
            ):
                self.assertIn(f"'{reason}'", table_sql)
            for token in ("case_id", "max_steps", "budget_exhausted", "provider_error"):
                self.assertNotIn(token, table_sql)
            init_database(database)
            with connect_database(database) as connection:
                version_rows = connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = 4"
                ).fetchone()[0]
                remaining = connection.execute(
                    "SELECT run_id FROM runtime_traces"
                ).fetchall()
                table_count = connection.execute(
                    """SELECT COUNT(*) FROM sqlite_master
                       WHERE type = 'table' AND name = 'runtime_traces'"""
                ).fetchone()[0]
        self.assertEqual(version_rows, 1)
        self.assertEqual(table_count, 1)
        self.assertEqual([row["run_id"] for row in remaining], [run_id])

    def test_pre_migration_backup_restore_drops_later_traces(self) -> None:
        self.assertEqual(
            MIGRATION_004_DATA_LOSS,
            "Restoring a backup taken before migration 4 discards every runtime_traces "
            "row written after that migration.",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "g5.db"
            _migrate_through(database, 3)
            _seed_durable_document(database)
            pre_migration = root / "pre-g5.sqlite3"
            _sqlite_backup(database, pre_migration)
            pre_details = verify_backup(pre_migration)
            self.assertEqual(pre_details["schema_version"], 3)
            self.assertEqual(pre_details["integrity"], "ok")

            init_database(database)
            with connect_database(database) as connection:
                self.assertEqual(current_schema_version(connection), 4)
                execution = execute_traced_runtime(connection, {"query": QUERY})
                self.assertTrue(execution.trace_ok)
                run_id = execution.run_id
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM runtime_traces").fetchone()[0],
                    1,
                )
            migrated, migrated_details = create_backup(database, root / "migrated")
            self.assertEqual(migrated_details["schema_version"], 4)
            self.assertEqual(verify_backup(migrated)["schema_version"], 4)

            restored = restore_backup(pre_migration, database, confirm=True)
            self.assertEqual(restored["schema_version"], 3)
            with connect_database(database) as connection:
                self.assertEqual(current_schema_version(connection), 3)
                self.assertIsNone(
                    connection.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type = 'table' AND name = 'runtime_traces'"""
                    ).fetchone()
                )
                versions = [
                    row["version"]
                    for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")
                ]
                document = connection.execute("SELECT filename, sha256 FROM documents").fetchone()
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = ?",
                        (run_id,),
                    ).fetchone()
                )
        self.assertEqual(versions, [1, 2, 3])
        self.assertEqual(document["filename"], "pre-g5-durable.pdf")
        self.assertEqual(document["sha256"], "c" * 64)

    def test_public_backup_precedes_migration_004(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "deploy.db"
            _migrate_through(database, 3)
            _seed_durable_document(database)
            default_target = root / "default-upgrades.db"
            _sqlite_backup(database, default_target)
            upgraded, upgraded_details = create_backup(default_target, root / "default-backup")
            self.assertEqual(upgraded_details["schema_version"], 4)
            self.assertEqual(verify_backup(upgraded)["schema_version"], 4)
            with connect_database(default_target) as connection:
                self.assertEqual(current_schema_version(connection), 4)

            pre_backup, pre_details = create_backup(database, root / "before-004", migrate=False)
            self.assertEqual(pre_details["schema_version"], 3)
            self.assertEqual(pre_details["integrity"], "ok")
            verified = verify_backup(pre_backup)
            self.assertEqual(verified["schema_version"], 3)
            self.assertTrue(verified["manifest_verified"])
            with connect_database(database) as connection:
                self.assertEqual(current_schema_version(connection), 3)
                self.assertIsNone(
                    connection.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type = 'table' AND name = 'runtime_traces'"""
                    ).fetchone()
                )

            init_database(database)
            with connect_database(database) as connection:
                self.assertEqual(current_schema_version(connection), 4)
                execution = execute_traced_runtime(connection, {"query": QUERY})
                self.assertTrue(execution.trace_ok)
                self.assertIsNotNone(execution.run_id)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM runtime_traces").fetchone()[0],
                    1,
                )

            restored = restore_backup(pre_backup, database, confirm=True)
            self.assertEqual(restored["schema_version"], 3)
            with connect_database(database) as connection:
                self.assertEqual(current_schema_version(connection), 3)
                self.assertIsNone(
                    connection.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type = 'table' AND name = 'runtime_traces'"""
                    ).fetchone()
                )
                versions = [
                    row["version"]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                document = connection.execute(
                    "SELECT filename, sha256 FROM documents"
                ).fetchone()
            self.assertEqual(verify_backup(pre_backup)["schema_version"], 3)
        self.assertEqual(versions, [1, 2, 3])
        self.assertEqual(document["filename"], "pre-g5-durable.pdf")
        self.assertEqual(document["sha256"], "c" * 64)


def _stored(row) -> str:
    return " ".join(str(row[name]) for name in row.keys())


def _knowledge(connection: sqlite3.Connection) -> dict[str, object]:
    rows = {
        name: [tuple(item) for item in connection.execute(sql)]
        for name, sql in _KNOWLEDGE_SQL.items()
    }
    tables = [
        item[0]
        for item in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name"
        )
    ]
    return {"rows": rows, "tables": tables}


@contextmanager
def _spies(core_kwargs: dict[str, object]) -> Iterator[tuple[object, object, object, object]]:
    with (
        patch(
            "support_knowledge_engine.evidence.execute_retrieval_tool",
            wraps=execute_retrieval_tool,
        ) as tool,
        patch(
            "support_knowledge_engine.repository.retrieve_with_context",
            **core_kwargs,
        ) as core,
        patch(
            "support_knowledge_engine.runtime.decide_evidence",
            wraps=decide_evidence,
        ) as decide,
        patch(
            "support_knowledge_engine.trace.run_runtime",
            wraps=run_runtime,
        ) as runtime_spy,
    ):
        yield tool, core, decide, runtime_spy


def _request(query: str = QUERY, **extra: object) -> RuntimeRequest:
    payload: dict[str, object] = {
        "request_schema_version": RUNTIME_REQUEST_SCHEMA_VERSION,
        "query": query,
    }
    payload.update(extra)
    return RuntimeRequest(**payload)  # type: ignore[arg-type]


def _product(connection, name: str, alias: str, status: str = "active") -> int:
    product_id = create_product(
        connection,
        {"standard_name": name, "product_series": "AeroCam", "status": status},
        "g5 fixture",
        "g5",
    )
    add_product_alias(connection, product_id, alias, "abbreviation", "g5 fixture", "g5")
    return product_id


def _document(
    connection,
    product_id: int,
    filename: str,
    content: str,
    *,
    status: str = "effective",
    firmware_range: str = "",
    authority_level: str = "reference",
) -> int:
    digest = hashlib.sha256(filename.encode("utf-8")).hexdigest()
    cursor = connection.execute(
        """INSERT INTO documents
           (file_path, filename, title, product_series, product_model, document_type,
            language, version, release_date, source_url, sha256, imported_at, status,
            page_count, error_reason, canonical_product_id, firmware_range, authority_level)
           VALUES (?, ?, ?, 'AeroCam', 'Model', 'Service Handbook', 'en-US', '1.0',
                   '2026-01-01', ?, ?, '2026-01-01T00:00:00+00:00', ?, 1, NULL, ?, ?, ?)""",
        (
            filename,
            filename,
            filename,
            f"https://example.test/{filename}",
            digest,
            status,
            product_id,
            firmware_range,
            authority_level,
        ),
    )
    document_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO pages (document_id, page_number, content) VALUES (?, 1, ?)",
        (document_id, content),
    )
    connection.execute(
        "INSERT INTO page_fts (content, document_id, page_number) VALUES (?, ?, ?)",
        (content, document_id, 1),
    )
    return document_id


def _migrate_through(database: Path, limit: int) -> None:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version INTEGER PRIMARY KEY,
                   name TEXT NOT NULL,
                   applied_at TEXT NOT NULL
               )"""
        )
        connection.commit()
        applied = {row["version"] for row in connection.execute("SELECT version FROM schema_migrations")}
        for version, name, migration in MIGRATIONS:
            if version > limit or version in applied:
                continue
            migration(connection)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, "2026-01-01T00:00:00+00:00"),
            )
            connection.commit()
    finally:
        connection.close()


def _seed_durable_document(database: Path) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            """INSERT INTO documents
               (file_path, filename, title, product_series, product_model, document_type,
                language, version, release_date, source_url, sha256, imported_at, status,
                page_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                "pre-g5-durable.pdf",
                "pre-g5-durable.pdf",
                "Pre G5",
                "AeroCam",
                "Model",
                "Guide",
                "en-US",
                "1.0",
                "2026-01-01",
                "https://example.test/pre-g5-durable.pdf",
                "c" * 64,
                "2026-01-01T00:00:00+00:00",
                "effective",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _sqlite_backup(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(source)
    target_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(target_connection)
    finally:
        target_connection.close()
        source_connection.close()


if __name__ == "__main__":
    unittest.main()
