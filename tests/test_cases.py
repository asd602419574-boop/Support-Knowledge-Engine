from __future__ import annotations

import ast
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import fitz

from support_knowledge_engine.backup import create_backup, restore_backup, verify_backup
from support_knowledge_engine.cases import (
    CASE_INVALID,
    CASE_INVALID_MESSAGE,
    CASE_NOT_DURABLE,
    CASE_NOT_DURABLE_MESSAGE,
    CLASS_A_POLICY,
    CLASS_A_POLICY_TEXT,
    CLASS_B_POLICY,
    CLASS_B_POLICY_TEXT,
    CLASS_C_CONTEXT_POLICY,
    CaseStoreError,
    allocate_case_id,
    create_case,
    read_case,
    write_case_context,
)
from support_knowledge_engine.cases import _RETRIEVAL_STATES
from support_knowledge_engine.db import connect_database, init_database
from support_knowledge_engine.evidence import (
    DECISION_ABSTAIN,
    DECISION_CONFLICT,
    DECISION_SUPPORTED,
    PACKET_SCHEMA_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    EvidenceDecision,
    EvidencePacket,
    EvidenceSnapshot,
    RequestContext,
    evidence_binding,
    snapshot_integrity_ok,
)
from support_knowledge_engine.migrations import (
    MIGRATION_005_DATA_LOSS,
    MIGRATION_006_DATA_LOSS,
    _migration_005_case_store,
    _migration_006_case_evidence_fidelity,
    current_schema_version,
)
from support_knowledge_engine.retrieval_tool import (
    RESPONSE_SCHEMA_VERSION,
    TOOL_NAME,
    TOOL_VERSION,
)
from support_knowledge_engine.importer import import_directory
from support_knowledge_engine.repository import MATCH_STATE_LABELS
from support_knowledge_engine.runtime import run_runtime
from support_knowledge_engine.trace import (
    REDACTION_REFUSAL,
    TracePersistenceError,
    commit_class_c_trace,
    execute_traced_runtime,
    project_class_c_record,
)
from tests.helpers import PROJECT_ROOT
from tests.test_runtime_trace import (
    ORIGINAL_PAGE,
    QUERY,
    SECRET_EMAIL,
    _document,
    _migrate_through,
    _product,
    _request,
    _seed_durable_document,
)


_RAW_QUERY = "13800138000 raw query"
_ILLEGAL_CASE_IDS = ("13800138000", "T12345", "9F3K2LQ8")


class CaseContractTests(unittest.TestCase):
    def test_case_store_does_not_read_live_knowledge_or_request_text(self) -> None:
        source = (PROJECT_ROOT / "support_knowledge_engine" / "cases.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        for name in (
            "execute_retrieval_tool",
            "retrieve_with_context",
            "capture_evidence_packet",
            "decide_evidence",
            "run_runtime",
            "insert_search_log",
        ):
            self.assertNotIn(name, calls)
        for token in (
            "original_query",
            "normalized_query",
            "retrieval_query",
            "FROM pages",
            "FROM documents",
            "FROM page_fts",
            "FROM search_logs",
            "FROM products",
            "page_fts",
            "investigating",
            "resolved",
            "abstained",
        ):
            self.assertNotIn(token, source, token)
        self.assertEqual(_RETRIEVAL_STATES, frozenset(MATCH_STATE_LABELS))
        self.assertIn("Class A", CLASS_A_POLICY_TEXT)
        self.assertIn("search_logs", CLASS_A_POLICY_TEXT)
        self.assertIn("Class B", CLASS_B_POLICY_TEXT)
        self.assertIn("class-C", CLASS_C_CONTEXT_POLICY)
        self.assertIn("snapshot_integrity_ok", source)
        self.assertNotIn("hashlib", source)
        for relative in (
            "support_knowledge_engine/importer.py",
            "support_knowledge_engine/routes.py",
            "support_knowledge_engine/__init__.py",
        ):
            text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("create_case", text)
            self.assertNotIn("support_cases", text)
        migration = (PROJECT_ROOT / "support_knowledge_engine" / "migrations.py").read_text(
            encoding="utf-8"
        )
        body = migration.split("def _migration_005_case_store", 1)[1].split(
            "\nMIGRATIONS", 1
        )[0]
        for token in ("page_fts", "search_logs", "executescript", "DROP TABLE documents"):
            self.assertNotIn(token, body, token)


class CaseBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "g6.db"
        init_database(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_captured_evidence_survives_live_edits_and_search_log_rebuild(self) -> None:
        self._write_case(content=f"{ORIGINAL_PAGE} {SECRET_EMAIL}")
        case_id = allocate_case_id()
        with connect_database(self.database) as connection:
            execution = execute_traced_runtime(connection, _request(), case_id=case_id)
        self.assertTrue(execution.trace_ok, execution.error)
        self.assertTrue(execution.runtime.ok)
        packet = execution.runtime.packet
        decision = execution.runtime.decision
        assert packet is not None and decision is not None
        frozen = packet.evidence[0]
        self.assertIn(SECRET_EMAIL, frozen.supporting_original_text)
        self.assertEqual(frozen.document_lifecycle, "effective")
        self.assertEqual(frozen.authority_level, "reference")
        self.assertEqual(frozen.product_lifecycle, "active")

        with connect_database(self.database) as connection:
            trace = connection.execute(
                "SELECT * FROM runtime_traces WHERE run_id = ?",
                (execution.run_id,),
            ).fetchone()
            self.assertEqual(trace["case_id"], case_id)
            trace_blob = _joined(trace)
            self.assertNotIn(SECRET_EMAIL, trace_blob)
            self.assertNotIn(frozen.supporting_original_text, trace_blob)
            self.assertNotIn(frozen.filename, trace_blob)
            self.assertNotIn(frozen.source_url, trace_blob)
            self.assertNotIn(frozen.canonical_product_name or "", trace_blob)
            for log in connection.execute("SELECT * FROM search_logs"):
                log_blob = _joined(log)
                self.assertNotIn(SECRET_EMAIL, log_blob)
                self.assertNotIn(frozen.supporting_original_text, log_blob)
                self.assertNotIn(frozen.filename, log_blob)
                self.assertNotIn(frozen.source_url, log_blob)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM support_cases").fetchone()[0],
                0,
            )
            connection.execute(
                "UPDATE pages SET content = ? WHERE document_id = ?",
                ("mutated page body", frozen.document_id),
            )
            connection.execute(
                "UPDATE page_fts SET content = ? WHERE document_id = ?",
                ("mutated page body", frozen.document_id),
            )
            connection.execute(
                """UPDATE documents
                   SET status = 'archived', authority_level = 'authoritative'
                   WHERE id = ?""",
                (frozen.document_id,),
            )
            connection.execute(
                "UPDATE products SET status = 'archived' WHERE id = ?",
                (frozen.canonical_product_id,),
            )
            connection.execute("DELETE FROM search_logs")
            connection.commit()
            self.assertFalse(connection.in_transaction)
            before = _knowledge(connection)
            with patch(
                "support_knowledge_engine.repository.retrieve_with_context"
            ) as core:
                created = create_case(
                    connection,
                    packet,
                    decision,
                    case_id=case_id,
                    run_id=execution.run_id,
                    note=_RAW_QUERY,
                )
            self.assertEqual(core.call_count, 0)
            self.assertEqual(_knowledge(connection), before)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM search_logs").fetchone()[0],
                0,
            )

        self.assertEqual(created.case_id, case_id)
        self.assertEqual(created.trace_run_ids, (execution.run_id,))
        self.assertEqual(created.evidence[0].supporting_original_text, frozen.supporting_original_text)
        self.assertEqual(created.evidence[0].document_lifecycle, "effective")
        self.assertEqual(created.evidence[0].authority_level, "reference")
        self.assertEqual(created.evidence[0].product_lifecycle, "active")
        self.assertEqual(created.evidence[0].source_policy, CLASS_A_POLICY)
        self.assertEqual(created.evidence[0].visible_policy, CLASS_B_POLICY)
        self.assertEqual(
            created.evidence[0].decision_visible_representation,
            frozen.decision_visible_representation,
        )
        self.assertTrue(str(created.context.note_marker).startswith("redacted:"))
        self.assertNotIn(_RAW_QUERY, created.context.note_marker or "")
        self.assertNotIn(SECRET_EMAIL, json.dumps(created.context.__dict__))
        self.assertEqual(
            set(json.loads(_case_context_blob(self.database, case_id))),
            {"note_marker", "schema_version"},
        )
        context_blob = _case_context_blob(self.database, case_id)
        self.assertNotIn(frozen.supporting_original_text, context_blob)
        self.assertNotIn(frozen.filename, context_blob)
        self.assertNotIn(frozen.source_url, context_blob)
        self.assertNotIn(frozen.canonical_product_name or "", context_blob)

        reopened = _reopen_case(self.database, case_id)
        self.assertTrue(snapshot_integrity_ok(_as_snapshot(reopened.evidence[0])))
        self.assertEqual(reopened.evidence[0].evidence_id, frozen.evidence_id)
        self.assertEqual(reopened.evidence[0].metadata_digest, frozen.metadata_digest)
        self.assertEqual(reopened.evidence[0].firmware_range, frozen.firmware_range)
        self.assertEqual(
            reopened.evidence[0].firmware_applicability, frozen.firmware_applicability
        )
        self.assertEqual(reopened.evidence[0].retrieval_tool_name, frozen.retrieval_tool_name)
        self.assertEqual(
            reopened.evidence[0].retrieval_tool_version, frozen.retrieval_tool_version
        )
        self.assertEqual(
            reopened.evidence[0].retrieval_response_schema_version,
            frozen.retrieval_response_schema_version,
        )
        self.assertEqual(reopened.evidence[0].transformation_version, frozen.transformation_version)
        self.assertEqual(reopened.evidence[0].source_locator, frozen.source_locator)
        self.assertEqual(reopened.evidence[0].filename, frozen.filename)
        self.assertEqual(reopened.evidence[0].document_id, frozen.document_id)
        self.assertEqual(
            reopened.evidence[0].supporting_original_text,
            frozen.supporting_original_text,
        )
        self.assertEqual(reopened.evidence[0].document_lifecycle, "effective")
        self.assertEqual(reopened.evidence[0].authority_level, "reference")
        self.assertNotIn(SECRET_EMAIL, reopened.context.note_marker or "")
        self.assertNotIn(_RAW_QUERY, _case_context_blob(self.database, case_id))
        with connect_database(self.database) as connection:
            current = connection.execute(
                "SELECT content FROM pages WHERE document_id = ?",
                (frozen.document_id,),
            ).fetchone()
            document = connection.execute(
                "SELECT status, authority_level FROM documents WHERE id = ?",
                (frozen.document_id,),
            ).fetchone()
            self.assertEqual(current["content"], "mutated page body")
            self.assertEqual(document["status"], "archived")
            self.assertEqual(document["authority_level"], "authoritative")
            log_sql = connection.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type = 'table' AND name = 'search_logs'"""
            ).fetchone()[0]
            connection.execute("DROP TABLE search_logs")
            connection.commit()
            after_drop = read_case(connection, case_id)
            connection.execute(log_sql)
            connection.commit()
        self.assertEqual(
            after_drop.evidence[0].supporting_original_text,
            frozen.supporting_original_text,
        )
        self.assertEqual(after_drop.evidence[0].document_lifecycle, "effective")
        self.assertEqual(after_drop.evidence[0].authority_level, "reference")

        with connect_database(self.database) as connection:
            before_evidence = connection.execute(
                "SELECT supporting_original_text, document_lifecycle, authority_level FROM case_evidence"
            ).fetchall()
            updated = write_case_context(connection, case_id, "")
            after_evidence = connection.execute(
                "SELECT supporting_original_text, document_lifecycle, authority_level FROM case_evidence"
            ).fetchall()
            self.assertEqual(list(before_evidence), list(after_evidence))
        self.assertIsNone(updated.context.note_marker)
        self.assertEqual(updated.evidence[0].supporting_original_text, frozen.supporting_original_text)

    def test_request_text_is_not_copied_and_illegal_case_id_is_rejected(self) -> None:
        packet, decision = _packet(original_query=_RAW_QUERY)
        with connect_database(self.database) as connection:
            created = create_case(connection, packet, decision, note="T12345")
            self.assertNotIn(_RAW_QUERY, _case_blob(connection))
            self.assertTrue(str(created.context.note_marker).startswith("redacted:"))
            self.assertNotIn("T12345", created.context.note_marker or "")
            with self.assertRaises(CaseStoreError) as caught:
                create_case(connection, packet, decision, case_id="13800138000")
            self.assertEqual(caught.exception.error_type, CASE_INVALID)
            self.assertEqual(str(caught.exception), CASE_INVALID_MESSAGE)
            self.assertNotIn("13800138000", str(caught.exception))
        self.assertNotIn(_RAW_QUERY, Path(self.database).read_bytes().decode("utf-8", "ignore"))
        reopened = _reopen_case(self.database, created.case_id)
        self.assertNotIn("T12345", reopened.context.note_marker or "")
        self.assertEqual(reopened.evidence[0].supporting_original_text, "frozen support text")

        with connect_database(self.database) as connection:
            for illegal in _ILLEGAL_CASE_IDS:
                execution = execute_traced_runtime(connection, {"query": QUERY}, case_id=illegal)
                self.assertFalse(execution.trace_ok)
                self.assertIsNone(execution.run_id)
                assert execution.error is not None
                self.assertEqual(execution.error.type, REDACTION_REFUSAL)
                self.assertNotIn(illegal, execution.error.message)
                assert execution.runtime.error is not None
                self.assertEqual(execution.runtime.error.type, "invalid_request")
            legal = allocate_case_id()
            execution = execute_traced_runtime(connection, {"query": QUERY}, case_id=legal)
            self.assertTrue(execution.trace_ok, execution.error)
            assert execution.runtime.error is not None
            self.assertEqual(execution.runtime.error.type, "invalid_request")
        with connect_database(self.database) as connection:
            row = connection.execute(
                "SELECT case_id, input_json FROM runtime_traces WHERE run_id = ?",
                (execution.run_id,),
            ).fetchone()
            leaked = connection.execute(
                """SELECT COUNT(*) FROM runtime_traces
                   WHERE case_id IN (?, ?, ?)
                      OR input_json LIKE ?
                      OR input_json LIKE ?
                      OR input_json LIKE ?""",
                (*_ILLEGAL_CASE_IDS, "%13800138000%", "%T12345%", "%9F3K2LQ8%"),
            ).fetchone()[0]
        self.assertEqual(row["case_id"], legal)
        self.assertEqual(leaked, 0)
        with connect_database(self.database) as connection:
            result = run_runtime(connection, _request())
            projected = project_class_c_record(
                result,
                0.0,
                run_id="ab" * 16,
                created_at="2026-01-04T00:00:00+00:00",
            )
            for illegal in _ILLEGAL_CASE_IDS:
                forged = projected.__class__(**{**projected.__dict__, "case_id": illegal})
                with self.assertRaises(TracePersistenceError) as caught:
                    commit_class_c_trace(connection, forged)
                self.assertEqual(caught.exception.error_type, REDACTION_REFUSAL)
                self.assertNotIn(illegal, str(caught.exception))
            absent = connection.execute(
                "SELECT COUNT(*) FROM runtime_traces WHERE run_id = ?",
                (projected.run_id,),
            ).fetchone()[0]
        self.assertEqual(absent, 0)

    def test_caller_transaction_is_not_committed(self) -> None:
        packet, decision = _packet()
        with connect_database(self.database) as connection:
            connection.execute("BEGIN")
            with self.assertRaises(Exception) as caught:
                create_case(connection, packet, decision)
            self.assertEqual(caught.exception.error_type, CASE_NOT_DURABLE)
            self.assertEqual(str(caught.exception), CASE_NOT_DURABLE_MESSAGE)
            self.assertTrue(connection.in_transaction)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM support_cases").fetchone()[0],
                0,
            )
            connection.rollback()
        with connect_database(self.database) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM support_cases").fetchone()[0],
                0,
            )

    def test_evidence_rows_are_append_only_and_runtime_does_not_open_a_case(self) -> None:
        self._write_case()
        with connect_database(self.database) as connection:
            run_runtime(connection, _request())
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM support_cases").fetchone()[0],
                0,
            )
            execution = execute_traced_runtime(connection, _request())
            stored_case = connection.execute(
                "SELECT case_id FROM runtime_traces WHERE run_id = ?",
                (execution.run_id,),
            ).fetchone()
            self.assertIsNone(stored_case["case_id"])
            packet = execution.runtime.packet
            decision = execution.runtime.decision
            assert packet is not None and decision is not None
            created = create_case(connection, packet, decision, run_id=execution.run_id)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE case_evidence SET supporting_original_text = 'changed'"
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE support_cases SET decision_type = 'abstain' WHERE case_id = ?",
                    (created.case_id,),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM support_cases")
            connection.rollback()
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(support_cases)")
            }
            fts_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'page_fts'"
            ).fetchone()[0]
        self.assertNotIn("status", columns)
        self.assertIn("trigram", fts_sql)
        reopened = _reopen_case(self.database, created.case_id)
        self.assertEqual(reopened.decision_type, decision.decision_type)
        self.assertEqual(
            reopened.evidence[0].supporting_original_text,
            packet.evidence[0].supporting_original_text,
        )

    def test_forged_binding_is_rejected_and_consistent_provenance_round_trips(self) -> None:
        packet, decision = _packet()
        base = packet.evidence[0]
        forgeries = (
            {"original_content_digest": "sha256:" + "ff" * 32},
            {"evidence_id": "ev1-" + "ff" * 32},
            {"firmware_range": "9.9.9-forged", "firmware_applicability": "unknown"},
            {"retrieval_tool_version": "9"},
        )
        with connect_database(self.database) as connection:
            before = connection.execute("SELECT COUNT(*) FROM support_cases").fetchone()[0]
            for changes in forgeries:
                forged_packet, forged_decision = _replace_evidence(packet, decision, **changes)
                with self.assertRaises(CaseStoreError) as caught:
                    create_case(connection, forged_packet, forged_decision)
                self.assertEqual(caught.exception.error_type, CASE_INVALID)
                self.assertEqual(str(caught.exception), CASE_INVALID_MESSAGE)
                for value in changes.values():
                    self.assertNotIn(str(value), str(caught.exception))
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM support_cases").fetchone()[0],
                before,
            )

            ranged = replace(base, firmware_range="9.9.9-forged")
            ranged_binding = evidence_binding(ranged)
            ranged = replace(
                ranged,
                firmware_applicability=ranged_binding.firmware_applicability,
                metadata_digest=ranged_binding.metadata_digest,
                evidence_id=ranged_binding.evidence_id,
            )
            versioned = replace(ranged, retrieval_tool_version="2")
            versioned_binding = evidence_binding(versioned)
            versioned = replace(versioned, evidence_id=versioned_binding.evidence_id)
            self.assertTrue(snapshot_integrity_ok(versioned))
            self.assertNotEqual(versioned.evidence_id, base.evidence_id)
            bound_packet, bound_decision = _replace_evidence(
                packet,
                decision,
                firmware_range=versioned.firmware_range,
                firmware_applicability=versioned.firmware_applicability,
                metadata_digest=versioned.metadata_digest,
                evidence_id=versioned.evidence_id,
                retrieval_tool_version=versioned.retrieval_tool_version,
            )
            created = create_case(connection, bound_packet, bound_decision, note=base.filename)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM search_logs").fetchone()[0],
                0,
            )
        reopened = _reopen_case(self.database, created.case_id)
        stored = reopened.evidence[0]
        self.assertTrue(snapshot_integrity_ok(_as_snapshot(stored)))
        self.assertEqual(stored.firmware_range, "9.9.9-forged")
        self.assertEqual(stored.firmware_applicability, "unknown")
        self.assertEqual(stored.retrieval_tool_version, "2")
        self.assertEqual(stored.evidence_id, versioned.evidence_id)
        self.assertEqual(stored.metadata_digest, versioned.metadata_digest)
        context_blob = _case_context_blob(self.database, created.case_id)
        self.assertNotIn(base.supporting_original_text, context_blob)
        self.assertNotIn(base.filename, context_blob)
        self.assertNotIn(base.source_url, context_blob)
        self.assertNotIn("9.9.9-forged", context_blob)
        self.assertNotIn(base.filename, reopened.context.note_marker or "")

    def test_import_does_not_create_a_case(self) -> None:
        pdf_dir = Path(self.temp.name) / "incoming"
        pdf_dir.mkdir()
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 72), "import does not open a support case")
            document.save(pdf_dir / "manual.pdf")
        finally:
            document.close()
        summary = import_directory(pdf_dir, self.database)
        self.assertEqual(summary.imported, 1)
        with connect_database(self.database) as connection:
            documents = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            cases = connection.execute("SELECT COUNT(*) FROM support_cases").fetchone()[0]
        self.assertEqual(documents, 1)
        self.assertEqual(cases, 0)

    def test_supported_decision_with_empty_evidence_ids_leaves_no_residue(self) -> None:
        execution = self._supported_execution()
        packet = execution.runtime.packet
        decision = execution.runtime.decision
        assert packet is not None and decision is not None
        emptied = replace(decision, evidence_ids=())
        self.assertEqual(emptied.decision_type, DECISION_SUPPORTED)
        self.assertEqual(emptied.evidence_ids, ())
        before_trace = _trace_row(self.database, execution.run_id)
        with connect_database(self.database) as connection:
            with self.assertRaises(CaseStoreError) as caught:
                create_case(connection, packet, emptied, run_id=execution.run_id)
            self.assertEqual(caught.exception.error_type, CASE_INVALID)
            self.assertEqual(str(caught.exception), CASE_INVALID_MESSAGE)
            self.assertNotIn(execution.run_id, str(caught.exception))
            self.assertEqual(_counts(connection), (0, 0, 0))
        self.assertEqual(_stored_counts(self.database), (0, 0, 0))
        self.assertEqual(_trace_row(self.database, execution.run_id), before_trace)

    def test_historical_supported_case_without_evidence_fails_closed(self) -> None:
        supported_id = allocate_case_id()
        abstain_id = allocate_case_id()
        _insert_historical_case(self.database, supported_id, DECISION_SUPPORTED)
        _insert_historical_case(self.database, abstain_id, DECISION_ABSTAIN)
        abstained = _reopen_case(self.database, abstain_id)
        self.assertEqual(abstained.decision_type, DECISION_ABSTAIN)
        self.assertEqual(abstained.evidence, ())
        with self.assertRaises(CaseStoreError) as caught:
            _reopen_case(self.database, supported_id)
        self.assertEqual(caught.exception.error_type, CASE_INVALID)
        self.assertEqual(str(caught.exception), CASE_INVALID_MESSAGE)
        self.assertNotIn(supported_id, str(caught.exception))
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            decision_type = connection.execute(
                "SELECT decision_type FROM support_cases WHERE case_id = ?",
                (supported_id,),
            ).fetchone()["decision_type"]
            evidence = connection.execute(
                "SELECT COUNT(*) FROM case_evidence WHERE case_id = ?",
                (supported_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(decision_type, DECISION_SUPPORTED)
        self.assertEqual(evidence, 0)

    def test_supported_case_with_evidence_recovers_across_connections(self) -> None:
        execution = self._supported_execution()
        packet = execution.runtime.packet
        decision = execution.runtime.decision
        assert packet is not None and decision is not None
        with connect_database(self.database) as connection:
            created = create_case(connection, packet, decision)
        self.assertEqual(created.decision_type, DECISION_SUPPORTED)
        self.assertEqual(
            tuple(item.evidence_id for item in created.evidence),
            decision.evidence_ids,
        )
        reopened = _reopen_case(self.database, created.case_id)
        self.assertEqual(reopened.decision_type, DECISION_SUPPORTED)
        self.assertEqual(reopened.evidence[0].evidence_id, decision.evidence_ids[0])
        self.assertEqual(
            reopened.evidence[0].supporting_original_text,
            packet.evidence[0].supporting_original_text,
        )
        self.assertTrue(snapshot_integrity_ok(_as_snapshot(reopened.evidence[0])))

    def test_abstain_and_conflict_keep_existing_evidence_semantics(self) -> None:
        with connect_database(self.database) as connection:
            product_id = _product(connection, "AeroCam Mini 2", "ACM2")
            _document(
                connection,
                product_id,
                "handbook.pdf",
                ORIGINAL_PAGE,
                firmware_range="1.2.3",
            )
            cited = execute_traced_runtime(
                connection,
                _request(firmware_version="1.2.3"),
            )
            empty = execute_traced_runtime(connection, _request("quantum toaster zz-999"))
        cited_packet = cited.runtime.packet
        cited_decision = cited.runtime.decision
        empty_packet = empty.runtime.packet
        empty_decision = empty.runtime.decision
        assert cited_packet is not None and cited_decision is not None
        assert empty_packet is not None and empty_decision is not None
        self.assertEqual(cited_decision.decision_type, DECISION_ABSTAIN)
        self.assertGreater(len(cited_decision.evidence_ids), 0)
        self.assertEqual(empty_decision.decision_type, DECISION_ABSTAIN)
        self.assertEqual(empty_decision.evidence_ids, ())
        with connect_database(self.database) as connection:
            stored_cited = create_case(connection, cited_packet, cited_decision)
            stored_empty = create_case(connection, empty_packet, empty_decision)
        self.assertEqual(stored_cited.decision_type, DECISION_ABSTAIN)
        self.assertEqual(
            tuple(item.evidence_id for item in stored_cited.evidence),
            cited_decision.evidence_ids,
        )
        self.assertEqual(stored_empty.decision_type, DECISION_ABSTAIN)
        self.assertEqual(stored_empty.evidence, ())
        self.assertEqual(
            tuple(item.evidence_id for item in _reopen_case(self.database, stored_cited.case_id).evidence),
            cited_decision.evidence_ids,
        )
        self.assertEqual(_reopen_case(self.database, stored_empty.case_id).evidence, ())

        with connect_database(self.database) as connection:
            _document(
                connection,
                product_id,
                "old.pdf",
                ORIGINAL_PAGE,
                status="superseded",
            )
            conflict_run = execute_traced_runtime(connection, _request())
        conflict_packet = conflict_run.runtime.packet
        conflict_decision = conflict_run.runtime.decision
        assert conflict_packet is not None and conflict_decision is not None
        self.assertEqual(conflict_decision.decision_type, DECISION_CONFLICT)
        self.assertGreater(len(conflict_decision.evidence_ids), 0)
        emptied = replace(conflict_decision, evidence_ids=())
        with connect_database(self.database) as connection:
            stored_conflict = create_case(connection, conflict_packet, conflict_decision)
            stored_emptied = create_case(connection, conflict_packet, emptied)
        self.assertEqual(stored_conflict.decision_type, DECISION_CONFLICT)
        self.assertEqual(
            tuple(item.evidence_id for item in stored_conflict.evidence),
            conflict_decision.evidence_ids,
        )
        self.assertEqual(stored_emptied.decision_type, DECISION_CONFLICT)
        self.assertEqual(stored_emptied.evidence, ())
        reopened_conflict = _reopen_case(self.database, stored_conflict.case_id)
        reopened_emptied = _reopen_case(self.database, stored_emptied.case_id)
        self.assertEqual(reopened_conflict.decision_type, DECISION_CONFLICT)
        self.assertEqual(
            tuple(item.evidence_id for item in reopened_conflict.evidence),
            conflict_decision.evidence_ids,
        )
        self.assertEqual(reopened_emptied.decision_type, DECISION_CONFLICT)
        self.assertEqual(reopened_emptied.evidence, ())

    def test_missing_trace_run_id_rejects_case_creation(self) -> None:
        packet, decision = _packet()
        missing = "ab" * 16
        with connect_database(self.database) as connection:
            with self.assertRaises(CaseStoreError) as caught:
                create_case(connection, packet, decision, run_id=missing)
            self.assertEqual(caught.exception.error_type, CASE_INVALID)
            self.assertEqual(str(caught.exception), CASE_INVALID_MESSAGE)
            self.assertNotIn(missing, str(caught.exception))
            self.assertEqual(_counts(connection), (0, 0, 0))
        self.assertEqual(_stored_counts(self.database), (0, 0, 0))

    def test_trace_owned_by_another_case_rejects_the_link(self) -> None:
        case_a = allocate_case_id()
        case_b = allocate_case_id()
        execution = self._supported_execution(case_a)
        packet = execution.runtime.packet
        decision = execution.runtime.decision
        assert packet is not None and decision is not None
        before_trace = _trace_row(self.database, execution.run_id)
        with connect_database(self.database) as connection:
            with self.assertRaises(CaseStoreError) as caught:
                create_case(
                    connection,
                    packet,
                    decision,
                    case_id=case_b,
                    run_id=execution.run_id,
                )
            self.assertEqual(caught.exception.error_type, CASE_INVALID)
            self.assertEqual(str(caught.exception), CASE_INVALID_MESSAGE)
            self.assertNotIn(case_b, str(caught.exception))
            self.assertNotIn(execution.run_id, str(caught.exception))
            self.assertEqual(_counts(connection), (0, 0, 0))
        self.assertEqual(_stored_counts(self.database), (0, 0, 0))
        self.assertEqual(_trace_row(self.database, execution.run_id), before_trace)
        self.assertEqual(before_trace[_trace_case_index(self.database)], case_a)

    def test_trace_owned_by_the_same_case_can_be_linked(self) -> None:
        case_a = allocate_case_id()
        execution = self._supported_execution(case_a)
        packet = execution.runtime.packet
        decision = execution.runtime.decision
        assert packet is not None and decision is not None
        before_trace = _trace_row(self.database, execution.run_id)
        with connect_database(self.database) as connection:
            created = create_case(
                connection,
                packet,
                decision,
                case_id=case_a,
                run_id=execution.run_id,
            )
        self.assertEqual(created.case_id, case_a)
        self.assertEqual(created.decision_type, DECISION_SUPPORTED)
        self.assertEqual(created.trace_run_ids, (execution.run_id,))
        self.assertEqual(
            tuple(item.evidence_id for item in created.evidence),
            decision.evidence_ids,
        )
        reopened = _reopen_case(self.database, case_a)
        self.assertEqual(reopened.trace_run_ids, (execution.run_id,))
        self.assertEqual(reopened.decision_type, DECISION_SUPPORTED)
        self.assertEqual(_trace_row(self.database, execution.run_id), before_trace)

    def test_null_trace_case_id_link_keeps_the_trace_unchanged(self) -> None:
        execution = self._supported_execution()
        packet = execution.runtime.packet
        decision = execution.runtime.decision
        assert packet is not None and decision is not None
        before_trace = _trace_row(self.database, execution.run_id)
        self.assertIsNone(before_trace[_trace_case_index(self.database)])
        with connect_database(self.database) as connection:
            created = create_case(connection, packet, decision, run_id=execution.run_id)
        self.assertEqual(created.trace_run_ids, (execution.run_id,))
        self.assertEqual(created.decision_type, DECISION_SUPPORTED)
        reopened = _reopen_case(self.database, created.case_id)
        self.assertEqual(reopened.trace_run_ids, (execution.run_id,))
        after_trace = _trace_row(self.database, execution.run_id)
        self.assertEqual(after_trace, before_trace)
        self.assertIsNone(after_trace[_trace_case_index(self.database)])

    def test_injected_invalid_trace_link_fails_closed_on_read(self) -> None:
        execution = self._supported_execution()
        packet = execution.runtime.packet
        decision = execution.runtime.decision
        assert packet is not None and decision is not None
        with connect_database(self.database) as connection:
            created = create_case(connection, packet, decision, run_id=execution.run_id)
        self.assertEqual(_reopen_case(self.database, created.case_id).trace_run_ids, (execution.run_id,))
        missing = "cd" * 16
        before_trace = _trace_row(self.database, execution.run_id)
        _insert_link(self.database, created.case_id, missing)
        with self.assertRaises(CaseStoreError) as caught:
            _reopen_case(self.database, created.case_id)
        self.assertEqual(caught.exception.error_type, CASE_INVALID)
        self.assertEqual(str(caught.exception), CASE_INVALID_MESSAGE)
        self.assertNotIn(missing, str(caught.exception))
        self.assertEqual(_trace_row(self.database, execution.run_id), before_trace)

        case_a = allocate_case_id()
        case_b = allocate_case_id()
        with connect_database(self.database) as connection:
            product_id = connection.execute("SELECT id FROM products ORDER BY id").fetchone()[0]
            owned = execute_traced_runtime(
                connection,
                _request(
                    product_id=str(product_id),
                    product_series="AeroCam",
                    document_type="Service Handbook",
                    status="effective",
                    association="linked",
                    firmware_version="1.2.3",
                ),
                case_id=case_a,
            )
        self.assertTrue(owned.trace_ok, owned.error)
        owned_packet = owned.runtime.packet
        owned_decision = owned.runtime.decision
        assert owned_packet is not None and owned_decision is not None
        self.assertEqual(owned_decision.decision_type, DECISION_SUPPORTED)
        with connect_database(self.database) as connection:
            other = create_case(connection, owned_packet, owned_decision, case_id=case_b)
        self.assertEqual(other.trace_run_ids, ())
        owned_before = _trace_row(self.database, owned.run_id)
        _insert_link(self.database, case_b, owned.run_id)
        with self.assertRaises(CaseStoreError) as mismatched:
            _reopen_case(self.database, case_b)
        self.assertEqual(mismatched.exception.error_type, CASE_INVALID)
        self.assertEqual(str(mismatched.exception), CASE_INVALID_MESSAGE)
        self.assertNotIn(owned.run_id, str(mismatched.exception))
        self.assertEqual(_trace_row(self.database, owned.run_id), owned_before)
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            links = connection.execute(
                "SELECT run_id FROM case_trace_links WHERE case_id = ? ORDER BY id",
                (case_b,),
            ).fetchall()
            decision_type = connection.execute(
                "SELECT decision_type FROM support_cases WHERE case_id = ?",
                (case_b,),
            ).fetchone()["decision_type"]
        finally:
            connection.close()
        self.assertEqual([row["run_id"] for row in links], [owned.run_id])
        self.assertEqual(decision_type, DECISION_SUPPORTED)

    def test_forced_link_insert_failure_rolls_back_and_keeps_the_trace(self) -> None:
        execution = self._supported_execution()
        packet = execution.runtime.packet
        decision = execution.runtime.decision
        assert packet is not None and decision is not None
        self.assertGreater(len(decision.evidence_ids), 0)
        before_trace = _trace_row(self.database, execution.run_id)
        with connect_database(self.database) as connection:
            connection.execute(
                """CREATE TEMP TRIGGER force_link_failure
                   BEFORE INSERT ON case_trace_links
                   BEGIN
                       SELECT RAISE(ABORT, 'forced link failure');
                   END"""
            )
            self.assertFalse(connection.in_transaction)
            with self.assertRaises(CaseStoreError) as caught:
                create_case(connection, packet, decision, run_id=execution.run_id)
            self.assertEqual(caught.exception.error_type, CASE_INVALID)
            self.assertEqual(str(caught.exception), CASE_INVALID_MESSAGE)
            self.assertNotIn("forced link failure", str(caught.exception))
            self.assertNotIn(execution.run_id, str(caught.exception))
            self.assertEqual(_counts(connection), (0, 0, 0))
            current = connection.execute(
                "SELECT * FROM runtime_traces WHERE run_id = ?",
                (execution.run_id,),
            ).fetchone()
            self.assertEqual(tuple(current), before_trace)
        self.assertEqual(_stored_counts(self.database), (0, 0, 0))
        self.assertEqual(_trace_row(self.database, execution.run_id), before_trace)

    def _supported_execution(self, case_id: str | None = None):
        product_id = self._write_case()
        request = _request(
            product_id=str(product_id),
            product_series="AeroCam",
            document_type="Service Handbook",
            status="effective",
            association="linked",
            firmware_version="1.2.3",
        )
        with connect_database(self.database) as connection:
            execution = execute_traced_runtime(connection, request, case_id=case_id)
        self.assertTrue(execution.trace_ok, execution.error)
        self.assertTrue(execution.runtime.ok)
        packet = execution.runtime.packet
        decision = execution.runtime.decision
        assert packet is not None and decision is not None
        self.assertEqual(decision.decision_type, DECISION_SUPPORTED)
        self.assertEqual(
            decision.evidence_ids,
            tuple(item.evidence_id for item in packet.evidence),
        )
        self.assertGreater(len(decision.evidence_ids), 0)
        return execution

    def _write_case(self, content: str = ORIGINAL_PAGE) -> int:
        with connect_database(self.database) as connection:
            product_id = _product(connection, "AeroCam Mini 2", "ACM2")
            _document(connection, product_id, "handbook.pdf", content)
            return product_id


class CaseMigrationTests(unittest.TestCase):
    def test_pre_migration_backup_restore_drops_case_rows(self) -> None:
        self.assertEqual(
            MIGRATION_005_DATA_LOSS,
            "Restoring a backup taken before migration 5 discards every support_cases, "
            "case_evidence, and case_trace_links row written after that migration, "
            "and discards runtime_traces.case_id values written after that migration.",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "g6.db"
            _migrate_through(database, 4)
            _seed_durable_document(database)
            preserved_run = "ab" * 16
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """INSERT INTO runtime_traces (
                           run_id, step_id, tool_name, tool_version, runtime_version,
                           request_schema_version, response_schema_version,
                           runtime_request_schema_version, runtime_response_schema_version,
                           input_json, output_json, decision_json, evidence_ids,
                           latency_ms, termination_reason, created_at, trace_schema_version
                       ) VALUES (?, '1', NULL, NULL, '1', NULL, NULL, NULL, '1',
                                 '{}', '{}', '{}', '[]', 0, 'invalid_request',
                                 '2026-01-01T00:00:00+00:00', '1')""",
                    (preserved_run,),
                )
                connection.commit()
            finally:
                connection.close()
            pre_backup, pre_details = create_backup(database, root / "before-005", migrate=False)
            self.assertEqual(pre_details["schema_version"], 4)
            self.assertEqual(verify_backup(pre_backup)["schema_version"], 4)

            init_database(database)
            with connect_database(database) as connection:
                self.assertEqual(current_schema_version(connection), 7)
                _migration_005_case_store(connection)
                _migration_005_case_store(connection)
                version_rows = connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = 5"
                ).fetchone()[0]
                preserved = connection.execute(
                    "SELECT case_id FROM runtime_traces WHERE run_id = ?",
                    (preserved_run,),
                ).fetchone()
                self.assertIsNone(preserved["case_id"])
                packet, decision = _packet()
                case_id = allocate_case_id()
                execution = execute_traced_runtime(
                    connection, {"query": QUERY}, case_id=case_id
                )
                self.assertTrue(execution.trace_ok, execution.error)
                create_case(
                    connection,
                    packet,
                    decision,
                    case_id=case_id,
                    run_id=execution.run_id,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM support_cases").fetchone()[0],
                    1,
                )
            self.assertEqual(version_rows, 1)
            migrated, migrated_details = create_backup(database, root / "migrated")
            self.assertEqual(migrated_details["schema_version"], 7)

            restored = restore_backup(pre_backup, database, confirm=True)
            self.assertEqual(restored["schema_version"], 4)
            with connect_database(database) as connection:
                self.assertEqual(current_schema_version(connection), 4)
                self.assertIsNone(
                    connection.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type = 'table' AND name = 'support_cases'"""
                    ).fetchone()
                )
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(runtime_traces)")
                }
                self.assertNotIn("case_id", columns)
                remaining = connection.execute(
                    "SELECT run_id FROM runtime_traces"
                ).fetchall()
                document = connection.execute(
                    "SELECT filename, sha256 FROM documents"
                ).fetchone()
        self.assertEqual([row["run_id"] for row in remaining], [preserved_run])
        self.assertEqual(document["filename"], "pre-g5-durable.pdf")
        self.assertEqual(document["sha256"], "c" * 64)

    def test_pre_migration_6_backup_drops_fidelity_columns(self) -> None:
        self.assertEqual(
            MIGRATION_006_DATA_LOSS,
            "Restoring a backup taken before migration 6 discards case_evidence "
            "identity, metadata digest, firmware applicability, tool provenance, "
            "transformation version, and source locator values written after that "
            "migration, and discards support_cases, case_evidence, and "
            "case_trace_links rows written after that backup.",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "g6-1.db"
            _migrate_through(database, 5)
            _seed_durable_document(database)
            legacy_case = allocate_case_id()
            legacy_text = "legacy forged page"
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """INSERT INTO support_cases (
                           case_id, created_at, context_json, context_schema_version,
                           context_updated_at, decision_type, reason_codes_json,
                           retrieval_state, packet_schema_version
                       ) VALUES (?, '2026-01-01T00:00:00+00:00', ?, '1',
                                 '2026-01-01T00:00:00+00:00', 'abstain', '[]',
                                 'insufficient_evidence', '2')""",
                    (
                        legacy_case,
                        json.dumps(
                            {"note_marker": None, "schema_version": "1"},
                            sort_keys=True,
                        ),
                    ),
                )
                connection.execute(
                    """INSERT INTO case_evidence (
                           case_id, evidence_id, snapshot_schema_version,
                           supporting_original_text, document_lifecycle,
                           product_lifecycle, authority_level, original_content_digest,
                           decision_visible_representation, decision_visible_digest,
                           pdf_sha256, page_number, document_identity, captured_at,
                           source_policy, visible_policy
                       ) VALUES (?, ?, '3', ?, 'effective', 'active', 'reference',
                                 ?, 'visible', ?, ?, 1, ?, '2026-01-01T00:00:00+00:00',
                                 ?, ?)""",
                    (
                        legacy_case,
                        "ev1-" + "ab" * 32,
                        legacy_text,
                        "sha256:" + "11" * 32,
                        "sha256:" + "33" * 32,
                        "cd" * 32,
                        "sha256:" + "cd" * 32,
                        CLASS_A_POLICY,
                        CLASS_B_POLICY,
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            pre_backup, pre_details = create_backup(database, root / "before-006", migrate=False)
            self.assertEqual(pre_details["schema_version"], 5)
            with connect_database(database) as connection:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(case_evidence)")
                }
                self.assertNotIn("metadata_digest", columns)

            init_database(database)
            with connect_database(database) as connection:
                self.assertEqual(current_schema_version(connection), 7)
                _migration_006_case_evidence_fidelity(connection)
                _migration_006_case_evidence_fidelity(connection)
                version_rows = connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = 6"
                ).fetchone()[0]
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(case_evidence)")
                }
                self.assertIn("metadata_digest", columns)
                self.assertIn("retrieval_tool_version", columns)
                with self.assertRaises(CaseStoreError) as caught:
                    read_case(connection, legacy_case)
                self.assertEqual(caught.exception.error_type, CASE_INVALID)
                self.assertNotIn(legacy_text, str(caught.exception))
                packet, decision = _packet()
                created = create_case(connection, packet, decision)
            self.assertEqual(version_rows, 1)
            reopened = _reopen_case(database, created.case_id)
            self.assertTrue(snapshot_integrity_ok(_as_snapshot(reopened.evidence[0])))

            restored = restore_backup(pre_backup, database, confirm=True)
            self.assertEqual(restored["schema_version"], 5)
            with connect_database(database) as connection:
                self.assertEqual(current_schema_version(connection), 5)
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(case_evidence)")
                }
                self.assertNotIn("metadata_digest", columns)
                self.assertNotIn("retrieval_tool_version", columns)
                remaining = connection.execute(
                    "SELECT case_id FROM support_cases"
                ).fetchall()
                document = connection.execute(
                    "SELECT filename, sha256 FROM documents"
                ).fetchone()
        self.assertEqual([row["case_id"] for row in remaining], [legacy_case])
        self.assertEqual(document["filename"], "pre-g5-durable.pdf")
        self.assertEqual(document["sha256"], "c" * 64)


