from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import time
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import support_knowledge_engine.evidence as evidence_module
from support_knowledge_engine.db import connect_database, init_database
from support_knowledge_engine.demo_data import seed_demo_data
from support_knowledge_engine.evidence import (
    DECISION_ABSTAIN,
    DECISION_CONFLICT,
    DECISION_SUPPORTED,
    DECISION_VISIBLE_SOURCE,
    PACKET_SCHEMA_VERSION,
    REASON_AMBIGUOUS_PRODUCT,
    REASON_AMBIGUOUS_PRODUCT_ALIAS,
    REASON_ARCHIVED_PRODUCT,
    REASON_AUTHORITY_UNKNOWN,
    REASON_DOCUMENT_DRAFT,
    REASON_DOCUMENT_LIFECYCLE_UNKNOWN,
    REASON_DOCUMENT_NEEDS_REVIEW,
    REASON_EVIDENCE_PRODUCT_MISMATCH,
    REASON_FIRMWARE_NOT_APPLICABLE,
    REASON_FIRMWARE_UNKNOWN,
    REASON_INACTIVE_PRODUCT,
    REASON_INSUFFICIENT_EVIDENCE,
    REASON_OUTDATED_DOCUMENT,
    REASON_PLANNED_PRODUCT,
    REASON_POSSIBLE_MATCH,
    REASON_PRODUCT_FILTER_CONFLICT,
    REASON_VERSION_CONFLICT,
    SNAPSHOT_SCHEMA_VERSION,
    SUPPORTING_TEXT_SOURCE,
    TRANSFORMATION_VERSION,
    EvidenceCaptureError,
    EvidencePacket,
    EvidenceSnapshot,
    RequestContext,
    capture_evidence_packet,
    classify_firmware,
    decide_evidence,
)
from support_knowledge_engine.governance import add_product_alias, create_product
from support_knowledge_engine.migrations import MIGRATIONS
from support_knowledge_engine.normalization import normalize_query
from support_knowledge_engine.repository import retrieve_with_context
from support_knowledge_engine.retrieval_tool import (
    REQUEST_SCHEMA_VERSION,
    RESPONSE_SCHEMA_VERSION,
    TOOL_NAME,
    TOOL_VERSION,
    execute_retrieval_tool,
)
from tests.helpers import SAMPLE_DIR


PHRASE = "calibration beacon zz-17"
ORIGINAL_PAGE = f"{PHRASE} service step"
LONG_PAGE = f"{PHRASE} service step " + ("unexcerpted source sentence " * 30)
CAPTURED_AT = "2026-10-08T00:00:00+00:00"
COUNTEREXAMPLE_QUERY = "ACP2 compass pulse AM3-18"
MINI3_HANDBOOK = "AeroCam-Mini-3_Service-Handbook_v2.0_en-US.pdf"
MINI3_SHA256 = "45d80e2cef7add5eaa3297e900ef4e00b5fcf5d0f4d7f14fbb5e1d06dccd6cba"
_SLOW_RETRIEVAL_SQL = (
    "WITH RECURSIVE c(x) AS ("
    "SELECT 1 UNION ALL SELECT x+1 FROM c LIMIT 500000000"
    ") SELECT max(x) FROM c"
)


class FirmwareGrammarTests(unittest.TestCase):
    def test_nonempty_firmware_range_is_unknown(self) -> None:
        self.assertEqual(classify_firmware("", None), "applicable")
        self.assertEqual(classify_firmware("   ", "1.2.3"), "applicable")
        cases = (
            ("1.2.3", None),
            ("1.2.3", "1.2.3"),
            ("1.2.3", "9.9.9"),
            ("2.0.0 - 2.9.x", "2.0.0"),
            ("2.9.x", "2.9.0"),
            ("AP2-44", "AP2-44"),
        )
        for stored, supplied in cases:
            self.assertEqual(classify_firmware(stored, supplied), "unknown", (stored, supplied))
            self.assertNotEqual(classify_firmware(stored, supplied), "applicable")
            self.assertNotEqual(classify_firmware(stored, supplied), "not_applicable")
        self.assertEqual(REASON_FIRMWARE_NOT_APPLICABLE, "firmware_not_applicable")


class EvidenceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "g3.db"
        init_database(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_supported_snapshot_is_stable_and_capture_does_not_write(self) -> None:
        self._write_case(content=LONG_PAGE)
        with connect_database(self.database) as connection:
            tables = _table_names(connection)
            logs = _log_count(connection)
            packet = capture_evidence_packet(
                connection, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT
            )
            again = capture_evidence_packet(
                connection, f"ACM2 {PHRASE}", captured_at="2026-10-08T00:00:01+00:00"
            )
            self.assertEqual(_table_names(connection), tables)
            self.assertEqual(_log_count(connection), logs)
        decision = decide_evidence(packet)
        self.assertEqual(decision.decision_type, DECISION_SUPPORTED)
        self.assertEqual(decision.reason_codes, ())
        self.assertEqual(decision.retrieval_state, "high_confidence")
        self.assertEqual(decision.evidence_ids, (packet.evidence[0].evidence_id,))
        self.assertEqual(packet.evidence[0].evidence_id, again.evidence[0].evidence_id)
        self.assertNotEqual(packet.captured_at, again.captured_at)
        snapshot = packet.evidence[0]
        self.assertEqual(snapshot.supporting_original_text, LONG_PAGE)
        self.assertEqual(snapshot.supporting_text_source, SUPPORTING_TEXT_SOURCE)
        self.assertEqual(snapshot.decision_visible_source, DECISION_VISIBLE_SOURCE)
        self.assertNotEqual(snapshot.decision_visible_representation, snapshot.supporting_original_text)
        self.assertIn(PHRASE, snapshot.decision_visible_representation)
        self.assertEqual(snapshot.transformation_version, TRANSFORMATION_VERSION)
        self.assertEqual(snapshot.snapshot_schema_version, "3")
        self.assertEqual(packet.packet_schema_version, "2")
        self.assertEqual(snapshot.original_content_digest, _digest(LONG_PAGE))
        self.assertEqual(
            snapshot.decision_visible_digest, _digest(snapshot.decision_visible_representation)
        )
        self.assertNotEqual(snapshot.original_content_digest, snapshot.decision_visible_digest)
        self.assertEqual(snapshot.original_content_digest, again.evidence[0].original_content_digest)
        self.assertEqual(snapshot.decision_visible_digest, again.evidence[0].decision_visible_digest)
        self.assertEqual(snapshot.decision_visible_representation, again.evidence[0].decision_visible_representation)
        self.assertEqual(packet.request.original_query, again.request.original_query)
        self.assertEqual(packet.request.normalized_query, again.request.normalized_query)
        self.assertEqual(packet.request.retrieval_query, again.request.retrieval_query)
        self.assertEqual(packet.request.request_schema_version, again.request.request_schema_version)
        self.assertEqual(snapshot.firmware_applicability, "applicable")
        self.assertEqual(snapshot.product_lifecycle, "active")
        self.assertEqual(snapshot.document_lifecycle, "effective")
        self.assertEqual(snapshot.authority_level, "reference")
        self.assertEqual(snapshot.retrieval_tool_name, TOOL_NAME)
        self.assertEqual(snapshot.retrieval_tool_version, TOOL_VERSION)
        self.assertEqual(snapshot.retrieval_response_schema_version, RESPONSE_SCHEMA_VERSION)
        self.assertTrue(snapshot.evidence_id.startswith("ev1-"))
        self.assertEqual(len(snapshot.evidence_id), 4 + 64)
        with self.assertRaises(FrozenInstanceError):
            packet.retrieval_state = "insufficient_evidence"

    def test_packet_provenance_comes_from_the_tool_response(self) -> None:
        self._write_case(content=LONG_PAGE)
        recorded: dict[str, object] = {}

        def mutate(connection, request, **kwargs):
            envelope = execute_retrieval_tool(connection, request, **kwargs)
            recorded["request"] = dict(request)
            recorded["kwargs"] = dict(kwargs)
            result = envelope["result"]
            recorded["result"] = {
                "original_query": result["original_query"],
                "normalized_query": result["normalized_query"],
                "retrieval_query": result["retrieval_query"],
            }
            recorded["issued"] = {
                "tool_name": envelope["tool_name"],
                "tool_version": envelope["tool_version"],
                "request_schema_version": envelope["request_schema_version"],
                "response_schema_version": envelope["response_schema_version"],
            }
            envelope["tool_name"] = "probe-tool"
            envelope["tool_version"] = "probe-version"
            envelope["request_schema_version"] = "probe-request-schema"
            envelope["response_schema_version"] = "probe-response-schema"
            return envelope

        with connect_database(self.database) as connection:
            logs = _log_count(connection)
            with (
                patch(
                    "support_knowledge_engine.evidence.execute_retrieval_tool",
                    side_effect=mutate,
                ) as tool_spy,
                patch(
                    "support_knowledge_engine.repository.retrieve_with_context",
                    wraps=retrieve_with_context,
                ) as core_spy,
                patch(
                    "support_knowledge_engine.repository.normalize_query",
                    wraps=normalize_query,
                ) as norm_spy,
            ):
                packet = capture_evidence_packet(
                    connection,
                    f"ACM2 {PHRASE}",
                    product_series="AeroCam",
                    document_type="Service Handbook",
                    firmware_version="1.2.3",
                    captured_at=CAPTURED_AT,
                )
            self.assertEqual(_log_count(connection), logs)
        self.assertEqual(tool_spy.call_count, 1)
        self.assertEqual(core_spy.call_count, 1)
        self.assertEqual(norm_spy.call_count, 1)
        self.assertNotIn("normalize_query", evidence_module.__dict__)
        self.assertIn("telemetry_sink", recorded["kwargs"])
        self.assertNotIn("retrieval_deadline_s", recorded["kwargs"])
        issued = recorded["issued"]
        self.assertEqual(issued["tool_name"], TOOL_NAME)
        self.assertEqual(issued["tool_version"], TOOL_VERSION)
        self.assertEqual(issued["response_schema_version"], RESPONSE_SCHEMA_VERSION)
        self.assertEqual(recorded["request"]["request_schema_version"], REQUEST_SCHEMA_VERSION)
        self.assertEqual(recorded["request"]["query"], f"ACM2 {PHRASE}")
        self.assertEqual(recorded["request"]["product_series"], "AeroCam")
        self.assertEqual(recorded["request"]["document_type"], "Service Handbook")
        self.assertEqual(packet.retrieval_tool_name, "probe-tool")
        self.assertEqual(packet.retrieval_tool_version, "probe-version")
        self.assertEqual(packet.retrieval_response_schema_version, "probe-response-schema")
        self.assertEqual(packet.evidence[0].retrieval_tool_name, "probe-tool")
        self.assertEqual(packet.evidence[0].retrieval_tool_version, "probe-version")
        self.assertEqual(packet.evidence[0].retrieval_response_schema_version, "probe-response-schema")
        self.assertEqual(packet.request.request_schema_version, "probe-request-schema")
        self.assertNotEqual(packet.request.request_schema_version, REQUEST_SCHEMA_VERSION)
        self.assertNotEqual(packet.retrieval_tool_name, TOOL_NAME)
        result = recorded["result"]
        self.assertEqual(packet.request.original_query, result["original_query"])
        self.assertEqual(packet.request.normalized_query, result["normalized_query"])
        self.assertEqual(packet.request.retrieval_query, result["retrieval_query"])
        self.assertEqual(packet.request.original_query, f"ACM2 {PHRASE}")
        self.assertEqual(packet.request.product_series, "AeroCam")
        self.assertEqual(packet.request.document_type, "Service Handbook")
        self.assertEqual(packet.request.firmware_version, "1.2.3")
        self.assertEqual(packet.evidence[0].firmware_applicability, "applicable")
        self.assertEqual(decide_evidence(packet).decision_type, DECISION_SUPPORTED)

    def test_retrieval_is_called_once_and_decision_does_not_query(self) -> None:
        self._write_case()
        with connect_database(self.database) as connection:
            with (
                patch(
                    "support_knowledge_engine.evidence.execute_retrieval_tool",
                    wraps=execute_retrieval_tool,
                ) as tool_spy,
                patch(
                    "support_knowledge_engine.repository.retrieve_with_context",
                    wraps=retrieve_with_context,
                ) as core_spy,
            ):
                packet = capture_evidence_packet(connection, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT)
            self.assertEqual(tool_spy.call_count, 1)
            self.assertEqual(core_spy.call_count, 1)
        decision = decide_evidence(packet)
        self.assertEqual(decision.decision_type, DECISION_SUPPORTED)
        self.assertEqual(
            decide_evidence.__code__.co_varnames[: decide_evidence.__code__.co_argcount],
            ("packet",),
        )

    def test_capture_reads_page_source_without_a_second_retrieval(self) -> None:
        self._write_case(content=LONG_PAGE)
        with connect_database(self.database) as connection:
            direct = retrieve_with_context(connection, f"ACM2 {PHRASE}")
            tracer = _StatementTracer(connection)
            calls = {"core": 0}

            def arm_after_retrieval(connection, *args, **kwargs):
                calls["core"] += 1
                try:
                    return retrieve_with_context(connection.inner, *args, **kwargs)
                finally:
                    connection.armed = True

            with patch(
                "support_knowledge_engine.repository.retrieve_with_context",
                arm_after_retrieval,
            ):
                packet = capture_evidence_packet(tracer, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT)
            logged = tracer.statements
        self.assertEqual(calls["core"], 1)
        self.assertTrue(any("from pages" in sql.casefold() for sql in logged))
        self.assertTrue(any("from page_fts" in sql.casefold() for sql in logged))
        self.assertTrue(any("products" in sql.casefold() for sql in logged))
        for sql in logged:
            folded = f" {sql.casefold()} "
            self.assertNotIn(" match ", folded)
            self.assertNotIn("documents", folded)
            self.assertNotIn("insert", folded)
            self.assertNotIn("update", folded)
            self.assertNotIn("delete", folded)
        evidence = packet.evidence[0]
        self.assertEqual(evidence.supporting_original_text, LONG_PAGE)
        self.assertEqual(evidence.decision_visible_representation, direct["results"][0]["snippet"])
        self.assertNotEqual(evidence.supporting_original_text, evidence.decision_visible_representation)
        self.assertEqual(evidence.original_content_digest, _digest(LONG_PAGE))
        self.assertEqual(evidence.decision_visible_digest, _digest(direct["results"][0]["snippet"]))

    def test_same_snapshot_excludes_a_committed_mutation(self) -> None:
        product_id = self._write_case()
        mutated_page = ORIGINAL_PAGE + " CONCURRENT-NEW-PAGE " + ("tail " * 20)
        original_load = evidence_module._load_snapshot_facts
        committed = {"ok": False}

        def load_after_commit(connection, result, explicit_product_id):
            writer = sqlite3.connect(self.database, timeout=1)
            try:
                writer.execute("PRAGMA busy_timeout = 1000")
                writer.execute("UPDATE pages SET content = ?", (mutated_page,))
                writer.execute(
                    """UPDATE documents
                       SET status = 'archived', authority_level = 'unlisted', firmware_range = '9.9.9'"""
                )
                writer.execute("UPDATE products SET status = 'archived' WHERE id = ?", (product_id,))
                writer.commit()
                committed["ok"] = True
            finally:
                writer.close()
            return original_load(connection, result, explicit_product_id)

        with connect_database(self.database) as connection:
            with patch(
                "support_knowledge_engine.evidence._load_snapshot_facts",
                load_after_commit,
            ):
                packet = capture_evidence_packet(connection, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT)
        self.assertTrue(committed["ok"])
        evidence = packet.evidence[0]
        self.assertEqual(evidence.supporting_original_text, ORIGINAL_PAGE)
        self.assertNotIn("CONCURRENT-NEW-PAGE", evidence.supporting_original_text)
        self.assertNotIn("CONCURRENT-NEW-PAGE", evidence.decision_visible_representation)
        self.assertEqual(evidence.product_lifecycle, "active")
        self.assertEqual(evidence.document_lifecycle, "effective")
        self.assertEqual(evidence.firmware_range, "")
        self.assertEqual(evidence.authority_level, "reference")
        self.assertEqual(evidence.firmware_applicability, "applicable")
        with connect_database(self.database) as connection:
            live_page = connection.execute("SELECT content FROM pages").fetchone()[0]
            live_product = connection.execute(
                "SELECT status FROM products WHERE id = ?", (product_id,)
            ).fetchone()[0]
            live_document = connection.execute("SELECT status, firmware_range FROM documents").fetchone()
        self.assertEqual(live_page, mutated_page)
        self.assertEqual(live_product, "archived")
        self.assertEqual(live_document["status"], "archived")
        self.assertEqual(live_document["firmware_range"], "9.9.9")
        self.assertNotEqual(evidence.supporting_original_text, live_page)
        self.assertNotEqual(evidence.product_lifecycle, live_product)
        self.assertNotEqual(evidence.document_lifecycle, live_document["status"])

    def test_g2_failure_does_not_create_a_packet(self) -> None:
        self._write_case()
        with connect_database(self.database) as connection:
            logs = _log_count(connection)
            with patch(
                "support_knowledge_engine.repository.retrieve_with_context",
                wraps=retrieve_with_context,
            ) as spy:
                with self.assertRaises(EvidenceCaptureError) as invalid_query:
                    capture_evidence_packet(connection, "x" * 2001, captured_at=CAPTURED_AT)
                with self.assertRaises(EvidenceCaptureError) as invalid_product:
                    capture_evidence_packet(
                        connection, f"ACM2 {PHRASE}", product_id="abc", captured_at=CAPTURED_AT
                    )
            self.assertEqual(spy.call_count, 0)
            self.assertEqual(invalid_query.exception.error_type, "invalid_request")
            self.assertEqual(invalid_product.exception.error_type, "invalid_request")

            with patch(
                "support_knowledge_engine.repository.retrieve_with_context",
                side_effect=RuntimeError("compute failed"),
            ) as failed:
                with self.assertRaises(EvidenceCaptureError) as failure:
                    capture_evidence_packet(connection, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT)
            self.assertEqual(failed.call_count, 1)
            self.assertEqual(failure.exception.error_type, "retrieval_failure")

            def slow_retrieval(connection, *args, **kwargs):
                del args, kwargs
                connection.execute(_SLOW_RETRIEVAL_SQL).fetchone()
                raise AssertionError("blocking retrieval finished instead of being interrupted")

            with patch(
                "support_knowledge_engine.repository.retrieve_with_context",
                side_effect=slow_retrieval,
            ) as timed:
                started = time.monotonic()
                with self.assertRaises(EvidenceCaptureError) as timeout:
                    capture_evidence_packet(
                        connection,
                        f"ACM2 {PHRASE}",
                        captured_at=CAPTURED_AT,
                        retrieval_deadline_s=0.25,
                    )
                elapsed = time.monotonic() - started
            self.assertEqual(timed.call_count, 1)
            self.assertLess(elapsed, 2.0)
            self.assertEqual(timeout.exception.error_type, "retrieval_timeout")
            self.assertEqual(_log_count(connection), logs)

            def missing_result(connection, request, **kwargs):
                del connection, request, kwargs
                return {"ok": True, "tool_name": "probe-tool", "result": None}

            def blank_tool_name(connection, request, **kwargs):
                envelope = execute_retrieval_tool(connection, request, **kwargs)
                envelope["tool_name"] = ""
                return envelope

            with patch("support_knowledge_engine.evidence.execute_retrieval_tool", missing_result):
                with self.assertRaises(EvidenceCaptureError) as malformed:
                    capture_evidence_packet(connection, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT)
            self.assertEqual(malformed.exception.error_type, "retrieval_failure")
            with patch("support_knowledge_engine.evidence.execute_retrieval_tool", blank_tool_name):
                with self.assertRaises(EvidenceCaptureError) as unnamed:
                    capture_evidence_packet(connection, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT)
            self.assertEqual(unnamed.exception.error_type, "retrieval_failure")
            self.assertEqual(_log_count(connection), logs)

    def test_missing_page_source_fails_capture(self) -> None:
        with connect_database(self.database) as connection:
            product_id = _product(connection, "AeroCam Mini 2", "ACM2")
            _document(
                connection,
                product_id,
                "handbook.pdf",
                ORIGINAL_PAGE,
                index_without_page=True,
            )
            with self.assertRaises(EvidenceCaptureError) as caught:
                capture_evidence_packet(connection, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT)
        self.assertEqual(caught.exception.error_type, "source_index_mismatch")

    def test_same_page_different_snippet_changes_evidence_id(self) -> None:
        tail = "quantum lantern qq-42"
        page = f"{PHRASE} service step " + ("gap token " * 40) + f"{tail} closing step"
        self._write_case(content=page)
        head_query = f"ACM2 {PHRASE}"
        tail_query = f"ACM2 {tail}"
        with connect_database(self.database) as connection:
            head = capture_evidence_packet(connection, head_query, captured_at=CAPTURED_AT)
            tail_packet = capture_evidence_packet(connection, tail_query, captured_at=CAPTURED_AT)
            again = capture_evidence_packet(
                connection, head_query, captured_at="2026-10-08T00:00:09+00:00"
            )
        head_evidence = head.evidence[0]
        tail_evidence = tail_packet.evidence[0]
        self.assertEqual(head_evidence.supporting_original_text, page)
        self.assertEqual(tail_evidence.supporting_original_text, page)
        self.assertEqual(head_evidence.original_content_digest, tail_evidence.original_content_digest)
        self.assertEqual(head_evidence.metadata_digest, tail_evidence.metadata_digest)
        self.assertEqual(head_evidence.pdf_sha256, tail_evidence.pdf_sha256)
        self.assertEqual(head_evidence.page_number, tail_evidence.page_number)
        self.assertNotEqual(
            head_evidence.decision_visible_representation,
            tail_evidence.decision_visible_representation,
        )
        self.assertIn(PHRASE, head_evidence.decision_visible_representation)
        self.assertIn(tail, tail_evidence.decision_visible_representation)
        self.assertNotEqual(head_evidence.decision_visible_digest, tail_evidence.decision_visible_digest)
        self.assertNotEqual(head_evidence.evidence_id, tail_evidence.evidence_id)
        self.assertEqual(head_evidence.evidence_id, again.evidence[0].evidence_id)
        self.assertEqual(
            head_evidence.decision_visible_digest, again.evidence[0].decision_visible_digest
        )
        self.assertNotEqual(head.captured_at, again.captured_at)

        def changed_tool(connection, request, **kwargs):
            envelope = execute_retrieval_tool(connection, request, **kwargs)
            envelope["tool_name"] = "probe-tool"
            envelope["tool_version"] = "probe-version"
            envelope["response_schema_version"] = "probe-response-schema"
            return envelope

        with connect_database(self.database) as connection:
            with patch(
                "support_knowledge_engine.evidence.execute_retrieval_tool",
                side_effect=changed_tool,
            ):
                altered = capture_evidence_packet(connection, head_query, captured_at=CAPTURED_AT)
        altered_evidence = altered.evidence[0]
        self.assertEqual(altered_evidence.decision_visible_digest, head_evidence.decision_visible_digest)
        self.assertEqual(altered_evidence.original_content_digest, head_evidence.original_content_digest)
        self.assertEqual(altered_evidence.metadata_digest, head_evidence.metadata_digest)
        self.assertEqual(altered_evidence.retrieval_tool_name, "probe-tool")
        self.assertEqual(altered_evidence.retrieval_tool_version, "probe-version")
        self.assertEqual(altered_evidence.retrieval_response_schema_version, "probe-response-schema")
        self.assertNotEqual(altered_evidence.evidence_id, head_evidence.evidence_id)

    def test_stale_index_does_not_become_evidence(self) -> None:
        divergent = "完全不同但非空的页面"
        self._write_case()
        with connect_database(self.database) as connection:
            connection.execute("UPDATE pages SET content = ?", (divergent,))
        with connect_database(self.database) as connection:
            direct = retrieve_with_context(connection, f"ACM2 {PHRASE}")
            self.assertTrue(direct["results"])
            self.assertIn(PHRASE, direct["results"][0]["snippet"])
            with patch(
                "support_knowledge_engine.repository.retrieve_with_context",
                wraps=retrieve_with_context,
            ) as spy:
                with self.assertRaises(EvidenceCaptureError) as caught:
                    capture_evidence_packet(connection, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT)
            self.assertEqual(spy.call_count, 1)
            self.assertEqual(caught.exception.error_type, "source_index_mismatch")
            self.assertEqual(connection.execute("SELECT content FROM pages").fetchone()[0], divergent)
            indexed = connection.execute("SELECT content FROM page_fts").fetchone()[0]
        self.assertIn(PHRASE, indexed)
        self.assertNotEqual(indexed, divergent)

    def test_duplicate_index_row_does_not_become_evidence(self) -> None:
        self._write_case()
        with connect_database(self.database) as connection:
            row = connection.execute(
                "SELECT document_id, page_number, content FROM page_fts"
            ).fetchone()
            connection.execute(
                "INSERT INTO page_fts (content, document_id, page_number) VALUES (?, ?, ?)",
                (row["content"], row["document_id"], row["page_number"]),
            )
        with connect_database(self.database) as connection:
            direct = retrieve_with_context(connection, f"ACM2 {PHRASE}")
            self.assertTrue(direct["results"])
            with patch(
                "support_knowledge_engine.repository.retrieve_with_context",
                wraps=retrieve_with_context,
            ) as spy:
                with self.assertRaises(EvidenceCaptureError) as caught:
                    capture_evidence_packet(connection, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT)
            self.assertEqual(spy.call_count, 1)
            self.assertEqual(caught.exception.error_type, "source_index_mismatch")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM page_fts").fetchone()[0], 2
            )

    def test_snapshot_drift_decision_keeps_the_original_packet(self) -> None:
        product_id = self._write_case(content=LONG_PAGE)
        with connect_database(self.database) as connection:
            packet = capture_evidence_packet(connection, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT)
            original = packet.evidence[0]
            saved_request = (
                packet.request.original_query,
                packet.request.normalized_query,
                packet.request.retrieval_query,
                packet.request.request_schema_version,
                packet.request.product_series,
                packet.request.document_type,
                packet.request.status,
                packet.request.association,
                packet.request.firmware_version,
            )
            mutated = f"{PHRASE} mutated marker " + ("replacement source sentence " * 30)
            connection.execute("DELETE FROM page_fts WHERE document_id = ?", (original.document_id,))
            connection.execute(
                "INSERT INTO page_fts (content, document_id, page_number) VALUES (?, ?, ?)",
                (mutated, original.document_id, original.page_number),
            )
            connection.execute(
                "UPDATE pages SET content = ? WHERE document_id = ?",
                (mutated, original.document_id),
            )
            connection.execute(
                """UPDATE documents
                   SET status = 'archived', authority_level = 'unlisted', firmware_range = '9.9.9'
                   WHERE id = ?""",
                (original.document_id,),
            )
            connection.execute(
                "UPDATE products SET status = 'archived' WHERE id = ?",
                (product_id,),
            )
        decision = decide_evidence(packet)
        self.assertEqual(decision.decision_type, DECISION_SUPPORTED)
        self.assertEqual(decision.retrieval_state, "high_confidence")
        self.assertEqual(decision.evidence_ids, (original.evidence_id,))
        self.assertEqual(packet.evidence[0].supporting_original_text, LONG_PAGE)
        self.assertEqual(packet.evidence[0].decision_visible_representation, original.decision_visible_representation)
        self.assertEqual(packet.evidence[0].original_content_digest, original.original_content_digest)
        self.assertEqual(packet.evidence[0].decision_visible_digest, original.decision_visible_digest)
        self.assertEqual(packet.evidence[0].metadata_digest, original.metadata_digest)
        self.assertEqual(packet.evidence[0].evidence_id, original.evidence_id)
        self.assertNotIn("mutated", packet.evidence[0].supporting_original_text)
        self.assertNotIn("mutated", packet.evidence[0].decision_visible_representation)
        self.assertEqual(packet.evidence[0].product_lifecycle, "active")
        self.assertEqual(packet.evidence[0].document_lifecycle, "effective")
        self.assertEqual(packet.evidence[0].authority_level, "reference")
        self.assertEqual(
            (
                packet.request.original_query,
                packet.request.normalized_query,
                packet.request.retrieval_query,
                packet.request.request_schema_version,
                packet.request.product_series,
                packet.request.document_type,
                packet.request.status,
                packet.request.association,
                packet.request.firmware_version,
            ),
            saved_request,
        )
        with connect_database(self.database) as connection:
            live = connection.execute("SELECT content FROM pages").fetchone()[0]
            self.assertEqual(live, mutated)
            later = capture_evidence_packet(connection, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT)
        later_decision = decide_evidence(later)
        later_evidence = later.evidence[0]
        self.assertEqual(later.retrieval_state, "outdated_only")
        self.assertEqual(later_decision.decision_type, DECISION_ABSTAIN)
        self.assertEqual(later_evidence.supporting_original_text, mutated)
        self.assertEqual(later_evidence.original_content_digest, _digest(mutated))
        self.assertEqual(
            later_evidence.decision_visible_digest,
            _digest(later_evidence.decision_visible_representation),
        )
        self.assertNotEqual(later_evidence.decision_visible_representation, mutated)
        self.assertNotEqual(later_evidence.decision_visible_digest, later_evidence.original_content_digest)
        self.assertNotEqual(later_evidence.supporting_original_text, LONG_PAGE)
        self.assertNotEqual(later_evidence.decision_visible_representation, original.decision_visible_representation)
        self.assertNotEqual(later_evidence.original_content_digest, original.original_content_digest)
        self.assertNotEqual(later_evidence.decision_visible_digest, original.decision_visible_digest)
        self.assertNotEqual(later_evidence.evidence_id, original.evidence_id)
        self.assertEqual(later_decision.evidence_ids, tuple(item.evidence_id for item in later.evidence))
        self.assertNotIn(original.evidence_id, later_decision.evidence_ids)
        self.assertEqual(later.request.original_query, saved_request[0])
        self.assertEqual(later.request.normalized_query, saved_request[1])
        self.assertEqual(later.request.retrieval_query, saved_request[2])
        self.assertEqual(later.request.request_schema_version, saved_request[3])
        self.assertEqual(later_evidence.product_lifecycle, "archived")
        self.assertEqual(later_evidence.document_lifecycle, "archived")
        self.assertEqual(later_evidence.authority_level, "unlisted")
        self.assertEqual(later_evidence.firmware_range, "9.9.9")
        self.assertEqual(later_evidence.firmware_applicability, "unknown")
        self.assertEqual(
            later_decision.reason_codes,
            (
                REASON_ARCHIVED_PRODUCT,
                REASON_AUTHORITY_UNKNOWN,
                REASON_FIRMWARE_UNKNOWN,
                REASON_OUTDATED_DOCUMENT,
            ),
        )

    def test_caller_transaction_is_not_rolled_back(self) -> None:
        with connect_database(self.database) as connection:
            product_id = _product(connection, "AeroCam Mini 2", "ACM2")
            _document(connection, product_id, "handbook.pdf", ORIGINAL_PAGE)
            packet = capture_evidence_packet(connection, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT)
            self.assertEqual(packet.retrieval_state, "high_confidence")
        with connect_database(self.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM products").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM pages").fetchone()[0], 1)

    def test_inactive_product_stays_high_confidence_and_is_not_supported(self) -> None:
        self._assert_lifecycle_blocks("inactive", REASON_INACTIVE_PRODUCT)

    def test_archived_product_stays_high_confidence_and_is_not_supported(self) -> None:
        self._assert_lifecycle_blocks("archived", REASON_ARCHIVED_PRODUCT)

    def test_planned_product_is_not_supported(self) -> None:
        self._assert_lifecycle_blocks("planned", REASON_PLANNED_PRODUCT)

    def test_archived_document_is_not_supported(self) -> None:
        self._write_case(document_status="archived")
        packet, decision = self._decide()
        direct = self._direct_state()
        self.assertEqual(direct, "outdated_only")
        self.assertEqual(packet.retrieval_state, "outdated_only")
        self.assertEqual(decision.decision_type, DECISION_ABSTAIN)
        self.assertEqual(decision.reason_codes, (REASON_OUTDATED_DOCUMENT,))
        self.assertEqual(packet.evidence[0].document_lifecycle, "archived")

    def test_outdated_only_is_not_supported(self) -> None:
        self._write_case(document_status="superseded")
        packet, decision = self._decide()
        self.assertEqual(packet.retrieval_state, "outdated_only")
        self.assertEqual(decision.decision_type, DECISION_ABSTAIN)
        self.assertNotEqual(decision.decision_type, DECISION_SUPPORTED)
        self.assertEqual(decision.reason_codes, (REASON_OUTDATED_DOCUMENT,))

    def test_needs_review_cannot_support(self) -> None:
        self._write_case(document_status="needs_review")
        packet, decision = self._decide()
        self.assertEqual(packet.retrieval_state, "possible_match")
        self.assertEqual(decision.decision_type, DECISION_ABSTAIN)
        self.assertEqual(decision.reason_codes, (REASON_DOCUMENT_NEEDS_REVIEW, REASON_POSSIBLE_MATCH))

    def test_draft_cannot_support(self) -> None:
        self._write_case(document_status="draft")
        packet, decision = self._decide()
        self.assertEqual(packet.retrieval_state, "possible_match")
        self.assertEqual(decision.decision_type, DECISION_ABSTAIN)
        self.assertEqual(decision.reason_codes, (REASON_DOCUMENT_DRAFT, REASON_POSSIBLE_MATCH))

    def test_effective_page_can_support_while_needs_review_page_cannot(self) -> None:
        with connect_database(self.database) as connection:
            product_id = _product(connection, "AeroCam Mini 2", "ACM2")
            _document(connection, product_id, "effective.pdf", ORIGINAL_PAGE)
            _document(
                connection,
                product_id,
                "review.pdf",
                ORIGINAL_PAGE,
                status="needs_review",
            )
        packet, decision = self._decide()
        self.assertEqual(packet.retrieval_state, "high_confidence")
        self.assertEqual(decision.decision_type, DECISION_SUPPORTED)
        self.assertEqual(decision.reason_codes, ())
        cited = [item for item in packet.evidence if item.evidence_id in decision.evidence_ids]
        self.assertEqual([item.document_lifecycle for item in cited], ["effective"])
        self.assertIn("needs_review", [item.document_lifecycle for item in packet.evidence])

    def test_unknown_document_lifecycle_cannot_support(self) -> None:
        self._write_case(document_status="retired")
        packet, decision = self._decide()
        self.assertEqual(packet.retrieval_state, "possible_match")
        self.assertEqual(
            decision.reason_codes,
            (REASON_DOCUMENT_LIFECYCLE_UNKNOWN, REASON_POSSIBLE_MATCH),
        )

    def test_matching_dotted_firmware_is_still_unknown(self) -> None:
        self._write_case(firmware_range="1.2.3")
        matched, matched_decision = self._decide(firmware_version="1.2.3")
        other, other_decision = self._decide(firmware_version="9.9.9")
        self.assertEqual(self._direct_state(), "high_confidence")
        self.assertEqual(matched.retrieval_state, "high_confidence")
        self.assertEqual(other.retrieval_state, "high_confidence")
        self.assertEqual(matched.evidence[0].firmware_applicability, "unknown")
        self.assertEqual(other.evidence[0].firmware_applicability, "unknown")
        self.assertEqual(matched_decision.decision_type, DECISION_ABSTAIN)
        self.assertEqual(other_decision.decision_type, DECISION_ABSTAIN)
        self.assertEqual(matched_decision.reason_codes, (REASON_FIRMWARE_UNKNOWN,))
        self.assertEqual(other_decision.reason_codes, (REASON_FIRMWARE_UNKNOWN,))
        self.assertNotIn(REASON_FIRMWARE_NOT_APPLICABLE, matched_decision.reason_codes)
        self.assertNotIn(REASON_FIRMWARE_NOT_APPLICABLE, other_decision.reason_codes)
        self.assertEqual(matched.evidence[0].evidence_id, other.evidence[0].evidence_id)

    def test_firmware_restriction_without_context_abstains(self) -> None:
        self._write_case(firmware_range="1.2.3")
        packet, decision = self._decide()
        self.assertEqual(packet.retrieval_state, "high_confidence")
        self.assertEqual(decision.decision_type, DECISION_ABSTAIN)
        self.assertEqual(decision.reason_codes, (REASON_FIRMWARE_UNKNOWN,))
        self.assertEqual(packet.evidence[0].firmware_applicability, "unknown")

    def test_unparsed_firmware_range_abstains_instead_of_matching(self) -> None:
        self._write_case(firmware_range="2.0.0 - 2.9.x")
        packet, decision = self._decide(firmware_version="2.0.0")
        self.assertEqual(packet.retrieval_state, "high_confidence")
        self.assertEqual(decision.decision_type, DECISION_ABSTAIN)
        self.assertEqual(decision.reason_codes, (REASON_FIRMWARE_UNKNOWN,))
        self.assertNotIn(REASON_FIRMWARE_NOT_APPLICABLE, decision.reason_codes)
        self.assertEqual(packet.evidence[0].firmware_range, "2.0.0 - 2.9.x")
        self.assertEqual(packet.evidence[0].firmware_applicability, "unknown")

    def test_unknown_authority_cannot_support_and_known_authority_can(self) -> None:
        self._write_case(authority_level="unlisted")
        packet, decision = self._decide()
        self.assertEqual(packet.retrieval_state, "high_confidence")
        self.assertEqual(decision.decision_type, DECISION_ABSTAIN)
        self.assertEqual(decision.reason_codes, (REASON_AUTHORITY_UNKNOWN,))
        self.tearDown()
        self.setUp()
        self._write_case(authority_level="authoritative")
        packet, decision = self._decide()
        self.assertEqual(decision.decision_type, DECISION_SUPPORTED)
        self.assertEqual(packet.evidence[0].authority_level, "authoritative")
        self.assertEqual(decision.reason_codes, ())

    def test_version_conflict_reason_is_preserved(self) -> None:
        with connect_database(self.database) as connection:
            product_id = _product(connection, "AeroCam Mini 2", "ACM2")
            _document(connection, product_id, "current.pdf", ORIGINAL_PAGE, status="effective")
            _document(connection, product_id, "old.pdf", ORIGINAL_PAGE, status="superseded")
        packet, decision = self._decide()
        self.assertEqual(packet.retrieval_state, "version_conflict")
        self.assertEqual(decision.decision_type, DECISION_CONFLICT)
        self.assertEqual(decision.retrieval_state, "version_conflict")
        self.assertIn(REASON_VERSION_CONFLICT, decision.reason_codes)
        self.assertEqual(
            decision.reason_codes,
            (REASON_OUTDATED_DOCUMENT, REASON_VERSION_CONFLICT),
        )
        self.assertEqual(decision.evidence_ids, tuple(item.evidence_id for item in packet.evidence))

    def test_ambiguous_product_and_alias_conflict_keep_distinct_reasons(self) -> None:
        with connect_database(self.database) as connection:
            _product(connection, "AeroCam Mini 2", "ACM2")
            _product(connection, "AeroCam Pro 2", "ACP2")
        packet, decision = self._decide(f"ACM2 ACP2 {PHRASE}")
        self.assertEqual(packet.retrieval_state, "ambiguous_product")
        self.assertFalse(packet.alias_conflict)
        self.assertEqual(decision.decision_type, DECISION_ABSTAIN)
        self.assertEqual(decision.reason_codes, (REASON_AMBIGUOUS_PRODUCT,))
        self.assertEqual(packet.evidence, ())

        with connect_database(self.database) as connection:
            pro = connection.execute(
                "SELECT id FROM products WHERE standard_name = 'AeroCam Pro 2'"
            ).fetchone()["id"]
            add_product_alias(connection, pro, "ACM2", "abbreviation", "g3 fixture", "g3")
        packet, decision = self._decide("ACM2 calibration")
        self.assertEqual(packet.retrieval_state, "ambiguous_product")
        self.assertTrue(packet.alias_conflict)
        self.assertEqual(decision.reason_codes, (REASON_AMBIGUOUS_PRODUCT_ALIAS,))
        self.assertNotIn(REASON_AMBIGUOUS_PRODUCT, decision.reason_codes)

    def test_insufficient_evidence_abstains(self) -> None:
        self._write_case()
        packet, decision = self._decide("quantum toaster zz-999")
        self.assertEqual(packet.retrieval_state, "insufficient_evidence")
        self.assertEqual(decision.decision_type, DECISION_ABSTAIN)
        self.assertEqual(decision.reason_codes, (REASON_INSUFFICIENT_EVIDENCE,))
        self.assertEqual(decision.evidence_ids, ())
        self.assertEqual(packet.evidence, ())

    def test_g3_adds_no_migration(self) -> None:
        self.assertEqual([version for version, _name, _fn in MIGRATIONS][:3], [1, 2, 3])

    def _assert_lifecycle_blocks(self, status: str, reason: str) -> None:
        self._write_case(product_status=status)
        direct = self._direct_state()
        packet, decision = self._decide()
        self.assertEqual(direct, "high_confidence")
        self.assertEqual(packet.retrieval_state, "high_confidence")
        self.assertEqual(decision.decision_type, DECISION_ABSTAIN)
        self.assertEqual(decision.reason_codes, (reason,))
        self.assertEqual(packet.evidence[0].product_lifecycle, status)

    def _decide(
        self, query: str | None = None, firmware_version: str | None = None
    ) -> tuple[EvidencePacket, object]:
        with connect_database(self.database) as connection:
            packet = capture_evidence_packet(
                connection,
                query or f"ACM2 {PHRASE}",
                firmware_version=firmware_version,
                captured_at=CAPTURED_AT,
            )
        return packet, decide_evidence(packet)

    def _direct_state(self) -> str:
        with connect_database(self.database) as connection:
            result = retrieve_with_context(connection, f"ACM2 {PHRASE}")
        return str(result["match_state"])

    def _write_case(
        self,
        *,
        product_status: str = "active",
        document_status: str = "effective",
        firmware_range: str = "",
        authority_level: str = "reference",
        filename: str = "handbook.pdf",
        sha_name: str | None = None,
        content: str = ORIGINAL_PAGE,
    ) -> int:
        with connect_database(self.database) as connection:
            product_id = _product(connection, "AeroCam Mini 2", "ACM2", status=product_status)
            _document(
                connection,
                product_id,
                filename,
                content,
                status=document_status,
                firmware_range=firmware_range,
                authority_level=authority_level,
                sha_name=sha_name,
            )
        return product_id


class CorpusProductConflictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temp.name) / "corpus.db"
        seed_demo_data(cls.database, SAMPLE_DIR)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def test_explicit_mini3_filter_keeps_retrieval_state_and_refuses_support(self) -> None:
        packet, decision, tool = self._capture()
        self.assertTrue(tool["ok"])
        self.assertEqual(tool["response_schema_version"], "1")
        self.assertEqual(tool["result"]["match_state"], "high_confidence")
        self.assertEqual(tool["result"]["original_query"], COUNTEREXAMPLE_QUERY)
        self.assertEqual(packet.retrieval_state, "high_confidence")
        self.assertEqual(packet.retrieval_tool_name, tool["tool_name"])
        self.assertEqual(packet.retrieval_tool_version, tool["tool_version"])
        self.assertEqual(packet.retrieval_response_schema_version, tool["response_schema_version"])
        self.assertEqual(packet.request.request_schema_version, tool["request_schema_version"])
        self.assertEqual(packet.request.original_query, tool["result"]["original_query"])
        self.assertEqual(packet.request.normalized_query, tool["result"]["normalized_query"])
        self.assertEqual(packet.request.retrieval_query, tool["result"]["retrieval_query"])
        self.assertEqual(decision.decision_type, DECISION_ABSTAIN)
        self.assertNotEqual(decision.decision_type, DECISION_SUPPORTED)
        self.assertEqual(
            decision.reason_codes,
            (REASON_EVIDENCE_PRODUCT_MISMATCH, REASON_PRODUCT_FILTER_CONFLICT),
        )
        self.assertEqual(packet.request.explicit_product_name, "AeroCam Mini 3")
        self.assertEqual(packet.recognized_products[0].name, "AeroCam Pro 2")

    def test_evidence_product_mismatch_is_not_supported(self) -> None:
        packet, decision, _tool = self._capture()
        evidence = packet.evidence[0]
        self.assertEqual(packet.retrieval_state, "high_confidence")
        self.assertNotEqual(evidence.canonical_product_id, packet.recognized_products[0].product_id)
        self.assertEqual(evidence.canonical_product_name, "AeroCam Mini 3")
        self.assertEqual(evidence.filename, MINI3_HANDBOOK)
        self.assertEqual(evidence.page_number, 2)
        self.assertEqual(evidence.pdf_sha256, MINI3_SHA256)
        self.assertIn("AM3-18", evidence.supporting_original_text)
        self.assertIn("AM3-18", evidence.decision_visible_representation)
        self.assertEqual(evidence.supporting_text_source, SUPPORTING_TEXT_SOURCE)
        self.assertEqual(evidence.decision_visible_source, DECISION_VISIBLE_SOURCE)
        self.assertEqual(evidence.document_identity, f"sha256:{MINI3_SHA256}")
        self.assertEqual(evidence.source_locator, f"sha256:{MINI3_SHA256}#page=2")
        self.assertNotEqual(evidence.supporting_original_text, evidence.decision_visible_representation)
        self.assertEqual(decision.decision_type, DECISION_ABSTAIN)
        self.assertIn(REASON_EVIDENCE_PRODUCT_MISMATCH, decision.reason_codes)
        self.assertIn(evidence.evidence_id, decision.evidence_ids)
        self.assertEqual(decision.evidence_ids, tuple(item.evidence_id for item in packet.evidence))

    def _capture(self):
        with connect_database(self.database) as connection:
            mini_id = connection.execute(
                "SELECT id FROM products WHERE standard_name = 'AeroCam Mini 3'"
            ).fetchone()["id"]
            tool = execute_retrieval_tool(
                connection,
                {
                    "request_schema_version": "1",
                    "query": COUNTEREXAMPLE_QUERY,
                    "product_id": str(mini_id),
                },
            )
            direct = retrieve_with_context(
                connection, COUNTEREXAMPLE_QUERY, product_id=str(mini_id)
            )
            packet = capture_evidence_packet(
                connection,
                COUNTEREXAMPLE_QUERY,
                product_id=str(mini_id),
                captured_at=CAPTURED_AT,
            )
            evidence = packet.evidence[0]
            page_text = connection.execute(
                "SELECT content FROM pages WHERE document_id = ? AND page_number = ?",
                (evidence.document_id, evidence.page_number),
            ).fetchone()["content"]
        self.assertEqual(direct["match_state"], "high_confidence")
        self.assertEqual(evidence.supporting_original_text, page_text)
        self.assertEqual(evidence.original_content_digest, _digest(page_text))
        self.assertEqual(evidence.decision_visible_representation, direct["results"][0]["snippet"])
        self.assertEqual(evidence.decision_visible_representation, tool["result"]["results"][0]["snippet"])
        self.assertEqual(evidence.decision_visible_digest, _digest(direct["results"][0]["snippet"]))
        self.assertEqual(packet.request.explicit_product_id, mini_id)
        return packet, decide_evidence(packet), tool


