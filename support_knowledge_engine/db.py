from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    file_path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    title TEXT NOT NULL,
    product_series TEXT NOT NULL,
    product_model TEXT NOT NULL,
    document_type TEXT NOT NULL,
    language TEXT NOT NULL,
    version TEXT NOT NULL,
    release_date TEXT NOT NULL,
    source_url TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    imported_at TEXT NOT NULL,
    status TEXT NOT NULL,
    page_count INTEGER NOT NULL,
    error_reason TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    content TEXT NOT NULL,
    UNIQUE(document_id, page_number)
);

CREATE TABLE IF NOT EXISTS import_runs (
    id INTEGER PRIMARY KEY,
    directory TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    discovered_count INTEGER NOT NULL DEFAULT 0,
    imported_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS import_items (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES import_runs(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    sha256 TEXT,
    outcome TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_documents_product_series ON documents(product_series);
CREATE INDEX IF NOT EXISTS idx_documents_document_type ON documents(document_type);
CREATE INDEX IF NOT EXISTS idx_import_items_run_id ON import_items(run_id);

CREATE VIRTUAL TABLE IF NOT EXISTS page_fts USING fts5(
    content,
    document_id UNINDEXED,
    page_number UNINDEXED,
    tokenize = 'trigram'
);
"""


@contextmanager
def connect_database(database_path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_database(database_path: str | Path) -> None:
    with connect_database(database_path) as connection:
        connection.executescript(SCHEMA)
