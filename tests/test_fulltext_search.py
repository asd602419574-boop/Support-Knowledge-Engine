from __future__ import annotations

import unittest

from support_knowledge_engine.repository import search_documents

from tests.helpers import ImportedDatabaseTestCase


class FullTextSearchTests(ImportedDatabaseTestCase):
    def test_chinese_keyword_search_returns_source_snippet(self) -> None:
        with self.connect() as connection:
            results = search_documents(connection, "量子灯塔")

        self.assertEqual(len(results), 1)
        self.assertIn("量子灯塔诊断码", results[0]["snippet"])
        self.assertEqual(results[0]["filename"], "星河路由器_XR-100_用户手册_v1.2_zh-CN.pdf")

    def test_filters_are_applied_to_fts_results(self) -> None:
        with self.connect() as connection:
            included = search_documents(connection, "calibration", "Nebula Switch", "Installation Guide")
            excluded = search_documents(connection, "calibration", "星河路由器", "")

        self.assertEqual(len(included), 1)
        self.assertEqual(excluded, [])


if __name__ == "__main__":
    unittest.main()

