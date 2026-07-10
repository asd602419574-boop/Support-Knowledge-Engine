from __future__ import annotations

import sqlite3
import unittest

from support_knowledge_engine.governance import ValidationError, update_document
from support_knowledge_engine.repository import get_document, get_document_field_values

from tests.governance_helpers import document_form_values
from tests.helpers import ImportedDatabaseTestCase


class MetadataGovernanceTests(ImportedDatabaseTestCase):
    def test_revision_preserves_extracted_value_and_updates_effective_value(self) -> None:
        with self.connect() as connection:
            document = connection.execute(
                "SELECT * FROM documents WHERE filename LIKE 'Nebula-%'"
            ).fetchone()
            original_title = document["title"]
            values = document_form_values(
                document,
                title="Nebula Switch NS-24 Verified Installation Guide",
                version="2.1",
            )
            changed = update_document(connection, document["id"], values, "核对测试发布记录", "测试维护者")

            current = get_document(connection, document["id"])
            fields = get_document_field_values(connection, document["id"])
            audits = connection.execute(
                "SELECT field_name FROM audit_log WHERE object_type = 'document' AND object_id = ?",
                (document["id"],),
            ).fetchall()

        self.assertEqual(changed, 2)
        self.assertEqual(current["title"], "Nebula Switch NS-24 Verified Installation Guide")
        self.assertEqual(fields["title"]["extracted_value"], original_title)
        self.assertEqual(fields["title"]["revised_value"], current["title"])
        self.assertEqual(fields["title"]["effective_value"], current["title"])
        self.assertEqual({row["field_name"] for row in audits}, {"title", "version"})

    def test_invalid_fields_return_explicit_errors(self) -> None:
        with self.connect() as connection:
            document = connection.execute("SELECT * FROM documents LIMIT 1").fetchone()
            values = document_form_values(
                document,
                title="",
                release_date="2026-99-90",
                source_url="ftp://invalid.example",
                status="deleted",
            )
            with self.assertRaises(ValidationError) as context:
                update_document(connection, document["id"], values, "测试校验", "测试维护者")

        self.assertIn("title", context.exception.errors)
        self.assertIn("release_date", context.exception.errors)
        self.assertIn("source_url", context.exception.errors)
        self.assertIn("status", context.exception.errors)

    def test_audit_log_is_append_only(self) -> None:
        with self.connect() as connection:
            document = connection.execute("SELECT * FROM documents LIMIT 1").fetchone()
            values = document_form_values(document, version="9.9-test")
            update_document(connection, document["id"], values, "验证审计不可变", "测试维护者")
            audit_id = connection.execute("SELECT id FROM audit_log LIMIT 1").fetchone()["id"]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE audit_log SET reason = '覆盖' WHERE id = ?", (audit_id,))


if __name__ == "__main__":
    unittest.main()