def _product(connection, name: str, alias: str, status: str = "active") -> int:
    product_id = create_product(
        connection,
        {"standard_name": name, "product_series": "AeroCam", "status": status},
        "g3 fixture",
        "g3",
    )
    add_product_alias(connection, product_id, alias, "abbreviation", "g3 fixture", "g3")
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
    sha_name: str | None = None,
    index_without_page: bool = False,
) -> int:
    digest = hashlib.sha256((sha_name or filename).encode("utf-8")).hexdigest()
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
    if not index_without_page:
        connection.execute(
            "INSERT INTO pages (document_id, page_number, content) VALUES (?, 1, ?)",
            (document_id, content),
        )
    connection.execute(
        "INSERT INTO page_fts (content, document_id, page_number) VALUES (?, ?, ?)",
        (content, document_id, 1),
    )
    return document_id


class _StatementTracer:
    """Records SQL issued after retrieval while forwarding to the real connection."""

    def __init__(self, inner):
        self.inner = inner
        self.armed = False
        self.statements: list[str] = []

    @property
    def in_transaction(self) -> bool:
        return self.inner.in_transaction

    def execute(self, sql, *args, **kwargs):
        if self.armed:
            self.statements.append(str(sql))
        return self.inner.execute(sql, *args, **kwargs)

    def rollback(self):
        return self.inner.rollback()


