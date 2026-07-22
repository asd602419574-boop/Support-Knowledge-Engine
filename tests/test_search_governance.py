from __future__ import annotations

import unittest

from support_knowledge_engine.repository import search_documents

from tests.helpers import ImportedDatabaseTestCase


class SearchGovernanceTests(ImportedDatabaseTestCase):
    def test_effective_documents_rank_before_non_effective_documents(self) -> None:
        with self.connect() as connection:
            mini = connection.execute(
                "SELECT id FROM documents WHERE filename = 'AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf'"
            ).fetchone()
            pro = connection.execute(
                "SELECT id FROM documents WHERE filename = 'AeroCam-Pro-2_Service-Handbook_v4.0_en-US.pdf'"
            ).fetchone()
            connection.execute("UPDATE documents SET status = 'needs_review' WHERE id = ?", (mini["id"],))
            connection.execute("UPDATE documents SET status = 'effective' WHERE id = ?", (pro["id"],))
            results = search_documents(connection, "Service Handbook")

        self.assertGreaterEqual(len(results), 2)
        ranks = {row["id"]: index for index, row in enumerate(results)}
        self.assertLess(ranks[pro["id"]], ranks[mini["id"]])
        self.assertEqual(results[ranks[pro["id"]]]["status"], "effective")

    def test_archived_documents_remain_searchable_by_status_filter(self) -> None:
        with self.connect() as connection:
            target = connection.execute(
                "SELECT id FROM documents WHERE filename = 'AeroCam-Pro-2_Service-Handbook_v4.0_en-US.pdf'"
            ).fetchone()
            connection.execute("UPDATE documents SET status = 'archived' WHERE id = ?", (target["id"],))
            results = search_documents(connection, "AP2-90", status="archived")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], target["id"])


if __name__ == "__main__":
    unittest.main()