def _packet(original_query: str = _RAW_QUERY) -> tuple[EvidencePacket, EvidenceDecision]:
    pdf_sha = "cd" * 32
    draft = EvidenceSnapshot(
        evidence_id="ev1-" + "ab" * 32,
        snapshot_schema_version=SNAPSHOT_SCHEMA_VERSION,
        document_id=1,
        document_identity=f"sha256:{pdf_sha}",
        filename="handbook.pdf",
        pdf_sha256=pdf_sha,
        page_number=1,
        source_locator=f"sha256:{pdf_sha}#page=1",
        source_url="https://example.test/handbook.pdf",
        supporting_original_text="frozen support text",
        supporting_text_source="page-content",
        original_content_digest="sha256:" + "11" * 32,
        metadata_digest="sha256:" + "22" * 32,
        canonical_product_id=1,
        canonical_product_name="AeroCam Mini 2",
        product_lifecycle="active",
        document_lifecycle="effective",
        firmware_range="",
        firmware_applicability="applicable",
        authority_level="reference",
        retrieval_tool_name=TOOL_NAME,
        retrieval_tool_version=TOOL_VERSION,
        retrieval_response_schema_version=RESPONSE_SCHEMA_VERSION,
        captured_at="2026-01-01T00:00:00+00:00",
        decision_visible_representation="frozen visible",
        decision_visible_digest="sha256:" + "33" * 32,
        decision_visible_source="G2 retrieval snippet",
        transformation_version="retrieval-excerpt-v1",
    )
    binding = evidence_binding(draft)
    snapshot = replace(
        draft,
        evidence_id=binding.evidence_id,
        document_identity=binding.document_identity,
        source_locator=binding.source_locator,
        original_content_digest=binding.original_content_digest,
        metadata_digest=binding.metadata_digest,
        firmware_applicability=binding.firmware_applicability,
        decision_visible_digest=binding.decision_visible_digest,
    )
    packet = EvidencePacket(
        packet_schema_version=PACKET_SCHEMA_VERSION,
        retrieval_state="high_confidence",
        recognized_products=(),
        request=RequestContext(
            original_query=original_query,
            normalized_query=original_query,
            retrieval_query=original_query,
            request_schema_version="1",
            explicit_product_id=None,
            explicit_product_name=None,
            explicit_product_lifecycle=None,
            firmware_version=None,
            product_series="",
            document_type="",
            status="",
            association="",
        ),
        alias_conflict=False,
        evidence=(snapshot,),
        captured_at="2026-01-01T00:00:00+00:00",
        retrieval_tool_name=TOOL_NAME,
        retrieval_tool_version=TOOL_VERSION,
        retrieval_response_schema_version=RESPONSE_SCHEMA_VERSION,
    )
    decision = EvidenceDecision(
        decision_type="supported",
        reason_codes=(),
        retrieval_state="high_confidence",
        evidence_ids=(snapshot.evidence_id,),
        packet_schema_version=PACKET_SCHEMA_VERSION,
    )
    return packet, decision


