from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from support_knowledge_engine.backup import create_backup, restore_backup, verify_backup
from support_knowledge_engine.db import connect_database, init_database


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "knowledge.db"
        init_database(self.database)
        with connect_database(self.database) as connection:
            connection.execute("CREATE TABLE marker(value TEXT)")
            connection.execute("INSERT INTO marker VALUES ('before')")

    def tearDown(self):
        self.temp.cleanup()

    def test_backup_verify_restore_and_safety_backup(self):
        backup, details = create_backup(self.database, self.root / "backups")
        self.assertEqual(details["integrity"], "ok")
        self.assertTrue(verify_backup(backup)["manifest_verified"])
        with connect_database(self.database) as connection:
            connection.execute("UPDATE marker SET value='after'")
        with self.assertRaisesRegex(ValueError, "默认禁止"):
            restore_backup(backup, self.database)
        restored = restore_backup(backup, self.database, confirm=True)
        self.assertTrue(Path(restored["safety_backup"]).is_file())
        with connect_database(self.database) as connection:
            self.assertEqual(connection.execute("SELECT value FROM marker").fetchone()[0], "before")

    def test_corrupt_backup_does_not_replace_current_database(self):
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"not sqlite")
        with self.assertRaises((ValueError, sqlite3.DatabaseError)):
            restore_backup(corrupt, self.database, confirm=True)
        with connect_database(self.database) as connection:
            self.assertEqual(connection.execute("SELECT value FROM marker").fetchone()[0], "before")


if __name__ == "__main__":
    unittest.main()
