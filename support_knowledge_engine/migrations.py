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


MIGRATIONS: tuple[Migration, ...] = (
    (1, "phase 1 baseline", _migration_001_baseline),
    (2, "knowledge governance and lifecycle", _migration_002_governance),
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
