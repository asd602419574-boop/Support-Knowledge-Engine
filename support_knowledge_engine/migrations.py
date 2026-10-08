from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone

from .db import BASE_SCHEMA


Migration = tuple[int, str, Callable[[sqlite3.Connection], None]]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _add_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
    column_name = definition.split()[0]
    if column_name not in _column_names(connection, table):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _migration_001_baseline(connection: sqlite3.Connection) -> None:
    connection.executescript(BASE_SCHEMA)


def _migration_002_governance(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS products (
               id INTEGER PRIMARY KEY,
               standard_name TEXT NOT NULL COLLATE NOCASE UNIQUE,
               product_series TEXT NOT NULL,
               status TEXT NOT NULL CHECK (status IN ('active', 'planned', 'inactive', 'archived')),
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS product_aliases (
               id INTEGER PRIMARY KEY,
               product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
               alias_text TEXT NOT NULL,
               normalized_alias TEXT NOT NULL,
               alias_type TEXT NOT NULL CHECK (
                   alias_type IN ('official_name', 'english_name', 'chinese_name', 'abbreviation', 'common')
               ),
               is_enabled INTEGER NOT NULL DEFAULT 1 CHECK (is_enabled IN (0, 1)),
               created_at TEXT NOT NULL,
               UNIQUE(product_id, normalized_alias)
           )"""
    )

    _add_column(connection, "documents", "canonical_product_id INTEGER REFERENCES products(id)")
    _add_column(connection, "documents", "superseded_by_document_id INTEGER REFERENCES documents(id)")
    _add_column(connection, "documents", "effective_date TEXT")
    _add_column(connection, "documents", "expiration_date TEXT")
    _add_column(connection, "documents", "firmware_range TEXT NOT NULL DEFAULT ''")
    _add_column(connection, "documents", "authority_level TEXT NOT NULL DEFAULT 'reference'")
    _add_column(connection, "documents", "status_note TEXT NOT NULL DEFAULT ''")

    connection.execute(
        """CREATE TABLE IF NOT EXISTS document_field_values (
               id INTEGER PRIMARY KEY,
               document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
               field_name TEXT NOT NULL,
               extracted_value TEXT NOT NULL,
               revised_value TEXT,
               updated_at TEXT NOT NULL,
               UNIQUE(document_id, field_name)
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS audit_log (
               id INTEGER PRIMARY KEY,
               object_type TEXT NOT NULL,
               object_id INTEGER NOT NULL,
               field_name TEXT NOT NULL,
               before_value TEXT,
               after_value TEXT,
               changed_at TEXT NOT NULL,
               reason TEXT NOT NULL,
               operator TEXT NOT NULL,
               operation_type TEXT NOT NULL
           )"""
    )

    connection.execute(
        "UPDATE documents SET status = 'effective' WHERE status = '已索引'"
    )
    connection.execute(
        "UPDATE documents SET status = 'needs_review' WHERE status = '待确认'"
    )

    editable_fields = (
        "title",
        "product_series",
        "product_model",
        "document_type",
        "language",
        "version",
        "release_date",
        "source_url",
        "status",
    )
    timestamp = _now()
    documents = connection.execute("SELECT * FROM documents").fetchall()
    for document in documents:
        for field_name in editable_fields:
            connection.execute(
                """INSERT OR IGNORE INTO document_field_values
                   (document_id, field_name, extracted_value, revised_value, updated_at)
                   VALUES (?, ?, ?, NULL, ?)""",
                (document["id"], field_name, str(document[field_name]), timestamp),
            )

    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_aliases_normalized ON product_aliases(normalized_alias, is_enabled)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_product_id ON documents(canonical_product_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_object ON audit_log(object_type, object_id, changed_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_changed_at ON audit_log(changed_at DESC)"
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS audit_log_no_update
           BEFORE UPDATE ON audit_log
           BEGIN
               SELECT RAISE(ABORT, 'audit_log is append-only');
           END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
           BEFORE DELETE ON audit_log
           BEGIN
               SELECT RAISE(ABORT, 'audit_log is append-only');
           END"""
    )


def _migration_003_corpus_pilot(connection: sqlite3.Connection) -> None:
    _add_column(connection, "import_runs", "duration_ms REAL")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS source_fetches (
               id INTEGER PRIMARY KEY,
               source_id TEXT NOT NULL,
               request_url TEXT,
               final_url TEXT,
               local_source_path TEXT,
               http_status INTEGER,
               content_type TEXT,
               etag TEXT,
               last_modified TEXT,
               fetched_at TEXT NOT NULL,
               file_size INTEGER,
               sha256 TEXT,
               result TEXT NOT NULL CHECK (
                   result IN ('downloaded', 'copied', 'duplicate', 'checked', 'failed', 'disabled')
               ),
               error_reason TEXT,
               saved_path TEXT,
               dry_run INTEGER NOT NULL DEFAULT 0 CHECK (dry_run IN (0, 1))
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS search_logs (
               id INTEGER PRIMARY KEY,
               original_query TEXT NOT NULL,
               normalized_query TEXT NOT NULL,
               applied_rules TEXT NOT NULL,
               recognized_products TEXT NOT NULL,
               match_state TEXT NOT NULL CHECK (match_state IN (
                   'high_confidence', 'possible_match', 'ambiguous_product',
                   'version_conflict', 'outdated_only', 'insufficient_evidence'
               )),
               result_count INTEGER NOT NULL,
               elapsed_ms REAL NOT NULL,
               created_at TEXT NOT NULL
           )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_fetches_source ON source_fetches(source_id, fetched_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_fetches_hash ON source_fetches(sha256)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_search_logs_created ON search_logs(created_at DESC)"
    )


# Code revert does not roll the database back. Restoring a backup taken before
# migration 4 discards every runtime_traces row written after that migration.
MIGRATION_004_DATA_LOSS = (
    "Restoring a backup taken before migration 4 discards every runtime_traces "
    "row written after that migration."
)


def _migration_004_runtime_trace(connection: sqlite3.Connection) -> None:
    # execute keeps this migration inside apply_migrations' transaction.
    connection.execute(
        """CREATE TABLE IF NOT EXISTS runtime_traces (
               id INTEGER PRIMARY KEY,
               run_id TEXT NOT NULL UNIQUE,
               step_id TEXT NOT NULL CHECK (step_id = '1'),
               tool_name TEXT,
               tool_version TEXT,
               runtime_version TEXT NOT NULL,
               request_schema_version TEXT,
               response_schema_version TEXT,
               runtime_request_schema_version TEXT,
               runtime_response_schema_version TEXT NOT NULL,
               input_json TEXT NOT NULL,
               output_json TEXT NOT NULL,
               decision_json TEXT NOT NULL,
               evidence_ids TEXT NOT NULL,
               latency_ms REAL NOT NULL CHECK (latency_ms >= 0),
               termination_reason TEXT NOT NULL CHECK (termination_reason IN (
                   'supported', 'abstain', 'conflict',
                   'invalid_request', 'retrieval_failure', 'retrieval_timeout',
                   'source_index_mismatch'
               )),
               created_at TEXT NOT NULL,
               trace_schema_version TEXT NOT NULL CHECK (trace_schema_version = '1')
           )"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS runtime_traces_no_update
           BEFORE UPDATE ON runtime_traces
           BEGIN
               SELECT RAISE(ABORT, 'runtime_traces is append-only');
           END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS runtime_traces_no_delete
           BEFORE DELETE ON runtime_traces
           BEGIN
               SELECT RAISE(ABORT, 'runtime_traces is append-only');
           END"""
    )
    # REPLACE deletes the old row without firing DELETE triggers unless
    # recursive_triggers is on. Reject that rewrite while the old row is visible.
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS runtime_traces_no_replace
           BEFORE INSERT ON runtime_traces
           WHEN EXISTS (
               SELECT 1 FROM runtime_traces
               WHERE run_id = NEW.run_id OR id = NEW.id
           )
           BEGIN
               SELECT RAISE(ABORT, 'runtime_traces is append-only');
           END"""
    )


# Code revert does not roll the database back. Restoring a backup taken before
# migration 5 discards case rows and case-linked trace ids written after it.
MIGRATION_005_DATA_LOSS = (
    "Restoring a backup taken before migration 5 discards every support_cases, "
    "case_evidence, and case_trace_links row written after that migration, "
    "and discards runtime_traces.case_id values written after that migration."
)


def _migration_005_case_store(connection: sqlite3.Connection) -> None:
    # execute keeps this migration inside apply_migrations' transaction.
    hex32 = "[0-9a-f]" * 32
    case_id_check = f"length(case_id) = 38 AND case_id GLOB 'case1-{hex32}'"
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS support_cases (
               id INTEGER PRIMARY KEY,
               case_id TEXT NOT NULL UNIQUE CHECK ({case_id_check}),
               created_at TEXT NOT NULL,
               context_json TEXT NOT NULL,
               context_schema_version TEXT NOT NULL CHECK (context_schema_version = '1'),
               context_updated_at TEXT NOT NULL,
               decision_type TEXT NOT NULL CHECK (
                   decision_type IN ('supported', 'abstain', 'conflict')
               ),
               reason_codes_json TEXT NOT NULL,
               retrieval_state TEXT NOT NULL CHECK (
                   retrieval_state IN (
                       'ambiguous_product', 'high_confidence', 'insufficient_evidence',
                       'outdated_only', 'possible_match', 'version_conflict'
                   )
               ),
               packet_schema_version TEXT NOT NULL
           )"""
    )
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS case_evidence (
               id INTEGER PRIMARY KEY,
               case_id TEXT NOT NULL REFERENCES support_cases(case_id) ON DELETE RESTRICT,
               evidence_id TEXT NOT NULL,
               snapshot_schema_version TEXT NOT NULL,
               supporting_original_text TEXT NOT NULL,
               document_lifecycle TEXT NOT NULL,
               product_lifecycle TEXT,
               authority_level TEXT NOT NULL,
               original_content_digest TEXT NOT NULL,
               decision_visible_representation TEXT NOT NULL,
               decision_visible_digest TEXT NOT NULL,
               pdf_sha256 TEXT NOT NULL,
               page_number INTEGER NOT NULL CHECK (page_number > 0),
               document_identity TEXT NOT NULL,
               captured_at TEXT NOT NULL,
               source_policy TEXT NOT NULL,
               visible_policy TEXT NOT NULL,
               UNIQUE(case_id, evidence_id)
           )"""
    )
    connection.execute(
        f"""CREATE TABLE IF NOT EXISTS case_trace_links (
               id INTEGER PRIMARY KEY,
               case_id TEXT NOT NULL REFERENCES support_cases(case_id) ON DELETE RESTRICT,
               run_id TEXT NOT NULL CHECK (length(run_id) = 32 AND run_id GLOB '{hex32}'),
               recorded_at TEXT NOT NULL,
               UNIQUE(case_id, run_id)
           )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_case_evidence_case ON case_evidence(case_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_case_trace_links_case ON case_trace_links(case_id)"
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS support_cases_no_delete
           BEFORE DELETE ON support_cases
           BEGIN
               SELECT RAISE(ABORT, 'support_cases identity is immutable');
           END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS support_cases_no_replace
           BEFORE INSERT ON support_cases
           WHEN EXISTS (
               SELECT 1 FROM support_cases
               WHERE id = NEW.id OR case_id = NEW.case_id
           )
           BEGIN
               SELECT RAISE(ABORT, 'support_cases identity is immutable');
           END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS support_cases_identity_no_update
           BEFORE UPDATE OF id, case_id, created_at, context_schema_version,
                             decision_type, reason_codes_json, retrieval_state,
                             packet_schema_version
           ON support_cases
           BEGIN
               SELECT RAISE(ABORT, 'support_cases identity is immutable');
           END"""
    )
    for table in ("case_evidence", "case_trace_links"):
        connection.execute(
            f"""CREATE TRIGGER IF NOT EXISTS {table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END"""
        )
        connection.execute(
            f"""CREATE TRIGGER IF NOT EXISTS {table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END"""
        )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS case_evidence_no_replace
           BEFORE INSERT ON case_evidence
           WHEN EXISTS (
               SELECT 1 FROM case_evidence
               WHERE id = NEW.id OR (case_id = NEW.case_id AND evidence_id = NEW.evidence_id)
           )
           BEGIN
               SELECT RAISE(ABORT, 'case_evidence is append-only');
           END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS case_trace_links_no_replace
           BEFORE INSERT ON case_trace_links
           WHEN EXISTS (
               SELECT 1 FROM case_trace_links
               WHERE id = NEW.id OR (case_id = NEW.case_id AND run_id = NEW.run_id)
           )
           BEGIN
               SELECT RAISE(ABORT, 'case_trace_links is append-only');
           END"""
    )
    _add_column(
        connection,
        "runtime_traces",
        "case_id TEXT CHECK (case_id IS NULL OR ("
        f"{case_id_check}))",
    )


# Code revert does not roll the database back. Restoring a backup taken before
# migration 6 discards fidelity columns and case rows written after it.
MIGRATION_006_DATA_LOSS = (
    "Restoring a backup taken before migration 6 discards case_evidence "
    "identity, metadata digest, firmware applicability, tool provenance, "
    "transformation version, and source locator values written after that "
    "migration, and discards support_cases, case_evidence, and "
    "case_trace_links rows written after that backup."
)


def _migration_006_case_evidence_fidelity(connection: sqlite3.Connection) -> None:
    # execute keeps this migration inside apply_migrations' transaction.
    for definition in (
        "document_id INTEGER",
        "filename TEXT",
        "source_locator TEXT",
        "source_url TEXT",
        "supporting_text_source TEXT",
        "metadata_digest TEXT",
        "canonical_product_id INTEGER",
        "canonical_product_name TEXT",
        "firmware_range TEXT",
        "firmware_applicability TEXT",
        "retrieval_tool_name TEXT",
        "retrieval_tool_version TEXT",
        "retrieval_response_schema_version TEXT",
        "decision_visible_source TEXT",
        "transformation_version TEXT",
    ):
        _add_column(connection, "case_evidence", definition)


MIGRATIONS: tuple[Migration, ...] = (
    (1, "phase 1 baseline", _migration_001_baseline),
    (2, "knowledge governance and lifecycle", _migration_002_governance),
    (3, "controlled corpus acquisition and search observability", _migration_003_corpus_pilot),
    (4, "runtime trace", _migration_004_runtime_trace),
    (5, "case store", _migration_005_case_store),
    (6, "case evidence fidelity", _migration_006_case_evidence_fidelity),
)


def apply_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
               version INTEGER PRIMARY KEY,
               name TEXT NOT NULL,
               applied_at TEXT NOT NULL
           )"""
    )
    connection.commit()
    applied = {
        row["version"]
        for row in connection.execute("SELECT version FROM schema_migrations")
    }

    for version, name, migration in MIGRATIONS:
        if version in applied:
            continue
        try:
            migration(connection)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (version, name, _now()),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def current_schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
    return int(row["version"] or 0)
