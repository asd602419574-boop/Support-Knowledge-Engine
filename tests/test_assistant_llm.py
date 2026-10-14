from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener

from support_knowledge_engine.assistant import _validate_answer

from support_knowledge_engine.assistant_llm import (
    MAX_OUTPUT_TOKENS, MAX_RESPONSE_BYTES, ModelError, complete_chat, configuration_status,
)


class ModelHTTPTests(unittest.TestCase):
    def setUp(self):
        test = self
        self.requests = []
        self.mode = 'success'
        self.content = '{"steps":[]}'
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                test.requests.append((self.path, dict(self.headers), body))
                if test.mode == 'timeout':
                    time.sleep(.15)
                if test.mode == 'redirect':
                    self.send_response(302)
                    self.send_header('Location', '/credential-leak')
                    self.end_headers()
                    return
                if test.mode in ('failure', '401', '402', '429'):
                    self.send_response(401 if test.mode == 'failure' else int(test.mode))
                    self.end_headers()
                    return
                choice = {'finish_reason': 'stop', 'message': {'content': test.content}}
                if test.mode == 'tool':
                    choice['message']['tool_calls'] = [{'function': {'name': 'write_database'}}]
                if test.mode == 'truncated':
                    choice['finish_reason'] = 'length'
                if test.mode == 'non_stop':
                    choice['finish_reason'] = 'content_filter'
                data = json.dumps({'choices': [choice]}).encode()
                if test.mode == 'oversize':
                    data = b'x' * (MAX_RESPONSE_BYTES + 1)
                if test.mode == 'malformed':
                    data = b'not json'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.config = {'LLM_BASE_URL': f'http://127.0.0.1:{self.server.server_port}/v1',
                       'LLM_API_KEY': 'test-only-secret', 'LLM_MODEL': 'fixture-model'}
        self.payload = {'question': 'How to inspect?', 'product': 'Fictional',
                        'evidence': [{'evidence_id': 'ev1-test', 'excerpts': ['Inspect the connector.']}]}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_real_http_request_is_minimal_and_bounded(self):
        self.assertEqual(complete_chat(self.config, self.payload), '{"steps":[]}')
        path, headers, body = self.requests[0]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual(headers['Authorization'], 'Bearer test-only-secret')
        self.assertEqual(body['max_tokens'], MAX_OUTPUT_TOKENS)
        self.assertEqual(json.loads(body['messages'][1]['content']), self.payload)
        self.assertNotIn('tools', body)
        self.assertNotIn('thinking', body)
        self.assertNotIn('response_format', body)
        self.assertNotIn('test-only-secret', json.dumps(body))
        self.assertIn('untrusted DATA', body['messages'][0]['content'])
        complete_chat(dict(self.config, LLM_TOKEN_LIMIT_FIELD='max_completion_tokens'), self.payload)
        alternate = self.requests[-1][2]
        self.assertEqual(alternate['max_completion_tokens'], MAX_OUTPUT_TOKENS)
        self.assertNotIn('max_tokens', alternate)

    def test_case_prompt_and_transport_keep_two_sources_distinct(self):
        payload={**self.payload,'case_records':[{'record_id':'case-pending_actions-1',
            'field':'pending_actions','label':'待确认','quote':'计划更换，尚未执行。'}]}
        complete_chat(self.config,payload)
        body=self.requests[0][2]
        self.assertEqual(json.loads(body['messages'][1]['content']),payload)
        self.assertIn('UNTRUSTED business DATA',body['messages'][0]['content'])
        self.assertIn('case_assessment',body['messages'][0]['content'])
        self.assertIn('not verified device conditions',body['messages'][0]['content'])
        self.assertIn('explicitly use 不得 or 禁止',body['messages'][0]['content'])
        self.assertNotIn('tools',body)

    def test_timeout_and_network_failure_are_classified(self):
        self.mode = 'timeout'
        with patch('support_knowledge_engine.assistant_llm.REQUEST_TIMEOUT', .03):
            with self.assertRaises(ModelError) as ctx:
                complete_chat(self.config, self.payload)
        self.assertEqual(ctx.exception.code, 'model_timeout')
        self.mode = 'failure'
        with self.assertRaises(ModelError) as ctx:
            complete_chat(self.config, self.payload)
        self.assertEqual(ctx.exception.code, 'model_unauthorized')

    def deepseek_call(self, base_url='https://api.deepseek.com'):
        test = self
        local_opener = build_opener(ProxyHandler({}))
        class OfficialEndpointFixture:
            def open(self, request, timeout):
                test.assertEqual(request.full_url, base_url.rstrip('/') + '/chat/completions')
                request.full_url = test.config['LLM_BASE_URL'] + '/chat/completions'
                return local_opener.open(request, timeout=timeout)
        config = dict(self.config, LLM_BASE_URL=base_url, LLM_MODEL='deepseek-flash')
        with patch('support_knowledge_engine.assistant_llm.build_opener', return_value=OfficialEndpointFixture()):
            return complete_chat(config, self.payload)

    def test_deepseek_official_parameters_and_existing_evidence_validation(self):
        self.content = json.dumps({'items': [{'type': 'action', 'text': '请检查连接器。',
                                  'evidence_id': 'ev1-test', 'quote': 'Inspect the connector.'}],
                                  'coverage': 'partial', 'missing_information': ['symptoms']})
        for base_url in ('https://api.deepseek.com', 'https://api.deepseek.com/v1/'):
            content = self.deepseek_call(base_url)
            self.assertEqual(_validate_answer(content, self.payload['evidence'])['items'][0]['text'], '请检查连接器。')
            body = self.requests[-1][2]
            self.assertEqual(body['thinking'], {'type': 'disabled'})
            self.assertEqual(body['response_format'], {'type': 'json_object'})
            self.assertIn('JSON', body['messages'][0]['content'])
            self.assertEqual(body['max_tokens'], MAX_OUTPUT_TOKENS)

    def test_deepseek_json_mode_does_not_relax_rejection(self):
        for content in ('not JSON', json.dumps({'items': [{'type': 'action', 'text': '检查连接器。',
                            'evidence_id': 'fake', 'quote': 'Inspect the connector.'}],
                            'coverage': 'partial', 'missing_information': ['symptoms']}),
                        json.dumps({'items': [{'type': 'action', 'text': '使用未经证实的维修方法。',
                            'evidence_id': 'ev1-test', 'quote': 'Invented solution.'}],
                            'coverage': 'partial', 'missing_information': ['symptoms']})):
            self.content = content
            with self.subTest(content=content), self.assertRaises(ModelError) as ctx:
                _validate_answer(self.deepseek_call(), self.payload['evidence'])
            self.assertEqual(ctx.exception.code, 'invalid_answer')
        self.mode = 'truncated'
        with self.assertRaises(ModelError) as ctx:
            self.deepseek_call()
        self.assertEqual(ctx.exception.code, 'invalid_answer')

    def test_deepseek_model_name_does_not_enable_parameters_on_other_targets(self):
        for url in ('https://proxy.example/v1', 'https://api.deepseek.com.evil.example',
                    'https://api.deepseek.com:8443', 'https://api.deepseek.com/other'):
            with self.subTest(url=url):
                self.deepseek_call(url)
                body = self.requests[-1][2]
                self.assertNotIn('thinking', body)
                self.assertNotIn('response_format', body)

    def test_http_error_codes_are_safe_and_actionable(self):
        for status, expected in (('401', 'model_unauthorized'), ('402', 'model_payment_required'),
                                 ('429', 'model_rate_limited')):
            self.mode = status
            with self.subTest(status=status), self.assertRaises(ModelError) as ctx:
                self.deepseek_call()
            self.assertEqual(ctx.exception.code, expected)
            self.assertNotIn(self.config['LLM_API_KEY'], str(ctx.exception))

    def test_redirect_does_not_forward_credentials_and_invalid_responses_rejected(self):
        for mode in ('redirect', 'tool', 'truncated', 'non_stop', 'oversize', 'malformed'):
            self.mode = mode
            with self.subTest(mode=mode), self.assertRaises(ModelError) as ctx:
                complete_chat(self.config, self.payload)
            self.assertEqual(ctx.exception.code, 'model_unavailable' if mode == 'redirect' else 'invalid_answer')
            if mode in ('truncated', 'non_stop', 'malformed'):
                self.assertEqual(ctx.exception.validation_reason, {
                    'truncated': 'response_truncated', 'non_stop': 'finish_reason', 'malformed': 'response_json'}[mode])
        self.assertTrue(all(path == '/v1/chat/completions' for path, _, _ in self.requests))

    def test_configuration_does_not_claim_connection_and_requires_tls(self):
        self.assertTrue(configuration_status(self.config)['configured'])
        self.assertNotIn('test-only-secret', json.dumps(configuration_status(self.config)))
        for url in ('http://remote.example/v1', 'https://user:secret@example.com/v1',
                    'https://example.com/v1?key=secret'):
            self.assertFalse(configuration_status(dict(self.config, LLM_BASE_URL=url))['configured'])
        self.assertFalse(configuration_status(dict(self.config, LLM_API_KEY=''))['configured'])


if __name__ == '__main__':
    unittest.main()