def _table_names(connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {row[0] for row in rows}


def _log_count(connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM search_logs").fetchone()[0])


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class SnapshotFieldTests(unittest.TestCase):
    def test_snapshot_and_packet_fields_cover_the_contract(self) -> None:
        self.assertEqual(SNAPSHOT_SCHEMA_VERSION, "3")
        self.assertEqual(PACKET_SCHEMA_VERSION, "2")
        self.assertEqual(TRANSFORMATION_VERSION, "retrieval-excerpt-v1")
        self.assertEqual(SUPPORTING_TEXT_SOURCE, "page-content")
        self.assertEqual(DECISION_VISIBLE_SOURCE, "G2 retrieval snippet")
        self.assertTrue(
            {
                "evidence_id",
                "snapshot_schema_version",
                "document_id",
                "document_identity",
                "filename",
                "pdf_sha256",
                "page_number",
                "source_locator",
                "source_url",
                "supporting_original_text",
                "supporting_text_source",
                "original_content_digest",
                "metadata_digest",
                "canonical_product_id",
                "canonical_product_name",
                "product_lifecycle",
                "document_lifecycle",
                "firmware_range",
                "firmware_applicability",
                "authority_level",
                "retrieval_tool_name",
                "retrieval_tool_version",
                "retrieval_response_schema_version",
                "captured_at",
                "decision_visible_representation",
                "decision_visible_digest",
                "decision_visible_source",
                "transformation_version",
            }
            <= set(EvidenceSnapshot.__dataclass_fields__)
        )
        self.assertTrue(
            {
                "packet_schema_version",
                "retrieval_state",
                "recognized_products",
                "request",
                "alias_conflict",
                "evidence",
                "captured_at",
                "retrieval_tool_name",
                "retrieval_tool_version",
                "retrieval_response_schema_version",
            }
            <= set(EvidencePacket.__dataclass_fields__)
        )
        self.assertTrue(
            {
                "original_query",
                "normalized_query",
                "retrieval_query",
                "request_schema_version",
                "explicit_product_id",
                "explicit_product_name",
                "explicit_product_lifecycle",
                "firmware_version",
                "product_series",
                "document_type",
                "status",
                "association",
            }
            <= set(RequestContext.__dataclass_fields__)
        )
