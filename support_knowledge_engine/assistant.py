"""MVP assistant: one G4 runtime capture, one optional model call, no writes."""
from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import sqlite3

from flask import Blueprint, current_app, g, jsonify, render_template, request, session, url_for

from .assistant_llm import ModelError, complete_chat, configuration_status
from .assistant_case import validate_case_context, validate_case_assessment
from .db import connect_database
from .evidence import decide_evidence, snapshot_integrity_ok
from .repository import get_product, get_product_aliases, list_products
from .normalization import normalize_query
from .runtime import RuntimeCompositionError, RuntimeRequest, run_runtime


bp = Blueprint('assistant', __name__)
logger = logging.getLogger(__name__)
MESSAGES = {
    'answered': '已生成基于资料的 AI 中文解释。请结合原文适用条件核查；这不是现场故障确诊。',
    'abstain': '证据不足或适用性未确认，无法给出确定性排查结论。请核对型号、版本或补充有效资料。',
    'conflict': '资料存在版本冲突，无法确定应采用哪份排查依据。请先核对有效版本。',
    'not_configured': '模型未配置。',
    'model_timeout': '模型请求超时，未生成回答。请稍后手动重试。',
    'model_unavailable': '模型服务连接失败或拒绝请求，未生成回答。请检查服务地址、密钥和模型权限。',
    'model_unauthorized': '模型服务认证失败（401），未生成回答。请检查本机 API Key 和访问权限。',
    'model_payment_required': '模型服务余额不足（402），未生成回答。请检查服务账户余额。',
    'model_rate_limited': '模型服务请求限流（429），未生成回答。请稍后手动重试。',
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
MISSING_INFORMATION = {
    'product_model': '请确认设备铭牌上的完整产品型号。',
    'symptoms': '请补充实际故障现象、出现时机和完整故障码。',
    'firmware_version': '请补充设备当前固件版本，核对资料适用条件。',
    'applicable_document': '请补充适用于该型号、故障和版本的有效资料。',
    'inspection_procedure': '现有回答尚缺具体检查流程，不能据此执行拆机或维修；请补充适用的检查流程资料。',
}
VALIDATION_MESSAGES = {
    'response_limit': '模型响应超过长度限制。',
    'response_truncated': '模型输出被截断，未取得完整回答。',
    'response_unusable': '模型没有返回可用的普通文本回答。',
    'finish_reason': '模型响应未正常完成。',
    'response_json': '模型服务响应不是合法 JSON。',
    'response_schema': '模型服务响应结构或 JSON 无效。',
    'answer_json': '回答不是合法 JSON。',
    'answer_schema': '回答字段或结构不符合要求。',
    'answer_limits': '回答类型、条数、长度或基础信息代码不符合要求。',
    'evidence_invalid': 'Evidence ID 不属于本次允许证据。',
    'quote_mismatch': '引用的完整原文与本次允许摘录不一致。',
    'text_invalid': '中文释义格式或长度不符合要求。',
    'item_type': '模型内容分类与原文类别不一致；故障说明不能作为操作步骤。',
    'label_missing': '中文释义遗漏了原文中的故障码、型号或版本。',
    'condition_missing': '中文释义未保留原文的否定或适用条件。',
    'operation_added': '中文释义增加了原文没有支持的维修操作。',
    'damage_claim': '原文只要求检查，中文释义却宣称已经损坏。',
    'coverage_invalid': '回答内容与覆盖度不一致。',
    'case_schema': '案例核对输出字段不合法。',
    'case_reference': '案例引用不属于本次确认记录或未覆盖全部记录。',
    'case_status': '案例处理状态不一致。',
}


@bp.get('/assistant')
def page():
    if 'assistant_csrf' not in session:
        session['assistant_csrf'] = secrets.token_urlsafe(32)
    with connect_database(current_app.config['DATABASE']) as db:
        products = list_products(db)
        case_products = [{'id': str(p['id']), 'name': p['standard_name'],
                          'aliases': [a['alias_text'] for a in get_product_aliases(db, p['id'])
                                      if a['is_enabled']]} for p in products]
    return render_template('assistant.html', products=products,
                           case_products=case_products,
                           model_status=configuration_status(current_app.config),
                           llm_model=current_app.config['LLM_MODEL'],
                           csrf_token=session['assistant_csrf'])


def _response(status, decision=None, *, model_called=False, http_status=200, message=None,
              steps=None, citations=None, warnings=None, items=None, coverage='none', missing_information=None,
              case_assessment=None):
    records = getattr(g, 'assistant_case_records', [])
    case_fields = {'case_records': records, 'case_assessment': case_assessment or []} if records else {}
    return jsonify(status=status, decision=decision.decision_type if decision else None,
                   reason_codes=list(decision.reason_codes) if decision else [],
                   model_called=model_called, message=message or MESSAGES.get(status, status),
                   steps=steps or [], citations=citations or [], warnings=warnings or [],
                   items=items or [], coverage=coverage, missing_information=missing_information or [], **case_fields), http_status


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
        if _quote_kind(line) is None:
            continue
        if sum(map(len, lines)) + len(line) > 1800:
            break
        lines.append(line)
    return lines


def _quote_kind(quote):
    """Conservative lexical categories, not a semantic classifier or proof."""
    if not re.search(r'[.!?。！？；]$', quote) or quote.startswith((
            'Support topic:', 'Procedure marker:', 'Synthetic corpus', '支持主题：',
            '文档标题：', 'This document uses fictional', '本文件为虚构', '虚构测试')):
        return None
    if re.search(r'\b(do not|never|not compatible|not applicable|only|supported firmware range)\b', quote, re.I) or re.search(r'不得|不要|禁止|不适用|仅适用|仅限', quote):
        return 'caution'
    if re.match(r'^(check|inspect|verify|confirm|run|record|disconnect|connect|repeat|ensure|perform)\b', quote, re.I) or re.match(r'^(请)?(检查|确认|记录|执行|断开|连接|运行)', quote):
        return 'action'
    return 'fact'


def _translation_guard_reason(quote, text):
    """Catch obvious loss of labels/conditions; cannot establish semantic accuracy."""
    labels = re.findall(r'\b[A-Za-z]{2,8}\d{0,3}-\d{2,6}\b|AeroCam (?:Mini|Pro) \d+|\b\d+\.\d+(?:\.[\dx]+)?\b', quote)
    if any(label.casefold() not in text.casefold() for label in labels):
        return 'label_missing'
    rules = ((r'\b(do not|never|not compatible|not applicable)\b|不得|不要|禁止|不适用', r'不要|不得|不应|禁止|不适用|不兼容|不能'),
             (r'\bonly\b|仅适用|仅限', r'仅|只|限于'),
             (r'\bafter\b', r'后|之后'), (r'\bbefore\b', r'前|之前'),
             (r'\b(if|when)\b', r'如果|若|当|时'))
    if any(re.search(source, quote, re.I) and not re.search(target, text) for source, target in rules):
        return 'condition_missing'
    # Reject obvious added repair operations even if mislabeled as a fact.
    # Allow explicit denials, but do not infer permission from a translated sentence.
    operations = (('拆机', r'disassembl|拆机'), ('刷机', r'flash|刷机'),
                  ('更换', r'replac|更换'), ('重置', r'reset|重置'),
                  ('校准', r'calibrat|校准'), ('维修', r'repair|维修'))
    positive = re.sub(r'(不要|不得|不能|不应|不建议|禁止)[^。；，]{0,24}(拆机|刷机|更换|重置|校准|维修)', '', text)
    if any(word in positive and not re.search(source, quote, re.I) for word, source in operations):
        return 'operation_added'
    if re.search(r'needs? inspection|需要检查', quote, re.I):
        # Positive damage claims are unsupported; allow explicit denial of diagnosis.
        cleaned = re.sub(r'(不能|不可|无法|尚不能|并非|不是|未|不代表|不意味着|不等于)(据此)?(认定|确定|确诊)?[^。；，]{0,24}?(损坏|故障)', '', text)
        if re.search(r'已经?[^。；]{0,6}损坏|确定[^。；]{0,6}损坏|必须更换', cleaned):
            return 'damage_claim'
    return None


def _basic_translation_guards(quote, text):
    return _translation_guard_reason(quote, text) is None


def _validate_answer(content, sources, case_records=None):
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        raise ModelError('invalid_answer', 'answer_json') from None
    expected = {'items', 'coverage', 'missing_information'} | ({'case_assessment'} if case_records else set())
    if not isinstance(value, dict) or set(value) != expected:
        raise ModelError('invalid_answer', 'answer_schema')
    assessment = validate_case_assessment(value['case_assessment'], case_records) if case_records else []
    entries, coverage, missing = value['items'], value['coverage'], value['missing_information']
    if (not isinstance(entries, list) or len(entries) > 6 or not isinstance(coverage, str)
            or coverage not in ('sufficient', 'partial', 'none') or not isinstance(missing, list)
            or len(missing) > 5 or any(not isinstance(code, str) or code not in MISSING_INFORMATION for code in missing)
            or len(set(missing)) != len(missing)):
        raise ModelError('invalid_answer', 'answer_limits')
    allowed = {source['evidence_id']: source['excerpts'] for source in sources}
    validated = []
    for step in entries:
        if not isinstance(step, dict) or set(step) != {'type', 'text', 'evidence_id', 'quote'}:
            raise ModelError('invalid_answer', 'answer_schema')
        evidence_id, quote, text, kind = step['evidence_id'], step['quote'], step['text'], step['type']
        if not isinstance(evidence_id, str) or evidence_id not in allowed:
            raise ModelError('invalid_answer', 'evidence_invalid')
        if not isinstance(quote, str) or quote not in allowed[evidence_id]:
            raise ModelError('invalid_answer', 'quote_mismatch')
        if (not isinstance(text, str) or not 1 <= len(text.strip()) <= 500
                or not re.search(r'[\u4e00-\u9fff]', text) or re.search(r'[<>]|https?://|www\.', text, re.I)
                ):
            raise ModelError('invalid_answer', 'text_invalid')
        if kind not in ('fact', 'action', 'caution'):
            raise ModelError('invalid_answer', 'answer_schema')
        if kind != _quote_kind(quote):
            raise ModelError('invalid_answer', 'item_type')
        reason = _translation_guard_reason(quote, text)
        if reason:
            raise ModelError('invalid_answer', reason)
        validated.append({'type': kind, 'text': text.strip(), 'quote': quote, 'evidence_id': evidence_id})
    if not validated:
        if coverage != 'none':
            raise ModelError('invalid_answer', 'coverage_invalid')
    elif coverage == 'none':
        raise ModelError('invalid_answer', 'coverage_invalid')
    elif not any(item['type'] == 'action' for item in validated):
        coverage = 'partial'
        if 'inspection_procedure' not in missing:
            missing = list(dict.fromkeys((*missing[:4], 'inspection_procedure')))
    if coverage == 'partial' and not missing:
        missing = ['applicable_document']
    if validated and missing:
        coverage = 'partial'
    return {'items': validated, 'coverage': coverage, 'missing_information': missing, 'case_assessment': assessment}


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
    if request.content_length and request.content_length > 20000:
        return _response('invalid_request', http_status=400, message='本次问答内容超过大小限制。')
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not set(body) <= {'query', 'product_id', 'firmware_version', 'case_context'}:
        return _response('invalid_request', http_status=400, message='请发送 JSON 问题。')
    query, product_id, firmware = (body.get('query'), body.get('product_id'), body.get('firmware_version', ''))
    if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 1000
            or not isinstance(product_id, str) or not product_id.isascii() or not product_id.isdigit()
            or len(product_id) > 10 or not isinstance(firmware, str) or len(firmware) > 80):
        return _response('invalid_request', http_status=400, message='请输入问题（最多 1000 字）、选择产品并核对固件版本。')
    case_records = []
    if 'case_context' in body:
        try:
            case_records = validate_case_context(body['case_context'], query, firmware)
        except ValueError:
            return _response('invalid_request', http_status=400,
                             message='案例上下文不合法：请确认发送同意，核对字段、长度、脱敏及已执行／计划状态；移除指令样式文本。')
        g.assistant_case_records = case_records
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
    if case_records:
        payload['case_records'] = case_records
        for source in sources:
            source['excerpt_types'] = [_quote_kind(quote) for quote in source['excerpts']]
    content = None
    try:
        content = complete_chat(current_app.config, payload)
        answer = _validate_answer(content, sources, case_records)
    except ModelError as exc:
        reason = exc.validation_reason if exc.validation_reason in VALIDATION_MESSAGES else None
        if exc.code == 'invalid_answer':
            # Fixed category and length only. Never log prompt, query, response or key.
            logger.warning('assistant_validation_failed category=%s content_length=%s',
                           reason or 'unknown', len(content) if isinstance(content, str) else 'unavailable')
        return _response(exc.code, decision, model_called=True,
                         http_status=504 if exc.code == 'model_timeout' else 502, warnings=warnings)
    items = answer['items']
    missing = [MISSING_INFORMATION[code] for code in answer['missing_information']]
    if not items:
        return _response('model_abstain', decision, model_called=True, warnings=warnings, missing_information=missing)
    # All provenance and links are produced locally, never supplied by the model.
    citations = []
    for evidence_id in dict.fromkeys(item['evidence_id'] for item in items):
        e = snapshots[evidence_id]
        citations.append({'evidence_id': evidence_id, 'document_id': e.document_id,
                          'filename': e.filename, 'page_number': e.page_number,
                          'original_text': e.supporting_original_text,
                          'pdf_sha256': e.pdf_sha256,
                          'pdf_url': url_for('main.document_file', document_id=e.document_id,
                                             sha256=e.pdf_sha256) + f'#page={e.page_number}',
                          'document_url': url_for('main.document_detail', document_id=e.document_id)})
    return _response('answered', decision, model_called=True, items=items, case_assessment=answer['case_assessment'],
                     steps=[item for item in items if item['type'] == 'action'],
                     coverage=answer['coverage'], missing_information=missing, citations=citations,
                     warnings=warnings + ['中文内容是 AI 基于资料的释义，不是原始手册文字或现场确诊。来源与原文匹配校验不能证明中文语义完全正确，请展开原文对照。'])
