from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support_knowledge_engine.db import connect_database, init_database
from support_knowledge_engine.importer import import_directory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DIR = PROJECT_ROOT / "sample_docs"


class ImportedDatabaseTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "test.db"
        init_database(self.database_path)
        self.summary = import_directory(SAMPLE_DIR, self.database_path)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def connect(self):
        return connect_database(self.database_path)

