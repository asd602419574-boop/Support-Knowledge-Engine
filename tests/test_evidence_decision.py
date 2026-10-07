from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from support_knowledge_engine.db import connect_database, init_database
from support_knowledge_engine.demo_data import seed_demo_data
from support_knowledge_engine.evidence import (
    DECISION_ABSTAIN,
    DECISION_CONFLICT,
    DECISION_SUPPORTED,
    FIRMWARE_GRAMMAR,
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
    SUPPORTING_TEXT_SOURCE,
    TRANSFORMATION_IDENTITY,
    EvidencePacket,
    EvidenceSnapshot,
    capture_evidence_packet,
    classify_firmware,
    decide_evidence,
)
from support_knowledge_engine.governance import add_product_alias, create_product
from support_knowledge_engine.migrations import MIGRATIONS
from support_knowledge_engine.repository import retrieve_with_context
from support_knowledge_engine.retrieval_tool import (
    RESPONSE_SCHEMA_VERSION,
    TOOL_NAME,
    TOOL_VERSION,
    execute_retrieval_tool,
)
from tests.helpers import SAMPLE_DIR


PHRASE = "calibration beacon zz-17"
CAPTURED_AT = "2026-10-08T00:00:00+00:00"
COUNTEREXAMPLE_QUERY = "ACP2 compass pulse AM3-18"
MINI3_HANDBOOK = "AeroCam-Mini-3_Service-Handbook_v2.0_en-US.pdf"
MINI3_SHA256 = "45d80e2cef7add5eaa3297e900ef4e00b5fcf5d0f4d7f14fbb5e1d06dccd6cba"


class FirmwareGrammarTests(unittest.TestCase):
    def test_only_exact_dotted_versions_are_decidable(self) -> None:
        self.assertEqual(FIRMWARE_GRAMMAR, "exact-dotted-v1")
        self.assertEqual(classify_firmware("", None), "applicable")
        self.assertEqual(classify_firmware("1.2.3", None), "unknown")
        self.assertEqual(classify_firmware("1.2.3", "1.2.3"), "applicable")
        self.assertEqual(classify_firmware("1.2.3", "9.9.9"), "not_applicable")
        self.assertEqual(classify_firmware("2.0.0 - 2.9.x", "2.0.0"), "unknown")
        self.assertEqual(classify_firmware("2.9.x", "2.9.0"), "unknown")
        self.assertEqual(classify_firmware("AP2-44", "AP2-44"), "unknown")


class EvidenceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "g3.db"
        init_database(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_supported_snapshot_is_stable_and_capture_does_not_write(self) -> None:
        self._write_case()
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
        self.assertEqual(packet.evidence[0].original_content_digest, again.evidence[0].original_content_digest)
        snapshot = packet.evidence[0]
        self.assertEqual(snapshot.supporting_text_source, SUPPORTING_TEXT_SOURCE)
        self.assertIn(PHRASE, snapshot.supporting_original_text)
        self.assertEqual(snapshot.decision_visible_representation, snapshot.supporting_original_text)
        self.assertEqual(snapshot.transformation_version, TRANSFORMATION_IDENTITY)
        self.assertEqual(snapshot.original_content_digest, _digest(snapshot.supporting_original_text))
        self.assertEqual(snapshot.decision_visible_digest, snapshot.original_content_digest)
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

    def test_retrieval_is_called_once_and_decision_does_not_query(self) -> None:
        self._write_case()
        with connect_database(self.database) as connection:
            with patch(
                "support_knowledge_engine.evidence.repository.retrieve_with_context",
                wraps=retrieve_with_context,
            ) as spy:
                packet = capture_evidence_packet(connection, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT)
            self.assertEqual(spy.call_count, 1)
        decision = decide_evidence(packet)
        self.assertEqual(decision.decision_type, DECISION_SUPPORTED)
        self.assertEqual(decide_evidence.__code__.co_varnames[: decide_evidence.__code__.co_argcount], ("packet",))

    def test_capture_reads_product_facts_without_rereading_page_source(self) -> None:
        self._write_case()
        with connect_database(self.database) as connection:
            direct = retrieve_with_context(connection, f"ACM2 {PHRASE}")
            tracer = _StatementTracer(connection)

            def arm_after_retrieval(connection, *args, **kwargs):
                try:
                    return retrieve_with_context(connection.inner, *args, **kwargs)
                finally:
                    connection.armed = True

            with patch(
                "support_knowledge_engine.evidence.repository.retrieve_with_context",
                arm_after_retrieval,
            ):
                packet = capture_evidence_packet(tracer, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT)
            logged = tracer.statements
        self.assertTrue(logged)
        self.assertTrue(any("products" in sql.casefold() for sql in logged))
        for sql in logged:
            folded = sql.casefold()
            self.assertNotIn("pages", folded)
            self.assertNotIn("documents", folded)
            self.assertNotIn("page_fts", folded)
        self.assertEqual(
            packet.evidence[0].supporting_original_text,
            direct["results"][0]["snippet"],
        )

    def test_snapshot_drift_decision_keeps_the_original_packet(self) -> None:
        product_id = self._write_case()
        with connect_database(self.database) as connection:
            packet = capture_evidence_packet(connection, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT)
            original = packet.evidence[0]
            saved = (
                original.supporting_original_text,
                original.original_content_digest,
                original.metadata_digest,
                original.evidence_id,
                original.product_lifecycle,
                original.document_lifecycle,
                original.authority_level,
                original.firmware_range,
            )
            mutated = f"{PHRASE} mutated marker"
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
        self.assertEqual(decision.evidence_ids, (saved[3],))
        self.assertEqual(packet.evidence[0].supporting_original_text, saved[0])
        self.assertNotIn("mutated", packet.evidence[0].supporting_original_text)
        self.assertEqual(packet.evidence[0].original_content_digest, saved[1])
        self.assertEqual(packet.evidence[0].metadata_digest, saved[2])
        self.assertEqual(packet.evidence[0].evidence_id, saved[3])
        self.assertEqual(packet.evidence[0].product_lifecycle, "active")
        self.assertEqual(packet.evidence[0].document_lifecycle, "effective")
        self.assertEqual(packet.evidence[0].authority_level, "reference")
        with connect_database(self.database) as connection:
            live = connection.execute("SELECT content FROM pages").fetchone()[0]
            self.assertEqual(live, mutated)
            later = capture_evidence_packet(connection, f"ACM2 {PHRASE}", captured_at=CAPTURED_AT)
        later_decision = decide_evidence(later)
        self.assertEqual(later.retrieval_state, "outdated_only")
        self.assertEqual(later_decision.decision_type, DECISION_ABSTAIN)
        self.assertNotEqual(later.evidence[0].supporting_original_text, saved[0])
        self.assertNotEqual(later.evidence[0].original_content_digest, saved[1])
        self.assertNotEqual(later.evidence[0].evidence_id, saved[3])
        self.assertEqual(later.evidence[0].product_lifecycle, "archived")
        self.assertEqual(later.evidence[0].document_lifecycle, "archived")
        self.assertEqual(later.evidence[0].authority_level, "unlisted")
        self.assertEqual(later.evidence[0].firmware_range, "9.9.9")
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
            _document(connection, product_id, "handbook.pdf", f"{PHRASE} service step")
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
            _document(connection, product_id, "effective.pdf", f"{PHRASE} service step")
            _document(
                connection,
                product_id,
                "review.pdf",
                f"{PHRASE} service step",
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

    def test_incompatible_firmware_is_not_supported(self) -> None:
        self._write_case(firmware_range="1.2.3")
        packet, decision = self._decide(firmware_version="9.9.9")
        self.assertEqual(self._direct_state(), "high_confidence")
        self.assertEqual(packet.retrieval_state, "high_confidence")
        self.assertEqual(decision.decision_type, DECISION_ABSTAIN)
        self.assertEqual(decision.reason_codes, (REASON_FIRMWARE_NOT_APPLICABLE,))
        self.assertEqual(packet.evidence[0].firmware_applicability, "not_applicable")

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
        self.assertEqual(packet.evidence[0].firmware_range, "2.0.0 - 2.9.x")

    def test_exact_firmware_match_can_support(self) -> None:
        self._write_case(firmware_range="1.2.3")
        packet, decision = self._decide(firmware_version="1.2.3")
        self.assertEqual(decision.decision_type, DECISION_SUPPORTED)
        self.assertEqual(packet.evidence[0].firmware_applicability, "applicable")

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
            _document(connection, product_id, "current.pdf", f"{PHRASE} service step", status="effective")
            _document(connection, product_id, "old.pdf", f"{PHRASE} service step", status="superseded")
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
        self.assertEqual([version for version, _name, _fn in MIGRATIONS], [1, 2, 3])

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
    ) -> int:
        with connect_database(self.database) as connection:
            product_id = _product(connection, "AeroCam Mini 2", "ACM2", status=product_status)
            _document(
                connection,
                product_id,
                filename,
                f"{PHRASE} service step",
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
        self.assertEqual(evidence.document_identity, f"sha256:{MINI3_SHA256}")
        self.assertEqual(evidence.source_locator, f"sha256:{MINI3_SHA256}#page=2")
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
        self.assertEqual(direct["match_state"], "high_confidence")
        self.assertEqual(packet.evidence[0].supporting_original_text, direct["results"][0]["snippet"])
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
                "evidence",
                "captured_at",
            }
            <= set(EvidencePacket.__dataclass_fields__)
        )
