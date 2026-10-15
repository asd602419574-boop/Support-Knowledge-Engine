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

# Hand-written test translations, never a production/model fallback.
TRANSLATIONS = {
    'Error code AM2-17 indicates that the gimbal home sensor needs inspection.':
        ('fact', 'AM2-17 表示需要检查云台归位传感器；不能据此认定该部件已经损坏。'),
    'Supported firmware range: 2.4.0 through 2.9.x.':
        ('caution', '资料列出的支持固件范围是 2.4.0 至 2.9.x，需核对设备实际版本和适用资料。'),
    'Do not apply procedures written for AeroCam Pro 2.':
        ('caution', '不得套用为 AeroCam Pro 2 编写的操作流程。'),
    'Run propeller calibration after replacing a motor or vibration damper.':
        ('action', '更换电机或减振器后，按文档要求进行螺旋桨校准。'),
    'Inspect the battery latch before enabling coastal wind compensation.':
        ('action', '在启用沿海风补偿功能前，检查电池锁扣。'),
    'Record the calibration result together with this page number.':
        ('action', '记录校准结果，并同时记录本页页码。'),
    'Error code AP2-90 indicates a dual-camera thermal alignment failure.':
        ('fact', 'AP2-90 表示双摄像头热对准发生故障。'),
    'Inspect the payload bay connector and repeat optical axis verification.':
        ('action', '检查载荷舱连接器，并重复进行光轴核验；资料未提供此处操作的详细流程。'),
    'Mini-series calibration instructions are not compatible with this model.':
        ('caution', 'Mini 系列的校准说明不兼容本型号，不应套用。'),
    'This procedure applies only to AeroCam Mini 3; do not substitute a similar model.':
        ('caution', '本流程仅适用于 AeroCam Mini 3，不得用相似型号替代。'),
}


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
            items = []
            for source in payload['evidence']:
                for quote in source['excerpts']:
                    if quote in TRANSLATIONS:
                        kind, text = TRANSLATIONS[quote]
                        items.append({'type': kind, 'text': text, 'quote': quote,
                                      'evidence_id': source['evidence_id']})
            answer = {'items': items[:6], 'coverage': 'partial' if items else 'none',
                      'missing_information': ['inspection_procedure', 'symptoms']}
            if mode == 'injection':
                answer['items'][0]['text'] = '<script>window.fixtureInjected=true</script>'
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