def _replace_evidence(
    packet: EvidencePacket,
    decision: EvidenceDecision,
    **changes: object,
) -> tuple[EvidencePacket, EvidenceDecision]:
    snapshot = replace(packet.evidence[0], **changes)
    return (
        replace(packet, evidence=(snapshot,)),
        replace(decision, evidence_ids=(snapshot.evidence_id,)),
    )


def _as_snapshot(item: object) -> EvidenceSnapshot:
    return EvidenceSnapshot(
        evidence_id=item.evidence_id,
        snapshot_schema_version=item.snapshot_schema_version,
        document_id=item.document_id,
        document_identity=item.document_identity,
        filename=item.filename,
        pdf_sha256=item.pdf_sha256,
        page_number=item.page_number,
        source_locator=item.source_locator,
        source_url=item.source_url,
        supporting_original_text=item.supporting_original_text,
        supporting_text_source=item.supporting_text_source,
        original_content_digest=item.original_content_digest,
        metadata_digest=item.metadata_digest,
        canonical_product_id=item.canonical_product_id,
        canonical_product_name=item.canonical_product_name,
        product_lifecycle=item.product_lifecycle,
        document_lifecycle=item.document_lifecycle,
        firmware_range=item.firmware_range,
        firmware_applicability=item.firmware_applicability,
        authority_level=item.authority_level,
        retrieval_tool_name=item.retrieval_tool_name,
        retrieval_tool_version=item.retrieval_tool_version,
        retrieval_response_schema_version=item.retrieval_response_schema_version,
        captured_at=item.captured_at,
        decision_visible_representation=item.decision_visible_representation,
        decision_visible_digest=item.decision_visible_digest,
        decision_visible_source=item.decision_visible_source,
        transformation_version=item.transformation_version,
    )


