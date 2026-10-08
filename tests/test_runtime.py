"""G4 single-step runtime kernel.

The kernel composes one evidence capture and, on success, one decision.
These tests do not change retrieval, ranking, or the evidence decision.
"""

from __future__ import annotations

import ast
import hashlib
import sqlite3
import sys
import tempfile
import time
import unittest
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from unittest.mock import patch

from support_knowledge_engine.db import connect_database, init_database
from support_knowledge_engine.demo_data import seed_demo_data
from support_knowledge_engine.evaluation import _metrics, load_cases
from support_knowledge_engine.evidence import (
    DECISION_ABSTAIN,
    DECISION_CONFLICT,
    DECISION_SUPPORTED,
    PACKET_SCHEMA_VERSION,
    REASON_EVIDENCE_PRODUCT_MISMATCH,
    REASON_FIRMWARE_UNKNOWN,
    REASON_INACTIVE_PRODUCT,
    REASON_INSUFFICIENT_EVIDENCE,
    REASON_OUTDATED_DOCUMENT,
    REASON_PRODUCT_FILTER_CONFLICT,
    REASON_VERSION_CONFLICT,
    EvidenceDecision,
    EvidencePacket,
    capture_evidence_packet,
    decide_evidence,
)
from support_knowledge_engine.governance import add_product_alias, create_product
from support_knowledge_engine.migrations import MIGRATIONS
from support_knowledge_engine.repository import retrieve_with_context
from support_knowledge_engine.retrieval_tool import (
    REQUEST_SCHEMA_VERSION,
    RESPONSE_SCHEMA_VERSION,
    TOOL_NAME,
    TOOL_VERSION,
    execute_retrieval_tool,
)
from support_knowledge_engine.runtime import (
    ERROR_INVALID_REQUEST,
    RUNTIME_REQUEST_SCHEMA_VERSION,
    RUNTIME_RESPONSE_SCHEMA_VERSION,
    RUNTIME_VERSION,
    RuntimeCompositionError,
    RuntimeFailure,
    RuntimeRequest,
    RuntimeResult,
    run_runtime,
)
from tests.helpers import PROJECT_ROOT, SAMPLE_DIR


