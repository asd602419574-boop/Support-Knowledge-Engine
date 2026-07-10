from __future__ import annotations

import unittest

from support_knowledge_engine.governance import ValidationError, update_document

from tests.governance_helpers import document_form_values
from tests.helpers import ImportedDatabaseTestCase


class DocumentLifecycleTests(ImportedDatabaseTestCase):
    def test_document_can_be_superseded_without_deletion(self) -> None:
        with self.connect() as connection:
            documents = connection.execute("SELECT * FROM documents ORDER BY id LIMIT 2").fetchall()
            old, replacement = documents
            values = document_form_values(
                old,
                status="superseded",
                superseded_by_document_id=str(replacement["id"]),
                effective_date="2026-01-01",
                expiration_date="2026-06-01",
                status_note="由新版测试文档替代",
            )
            update_document(connection, old["id"], values, "建立版本替代关系", "测试维护者")
            stored = connection.execute(
                "SELECT status, superseded_by_document_id FROM documents WHERE id = ?", (old["id"],)
            ).fetchone()
            count = connection.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

        self.assertEqual(stored["status"], "superseded")
        self.assertEqual(stored["superseded_by_document_id"], replacement["id"])
        self.assertGreaterEqual(count, 2)

    def test_circular_supersession_is_blocked(self) -> None:
        with self.connect() as connection:
            first, second = connection.execute("SELECT * FROM documents ORDER BY id LIMIT 2").fetchall()
            update_document(
                connection,
                first["id"],
                document_form_values(first, status="superseded", superseded_by_document_id=str(second["id"])),
                "第一条替代关系",
                "测试维护者",
            )
            refreshed_second = connection.execute(
                "SELECT * FROM documents WHERE id = ?", (second["id"],)
            ).fetchone()
            with self.assertRaises(ValidationError) as context:
                update_document(
                    connection,
                    second["id"],
                    document_form_values(
                        refreshed_second,
                        status="superseded",
                        superseded_by_document_id=str(first["id"]),
                    ),
                    "尝试形成循环",
                    "测试维护者",
                )

        self.assertIn("superseded_by_document_id", context.exception.errors)

    def test_expiration_cannot_precede_effective_date(self) -> None:
        with self.connect() as connection:
            document = connection.execute("SELECT * FROM documents LIMIT 1").fetchone()
            with self.assertRaises(ValidationError) as context:
                update_document(
                    connection,
                    document["id"],
                    document_form_values(
                        document, effective_date="2026-06-01", expiration_date="2026-05-01"
                    ),
                    "验证日期顺序",
                    "测试维护者",
                )
        self.assertIn("expiration_date", context.exception.errors)


if __name__ == "__main__":
    unittest.main()
