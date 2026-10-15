from __future__ import annotations

import ast
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from support_knowledge_engine.backup import create_backup, restore_backup, verify_backup
from support_knowledge_engine.cases import (
    CaseRecord,
    allocate_case_id,
    create_case,
    read_case,
)
from support_knowledge_engine.db import connect_database, init_database
from support_knowledge_engine.evidence import (
    DECISION_ABSTAIN,
    DECISION_CONFLICT,
    DECISION_SUPPORTED,
    snapshot_integrity_ok,
)
from support_knowledge_engine.migrations import (
    MIGRATION_007_DATA_LOSS,
    _migration_007_support_workflow,
    current_schema_version,
)
from support_knowledge_engine.trace import execute_traced_runtime
from support_knowledge_engine.workflow import (
    STATE_ABSTAINED,
    STATE_INVESTIGATING,
    STATE_OPENED,
    STATE_RESOLVED,
    WORKFLOW_INVALID,
    WORKFLOW_INVALID_MESSAGE,
    WORKFLOW_LOCKED,
    WORKFLOW_NOT_DURABLE,
    WORKFLOW_NOT_DURABLE_MESSAGE,
    WorkflowError,
    read_workflow,
    transition_workflow,
    workflow_decision_reference,
)
from tests.helpers import PROJECT_ROOT
from tests.test_cases import _as_snapshot, _packet
from tests.test_runtime_trace import (
    ORIGINAL_PAGE,
    QUERY,
    _document,
    _migrate_through,
    _product,
    _request,
    _seed_durable_document,
)


class WorkflowContractTests(unittest.TestCase):
    def test_workflow_does_not_retrieve_decide_or_copy_evidence_text(self) -> None:
        source = (PROJECT_ROOT / "support_knowledge_engine" / "workflow.py").read_text(encoding="utf-8")
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
            self.assertNotIn(name, source)
        for token in (
            "original_query",
            "normalized_query",
            "retrieval_query",
            "supporting_original_text",
            "decision_visible_representation",
            "FROM pages",
            "FROM documents",
            "FROM page_fts",
            "FROM search_logs",
            "FROM products",
            "page_fts",
            "openai",
            "anthropic",
            "max_steps",
            "budget_exhausted",
            "provider_error",
            "UPDATE support_cases",
            "UPDATE runtime_traces",
            "UPDATE documents",
        ):
            self.assertNotIn(token, source, token)
        for relative in (
            "support_knowledge_engine/importer.py",
            "support_knowledge_engine/routes.py",
            "support_knowledge_engine/__init__.py",
            "support_knowledge_engine/cases.py",
            "support_knowledge_engine/trace.py",
        ):
            text = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("transition_workflow", text)
            self.assertNotIn("workflow_events", text)


class WorkflowBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "g7.db"
        init_database(self.database)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_new_case_starts_opened(self) -> None:
        created = self._supported_case()
        opened = _reopen_workflow(self.database, created.case_id)
        self.assertEqual(opened.state, STATE_OPENED)
        self.assertEqual(opened.events, ())
        self.assertEqual(_event_count(self.database), 0)

    def test_opened_moves_to_investigating(self) -> None:
        created = self._supported_case()
        with connect_database(self.database) as connection:
            moved = transition_workflow(connection, created.case_id, STATE_INVESTIGATING)
        self.assertEqual(moved.state, STATE_INVESTIGATING)
        self.assertEqual(moved.events[0].from_state, STATE_OPENED)
        self.assertEqual(moved.events[0].to_state, STATE_INVESTIGATING)
        self.assertIsNone(moved.events[0].evidence_id)
        self.assertIsNone(moved.events[0].decision_reference)
        self.assertEqual(moved.events[0].sequence, 1)
        reopened = _reopen_workflow(self.database, created.case_id)
        self.assertEqual(reopened.state, STATE_INVESTIGATING)
        self.assertEqual(len(reopened.events), 1)

    def test_investigating_resolves_with_case_evidence(self) -> None:
        created = self._supported_case()
        evidence_id = created.evidence[0].evidence_id
        self._investigate(created.case_id)
        with connect_database(self.database) as connection:
            resolved = transition_workflow(
                connection,
                created.case_id,
                STATE_RESOLVED,
                evidence_id=evidence_id,
            )
        self.assertEqual(resolved.state, STATE_RESOLVED)
        self.assertEqual(resolved.events[1].evidence_id, evidence_id)
        self.assertIsNone(resolved.events[1].decision_reference)
        reopened = _reopen_workflow(self.database, created.case_id)
        self.assertEqual(reopened.state, STATE_RESOLVED)
        self.assertEqual(reopened.events[1].evidence_id, evidence_id)
        stored = read_case_on(self.database, created.case_id)
        self.assertEqual(stored.decision_type, DECISION_SUPPORTED)
        self.assertTrue(snapshot_integrity_ok(_as_snapshot(stored.evidence[0])))
        self.assertNotIn(ORIGINAL_PAGE, _event_blob(self.database))
        self.assertNotIn(QUERY, _event_blob(self.database))

    def test_resolved_rejects_missing_forged_and_foreign_evidence(self) -> None:
        supported = self._supported_case()
        empty = self._abstain_case()
        self._investigate(supported.case_id)
        self._investigate(empty.case_id)
        foreign = supported.evidence[0].evidence_id
        forged = "ev1-" + "ff" * 32
        for cited in (None, forged, foreign):
            self._reject_resolved(empty.case_id, cited)
        self._reject_resolved(supported.case_id, forged)
        self._reject_resolved(supported.case_id, None)
        self.assertEqual(_reopen_workflow(self.database, empty.case_id).state, STATE_INVESTIGATING)
        self.assertEqual(_reopen_workflow(self.database, supported.case_id).state, STATE_INVESTIGATING)
        with connect_database(self.database) as connection:
            resolved = transition_workflow(
                connection,
                supported.case_id,
                STATE_RESOLVED,
                evidence_id=foreign,
            )
        self.assertEqual(resolved.state, STATE_RESOLVED)
        self.assertEqual(_event_rows(self.database, empty.case_id), 1)

    def test_abstained_requires_the_stored_abstain_decision(self) -> None:
        created = self._abstain_case()
        self._investigate(created.case_id)
        with connect_database(self.database) as connection:
            case = read_case(connection, created.case_id)
            reference = workflow_decision_reference(case)
            abstained = transition_workflow(
                connection,
                created.case_id,
                STATE_ABSTAINED,
                decision_reference=reference,
            )
        self.assertEqual(case.decision_type, DECISION_ABSTAIN)
        self.assertEqual(abstained.state, STATE_ABSTAINED)
        self.assertEqual(abstained.events[1].decision_reference, reference)
        self.assertIsNone(abstained.events[1].evidence_id)
        reopened = _reopen_workflow(self.database, created.case_id)
        self.assertEqual(reopened.state, STATE_ABSTAINED)
        stored = read_case_on(self.database, created.case_id)
        self.assertEqual(stored.decision_type, DECISION_ABSTAIN)
        self.assertEqual(workflow_decision_reference(stored), reference)

    def test_supported_and_conflict_cannot_be_forged_into_abstain(self) -> None:
        supported = self._supported_case()
        self._investigate(supported.case_id)
        self._reject_forged_abstain(supported.case_id, DECISION_SUPPORTED)
        with connect_database(self.database) as connection:
            product_id = connection.execute("SELECT id FROM products ORDER BY id").fetchone()[0]
            _document(connection, int(product_id), "old.pdf", ORIGINAL_PAGE, status="superseded")
        with connect_database(self.database) as connection:
            execution = execute_traced_runtime(connection, _request())
            packet = execution.runtime.packet
            decision = execution.runtime.decision
            assert packet is not None and decision is not None
            self.assertEqual(decision.decision_type, DECISION_CONFLICT)
            self.assertGreater(len(decision.evidence_ids), 0)
            conflict = create_case(connection, packet, decision)
        self._investigate(conflict.case_id)
        self._reject_forged_abstain(conflict.case_id, DECISION_CONFLICT)

    def test_illegal_jumps_terminal_moves_and_repeats_are_rejected(self) -> None:
        created = self._supported_case()
        evidence_id = created.evidence[0].evidence_id
        for target in (STATE_RESOLVED, STATE_ABSTAINED, STATE_OPENED):
            self._reject(created.case_id, target, evidence_id=evidence_id)
        self.assertEqual(_reopen_workflow(self.database, created.case_id).state, STATE_OPENED)
        self._investigate(created.case_id)
        self._reject(created.case_id, STATE_INVESTIGATING)
        self._reject(created.case_id, STATE_OPENED)
        with connect_database(self.database) as connection:
            transition_workflow(
                connection,
                created.case_id,
                STATE_RESOLVED,
                evidence_id=evidence_id,
            )
        for target in (STATE_RESOLVED, STATE_ABSTAINED, STATE_INVESTIGATING, STATE_OPENED):
            self._reject(created.case_id, target, evidence_id=evidence_id)
        reopened = _reopen_workflow(self.database, created.case_id)
        self.assertEqual(reopened.state, STATE_RESOLVED)
        self.assertEqual([event.to_state for event in reopened.events], [STATE_INVESTIGATING, STATE_RESOLVED])

    def test_concurrent_transitions_leave_one_history(self) -> None:
        created = self._supported_case()
        evidence_id = created.evidence[0].evidence_id
        first = self._race(created.case_id, STATE_INVESTIGATING)
        self.assertEqual(first, 1)
        opened = _reopen_workflow(self.database, created.case_id)
        self.assertEqual(opened.state, STATE_INVESTIGATING)
        self.assertEqual([event.sequence for event in opened.events], [1])
        second = self._race(created.case_id, STATE_RESOLVED, evidence_id=evidence_id)
        self.assertEqual(second, 1)
        reopened = _reopen_workflow(self.database, created.case_id)
        self.assertEqual(reopened.state, STATE_RESOLVED)
        self.assertEqual(
            [event.to_state for event in reopened.events],
            [STATE_INVESTIGATING, STATE_RESOLVED],
        )

    def test_event_insert_failure_rolls_back(self) -> None:
        created = self._supported_case()
        before_case = read_case_on(self.database, created.case_id)
        before_trace = _trace_row(self.database, created.trace_run_ids[0])
        before_status = _document_status(self.database)
        with connect_database(self.database) as connection:
            connection.execute(
                """CREATE TEMP TRIGGER force_workflow_failure
                   BEFORE INSERT ON workflow_events
                   BEGIN
                       SELECT RAISE(ABORT, 'forced workflow failure');
                   END"""
            )
            self.assertFalse(connection.in_transaction)
            with self.assertRaises(WorkflowError) as caught:
                transition_workflow(connection, created.case_id, STATE_INVESTIGATING)
            self.assertEqual(caught.exception.error_type, WORKFLOW_INVALID)
            self.assertEqual(str(caught.exception), WORKFLOW_INVALID_MESSAGE)
            self.assertNotIn("forced workflow failure", str(caught.exception))
            self.assertEqual(_event_count_on(connection), 0)
        self.assertEqual(_event_count(self.database), 0)
        self.assertEqual(_reopen_workflow(self.database, created.case_id).state, STATE_OPENED)
        after_case = read_case_on(self.database, created.case_id)
        self.assertEqual(after_case.decision_type, before_case.decision_type)
        self.assertEqual(after_case.evidence[0].evidence_id, before_case.evidence[0].evidence_id)
        self.assertEqual(_trace_row(self.database, created.trace_run_ids[0]), before_trace)
        self.assertEqual(_document_status(self.database), before_status)

    def test_injected_illegal_history_fails_closed(self) -> None:
        created = self._supported_case()
        evidence_id = created.evidence[0].evidence_id
        _insert_event(
            self.database,
            created.case_id,
            1,
            STATE_OPENED,
            STATE_INVESTIGATING,
        )
        self.assertEqual(_reopen_workflow(self.database, created.case_id).state, STATE_INVESTIGATING)
        _insert_event(
            self.database,
            created.case_id,
            2,
            STATE_INVESTIGATING,
            STATE_RESOLVED,
            evidence_id="ev1-" + "ff" * 32,
        )
        with self.assertRaises(WorkflowError) as forged:
            _reopen_workflow(self.database, created.case_id)
        self.assertEqual(forged.exception.error_type, WORKFLOW_INVALID)
        self.assertEqual(str(forged.exception), WORKFLOW_INVALID_MESSAGE)
        self.assertNotIn(evidence_id, str(forged.exception))

        other = self._abstain_case()
        _insert_event(self.database, other.case_id, 1, STATE_OPENED, STATE_INVESTIGATING)
        _insert_event(
            self.database,
            other.case_id,
            2,
            STATE_OPENED,
            STATE_INVESTIGATING,
        )
        with self.assertRaises(WorkflowError) as chained:
            _reopen_workflow(self.database, other.case_id)
        self.assertEqual(chained.exception.error_type, WORKFLOW_INVALID)
        self.assertEqual(_stored_targets(self.database, other.case_id), [STATE_INVESTIGATING, STATE_INVESTIGATING])

        timed = self._packet_case()
        _insert_event(
            self.database,
            timed.case_id,
            1,
            STATE_OPENED,
            STATE_INVESTIGATING,
            recorded_at="not-a-timestamp",
        )
        with self.assertRaises(WorkflowError) as stale:
            _reopen_workflow(self.database, timed.case_id)
        self.assertEqual(stale.exception.error_type, WORKFLOW_INVALID)

    def test_document_edits_do_not_change_workflow_or_evidence(self) -> None:
        created = self._supported_case()
        evidence_id = created.evidence[0].evidence_id
        self._investigate(created.case_id)
        with connect_database(self.database) as connection:
            transition_workflow(
                connection,
                created.case_id,
                STATE_RESOLVED,
                evidence_id=evidence_id,
            )
        before_events = _event_blob(self.database)
        before_case = read_case_on(self.database, created.case_id)
        with connect_database(self.database) as connection:
            connection.execute(
                "UPDATE pages SET content = ? WHERE document_id = ?",
                ("mutated page body", before_case.evidence[0].document_id),
            )
            connection.execute(
                "UPDATE page_fts SET content = ? WHERE document_id = ?",
                ("mutated page body", before_case.evidence[0].document_id),
            )
            connection.execute(
                """UPDATE documents
                   SET status = 'archived', authority_level = 'authoritative'
                   WHERE id = ?""",
                (before_case.evidence[0].document_id,),
            )
            connection.execute(
                "UPDATE products SET status = 'archived' WHERE id = ?",
                (before_case.evidence[0].canonical_product_id,),
            )
        reopened = _reopen_workflow(self.database, created.case_id)
        stored = read_case_on(self.database, created.case_id)
        self.assertEqual(reopened.state, STATE_RESOLVED)
        self.assertEqual(reopened.events[-1].evidence_id, evidence_id)
        self.assertEqual(_event_blob(self.database), before_events)
        self.assertEqual(stored.evidence[0].supporting_original_text, before_case.evidence[0].supporting_original_text)
        self.assertEqual(stored.evidence[0].document_lifecycle, "effective")
        self.assertEqual(stored.evidence[0].authority_level, "reference")
        self.assertEqual(_document_status(self.database), "archived")

    def test_case_transition_does_not_change_document_status(self) -> None:
        created = self._supported_case()
        before = _document_status(self.database)
        self.assertEqual(before, "effective")
        logs_before = _log_count(self.database)
        self._investigate(created.case_id)
        with connect_database(self.database) as connection:
            transition_workflow(
                connection,
                created.case_id,
                STATE_RESOLVED,
                evidence_id=created.evidence[0].evidence_id,
            )
            product_status = connection.execute("SELECT status FROM products").fetchone()[0]
        self.assertEqual(_document_status(self.database), before)
        self.assertEqual(product_status, "active")
        self.assertEqual(_log_count(self.database), logs_before)
        with connect_database(self.database) as connection:
            logs = " ".join(
                " ".join(str(row[name]) for name in row.keys())
                for row in connection.execute("SELECT * FROM search_logs")
            )
        self.assertNotIn(created.evidence[0].evidence_id, logs)

    def test_transition_preserves_trace_link_and_case_identity(self) -> None:
        created = self._supported_case()
        run_id = created.trace_run_ids[0]
        before_trace = _trace_row(self.database, run_id)
        before_case = read_case_on(self.database, created.case_id)
        self._investigate(created.case_id)
        after_case = read_case_on(self.database, created.case_id)
        self.assertEqual(after_case.decision_type, before_case.decision_type)
        self.assertEqual(after_case.evidence[0].evidence_id, before_case.evidence[0].evidence_id)
        self.assertEqual(after_case.evidence[0].metadata_digest, before_case.evidence[0].metadata_digest)
        self.assertEqual(after_case.trace_run_ids, (run_id,))
        self.assertEqual(_trace_row(self.database, run_id), before_trace)
        self.assertTrue(snapshot_integrity_ok(_as_snapshot(after_case.evidence[0])))

    def test_caller_transaction_is_not_committed(self) -> None:
        created = self._supported_case()
        with connect_database(self.database) as connection:
            connection.execute("BEGIN")
            with self.assertRaises(WorkflowError) as caught:
                transition_workflow(connection, created.case_id, STATE_INVESTIGATING)
            self.assertEqual(caught.exception.error_type, WORKFLOW_NOT_DURABLE)
            self.assertEqual(str(caught.exception), WORKFLOW_NOT_DURABLE_MESSAGE)
            self.assertTrue(connection.in_transaction)
            self.assertEqual(_event_count_on(connection), 0)
            connection.rollback()
        self.assertEqual(_event_count(self.database), 0)
        self.assertEqual(_reopen_workflow(self.database, created.case_id).state, STATE_OPENED)

    def test_transition_does_not_call_runtime_or_decision(self) -> None:
        created = self._supported_case()
        with (
            patch("support_knowledge_engine.runtime.decide_evidence") as decide,
            patch("support_knowledge_engine.runtime.run_runtime") as runtime,
            patch("support_knowledge_engine.evidence.decide_evidence") as evidence_decide,
        ):
            self._investigate(created.case_id)
        self.assertEqual(decide.call_count, 0)
        self.assertEqual(runtime.call_count, 0)
        self.assertEqual(evidence_decide.call_count, 0)

    def _supported_case(self) -> CaseRecord:
        with connect_database(self.database) as connection:
            product_id = _product(connection, "AeroCam Mini 2", "ACM2")
            _document(connection, product_id, "handbook.pdf", ORIGINAL_PAGE)
        with connect_database(self.database) as connection:
            case_id = allocate_case_id()
            execution = execute_traced_runtime(
                connection,
                _request(
                    product_id=str(product_id),
                    product_series="AeroCam",
                    document_type="Service Handbook",
                    status="effective",
                    association="linked",
                    firmware_version="1.2.3",
                ),
                case_id=case_id,
            )
            self.assertTrue(execution.trace_ok, execution.error)
            packet = execution.runtime.packet
            decision = execution.runtime.decision
            assert packet is not None and decision is not None
            self.assertEqual(decision.decision_type, DECISION_SUPPORTED)
            self.assertGreater(len(decision.evidence_ids), 0)
            return create_case(
                connection,
                packet,
                decision,
                case_id=case_id,
                run_id=execution.run_id,
            )

    def _packet_case(self) -> CaseRecord:
        packet, decision = _packet()
        with connect_database(self.database) as connection:
            return create_case(connection, packet, decision)

    def _abstain_case(self) -> CaseRecord:
        if _product_count(self.database) == 0:
            with connect_database(self.database) as connection:
                product_id = _product(connection, "AeroCam Mini 2", "ACM2")
                _document(connection, product_id, "handbook.pdf", ORIGINAL_PAGE)
        with connect_database(self.database) as connection:
            execution = execute_traced_runtime(connection, _request("quantum toaster zz-999"))
            packet = execution.runtime.packet
            decision = execution.runtime.decision
            assert packet is not None and decision is not None
            self.assertEqual(decision.decision_type, DECISION_ABSTAIN)
            self.assertEqual(decision.evidence_ids, ())
            return create_case(connection, packet, decision)

    def _investigate(self, case_id: str) -> None:
        with connect_database(self.database) as connection:
            moved = transition_workflow(connection, case_id, STATE_INVESTIGATING)
        self.assertEqual(moved.state, STATE_INVESTIGATING)

    def _reject(self, case_id: str, to_state: str, evidence_id: str | None = None) -> None:
        before = _event_count(self.database)
        with connect_database(self.database) as connection:
            with self.assertRaises(WorkflowError) as caught:
                transition_workflow(
                    connection,
                    case_id,
                    to_state,
                    evidence_id=evidence_id,
                )
            self.assertEqual(caught.exception.error_type, WORKFLOW_INVALID)
            self.assertEqual(str(caught.exception), WORKFLOW_INVALID_MESSAGE)
            if evidence_id is not None:
                self.assertNotIn(evidence_id, str(caught.exception))
        self.assertEqual(_event_count(self.database), before)

    def _reject_resolved(self, case_id: str, evidence_id: str | None) -> None:
        before = _event_rows(self.database, case_id)
        with connect_database(self.database) as connection:
            with self.assertRaises(WorkflowError) as caught:
                transition_workflow(
                    connection,
                    case_id,
                    STATE_RESOLVED,
                    evidence_id=evidence_id,
                )
            self.assertEqual(caught.exception.error_type, WORKFLOW_INVALID)
            self.assertEqual(str(caught.exception), WORKFLOW_INVALID_MESSAGE)
            if isinstance(evidence_id, str):
                self.assertNotIn(evidence_id, str(caught.exception))
        self.assertEqual(_event_rows(self.database, case_id), before)

    def _reject_forged_abstain(self, case_id: str, expected_type: str) -> None:
        with connect_database(self.database) as connection:
            case = read_case(connection, case_id)
            real_reference = workflow_decision_reference(case)
            forged = "dec1-" + "ab" * 32
            for reference in (real_reference, forged):
                with self.assertRaises(WorkflowError) as caught:
                    transition_workflow(
                        connection,
                        case_id,
                        STATE_ABSTAINED,
                        decision_reference=reference,
                    )
                self.assertEqual(caught.exception.error_type, WORKFLOW_INVALID)
                self.assertNotIn(reference, str(caught.exception))
            stored_type = connection.execute(
                "SELECT decision_type FROM support_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()[0]
        self.assertEqual(case.decision_type, expected_type)
        self.assertEqual(stored_type, expected_type)
        self.assertEqual(_reopen_workflow(self.database, case_id).state, STATE_INVESTIGATING)

    def _race(self, case_id: str, to_state: str, evidence_id: str | None = None) -> int:
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        guard = threading.Lock()

        def attempt() -> None:
            try:
                with connect_database(self.database) as connection:
                    barrier.wait(timeout=5)
                    transition_workflow(
                        connection,
                        case_id,
                        to_state,
                        evidence_id=evidence_id,
                    )
                with guard:
                    outcomes.append("ok")
            except WorkflowError as exc:
                self.assertIn(exc.error_type, {WORKFLOW_INVALID, WORKFLOW_LOCKED})
                self.assertNotIn(case_id, str(exc))
                with guard:
                    outcomes.append("err")

        threads = [threading.Thread(target=attempt) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual([thread.is_alive() for thread in threads], [False, False])
        self.assertEqual(sorted(outcomes), ["err", "ok"])
        return outcomes.count("ok")


class WorkflowMigrationTests(unittest.TestCase):
    def test_migration_007_is_idempotent_and_restore_drops_events(self) -> None:
        self.assertEqual(
            MIGRATION_007_DATA_LOSS,
            "Restoring a backup taken before migration 7 discards every workflow_events "
            "row written after that backup. The restored database returns to schema 6 "
            "and has no support workflow history.",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "g7.db"
            _migrate_through(database, 6)
            _seed_durable_document(database)
            packet, decision = _packet()
            with connect_database(database) as connection:
                self.assertEqual(current_schema_version(connection), 6)
                created = create_case(connection, packet, decision)
            pre_backup, pre_details = create_backup(database, root / "before-007", migrate=False)
            self.assertEqual(pre_details["schema_version"], 6)
            self.assertEqual(verify_backup(pre_backup)["schema_version"], 6)

            init_database(database)
            init_database(database)
            with connect_database(database) as connection:
                self.assertEqual(current_schema_version(connection), 7)
                _migration_007_support_workflow(connection)
                _migration_007_support_workflow(connection)
                version_rows = connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version = 7"
                ).fetchone()[0]
                self.assertIsNotNone(
                    connection.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type = 'table' AND name = 'workflow_events'"""
                    ).fetchone()
                )
                opened = read_workflow(connection, created.case_id)
                self.assertEqual(opened.state, STATE_OPENED)
                moved = transition_workflow(connection, created.case_id, STATE_INVESTIGATING)
                self.assertEqual(moved.state, STATE_INVESTIGATING)
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE workflow_events SET to_state = 'resolved'"
                    )
                connection.rollback()
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("DELETE FROM workflow_events")
                connection.rollback()
            self.assertEqual(version_rows, 1)
            self.assertEqual(_reopen_workflow(database, created.case_id).state, STATE_INVESTIGATING)

            restored = restore_backup(pre_backup, database, confirm=True)
            self.assertEqual(restored["schema_version"], 6)
            with connect_database(database) as connection:
                self.assertEqual(current_schema_version(connection), 6)
                self.assertIsNone(
                    connection.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type = 'table' AND name = 'workflow_events'"""
                    ).fetchone()
                )
                stored = read_case(connection, created.case_id)
                document = connection.execute(
                    "SELECT filename, sha256, status FROM documents"
                ).fetchone()
            self.assertEqual(stored.case_id, created.case_id)
            self.assertEqual(stored.evidence[0].evidence_id, created.evidence[0].evidence_id)
            self.assertEqual(document["filename"], "pre-g5-durable.pdf")
            self.assertEqual(document["sha256"], "c" * 64)
            self.assertEqual(document["status"], "effective")


def _reopen_workflow(database: Path, case_id: str):
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        return read_workflow(connection, case_id)
    finally:
        connection.close()


def read_case_on(database: Path, case_id: str) -> CaseRecord:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    try:
        return read_case(connection, case_id)
    finally:
        connection.close()


def _event_count(database: Path) -> int:
    connection = sqlite3.connect(database)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0])
    finally:
        connection.close()


