"""MVP assistant: one G4 runtime capture, one optional model call, no writes."""
from __future__ import annotations

import hmac
import json
import re
import secrets
import sqlite3

from flask import Blueprint, current_app, jsonify, render_template, request, session, url_for

from .assistant_llm import ModelError, complete_chat, configuration_status
from .db import connect_database
from .evidence import decide_evidence, snapshot_integrity_ok
from .repository import get_product, list_products
from .normalization import normalize_query
from .runtime import RuntimeCompositionError, RuntimeRequest, run_runtime


bp = Blueprint('assistant', __name__)
MESSAGES = {
    'answered': '已根据可信 PDF 证据生成排查清单。请结合原文的适用条件核查；这不是现场故障确诊。',
    'abstain': '证据不足或适用性未确认，无法给出确定性排查结论。请核对型号、版本或补充有效资料。',
    'conflict': '资料存在版本冲突，无法确定应采用哪份排查依据。请先核对有效版本。',
    'not_configured': '模型未配置。',
    'model_timeout': '模型请求超时，未生成回答。请稍后手动重试。',
    'model_unavailable': '模型服务连接失败或拒绝请求，未生成回答。请检查服务地址、密钥和模型权限。',
    'invalid_answer': '模型输出未通过证据校验，已拒绝展示排查建议。',
    'model_abstain': '模型未找到能够回答问题的证据语句，无法给出确定性结论。',
    'retrieval_failed': '知识检索服务失败，未生成回答。',
}
REASONS = {
    'firmware_unknown': '固件适用性未知：现有规则不解析非空固件范围，输入版本不会将未知升级为适用。',
    'firmware_not_applicable': '证据不适用于所选固件。',
    'product_filter_conflict': '问题中的产品与所选产品不一致。',
    'evidence_product_mismatch': '资料产品与问题识别的产品不一致。',
    'version_conflict': '资料版本冲突。',
    'outdated_document': '仅有历史或已替代资料。',
    'possible_match': '只有可能匹配的资料，可信度不足。',
    'insufficient_evidence': '没有足够相关证据。',
    'ambiguous_product': '产品识别存在歧义。',
    'ambiguous_product_alias': '产品别名存在冲突。',
}


@bp.get('/assistant')
def page():
    if 'assistant_csrf' not in session:
        session['assistant_csrf'] = secrets.token_urlsafe(32)
    with connect_database(current_app.config['DATABASE']) as db:
        products = list_products(db)
    return render_template('assistant.html', products=products,
                           model_status=configuration_status(current_app.config),
                           llm_model=current_app.config['LLM_MODEL'],
                           csrf_token=session['assistant_csrf'])


def _response(status, decision=None, *, model_called=False, http_status=200, message=None,
              steps=None, citations=None, warnings=None):
    return jsonify(status=status, decision=decision.decision_type if decision else None,
                   reason_codes=list(decision.reason_codes) if decision else [],
                   model_called=model_called, message=message or MESSAGES.get(status, status),
                   steps=steps or [], citations=citations or [], warnings=warnings or []), http_status


def _excerpts(snapshot):
    # Keep complete PDF text lines so a quote cannot drop a negation/condition.
    # Exclude metadata and very long lines; never send a whole PDF/page blob.
    lines = []
    for raw in snapshot.supporting_original_text.splitlines():
        line = raw.strip()
        if not 15 <= len(line) <= 700 or ': https://' in line or ': http://' in line:
            continue
        if line.startswith(('Document Title:', 'Product Series:', 'Product Model:',
                            'Document Type:', 'Language:', 'Version:', 'Release Date:',
                            'Source URL:', '文档标题：', '产品系列：', '产品型号：', '原始来源网址：')):
            continue
        if sum(map(len, lines)) + len(line) > 1800:
            break
        lines.append(line)
    return lines


def _validate_answer(content, sources):
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        raise ModelError('invalid_answer') from None
    if not isinstance(value, dict) or set(value) != {'steps'}:
        raise ModelError('invalid_answer')
    steps = value['steps']
    if not isinstance(steps, list) or len(steps) > 6:
        raise ModelError('invalid_answer')
    allowed = {source['evidence_id']: source['excerpts'] for source in sources}
    validated = []
    for step in steps:
        if not isinstance(step, dict) or set(step) != {'evidence_id', 'quote'}:
            raise ModelError('invalid_answer')
        evidence_id, quote = step['evidence_id'], step['quote']
        if (not isinstance(evidence_id, str) or not isinstance(quote, str)
                or evidence_id not in allowed or quote not in allowed[evidence_id]):
            raise ModelError('invalid_answer')
        validated.append({'text': quote, 'evidence_id': evidence_id})
    return validated