PHRASE = "calibration beacon zz-17"
ORIGINAL_PAGE = f"{PHRASE} service step"
QUERY = f"ACM2 {PHRASE}"
COUNTEREXAMPLE_QUERY = "ACP2 compass pulse AM3-18"
_SLOW_RETRIEVAL_SQL = (
    "WITH RECURSIVE c(x) AS ("
    "SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 500000000"
    ") SELECT max(x) FROM c"
)
_FORBIDDEN_IMPORTS = frozenset(
    {
        "openai",
        "anthropic",
        "xai",
        "xai_sdk",
        "grok",
        "httpx",
        "requests",
        "urllib",
        "aiohttp",
        "flask",
    }
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


class RuntimeContractTests(unittest.TestCase):
    def test_versions_and_fields_are_fixed(self) -> None:
        self.assertEqual(RUNTIME_VERSION, "1")
        self.assertEqual(RUNTIME_REQUEST_SCHEMA_VERSION, "1")
        self.assertEqual(RUNTIME_RESPONSE_SCHEMA_VERSION, "1")
        self.assertEqual(
            {item.name for item in fields(RuntimeRequest)},
            {
                "request_schema_version",
                "query",
                "product_id",
                "product_series",
                "document_type",
                "status",
                "association",
                "firmware_version",
            },
        )
        self.assertNotIn("retrieval_deadline_s", {item.name for item in fields(RuntimeRequest)})
        self.assertEqual(
            {item.name for item in fields(RuntimeResult)},
            {
                "ok",
                "runtime_version",
                "runtime_response_schema_version",
                "request",
                "packet",
                "decision",
                "error",
            },
        )
        self.assertEqual({item.name for item in fields(RuntimeFailure)}, {"type", "message"})
        request = RuntimeRequest(request_schema_version="1", query="q")
        failure = RuntimeFailure(type="invalid_request", message="probe")
        result = RuntimeResult(
            ok=False,
            runtime_version=RUNTIME_VERSION,
            runtime_response_schema_version=RUNTIME_RESPONSE_SCHEMA_VERSION,
            request=request,
            packet=None,
            decision=None,
            error=failure,
        )
        with self.assertRaises(FrozenInstanceError):
            request.query = "other"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            failure.type = "other"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            result.ok = True  # type: ignore[misc]

    def test_source_has_no_model_or_direct_retrieval_call(self) -> None:
        source = (PROJECT_ROOT / "support_knowledge_engine" / "runtime.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: list[str] = []
        calls: list[str] = []
        handlers: list[str] = []
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
            elif isinstance(node, ast.ExceptHandler):
                handlers.append(node.type.id if isinstance(node.type, ast.Name) else "bare")
            self.assertNotIsInstance(node, (ast.For, ast.While, ast.AsyncFor))
        self.assertTrue(set(imported).isdisjoint(_FORBIDDEN_IMPORTS))
        self.assertEqual(handlers, ["EvidenceCaptureError"])
        self.assertEqual(calls.count("capture_evidence_packet"), 1)
        self.assertEqual(calls.count("decide_evidence"), 1)
        self.assertNotIn("run_runtime", calls)
        self.assertNotIn("execute_retrieval_tool", calls)
        self.assertNotIn("retrieve_with_context", calls)
        self.assertNotIn("normalize_query", calls)
        lowered = source.lower()
        for token in (
            "openai",
            "anthropic",
            "xai",
            "grok",
            "httpx",
            "aiohttp",
            "chat.completions",
            "provider",
            "execute_retrieval_tool",
            "retrieve_with_context",
            "normalize_query",
            "answer_text",
            "run_id",
            "case_id",
            "search_logs",
        ):
            self.assertNotIn(token, lowered, token)
        routes = (PROJECT_ROOT / "support_knowledge_engine" / "routes.py").read_text(encoding="utf-8")
        package = (PROJECT_ROOT / "support_knowledge_engine" / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("runtime", routes.lower())
        self.assertNotIn("runtime", package.lower())

    def test_runtime_adds_no_migration(self) -> None:
        self.assertEqual([version for version, _name, _fn in MIGRATIONS], [1, 2, 3])


class RuntimeExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "g4.db"
        init_database(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_supported_returns_the_captured_packet_and_decision(self) -> None:
        product_id = self._write_case()
        request = _request(
            product_id=str(product_id),
            product_series="AeroCam",
            document_type="Service Handbook",
            status="effective",
            association="linked",
            firmware_version="1.2.3",
        )
        before_modules = set(sys.modules)
        result, tool, core, capture, decide = self._execute(request)
        new_roots = {name.split(".")[0] for name in set(sys.modules) - before_modules}
        self._assert_counts(tool, core, capture, decide, tool_count=1, core_count=1, capture_count=1, decide_count=1)
        self._assert_success(result, DECISION_SUPPORTED, capture=capture, decide=decide)
        self.assertIs(result.request, request)
        packet = result.packet
        decision = result.decision
        assert packet is not None and decision is not None
        self.assertEqual(packet.packet_schema_version, PACKET_SCHEMA_VERSION)
        self.assertEqual(decision.reason_codes, ())
        self.assertEqual(decision.evidence_ids, tuple(item.evidence_id for item in packet.evidence))
        self.assertEqual(packet.retrieval_state, "high_confidence")
        self.assertEqual(packet.retrieval_tool_name, TOOL_NAME)
        self.assertEqual(packet.retrieval_tool_version, TOOL_VERSION)
        self.assertEqual(packet.retrieval_response_schema_version, RESPONSE_SCHEMA_VERSION)
        self.assertEqual(packet.request.request_schema_version, REQUEST_SCHEMA_VERSION)
        self.assertEqual(packet.request.original_query, QUERY)
        self.assertEqual(packet.request.explicit_product_id, product_id)
        self.assertEqual(packet.request.product_series, "AeroCam")
        self.assertEqual(packet.request.document_type, "Service Handbook")
        self.assertEqual(packet.request.status, "effective")
        self.assertEqual(packet.request.association, "linked")
        self.assertEqual(packet.request.firmware_version, "1.2.3")
        self.assertEqual(packet.evidence[0].firmware_applicability, "applicable")
        self.assertEqual(packet.evidence[0].retrieval_tool_name, TOOL_NAME)
        self.assertTrue(packet.evidence[0].evidence_id.startswith("ev1-"))
        self.assertTrue(
            new_roots.isdisjoint({"openai", "anthropic", "xai", "xai_sdk", "grok", "httpx", "requests", "aiohttp"})
        )

    def test_firmware_unknown_abstains_once(self) -> None:
        self._write_case(firmware_range="1.2.3")
        result, tool, core, capture, decide = self._execute(_request(firmware_version="1.2.3"))
        self._assert_counts(tool, core, capture, decide, tool_count=1, core_count=1, capture_count=1, decide_count=1)
        self._assert_success(result, DECISION_ABSTAIN, capture=capture, decide=decide)
        assert result.packet is not None and result.decision is not None
        self.assertEqual(result.packet.retrieval_state, "high_confidence")
        self.assertEqual(result.decision.reason_codes, (REASON_FIRMWARE_UNKNOWN,))

    def test_inactive_product_abstains_once(self) -> None:
        self._write_case(product_status="inactive")
        result, tool, core, capture, decide = self._execute(_request())
        self._assert_counts(tool, core, capture, decide, tool_count=1, core_count=1, capture_count=1, decide_count=1)
        self._assert_success(result, DECISION_ABSTAIN, capture=capture, decide=decide)
        assert result.packet is not None and result.decision is not None
        self.assertEqual(result.packet.retrieval_state, "high_confidence")
        self.assertEqual(result.decision.reason_codes, (REASON_INACTIVE_PRODUCT,))

    def test_insufficient_evidence_abstains_once(self) -> None:
        self._write_case()
        result, tool, core, capture, decide = self._execute(_request("quantum toaster zz-999"))
        self._assert_counts(tool, core, capture, decide, tool_count=1, core_count=1, capture_count=1, decide_count=1)
        self._assert_success(result, DECISION_ABSTAIN, capture=capture, decide=decide)
        assert result.packet is not None and result.decision is not None
        self.assertEqual(result.packet.retrieval_state, "insufficient_evidence")
        self.assertEqual(result.decision.reason_codes, (REASON_INSUFFICIENT_EVIDENCE,))
        self.assertEqual(result.packet.evidence, ())
        self.assertEqual(result.decision.evidence_ids, ())

    def test_version_conflict_stops_without_retry(self) -> None:
        with connect_database(self.database) as connection:
            product_id = _product(connection, "AeroCam Mini 2", "ACM2")
            _document(connection, product_id, "current.pdf", ORIGINAL_PAGE, status="effective")
            _document(connection, product_id, "old.pdf", ORIGINAL_PAGE, status="superseded")
        result, tool, core, capture, decide = self._execute(_request())
        self._assert_counts(tool, core, capture, decide, tool_count=1, core_count=1, capture_count=1, decide_count=1)
        self._assert_success(result, DECISION_CONFLICT, capture=capture, decide=decide)
        assert result.packet is not None and result.decision is not None
        self.assertEqual(result.packet.retrieval_state, "version_conflict")
        self.assertEqual(
            result.decision.reason_codes,
            (REASON_OUTDATED_DOCUMENT, REASON_VERSION_CONFLICT),
        )
        self.assertEqual(
            result.decision.evidence_ids,
            tuple(item.evidence_id for item in result.packet.evidence),
        )

    def test_same_request_keeps_semantic_output(self) -> None:
        self._write_case()
        request = _request()
        first, first_tool, first_core, _capture, _decide = self._execute(request)
        second, second_tool, second_core, _capture, _decide = self._execute(request)
        self.assertEqual(first_tool.call_count, 1)
        self.assertEqual(second_tool.call_count, 1)
        self.assertEqual(first_core.call_count, 1)
        self.assertEqual(second_core.call_count, 1)
        assert first.packet is not None and second.packet is not None
        assert first.decision is not None and second.decision is not None
        self.assertEqual(first.packet.retrieval_state, second.packet.retrieval_state)
        self.assertEqual(
            tuple(item.evidence_id for item in first.packet.evidence),
            tuple(item.evidence_id for item in second.packet.evidence),
        )
        self.assertEqual(first.decision.decision_type, second.decision.decision_type)
        self.assertEqual(first.decision.reason_codes, second.decision.reason_codes)
        self.assertEqual(_without_timestamps(first.packet), _without_timestamps(second.packet))
        self.assertEqual(first.decision, second.decision)

    def test_invalid_product_filter_stops_before_core(self) -> None:
        self._write_case()
        result, tool, core, capture, decide = self._execute(_request(product_id="abc"))
        self._assert_counts(tool, core, capture, decide, tool_count=1, core_count=0, capture_count=1, decide_count=0)
        self._assert_failure(result, "invalid_request", decide=decide)
        self.assertEqual(result.error and result.error.message, "product_id 必须是数字。")

    def test_unsupported_schema_does_not_capture(self) -> None:
        request = _request()
        request = replace(request, request_schema_version="2")
        result, tool, core, capture, decide = self._execute(request)
        self._assert_counts(tool, core, capture, decide, tool_count=0, core_count=0, capture_count=0, decide_count=0)
        self._assert_failure(result, ERROR_INVALID_REQUEST, decide=decide)
        self.assertIs(result.request, request)
        self.assertEqual(result.error and result.error.message, "request_schema_version 不受支持。")

    def test_non_string_firmware_version_does_not_retrieve_or_decide(self) -> None:
        self._write_case()
        request = replace(_request(), firmware_version=1)  # type: ignore[arg-type]
        result, tool, core, capture, decide = self._execute(request)
        self._assert_counts(tool, core, capture, decide, tool_count=0, core_count=0, capture_count=0, decide_count=0)
        self._assert_failure(result, ERROR_INVALID_REQUEST, decide=decide)
        self.assertIs(result.request, request)
        self.assertIsNone(result.packet)
        self.assertIsNone(result.decision)
        self.assertFalse(result.ok)
        self.assertEqual(result.error and result.error.message, "firmware_version 必须是字符串或 None。")

    def test_non_request_does_not_capture(self) -> None:
        result, tool, core, capture, decide = self._execute({"query": QUERY})
        self._assert_counts(tool, core, capture, decide, tool_count=0, core_count=0, capture_count=0, decide_count=0)
        self._assert_failure(result, ERROR_INVALID_REQUEST, decide=decide)
        self.assertIsNone(result.request)
        self.assertEqual(result.error and result.error.message, "runtime request 必须是 RuntimeRequest。")

    def test_retrieval_timeout_is_not_abstain(self) -> None:
        self._write_case()

        def slow(connection, *args, **kwargs):
            del args, kwargs
            connection.execute(_SLOW_RETRIEVAL_SQL).fetchone()
            raise AssertionError("blocking retrieval finished instead of being interrupted")

        started = time.monotonic()
        result, tool, core, capture, decide = self._execute(
            _request(), deadline=0.25, core_side_effect=slow
        )
        self.assertLess(time.monotonic() - started, 2.0)
        self._assert_counts(tool, core, capture, decide, tool_count=1, core_count=1, capture_count=1, decide_count=0)
        self._assert_failure(result, "retrieval_timeout", decide=decide)
        self.assertEqual(result.error and result.error.message, "检索超过时限。")
        self.assertNotEqual(result.error and result.error.type, DECISION_ABSTAIN)

    def test_retrieval_failure_is_not_abstain(self) -> None:
        self._write_case()
        result, tool, core, capture, decide = self._execute(
            _request(), core_side_effect=RuntimeError("compute failed")
        )
        self._assert_counts(tool, core, capture, decide, tool_count=1, core_count=1, capture_count=1, decide_count=0)
        self._assert_failure(result, "retrieval_failure", decide=decide)
        self.assertEqual(result.error and result.error.message, "检索执行失败。")

    def test_source_index_mismatch_does_not_return_a_packet(self) -> None:
        self._write_case()
        divergent = "完全不同但非空的页面"
        with connect_database(self.database) as connection:
            connection.execute("UPDATE pages SET content = ?", (divergent,))
        result, tool, core, capture, decide = self._execute(_request())
        self._assert_counts(tool, core, capture, decide, tool_count=1, core_count=1, capture_count=1, decide_count=0)
        self._assert_failure(result, "source_index_mismatch", decide=decide)
        self.assertEqual(result.error and result.error.message, "页面原文与全文索引内容不一致。")
        with connect_database(self.database) as connection:
            self.assertEqual(connection.execute("SELECT content FROM pages").fetchone()[0], divergent)
            indexed = connection.execute("SELECT content FROM page_fts").fetchone()[0]
        self.assertIn(PHRASE, indexed)
        self.assertNotEqual(indexed, divergent)

    def test_decision_exception_is_not_rewritten(self) -> None:
        self._write_case()
        request = _request()
        with connect_database(self.database) as connection:
            before = _snapshot(connection)
            with patch(
                "support_knowledge_engine.runtime.decide_evidence",
                side_effect=ValueError("probe"),
            ):
                with self.assertRaises(ValueError):
                    run_runtime(connection, request)
            self.assertEqual(_snapshot(connection), before)

    def test_broken_composition_is_not_rewritten(self) -> None:
        self._write_case()
        request = _request()
        foreign = EvidenceDecision(
            decision_type=DECISION_SUPPORTED,
            reason_codes=(),
            retrieval_state="high_confidence",
            evidence_ids=("ev1-not-in-packet",),
            packet_schema_version=PACKET_SCHEMA_VERSION,
        )
        with connect_database(self.database) as connection:
            before = _snapshot(connection)
            with patch("support_knowledge_engine.runtime.decide_evidence", return_value=foreign):
                with self.assertRaises(RuntimeCompositionError):
                    run_runtime(connection, request)

            def bad_schema(packet: EvidencePacket) -> EvidenceDecision:
                return replace(decide_evidence(packet), packet_schema_version="other")

            with patch("support_knowledge_engine.runtime.decide_evidence", side_effect=bad_schema):
                with self.assertRaises(RuntimeCompositionError):
                    run_runtime(connection, request)
            self.assertEqual(_snapshot(connection), before)

    def _write_case(
        self,
        *,
        product_status: str = "active",
        firmware_range: str = "",
    ) -> int:
        with connect_database(self.database) as connection:
            product_id = _product(connection, "AeroCam Mini 2", "ACM2", status=product_status)
            _document(
                connection,
                product_id,
                "handbook.pdf",
                ORIGINAL_PAGE,
                firmware_range=firmware_range,
            )
        return product_id

    def _execute(self, request, *, deadline=None, core_side_effect=None):
        core_kwargs = (
            {"side_effect": core_side_effect}
            if core_side_effect is not None
            else {"wraps": retrieve_with_context}
        )
        with connect_database(self.database) as connection:
            before = _snapshot(connection)
            with _spies(core_kwargs) as (tool, core, capture, decide):
                arguments = {}
                if deadline is not None:
                    arguments["retrieval_deadline_s"] = deadline
                result = run_runtime(connection, request, **arguments)
            self.assertEqual(_snapshot(connection), before)
        return result, tool, core, capture, decide

    def _assert_counts(
        self,
        tool,
        core,
        capture,
        decide,
        *,
        tool_count: int,
        core_count: int,
        capture_count: int,
        decide_count: int,
    ) -> None:
        self.assertEqual(
            (tool.call_count, core.call_count, capture.call_count, decide.call_count),
            (tool_count, core_count, capture_count, decide_count),
        )
        self.assertLessEqual(tool_count, 1)
        self.assertLessEqual(core_count, 1)

    def _assert_success(self, result, decision_type: str, *, capture, decide) -> None:
        self.assertTrue(result.ok)
        self.assertIsNone(result.error)
        self.assertIsNotNone(result.packet)
        self.assertIsNotNone(result.decision)
        self.assertEqual(result.runtime_version, RUNTIME_VERSION)
        self.assertEqual(result.runtime_response_schema_version, RUNTIME_RESPONSE_SCHEMA_VERSION)
        self.assertEqual(result.decision.decision_type, decision_type)
        self.assertEqual(result.decision.packet_schema_version, result.packet.packet_schema_version)
        known = {item.evidence_id for item in result.packet.evidence}
        self.assertTrue(all(item in known for item in result.decision.evidence_ids))
        self.assertIs(result.packet, capture.recorded[0])
        self.assertIs(result.decision, decide.recorded[0])
        self.assertTrue(result.packet.retrieval_tool_name)

    def _assert_failure(self, result, error_type: str, *, decide) -> None:
        self.assertFalse(result.ok)
        self.assertIsNone(result.packet)
        self.assertIsNone(result.decision)
        self.assertIsNotNone(result.error)
        self.assertEqual(result.error.type, error_type)
        self.assertTrue(result.error.message)
        self.assertEqual(decide.call_count, 0)
        self.assertEqual(result.runtime_version, RUNTIME_VERSION)
        self.assertEqual(result.runtime_response_schema_version, RUNTIME_RESPONSE_SCHEMA_VERSION)


class RuntimeCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temp.name) / "corpus.db"
        seed_demo_data(cls.database, SAMPLE_DIR)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_product_filter_mismatch_abstains_once(self) -> None:
        with connect_database(self.database) as connection:
            mini_id = connection.execute(
                "SELECT id FROM products WHERE standard_name = 'AeroCam Mini 3'"
            ).fetchone()["id"]
        request = _request(COUNTEREXAMPLE_QUERY, product_id=str(mini_id))
        result, tool, core, capture, decide = self._execute(request)
        self.assertEqual(
            (tool.call_count, core.call_count, capture.call_count, decide.call_count),
            (1, 1, 1, 1),
        )
        self.assertTrue(result.ok)
        assert result.packet is not None and result.decision is not None
        self.assertEqual(result.packet.retrieval_state, "high_confidence")
        self.assertEqual(result.decision.decision_type, DECISION_ABSTAIN)
        self.assertNotEqual(result.decision.decision_type, DECISION_SUPPORTED)
        self.assertEqual(
            result.decision.reason_codes,
            (REASON_EVIDENCE_PRODUCT_MISMATCH, REASON_PRODUCT_FILTER_CONFLICT),
        )
        self.assertEqual(result.packet.request.explicit_product_name, "AeroCam Mini 3")
        self.assertEqual(result.packet.recognized_products[0].name, "AeroCam Pro 2")
        self.assertIs(result.packet, capture.recorded[0])
        self.assertIs(result.decision, decide.recorded[0])
        known = {item.evidence_id for item in result.packet.evidence}
        self.assertTrue(all(item in known for item in result.decision.evidence_ids))

    def test_corpus_pilot_replay_keeps_retrieval_baseline(self) -> None:
        cases = load_cases(PROJECT_ROOT / "evals" / "corpus_pilot_cases.json")
        self.assertEqual(len(cases), 84)
        with connect_database(self.database) as connection:
            before = _snapshot(connection)
            with _spies({"wraps": retrieve_with_context}) as (tool, core, capture, decide):
                results = [
                    run_runtime(
                        connection,
                        RuntimeRequest(
                            request_schema_version=RUNTIME_REQUEST_SCHEMA_VERSION,
                            query=case["question"],
                        ),
                    )
                    for case in cases
                ]
            self.assertEqual(_snapshot(connection), before)
        self.assertEqual((tool.call_count, core.call_count, capture.call_count, decide.call_count), (84, 84, 84, 84))
        self.assertEqual(len(capture.recorded), 84)
        self.assertEqual(len(decide.recorded), 84)
        failed = [
            (case["id"], result.error.type if result.error is not None else None)
            for case, result in zip(cases, results, strict=True)
            if not result.ok
        ]
        self.assertEqual(failed, [])
        projected = []
        for case, result in zip(cases, results, strict=True):
            packet = result.packet
            decision = result.decision
            assert packet is not None and decision is not None
            self.assertIs(packet, capture.recorded[len(projected)])
            self.assertIs(decision, decide.recorded[len(projected)])
            self.assertEqual(decision.packet_schema_version, packet.packet_schema_version)
            known = {item.evidence_id for item in packet.evidence}
            self.assertTrue(all(item in known for item in decision.evidence_ids))
            self.assertIn(decision.decision_type, {DECISION_SUPPORTED, DECISION_ABSTAIN, DECISION_CONFLICT})
            self.assertTrue(packet.retrieval_tool_name)
            projected.append(_project_retrieval_case(case, packet))
        metrics = _metrics(projected)
        passed = sum(row["passed"] for row in projected)
        self.assertEqual(len(results), 84)
        self.assertEqual(sum(result.ok for result in results), 84)
        self.assertEqual(passed, 84)
        self.assertEqual(metrics["recall_at_1"], 1.0)
        self.assertEqual(metrics["recall_at_3"], 1.0)
        self.assertEqual(metrics["mrr"], 1.0)
        self.assertEqual(metrics["product_leakage_rate"], 0.0)
        self.assertEqual(metrics["outdated_document_mis_hit_rate"], 0.0)
        self.assertEqual(metrics["no_answer_false_return_rate"], 0.0)
        self.assertEqual(metrics["alias_recognition_success_rate"], 1.0)
        # The retrieval baseline is locked above. Decision mix is not pinned:
        # a G3 abstain stays an abstain and is not rewritten into support.
        counts = Counter(result.decision.decision_type for result in results if result.decision is not None)
        self.assertEqual(sum(counts.values()), 84)
        self.assertTrue(set(counts).issubset({DECISION_SUPPORTED, DECISION_ABSTAIN, DECISION_CONFLICT}))

    def _execute(self, request):
        with connect_database(self.database) as connection:
            before = _snapshot(connection)
            with _spies({"wraps": retrieve_with_context}) as (tool, core, capture, decide):
                result = run_runtime(connection, request)
            self.assertEqual(_snapshot(connection), before)
        return result, tool, core, capture, decide


@contextmanager
def _spies(core_kwargs: dict[str, object]) -> Iterator[tuple[object, object, object, object]]:
    recorded_packets: list[EvidencePacket] = []
    recorded_decisions: list[EvidenceDecision] = []

    def record_capture(*args, **kwargs):
        packet = capture_evidence_packet(*args, **kwargs)
        recorded_packets.append(packet)
        return packet

    def record_decision(packet):
        decision = decide_evidence(packet)
        recorded_decisions.append(decision)
        return decision

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
            "support_knowledge_engine.runtime.capture_evidence_packet",
            side_effect=record_capture,
        ) as capture,
        patch(
            "support_knowledge_engine.runtime.decide_evidence",
            side_effect=record_decision,
        ) as decide,
    ):
        capture.recorded = recorded_packets
        decide.recorded = recorded_decisions
        yield tool, core, capture, decide


def _request(query: str = QUERY, **extra: object) -> RuntimeRequest:
    payload: dict[str, object] = {
        "request_schema_version": RUNTIME_REQUEST_SCHEMA_VERSION,
        "query": query,
    }
    payload.update(extra)
    return RuntimeRequest(**payload)  # type: ignore[arg-type]


def _project_retrieval_case(case: dict, packet: EvidencePacket) -> dict:
    """Project the G1 retrieval facts from one packet.

    The score uses retrieval state and evidence order. It does not read the
    evidence decision, and it does not search again.
    """

    results = [
        {
            "filename": item.filename,
            "page_number": item.page_number,
            "canonical_product_name": item.canonical_product_name,
            "status": item.document_lifecycle,
        }
        for item in packet.evidence
    ]
    target_document = case.get("target_document")
    target_page = case.get("target_page")
    target_rank = None
    for rank, result in enumerate(results, start=1):
        if target_document and result["filename"] == target_document:
            if target_page is None or int(result["page_number"]) == int(target_page):
                target_rank = rank
                break
    forbidden = set(case.get("forbidden_products", []))
    found = sorted(
        {
            str(row["canonical_product_name"])
            for row in results[:3]
            if row.get("canonical_product_name") in forbidden
        }
    )
    allowed_statuses = set(case.get("allowed_document_statuses", []))
    outdated_mis_hit = bool(allowed_statuses) and any(
        str(row["status"]) not in allowed_statuses for row in results[:3]
    )
    should_answer = bool(case["should_return_answer"])
    returned_answer = bool(results) and packet.retrieval_state != "insufficient_evidence"
    if should_answer:
        passed = target_rank is not None and target_rank <= int(case.get("expected_max_rank", 3))
        passed = passed and not found and not outdated_mis_hit
    else:
        passed = not returned_answer
    expected_product = case.get("target_product")
    recognized = {item.name for item in packet.recognized_products}
    alias_expected = bool(case.get("alias_expected", False))
    alias_recognized = (expected_product in recognized) if alias_expected else None
    return {
        "id": case["id"],
        "category": case.get("category", "uncategorized"),
        "should_return_answer": should_answer,
        "target_rank": target_rank,
        "forbidden_product_found": bool(found),
        "outdated_mis_hit": outdated_mis_hit,
        "returned_answer": returned_answer,
        "alias_recognized": alias_recognized,
        "passed": passed,
        "elapsed_ms": 0.0,
    }


def _without_timestamps(packet: EvidencePacket) -> EvidencePacket:
    return replace(
        packet,
        captured_at="",
        evidence=tuple(replace(item, captured_at="") for item in packet.evidence),
    )


def _snapshot(connection: sqlite3.Connection) -> dict[str, object]:
    rows = {
        name: [tuple(row) for row in connection.execute(sql)]
        for name, sql in _KNOWLEDGE_SQL.items()
    }
    tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name"
    ).fetchall()
    return {"rows": rows, "tables": [row[0] for row in tables]}


def _product(connection, name: str, alias: str, status: str = "active") -> int:
    product_id = create_product(
        connection,
        {"standard_name": name, "product_series": "AeroCam", "status": status},
        "g4 fixture",
        "g4",
    )
    add_product_alias(connection, product_id, alias, "abbreviation", "g4 fixture", "g4")
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
