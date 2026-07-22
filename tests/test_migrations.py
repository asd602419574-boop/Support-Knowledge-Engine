from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from support_knowledge_engine.db import BASE_SCHEMA, connect_database, init_database


class MigrationTests(unittest.TestCase):
    def test_phase_one_database_is_upgraded_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "phase-one.db"
            connection = sqlite3.connect(database_path)
            connection.executescript(BASE_SCHEMA)
            connection.execute(
                """INSERT INTO documents
                   (file_path, filename, title, product_series, product_model,
                    document_type, language, version, release_date, source_url,
                    sha256, imported_at, status, page_count, error_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)""",
                (
                    "C:/虚构/legacy.pdf", "legacy.pdf", "Legacy Guide", "Legacy", "L-1",
                    "Guide", "en-US", "1.0", "2026-01-01", "https://example.com/legacy",
                    "a" * 64, "2026-01-01T00:00:00+08:00", "已索引",
                ),
            )
            connection.execute(
                "INSERT INTO pages (document_id, page_number, content) VALUES (1, 1, 'legacy searchable text')"
            )
            connection.execute(
                "INSERT INTO page_fts (content, document_id, page_number) VALUES ('legacy searchable text', 1, 1)"
            )
            connection.commit()
            connection.close()

            init_database(database_path)
            init_database(database_path)

            with connect_database(database_path) as upgraded:
                versions = [row["version"] for row in upgraded.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )]
                document = upgraded.execute("SELECT * FROM documents").fetchone()
                field_count = upgraded.execute(
                    "SELECT COUNT(*) FROM document_field_values WHERE document_id = 1"
                ).fetchone()[0]
                fts_count = upgraded.execute("SELECT COUNT(*) FROM page_fts").fetchone()[0]

        self.assertEqual(versions, [1, 2, 3])
        self.assertEqual(document["status"], "effective")
        self.assertEqual(field_count, 9)
        self.assertEqual(fts_count, 1)


if __name__ == "__main__":
    unittest.main()
