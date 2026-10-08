from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz

from support_knowledge_engine.backup import create_backup, restore_backup
from support_knowledge_engine.db import BASE_SCHEMA, connect_database, init_database
from support_knowledge_engine.dji_catalog import ensure_dji_products, load_dji_catalog
from support_knowledge_engine.importer import (
    MAX_IN_MEMORY_PDF_BYTES,
    extract_pdf,
    import_directory,
)
from support_knowledge_engine.migrations import _migration_002_governance
from support_knowledge_engine.repository import search_documents, search_with_context
from support_knowledge_engine.sources import acquire_sources


class _FakePage:
    def get_text(self, _mode: str) -> str:
        return "Synthetic large manual page"


class _FakeDocument:
    needs_pass = False
    metadata: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter([_FakePage()])


class _FakePdfBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def open(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _FakeDocument()


class IntegrationCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "knowledge.db"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    @staticmethod
    def _write_pdf(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        document = fitz.open()
        try:
            page = document.new_page()
            page.insert_text((72, 72), text)
            document.save(path)
        finally:
            document.close()

    def _create_dji_catalog(self) -> tuple[Path, Path]:
        catalog_root = self.root / "vendor-catalog"
        pdf_path = catalog_root / "files" / "fiction-action" / "manual.pdf"
        self._write_pdf(pdf_path, "calibration beacon synthetic evidence")
        manifest = {
            "summary": {"region": "CN", "language": "zh-CN"},
            "documents": [
                {
                    "series_title": "Fiction Action Series",
                    "product_title": "Fiction Action Test",
                    "product_slug": "fiction-action-test",
                    "manual_title": "Fiction Action Test User Manual v1.0",
                    "manual_category": "User Manual",
                    "version": "v1.0",
                    "release_at": "2026-01-02",
                    "language": "en-US",
                    "source_url": "https://example.com/fiction/manual.pdf",
                    "local_path": pdf_path.relative_to(catalog_root).as_posix(),
                    "download_status": "downloaded",
                }
            ],
        }
        (catalog_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        return catalog_root, pdf_path

    def _load_products_and_import_catalog(self, catalog_root: Path):
        init_database(self.database)
        catalog = load_dji_catalog(catalog_root)
        self.assertIsNotNone(catalog)
        assert catalog is not None
        with connect_database(self.database) as connection:
            ensure_dji_products(connection, catalog)
        return import_directory(catalog_root, self.database)

    def test_dji_catalog_works_with_migration3_search_logging_and_confidence(self) -> None:
        catalog_root, _ = self._create_dji_catalog()
        summary = self._load_products_and_import_catalog(catalog_root)
        self.assertEqual(summary.imported, 1)

        with connect_database(self.database) as connection:
            version = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
            document = connection.execute("SELECT * FROM documents").fetchone()
            fields = {
                row["field_name"]: row["extracted_value"]
                for row in connection.execute(
                    "SELECT * FROM document_field_values WHERE document_id = ?",
                    (document["id"],),
                )
            }
            alias_owners = connection.execute(
                """SELECT COUNT(DISTINCT product_id) FROM product_aliases
                   WHERE normalized_alias = 'fiction-action-test'"""
            ).fetchone()[0]
            outcome = search_with_context(
                connection, "fiction-action-test calibration beacon"
            )
            no_answer = search_with_context(
                connection, "fiction-action-test quantum toaster Z-999"
            )
            logs = connection.execute(
                "SELECT * FROM search_logs ORDER BY id"
            ).fetchall()

        self.assertEqual(version, 5)
        self.assertEqual(document["authority_level"], "authoritative")
        self.assertEqual(fields["title"], "Fiction Action Test User Manual v1.0")
        self.assertEqual(alias_owners, 1)
        self.assertEqual(outcome["match_state"], "high_confidence")
        self.assertEqual(len(outcome["results"]), 1)
        self.assertEqual(outcome["results"][0]["page_number"], 1)
        self.assertIn("Fiction Action Test", logs[0]["recognized_products"])
        self.assertEqual(no_answer["match_state"], "insufficient_evidence")
        self.assertEqual(no_answer["results"], [])
        self.assertEqual(len(logs), 2)

    def test_same_pdf_across_controlled_and_dji_inputs_is_deduplicated_and_enriched(self) -> None:
        catalog_root, original_pdf = self._create_dji_catalog()
        original_bytes = original_pdf.read_bytes()
        source_manifest = self.root / "sources.json"
        source_manifest.write_text(
            json.dumps(
                {
                    "sources": [
                        {
                            "source_id": "fiction-controlled",
                            "product_name": "Fiction Action Test",
                            "document_type": "User Manual",
                            "local_path": str(original_pdf),
                            "language": "en-US",
                            "expected_filename": "controlled-copy.pdf",
                            "enabled": True,
                            "notes": "synthetic compatibility fixture",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        raw_directory = self.root / "controlled-raw"
        acquired = acquire_sources(source_manifest, self.database, raw_directory)
        first_import = import_directory(raw_directory, self.database)

        catalog = load_dji_catalog(catalog_root)
        assert catalog is not None
        with connect_database(self.database) as connection:
            ensure_dji_products(connection, catalog)
        second_import = import_directory(catalog_root, self.database)

        with connect_database(self.database) as connection:
            documents = connection.execute("SELECT * FROM documents").fetchall()
            duplicate_log = connection.execute(
                """SELECT outcome FROM import_items
                   WHERE file_path = ? ORDER BY id DESC LIMIT 1""",
                (str(original_pdf.resolve()),),
            ).fetchone()

        self.assertEqual(acquired.copied, 1)
        self.assertEqual(first_import.imported, 1)
        self.assertEqual(second_import.duplicates, 1)
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["authority_level"], "authoritative")
        self.assertEqual(documents[0]["title"], "Fiction Action Test User Manual v1.0")
        self.assertEqual(duplicate_log["outcome"], "重复")
        self.assertEqual(original_pdf.read_bytes(), original_bytes)
        self.assertNotEqual(Path(documents[0]["file_path"]), original_pdf.resolve())

    def test_large_pdf_uses_path_backend_without_reading_file_into_memory(self) -> None:
        large_pdf = self.root / "synthetic-large.pdf"
        with large_pdf.open("wb") as stream:
            stream.seek(MAX_IN_MEMORY_PDF_BYTES)
            stream.write(b"\0")
        backend = _FakePdfBackend()

        with patch(
            "support_knowledge_engine.importer._load_pdf_backend",
            return_value=backend,
        ):
            _metadata, pages = extract_pdf(large_pdf)

        self.assertEqual(pages, ["Synthetic large manual page"])
        self.assertEqual(len(backend.calls), 1)
        args, kwargs = backend.calls[0]
        self.assertEqual(args, (str(large_pdf),))
        self.assertEqual(kwargs, {"filetype": "pdf"})

    def test_backup_restore_preserves_migration3_tables_and_rows(self) -> None:
        init_database(self.database)
        with connect_database(self.database) as connection:
            connection.execute(
                """INSERT INTO source_fetches
                   (source_id, fetched_at, result, dry_run)
                   VALUES ('synthetic-source', '2026-01-01T00:00:00+08:00', 'checked', 1)"""
            )
            connection.execute(
                """INSERT INTO search_logs
                   (original_query, normalized_query, applied_rules,
                    recognized_products, match_state, result_count, elapsed_ms, created_at)
                   VALUES ('query', 'query', '[]', '[]',
                           'insufficient_evidence', 0, 1.0,
                           '2026-01-01T00:00:00+08:00')"""
            )

        backup, details = create_backup(self.database, self.root / "backups")
        self.assertEqual(details["schema_version"], 5)
        with connect_database(self.database) as connection:
            connection.execute("DELETE FROM source_fetches")
            connection.execute("DELETE FROM search_logs")
        restore_backup(
            backup,
            self.database,
            confirm=True,
            safety_directory=self.root / "safety",
        )

        with connect_database(self.database) as connection:
            source_count = connection.execute(
                "SELECT COUNT(*) FROM source_fetches"
            ).fetchone()[0]
            search_count = connection.execute(
                "SELECT COUNT(*) FROM search_logs"
            ).fetchone()[0]
        self.assertEqual((source_count, search_count), (1, 1))

    def test_version2_upgrade_preserves_documents_pages_fts_and_audit(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.executescript(BASE_SCHEMA)
        connection.execute(
            """CREATE TABLE schema_migrations (
                   version INTEGER PRIMARY KEY,
                   name TEXT NOT NULL,
                   applied_at TEXT NOT NULL
               )"""
        )
        connection.execute(
            "INSERT INTO schema_migrations VALUES (1, 'phase 1 baseline', '2026-01-01')"
        )
        connection.execute(
            """INSERT INTO documents
               (file_path, filename, title, product_series, product_model,
                document_type, language, version, release_date, source_url,
                sha256, imported_at, status, page_count, error_reason)
               VALUES ('C:/synthetic/legacy.pdf', 'legacy.pdf', 'Legacy Fiction Guide',
                       'Fiction', 'F-2', 'Guide', 'en-US', '2.0', '2026-01-01',
                       'https://example.com/legacy.pdf', ?, '2026-01-01',
                       '已索引', 1, NULL)""",
            ("a" * 64,),
        )
        connection.execute(
            "INSERT INTO pages (document_id, page_number, content) VALUES (1, 7, 'legacy mapping phrase')"
        )
        connection.execute(
            "INSERT INTO page_fts (content, document_id, page_number) VALUES ('legacy mapping phrase', 1, 7)"
        )
        _migration_002_governance(connection)
        connection.execute(
            "INSERT INTO schema_migrations VALUES (2, 'knowledge governance and lifecycle', '2026-01-02')"
        )
        connection.execute(
            """INSERT INTO audit_log
               (object_type, object_id, field_name, before_value, after_value,
                changed_at, reason, operator, operation_type)
               VALUES ('document', 1, 'title', 'old', 'new', '2026-01-02',
                       'synthetic migration fixture', 'test', 'update')"""
        )
        connection.commit()
        connection.close()

        init_database(self.database)
        init_database(self.database)
        with connect_database(self.database) as upgraded:
            versions = [
                row["version"]
                for row in upgraded.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]
            results = search_documents(upgraded, "legacy mapping phrase")
            audit_count = upgraded.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            new_tables = {
                row["name"]
                for row in upgraded.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type = 'table' AND name IN ('source_fetches', 'search_logs')"""
                )
            }

        self.assertEqual(versions, [1, 2, 3, 4, 5])
        self.assertEqual(new_tables, {"source_fetches", "search_logs"})
        self.assertEqual(audit_count, 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["page_number"], 7)


if __name__ == "__main__":
    unittest.main()