def _counts(connection: sqlite3.Connection) -> tuple[int, int, int]:
    return tuple(
        connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        for name in ("support_cases", "case_evidence", "case_trace_links")
    )


def _stored_counts(database: Path) -> tuple[int, int, int]:
    connection = sqlite3.connect(database)
    try:
        return _counts(connection)
    finally:
        connection.close()


def _trace_case_index(database: Path) -> int:
    connection = sqlite3.connect(database)
    try:
        columns = [
            row[1] for row in connection.execute("PRAGMA table_info(runtime_traces)")
        ]
    finally:
        connection.close()
    return columns.index("case_id")


def _trace_row(database: Path, run_id: str) -> tuple:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT * FROM runtime_traces WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return tuple(row)


def _insert_historical_case(database: Path, case_id: str, decision_type: str) -> None:
    context = json.dumps(
        {"note_marker": None, "schema_version": "1"},
        ensure_ascii=False,
        sort_keys=True,
    )
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """INSERT INTO support_cases (
                   case_id, created_at, context_json, context_schema_version,
                   context_updated_at, decision_type, reason_codes_json,
                   retrieval_state, packet_schema_version
               ) VALUES (?, '2026-01-02T00:00:00+00:00', ?, '1',
                         '2026-01-02T00:00:00+00:00', ?, '[]',
                         'high_confidence', ?)""",
            (case_id, context, decision_type, PACKET_SCHEMA_VERSION),
        )
        connection.commit()
    finally:
        connection.close()


