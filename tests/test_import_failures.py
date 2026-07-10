from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support_knowledge_engine.db import connect_database, init_database
from support_knowledge_engine.importer import import_directory


class ImportFailureTests(unittest.TestCase):
    def test_invalid_pdf_is_recorded_in_failure_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "test.db"
            documents = root / "文档目录"
            documents.mkdir()
            (documents / "损坏文档.pdf").write_bytes(b"not a valid PDF")
            init_database(database_path)

            summary = import_directory(documents, database_path)

            self.assertEqual(summary.failed, 1)
            with connect_database(database_path) as connection:
                item = connection.execute(
                    "SELECT outcome, message FROM import_items"
                ).fetchone()
                run = connection.execute("SELECT status, failed_count FROM import_runs").fetchone()
            self.assertEqual(item["outcome"], "失败")
            self.assertTrue(item["message"])
            self.assertEqual(run["status"], "部分失败")
            self.assertEqual(run["failed_count"], 1)


if __name__ == "__main__":
    unittest.main()

