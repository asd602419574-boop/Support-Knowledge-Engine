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

        self.assertIn("通过率", report_text)
        self.assertIn("ACM2", report_text)
        # G1 baseline lock for evals/search_cases.json. These 13 cases have no
        # should_return_answer field, so the evaluator uses the legacy path and
        # calls search_documents directly. The assertions below pin that path's
        # current retrieval output. They do not add coverage.
        metrics = report["metrics"]
        self.assertEqual(report["total"], 13)
        self.assertEqual(report["passed"], 13)
        self.assertEqual(report["pass_rate"], 1.0)
        self.assertEqual(metrics["recall_at_1"], 1.0)
        self.assertEqual(metrics["recall_at_3"], 1.0)
        self.assertEqual(metrics["mrr"], 1.0)
        self.assertEqual(metrics["product_leakage_rate"], 0.0)
        # The next three numbers are the current evaluator output only.
        # alias_recognized is None for every legacy case, so the alias rate uses
        # an empty sample and is 0. That 0 is not an alias failure.
        # should_return_answer is forced true, so there is no no-answer sample.
        # outdated_mis_hit stays false because these cases do not describe an
        # outdated-document scenario. Those zeros do not show that either
        # scenario is covered. average_search_ms is intentionally not locked.
        self.assertEqual(metrics["alias_recognition_success_rate"], 0.0)
        self.assertEqual(metrics["no_answer_false_return_rate"], 0.0)
        self.assertEqual(metrics["outdated_document_mis_hit_rate"], 0.0)


if __name__ == "__main__":
    unittest.main()