def _insert_link(database: Path, case_id: str, run_id: str) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """INSERT INTO case_trace_links (case_id, run_id, recorded_at)
               VALUES (?, ?, '2026-01-03T00:00:00+00:00')""",
            (case_id, run_id),
        )
        connection.commit()
    finally:
        connection.close()


def _reopen_case(database: Path, case_id: str):
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        return read_case(connection, case_id)
    finally:
        connection.close()


def _case_context_blob(database: Path, case_id: str) -> str:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute(
            "SELECT context_json FROM support_cases WHERE case_id = ?",
            (case_id,),
        ).fetchone()
    finally:
        connection.close()
    return str(row["context_json"])


def _case_blob(connection: sqlite3.Connection) -> str:
    parts: list[str] = []
    for table in ("support_cases", "case_evidence", "case_trace_links"):
        rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        for row in rows:
            parts.append(_joined(row))
    return "\n".join(parts)


def _joined(row: sqlite3.Row) -> str:
    return " ".join(str(row[name]) for name in row.keys())


def _knowledge(connection: sqlite3.Connection) -> dict[str, list[tuple]]:
    statements = {
        "documents": "SELECT id, status, authority_level FROM documents ORDER BY id",
        "pages": "SELECT document_id, page_number, content FROM pages ORDER BY id",
        "products": "SELECT id, status FROM products ORDER BY id",
        "search_logs": "SELECT id, original_query FROM search_logs ORDER BY id",
    }
    return {
        name: [tuple(row) for row in connection.execute(sql)]
        for name, sql in statements.items()
    }


if __name__ == "__main__":
    unittest.main()
