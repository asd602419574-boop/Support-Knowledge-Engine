from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support_knowledge_engine.demo_data import seed_demo_data
from support_knowledge_engine.evaluation import run_evaluation

from tests.helpers import PROJECT_ROOT, SAMPLE_DIR


class EvaluationTests(unittest.TestCase):
    def test_retrieval_evaluation_command_data_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            database_path = root / "evaluation.db"
            report_path = root / "report.md"
            seed_demo_data(database_path, SAMPLE_DIR)

            report = run_evaluation(
                database_path,
                PROJECT_ROOT / "evals" / "search_cases.json",
                report_path,
            )

            report_text = report_path.read_text(encoding="utf-8")

        self.assertGreaterEqual(report["total"], 12)
        self.assertEqual(report["passed"], report["total"])
        self.assertIn("通过率", report_text)
        self.assertIn("ACM2", report_text)


if __name__ == "__main__":
    unittest.main()
