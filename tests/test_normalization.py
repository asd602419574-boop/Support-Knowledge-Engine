from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support_knowledge_engine.db import connect_database, init_database
from support_knowledge_engine.governance import add_product_alias, create_product
from support_knowledge_engine.normalization import normalize_query


class QueryNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "test.db"
        init_database(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_nfkc_case_spacing_synonym_spelling_and_product_separation(self):
        with connect_database(self.database) as connection:
            product_id = create_product(
                connection, {"standard_name": "AeroCam Mini 2", "product_series": "AeroCam", "status": "active"},
                "测试", "测试者",
            )
            add_product_alias(connection, product_id, "ACM2", "abbreviation", "测试", "测试者")
            result = normalize_query(connection, "  ＡＣＭ２，CALBRATION  开不了机！！ ")
        self.assertEqual(result.product_ids, (product_id,))
        self.assertIn("calibration", result.normalized)
        self.assertIn("无法开机", result.normalized)
        self.assertNotIn("acm2", result.retrieval_query)
        self.assertTrue(any(rule.startswith("unicode:") for rule in result.applied_rules))
        self.assertTrue(any(rule.startswith("spelling:") for rule in result.applied_rules))
        self.assertTrue(any(rule.startswith("synonym:") for rule in result.applied_rules))

    def test_unknown_text_is_preserved_conservatively(self):
        with connect_database(self.database) as connection:
            result = normalize_query(connection, "unknown-token-739")
        self.assertIn("unknown-token-739", result.original)
        self.assertEqual(result.product_ids, ())


if __name__ == "__main__":
    unittest.main()
