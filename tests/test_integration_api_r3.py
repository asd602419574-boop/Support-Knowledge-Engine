import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from support_knowledge_engine import create_app
from scripts.seed_r3_fixture import seed, PRODUCT


@pytest.fixture
def client():
    with tempfile.TemporaryDirectory() as tmp:
        report = seed(tmp)
        app = create_app({'TESTING': True, 'DATABASE': report['database'],
                          'INTEGRATION_TOKEN': 'fictional-test-token', 'INTEGRATION_SITE_ID': 'site-A'})
        yield app.test_client()


def ask(client, query, **extra):
    return client.post('/api/integration/evidence/query', json=dict(
        contract_version='1', site_id='site-A', query=query, product_alias=PRODUCT, **extra),
        headers={'Authorization': 'Bearer fictional-test-token'})


def test_auth_scope_and_validation(client):
    assert client.post('/api/integration/evidence/test').status_code == 401
    assert ask(client, '星环校准', max_results=100).status_code == 400
    assert client.post('/api/integration/evidence/query', json={'contract_version':'1', 'site_id':'site-B'},
                       headers={'Authorization':'Bearer fictional-test-token'}).status_code == 400


def test_real_pdf_runtime_supported_and_limits(client):
    out = ask(client, '星环校准', document_type='SOP').get_json()
    assert out['status'] == 'ok' and out['decision'] == 'supported'
    item = out['items'][0]
    assert item['page_number'] == 2 and item['version'] == '2.0'
    assert '仅记录蓝色诊断灯闪烁次数' in item['original_excerpt']
    assert len(item['pdf_sha256']) == 64 and not item['official_authorization']
    unknown = ask(client, '不存在的晶格故障', document_type='SOP').get_json()
    assert unknown['status'] == 'no_match' and unknown['decision'] == 'abstain'


def test_conflict_expired_and_runtime_error_stay_distinct(client):
    out = ask(client, '共振标记', document_type='SOP').get_json()
    assert out['status'] == 'conflict' and out['decision'] == 'conflict'
    assert {e['version'] for e in out['items']} == {'1.0', '2.0'}
    out = ask(client, '过期虚构测试资料', document_type='PCN').get_json()
    assert out['status'] == 'abstain' and 'document_expired' in out['reason_code']
    from support_knowledge_engine.runtime import RuntimeResult, RuntimeFailure
    with patch('support_knowledge_engine.integration.run_runtime', return_value=RuntimeResult(
            False, '1', '1', None, None, None, RuntimeFailure('retrieval_timeout', 'timeout'))):
        response = ask(client, '星环校准')
    assert response.status_code == 503
    assert response.get_json()['reason_code'] == 'retrieval_timeout'
