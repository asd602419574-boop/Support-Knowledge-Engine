from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from support_knowledge_engine import create_app
from support_knowledge_engine.db import connect_database
from support_knowledge_engine.demo_data import seed_demo_data
from tests.helpers import SAMPLE_DIR


class AssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / 'knowledge.db'
        seed_demo_data(self.database, SAMPLE_DIR)
        self.app = create_app({'TESTING': True, 'DATABASE': str(self.database),
                               'LLM_BASE_URL': 'https://example.com/v1',
                               'LLM_API_KEY': 'test-only', 'LLM_MODEL': 'test-model'})
        self.client = self.app.test_client()
        self.client.get('/assistant')
        with self.client.session_transaction() as session:
            self.token = session.get('assistant_csrf', '')

    def tearDown(self):
        self.temp.cleanup()

    def ask(self, query='ACM2 gimbal home sensor', **overrides):
        body = {'query': query, 'product_id': '1', 'firmware_version': ''}
        body.update(overrides)
        return self.client.post('/api/assistant/ask', json=body,
                                headers={'X-Assistant-CSRF': self.token})

    @staticmethod
    def completion(config, payload):
        source = payload['evidence'][0]
        quote = next(line for line in source['excerpts'] if 'AM2-17' in line)
        return json.dumps({'steps': [{'evidence_id': source['evidence_id'], 'quote': quote}]})

    def test_page_has_product_form_configuration_and_navigation(self):
        response = self.client.get('/assistant')
        self.assertEqual(response.status_code, 200)
        self.assertIn('AeroCam Mini 2', response.text)
        self.assertIn('尚未验证连接', response.text)
        self.assertIn('/assistant', self.client.get('/').text)

    def test_supported_answer_has_real_pdf_reference_and_no_database_writes(self):
        with connect_database(self.database) as db:
            before = '\n'.join(db.iterdump())
        with patch('support_knowledge_engine.assistant.complete_chat', self.completion):
            response = self.ask()
        self.assertEqual(response.status_code, 200)
        data = response.json
        self.assertEqual(data['decision'], 'supported')
        self.assertEqual(data['status'], 'answered')
        self.assertTrue(data['model_called'])
        self.assertIn('AM2-17', data['steps'][0]['text'])
        citation = data['citations'][0]
        self.assertEqual(citation['page_number'], 3)
        self.assertEqual(citation['filename'], 'AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf')
        self.assertIn('#page=3', citation['pdf_url'])
        with self.client.get(citation['pdf_url']) as pdf:
            self.assertEqual(pdf.mimetype, 'application/pdf')
        with connect_database(self.database) as db:
            self.assertEqual(before, '\n'.join(db.iterdump()))

    def test_natural_language_fault_code_question_preserves_product_scope(self):
        with patch('support_knowledge_engine.assistant.complete_chat', self.completion):
            response = self.ask('ACM2 出现 AM2-17，gimbal home sensor 应如何检查？')
        self.assertEqual(response.json['status'], 'answered')
        self.assertTrue(any('AM2-17' in warning for warning in response.json['warnings']))
        with patch('support_knowledge_engine.assistant.complete_chat') as model:
            mismatch = self.ask('ACP2 出现 AM2-17 应如何检查？')
            ambiguous = self.ask('ACM2 和 ACP2 出现 AM2-17 应如何检查？')
        self.assertNotEqual(mismatch.json['decision'], 'supported')
        self.assertNotEqual(ambiguous.json['decision'], 'supported')
        model.assert_not_called()

    def test_abstain_never_calls_model(self):
        with patch('support_knowledge_engine.assistant.complete_chat') as model:
            response = self.ask('totally unknown nonexistent fault zzq991')
        self.assertEqual(response.json['decision'], 'abstain')
        self.assertFalse(response.json['model_called'])
        self.assertEqual(response.json['steps'], [])
        model.assert_not_called()

    def test_changed_pdf_identity_cannot_silently_retarget_citation(self):
        with patch('support_knowledge_engine.assistant.complete_chat', self.completion):
            citation = self.ask().json['citations'][0]
        with connect_database(self.database) as db:
            other = db.execute('SELECT file_path, sha256 FROM documents WHERE id != ? LIMIT 1',
                               (citation['document_id'],)).fetchone()
            # Simulate reimport into the same document row while its citation is open.
            db.execute('DELETE FROM documents WHERE sha256=?', (other['sha256'],))
            db.execute('UPDATE documents SET sha256=?, file_path=? WHERE id=?',
                       (other['sha256'], other['file_path'], citation['document_id']))
        self.assertEqual(self.client.get(citation['pdf_url']).status_code, 409)

    def test_version_conflict_preserves_g3_decision_without_model(self):
        with patch('support_knowledge_engine.assistant.complete_chat') as model:
            response = self.ask('ACM2 Service Handbook')
        self.assertEqual(response.json['decision'], 'conflict')
        self.assertIn('version_conflict', response.json['reason_codes'])
        self.assertEqual(response.json['steps'], [])
        model.assert_not_called()

    def test_prompt_injection_cannot_add_actions_or_script_to_answer(self):
        def injection(config, payload):
            source = payload['evidence'][0]
            return json.dumps({'steps': [{'evidence_id': source['evidence_id'],
                                         'quote': '<script>fetch("/cases/delete")</script>'}],
                               'tool': 'write_database'})
        with patch('support_knowledge_engine.assistant.complete_chat', injection):
            response = self.ask()
        self.assertEqual(response.json['status'], 'invalid_answer')
        self.assertNotIn('<script>', response.text)

    def test_product_mismatch_never_calls_model(self):
        with patch('support_knowledge_engine.assistant.complete_chat') as model:
            response = self.ask('ACP2 compass pulse AM3-18')
        self.assertNotEqual(response.json['decision'], 'supported')
        model.assert_not_called()

    def test_firmware_unknown_never_upgraded(self):
        with connect_database(self.database) as db:
            db.execute("UPDATE documents SET firmware_range='2.4.0 through 2.9.x' WHERE filename=?",
                       ('AeroCam-Mini-2_Service-Handbook_v3.1_en-US.pdf',))
        with patch('support_knowledge_engine.assistant.complete_chat') as model:
            response = self.ask(firmware_version='2.5.0')
        self.assertEqual(response.json['decision'], 'abstain')
        self.assertIn('firmware_unknown', response.json['reason_codes'])
        model.assert_not_called()

    def test_missing_api_key_is_explicit(self):
        self.app.config['LLM_API_KEY'] = ''
        with patch('support_knowledge_engine.assistant.complete_chat') as model:
            response = self.ask()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json['status'], 'not_configured')
        self.assertIn('SUPPORT_KE_LLM_API_KEY', response.json['message'])
        model.assert_not_called()

    def test_unknown_or_unapproved_evidence_and_fabricated_facts_are_rejected(self):
        for answer in (
            {'steps': [{'evidence_id': 'ev1-invented', 'quote': 'reset everything'}]},
            {'steps': [{'evidence_id': 'unused', 'quote': 'reset everything'}]},
        ):
            with self.subTest(answer=answer), patch('support_knowledge_engine.assistant.complete_chat',
                                                    return_value=json.dumps(answer)):
                response = self.ask()
                self.assertEqual(response.json['status'], 'invalid_answer')
                self.assertEqual(response.json['steps'], [])
        def fabricate(config, payload):
            return json.dumps({'steps': [{'evidence_id': payload['evidence'][0]['evidence_id'],
                                         'quote': 'Factory reset fixes all AM2-17 errors.'}]})
        with patch('support_knowledge_engine.assistant.complete_chat', fabricate):
            self.assertEqual(self.ask().json['status'], 'invalid_answer')

    def test_input_and_cross_origin_protection(self):
        self.assertEqual(self.ask(query='').status_code, 400)
        self.assertEqual(self.ask(product_id='99999').status_code, 400)
        self.assertEqual(self.ask(firmware_version=['bad']).status_code, 400)
        self.assertEqual(self.client.post('/api/assistant/ask', json={}).status_code, 403)
        self.assertEqual(self.client.post('/api/assistant/ask', json={}, headers={
            'X-Assistant-CSRF': self.token, 'Origin': 'https://untrusted.example'}).status_code, 403)

    def test_service_failures_do_not_fake_success(self):
        from support_knowledge_engine.assistant_llm import ModelError
        for code in ('model_timeout', 'model_unavailable', 'model_unauthorized',
                     'model_payment_required', 'model_rate_limited'):
            with self.subTest(code=code), patch('support_knowledge_engine.assistant.complete_chat',
                                               side_effect=ModelError(code)):
                response = self.ask()
                self.assertGreaterEqual(response.status_code, 500)
                self.assertEqual(response.json['status'], code)
                self.assertEqual(response.json['steps'], [])
                self.assertEqual(response.json['decision'], 'supported')
                self.assertNotIn('test-only', response.text)
                self.assertNotEqual(response.json['message'], code)


if __name__ == '__main__':
    unittest.main()
