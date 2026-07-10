from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support_knowledge_engine.db import connect_database, init_database
from support_knowledge_engine.importer import calculate_sha256, import_directory

from tests.helpers import SAMPLE_DIR


class DeduplicationTests(unittest.TestCase):
    def test_sha256_is_stable_and_second_import_is_duplicate(self) -> None:
        sample = next(SAMPLE_DIR.glob("*.pdf"))
        self.assertEqual(calculate_sha256(sample), calculate_sha256(sample))
        self.assertEqual(len(calculate_sha256(sample)), 64)

        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "test.db"
            init_database(database_path)
            first = import_directory(SAMPLE_DIR, database_path)
            second = import_directory(SAMPLE_DIR, database_path)

            self.assertEqual(first.imported, 3)
            self.assertEqual(second.imported, 0)
            self.assertEqual(second.duplicates, 3)
            with connect_database(database_path) as connection:
                count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            self.assertEqual(count, 3)


if __name__ == "__main__":
    unittest.main()

