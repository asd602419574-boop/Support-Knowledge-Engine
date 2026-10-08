from __future__ import annotations

import tempfile
import tomllib
import unittest
from pathlib import Path

from support_knowledge_engine import create_app
from support_knowledge_engine.db import connect_database, init_database
from support_knowledge_engine.migrations import (
    _migration_001_baseline,
    _migration_002_governance,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ReleaseMetadataTests(unittest.TestCase):
    def test_version_favicon_and_primary_pages(self) -> None:
        project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["version"], "0.3.1")

        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "release-metadata.db"
            app = create_app(
                {
                    "TESTING": True,
                    "DATABASE": str(database),
                    "OPERATOR_NAME": "虚构测试操作者",
                }
            )
            client = app.test_client()

            favicon = client.get("/favicon.ico")
            self.assertEqual(favicon.status_code, 200)
            self.assertEqual(favicon.mimetype, "image/svg+xml")
            self.assertNotIn(b"DJI", favicon.data)
            favicon.close()

            for path in ("/", "/logs"):
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                response.close()

    def test_migration_zero_and_two_reach_schema_three(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zero_database = root / "migration-zero.db"
            init_database(zero_database)

            with connect_database(zero_database) as connection:
                zero_versions = [
                    row["version"]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]

            two_database = root / "migration-two.db"
            with connect_database(two_database) as connection:
                connection.execute(
                    """CREATE TABLE schema_migrations (
                           version INTEGER PRIMARY KEY,
                           name TEXT NOT NULL,
                           applied_at TEXT NOT NULL
                       )"""
                )
                _migration_001_baseline(connection)
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (1, 'phase 1 baseline', '2026-01-01')"
                )
                _migration_002_governance(connection)
                connection.execute(
                    """INSERT INTO schema_migrations
                       VALUES (2, 'knowledge governance and lifecycle', '2026-01-02')"""
                )
                connection.commit()

            init_database(two_database)
            with connect_database(two_database) as connection:
                two_versions = [
                    row["version"]
                    for row in connection.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]

        self.assertEqual(zero_versions, [1, 2, 3, 4, 5, 6])
        self.assertEqual(two_versions, [1, 2, 3, 4, 5, 6])


if __name__ == "__main__":
    unittest.main()
