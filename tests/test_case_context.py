"""Local case parsing and catalog delivery; no provider credentials required."""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from support_knowledge_engine import create_app
from support_knowledge_engine.demo_data import seed_demo_data
from tests.helpers import PROJECT_ROOT, SAMPLE_DIR


class CaseContextTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js required for browser parser tests')
    def test_local_parser_scenarios(self):
        result = subprocess.run([shutil.which('node'), '--test', 'tests/case_context.test.cjs'],
                                cwd=PROJECT_ROOT, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout.decode('utf-8', errors='replace'))

    def test_page_catalog_and_case_controls_have_no_submission_names(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / 'case.db'
            seed_demo_data(database, SAMPLE_DIR)
            app = create_app({'TESTING': True, 'DATABASE': str(database)})
            html = app.test_client().get('/assistant').text
            catalog = json.loads(re.search(r'id="assistant-product-catalog"[^>]*>(.*?)</script>', html).group(1))
            mini = next(p for p in catalog if p['name'] == 'AeroCam Mini 2')
            self.assertIn('ACM2', mini['aliases'])
            self.assertTrue(mini['id'])
            case_controls = re.findall(r'<(?:input|textarea|select)\b[^>]*id="case-[^"]+"[^>]*>', html)
            self.assertGreater(len(case_controls), 8)
            self.assertTrue(all('name=' not in control for control in case_controls))