def _runtime_query(db, question):
    """Prepare fault-code questions without changing the existing retrieval engine.

    Keep every recognized product term, including conflicting aliases. G3 still
    decides the prepared request. The original question is kept for the model.
    Questions without explicit fault codes use the original input unchanged.
    """
    codes = re.findall(r'(?<![A-Za-z0-9])[A-Za-z]{2,8}\d{0,3}-\d{2,6}(?![A-Za-z0-9])', question)
    if not codes:
        return question
    normalized = normalize_query(db, question)
    return ' '.join(dict.fromkeys((*normalized.product_terms, *codes)))


@bp.post('/api/assistant/ask')
def ask():
    token = request.headers.get('X-Assistant-CSRF', '')
    origin = request.headers.get('Origin')
    if (not token or not token.isascii() or not hmac.compare_digest(token, session.get('assistant_csrf', ''))
            or (origin and origin.rstrip('/') != request.host_url.rstrip('/'))
            or request.headers.get('Sec-Fetch-Site') == 'cross-site'):
        return _response('forbidden', http_status=403, message='请求校验失败，请刷新助手页面后重试。')
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return _response('invalid_request', http_status=400, message='请发送 JSON 问题。')
    query, product_id, firmware = (body.get('query'), body.get('product_id'), body.get('firmware_version', ''))
    if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000
            or not isinstance(product_id, str) or not product_id.isascii() or not product_id.isdigit()
            or len(product_id) > 10 or not isinstance(firmware, str) or len(firmware) > 80):
        return _response('invalid_request', http_status=400, message='请输入问题（最多 1000 字）、选择产品并核对固件版本。')
    try:
        with connect_database(current_app.config['DATABASE']) as db:
            product = get_product(db, int(product_id))
            if product is None:
                return _response('invalid_request', http_status=400, message='所选产品不存在。')
            retrieval_query = _runtime_query(db, query.strip())
            result = run_runtime(db, RuntimeRequest('1', retrieval_query, product_id=product_id,
                                 firmware_version=firmware.strip() or None), retrieval_deadline_s=3.0)
    except (sqlite3.Error, RuntimeCompositionError):
        return _response('retrieval_failed', http_status=503)
    if not result.ok or result.packet is None or result.decision is None:
        return _response('retrieval_failed', http_status=503)
    packet, decision = result.packet, result.decision
    if decision != decide_evidence(packet) or any(not snapshot_integrity_ok(e) for e in packet.evidence):
        return _response('retrieval_failed', http_status=503)
    warnings = [REASONS.get(code, '证据治理限制：' + code) for code in decision.reason_codes]
    if retrieval_query != query.strip():
        warnings.append('已从问题提取故障码，并保留全部已识别产品；本次检索输入：' + retrieval_query)
    if decision.decision_type != 'supported':
        return _response(decision.decision_type, decision, warnings=warnings)
    config = configuration_status(current_app.config)
    if not config['configured']:
        return _response('not_configured', decision, http_status=503, message=config['message'], warnings=warnings)
    approved = set(decision.evidence_ids)
    snapshots = {e.evidence_id: e for e in packet.evidence if e.evidence_id in approved}
    sources = [{'evidence_id': e.evidence_id, 'excerpts': _excerpts(e)}
               for e in list(snapshots.values())[:4]]
    sources = [source for source in sources if source['excerpts']]
    if not sources:
        return _response('model_abstain', decision, warnings=warnings)
    payload = {'question': query.strip(), 'product': product['standard_name'],
               'firmware_version': firmware.strip() or None, 'evidence': sources}
    try:
        content = complete_chat(current_app.config, payload)
        steps = _validate_answer(content, sources)
    except ModelError as exc:
        return _response(exc.code, decision, model_called=True,
                         http_status=504 if exc.code == 'model_timeout' else 502, warnings=warnings)
    if not steps:
        return _response('model_abstain', decision, model_called=True, warnings=warnings)
    # All provenance and links are produced locally, never supplied by the model.
    citations = []
    for evidence_id in dict.fromkeys(step['evidence_id'] for step in steps):
        e = snapshots[evidence_id]
        citations.append({'evidence_id': evidence_id, 'document_id': e.document_id,
                          'filename': e.filename, 'page_number': e.page_number,
                          'original_text': e.supporting_original_text,
                          'pdf_sha256': e.pdf_sha256,
                          'pdf_url': url_for('main.document_file', document_id=e.document_id,
                                             sha256=e.pdf_sha256) + f'#page={e.page_number}',
                          'document_url': url_for('main.document_detail', document_id=e.document_id)})
    return _response('answered', decision, model_called=True, steps=steps, citations=citations,
                     warnings=warnings + ['首版使用原文排查清单，保留原文语言，不自由改写关键事实。'])