def _event_count_on(connection: sqlite3.Connection) -> int:
    return int(connection.execute("SELECT COUNT(*) FROM workflow_events").fetchone()[0])


def _event_rows(database: Path, case_id: str) -> int:
    connection = sqlite3.connect(database)
    try:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM workflow_events WHERE case_id = ?",
                (case_id,),
            ).fetchone()[0]
        )
    finally:
        connection.close()


def _event_blob(database: Path) -> str:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("SELECT * FROM workflow_events").fetchall()
    finally:
        connection.close()
    return "\n".join(" ".join(str(row[name]) for name in row.keys()) for row in rows)


def _stored_targets(database: Path, case_id: str) -> list[str]:
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """SELECT to_state FROM workflow_events
               WHERE case_id = ? ORDER BY sequence""",
            (case_id,),
        ).fetchall()
    finally:
        connection.close()
    return [str(row["to_state"]) for row in rows]


def _insert_event(
    database: Path,
    case_id: str,
    sequence: int,
    from_state: str,
    to_state: str,
    *,
    evidence_id: str | None = None,
    decision_reference: str | None = None,
    recorded_at: str = "2026-01-04T00:00:00+00:00",
) -> None:
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """INSERT INTO workflow_events (
                   case_id, sequence, from_state, to_state, evidence_id,
                   decision_reference, recorded_at, event_schema_version
               ) VALUES (?, ?, ?, ?, ?, ?, ?, '1')""",
            (
                case_id,
                sequence,
                from_state,
                to_state,
                evidence_id,
                decision_reference,
                recorded_at,
            ),
        )
        connection.commit()
    finally:
        connection.close()


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


def _document_status(database: Path) -> str:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute(
            "SELECT status FROM documents ORDER BY id"
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return str(row[0])


def _log_count(database: Path) -> int:
    connection = sqlite3.connect(database)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM search_logs").fetchone()[0])
    finally:
        connection.close()


def _product_count(database: Path) -> int:
    connection = sqlite3.connect(database)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM products").fetchone()[0])
    finally:
        connection.close()
