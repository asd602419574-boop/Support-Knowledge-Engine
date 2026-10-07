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

    def _create_product(self, connection, name: str, *aliases: str) -> int:
        product_id = create_product(
            connection, {"standard_name": name, "product_series": "AeroCam", "status": "active"},
            "测试", "测试者",
        )
        for alias in aliases:
            add_product_alias(connection, product_id, alias, "abbreviation", "测试", "测试者")
        return product_id

    def test_alias_does_not_match_inside_a_longer_model_token(self):
        # Regression: alias matching used a bare substring test, so a near-miss
        # model number was silently linked to the wrong product.
        near_misses = (
            ("ACM25 gimbal home sensor", "acm25"),
            ("xACM2y gimbal", "xacm2y"),
            ("ACM22 battery", "acm22"),
            ("Mini 20 gimbal", "mini 20"),
        )
        with connect_database(self.database) as connection:
            self._create_product(connection, "AeroCam Mini 2", "ACM2", "Mini 2")
            for query, token in near_misses:
                with self.subTest(query=query):
                    result = normalize_query(connection, query)
                    self.assertEqual(result.product_ids, ())
                    self.assertFalse(result.ambiguous)
                    # The unmatched token must survive into the retrieval query.
                    self.assertIn(token, result.retrieval_query)

    def test_similar_model_numbers_resolve_to_their_own_product(self):
        # "Mini 2" is a prefix of "Mini 20"; neither query may match both.
        with connect_database(self.database) as connection:
            mini_2 = self._create_product(connection, "AeroCam Mini 2", "Mini 2")
            mini_20 = self._create_product(connection, "AeroCam Mini 20", "Mini 20")
            single = normalize_query(connection, "Mini 2 battery")
            double = normalize_query(connection, "mini 20 battery")
        self.assertEqual(single.product_ids, (mini_2,))
        self.assertFalse(single.ambiguous)
        self.assertEqual(double.product_ids, (mini_20,))
        self.assertFalse(double.ambiguous)

    def test_alias_still_matches_as_a_whole_token_in_mixed_text(self):
        queries = (
            "ACM2 gimbal home sensor",
            "gimbal home sensor ACM2",
            "ACM2: gimbal",
            "acm2-gimbal",
            "ＡＣＭ２ gimbal",
            "ACM2故障",
            "故障ACM2",
            "航拍迷你二代，gimbal home sensor",
            "航拍迷你二代故障",
        )
        with connect_database(self.database) as connection:
            product_id = self._create_product(connection, "AeroCam Mini 2", "ACM2", "航拍迷你二代")
            for query in queries:
                with self.subTest(query=query):
                    result = normalize_query(connection, query)
                    self.assertEqual(result.product_ids, (product_id,))
                    self.assertNotIn("acm2", result.retrieval_query)
                    self.assertNotIn("航拍迷你二代", result.retrieval_query)


if __name__ == "__main__":
    unittest.main()
