from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support_knowledge_engine.db import connect_database
from support_knowledge_engine.demo_data import seed_demo_data
from support_knowledge_engine.governance import add_product_alias, create_product
from support_knowledge_engine.repository import search_with_context
from tests.helpers import SAMPLE_DIR


class SearchConfidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "confidence.db"
        seed_demo_data(self.database, SAMPLE_DIR)

    def tearDown(self):
        self.temp.cleanup()

    def test_high_confidence_outdated_no_answer_and_version_conflict(self):
        with connect_database(self.database) as connection:
            high = search_with_context(connection, "ACM2 gimbal home sensor")
            outdated = search_with_context(connection, "legacy horizon drift reset")
            absent = search_with_context(connection, "quantum toaster error Z-999")
            conflict = search_with_context(connection, "Service Handbook")
        self.assertEqual(high["match_state"], "high_confidence")
        self.assertEqual(outdated["match_state"], "outdated_only")
        self.assertEqual(absent["match_state"], "insufficient_evidence")
        self.assertEqual(conflict["match_state"], "version_conflict")

    def test_ambiguous_alias_returns_no_results_and_is_logged(self):
        with connect_database(self.database) as connection:
            other = create_product(
                connection, {"standard_name": "AeroCam Micro 2", "product_series": "AeroCam", "status": "active"},
                "冲突测试", "测试者",
            )
            add_product_alias(connection, other, "ACM2", "abbreviation", "冲突测试", "测试者")
            outcome = search_with_context(connection, "ACM2 calibration")
            log = connection.execute("SELECT * FROM search_logs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(outcome["match_state"], "ambiguous_product")
        self.assertEqual(outcome["results"], [])
        self.assertEqual(log["original_query"], "ACM2 calibration")
        self.assertIn("AeroCam Micro 2", log["recognized_products"])
