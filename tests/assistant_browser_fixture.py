"""Optional browser test harness. This is a HTTP fixture, NOT a real LLM.

Run from repository root: python -m tests.assistant_browser_fixture
It uses its own ignored database and binds Flask to 127.0.0.1:5052.
Write success/failure/timeout/injection to instance/assistant-fixture-mode.txt
to exercise transport and safe browser rendering. No production fallback exists.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from support_knowledge_engine import create_app
from support_knowledge_engine.demo_data import seed_demo_data
from tests.helpers import PROJECT_ROOT, SAMPLE_DIR


def main():
    database = PROJECT_ROOT / 'instance' / 'assistant-browser-fixture.db'
    mode_file = PROJECT_ROOT / 'instance' / 'assistant-fixture-mode.txt'
    seed_demo_data(database, SAMPLE_DIR)
    mode_file.write_text('success', encoding='utf-8')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            payload = json.loads(body['messages'][1]['content'])
            mode = mode_file.read_text(encoding='utf-8').strip()
            if mode == 'failure':
                self.send_response(503)
                self.end_headers()
                return
            if mode == 'timeout':
                time.sleep(22)
            source = payload['evidence'][0]
            quote = next((line for line in source['excerpts'] if 'AM2-17' in line), source['excerpts'][0])
            answer = {'steps': [{'evidence_id': source['evidence_id'], 'quote': quote}]}
            if mode == 'injection':
                answer['steps'][0]['quote'] = '<script>window.fixtureInjected=true</script>'
            data = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {
                'content': json.dumps(answer)}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    app = create_app({'DATABASE': str(database),
                      'LLM_BASE_URL': f'http://127.0.0.1:{server.server_port}/v1',
                      'LLM_API_KEY': 'test-fixture-not-real-credential',
                      'LLM_MODEL': 'TEST-FIXTURE-NOT-A-REAL-MODEL'})
    print('TEST FIXTURE ONLY: this server does not call a real model.', flush=True)
    try:
        app.run(host='127.0.0.1', port=5052, debug=False)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
