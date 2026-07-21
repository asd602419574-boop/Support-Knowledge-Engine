from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from support_knowledge_engine.db import connect_database, init_database
from support_knowledge_engine.dji_catalog import ensure_dji_products, load_dji_catalog
from support_knowledge_engine.importer import import_directory

from tests.helpers import SAMPLE_DIR


class DjiCatalogTests(unittest.TestCase):
    def _create_catalog(self, root: Path) -> Path:
        target = root / "files" / "Osmo Action 系列" / "osmo-action-test" / "manual.pdf"
        target.parent.mkdir(parents=True)
        shutil.copyfile(next(SAMPLE_DIR.glob("*.pdf")), target)
        manifest = {
            "summary": {"region": "CN", "language": "zh-CN"},
            "documents": [
                {
                    "series_title": "Osmo Action 系列",
                    "product_title": "Osmo Action Test",
                    "product_slug": "osmo-action-test",
                    "manual_title": "Osmo Action Test - 用户手册 v1.0",
                    "manual_category": "用户手册",
                    "version": "v1.0",
                    "release_at": "2026-01-02",
                    "language": "zh-CN",
                    "source_url": "https://dl.djicdn.com/test/manual.pdf",
                    "local_path": target.relative_to(root).as_posix(),
                    "download_status": "downloaded",
                },
                {
                    "series_title": "Osmo Action 系列",
                    "product_title": "Osmo Action Test",
                    "product_slug": "osmo-action-test",
                    "download_status": "failed",
                    "error": "document HTTP 404",
                },
            ],
        }
        (root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        return target

    def test_manifest_metadata_and_product_are_applied_during_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = self._create_catalog(root)
            database = root / "knowledge.db"
            catalog = load_dji_catalog(root)
            self.assertIsNotNone(catalog)
            assert catalog is not None
            self.assertEqual(catalog.failed_downloads, 1)
            self.assertIn(target.resolve(), catalog.documents_by_path)

            init_database(database)
            with connect_database(database) as connection:
                result = ensure_dji_products(connection, catalog)
            summary = import_directory(root, database)

            self.assertEqual(result["created_products"], 1)
            self.assertEqual(summary.imported, 1)
            with connect_database(database) as connection:
                document = connection.execute("SELECT * FROM documents").fetchone()
                product_count = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            self.assertEqual(product_count, 1)
            self.assertEqual(document["title"], "Osmo Action Test - 用户手册 v1.0")
            self.assertEqual(document["product_series"], "Osmo Action 系列")
            self.assertEqual(document["product_model"], "Osmo Action Test")
            self.assertEqual(document["document_type"], "用户手册")
            self.assertEqual(document["source_url"], "https://dl.djicdn.com/test/manual.pdf")
            self.assertEqual(document["authority_level"], "authoritative")
            self.assertEqual(document["status"], "effective")
            self.assertIsNotNone(document["canonical_product_id"])

    def test_manifest_rejects_paths_outside_catalog_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "manifest.json").write_text(
                json.dumps(
                    {
                        "documents": [
                            {
                                "series_title": "Osmo Action 系列",
                                "product_title": "Osmo Action Test",
                                "product_slug": "osmo-action-test",
                                "local_path": "../outside.pdf",
                                "download_status": "downloaded",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "越出资料目录"):
                load_dji_catalog(root)


if __name__ == "__main__":
    unittest.main()
