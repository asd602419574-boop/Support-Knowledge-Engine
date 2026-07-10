from __future__ import annotations

import unittest

from support_knowledge_engine.repository import get_document_pages, search_documents

from tests.helpers import ImportedDatabaseTestCase


class PageMappingTests(ImportedDatabaseTestCase):
    def test_search_result_keeps_pdf_page_number(self) -> None:
        with self.connect() as connection:
            results = search_documents(connection, "琥珀回声")
            self.assertEqual(len(results), 1)
            result = results[0]
            pages = get_document_pages(connection, result["id"])

        self.assertEqual(result["page_number"], 2)
        self.assertEqual(pages[1]["page_number"], 2)
        self.assertIn("琥珀回声测试短语", pages[1]["content"])


if __name__ == "__main__":
    unittest.main()

