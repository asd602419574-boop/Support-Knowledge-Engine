from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support_knowledge_engine.corpus_report import collect_corpus_statistics
from support_knowledge_engine.demo_data import seed_demo_data
from support_knowledge_engine.evaluation import run_evaluation
from tests.helpers import PROJECT_ROOT, SAMPLE_DIR


class CorpusPilotTests(unittest.TestCase):
    def test_corpus_distribution_and_evaluation_command(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "pilot.db"
            seed_demo_data(database, SAMPLE_DIR)
            stats = collect_corpus_statistics(database)
            report = run_evaluation(
                database, PROJECT_ROOT / "evals" / "corpus_pilot_cases.json", root / "pilot.md"
            )
            self.assertTrue((root / "pilot.json").is_file())
        self.assertGreaterEqual(stats["document_count"], 20)
        self.assertGreaterEqual(stats["product_count"], 4)
        self.assertGreaterEqual(stats["duplicate_file_count"], 2)
        self.assertGreaterEqual(report["total"], 80)
        self.assertIn("recall_at_3", report["metrics"])
        self.assertEqual(report["metrics"]["product_leakage_rate"], 0)
