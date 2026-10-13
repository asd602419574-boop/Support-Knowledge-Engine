from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from support_knowledge_engine.assistant_llm import (
    MAX_OUTPUT_TOKENS, MAX_RESPONSE_BYTES, ModelError, complete_chat, configuration_status,
)


class ModelHTTPTests(unittest.TestCase):
    def setUp(self):
        test = self
        self.requests = []
        self.mode = 'success'
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
                if test.mode == 'failure':
                    self.send_response(401)
                    self.end_headers()
                    return
                choice = {'finish_reason': 'stop', 'message': {'content': '{"steps":[]}'}}
                if test.mode == 'tool':
                    choice['message']['tool_calls'] = [{'function': {'name': 'write_database'}}]
                if test.mode == 'truncated':
                    choice['finish_reason'] = 'length'
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
        self.assertNotIn('test-only-secret', json.dumps(body))
        self.assertIn('untrusted DATA', body['messages'][0]['content'])
        complete_chat(dict(self.config, LLM_TOKEN_LIMIT_FIELD='max_completion_tokens'), self.payload)
        alternate = self.requests[-1][2]
        self.assertEqual(alternate['max_completion_tokens'], MAX_OUTPUT_TOKENS)
        self.assertNotIn('max_tokens', alternate)

    def test_timeout_and_network_failure_are_classified(self):
        self.mode = 'timeout'
        with patch('support_knowledge_engine.assistant_llm.REQUEST_TIMEOUT', .03):
            with self.assertRaises(ModelError) as ctx:
                complete_chat(self.config, self.payload)
        self.assertEqual(ctx.exception.code, 'model_timeout')
        self.mode = 'failure'
        with self.assertRaises(ModelError) as ctx:
            complete_chat(self.config, self.payload)
        self.assertEqual(ctx.exception.code, 'model_unavailable')

    def test_redirect_does_not_forward_credentials_and_invalid_responses_rejected(self):
        for mode in ('redirect', 'tool', 'truncated', 'oversize', 'malformed'):
            self.mode = mode
            with self.subTest(mode=mode), self.assertRaises(ModelError) as ctx:
                complete_chat(self.config, self.payload)
            self.assertEqual(ctx.exception.code, 'model_unavailable' if mode == 'redirect' else 'invalid_answer')
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
