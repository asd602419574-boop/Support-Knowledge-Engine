"""Read-only, bounded NTF review. No model, persistence or generic rule engine."""
import hashlib
import hmac
import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request, session, url_for

from .assistant_case import validate_case_context
from .db import connect_database
from .evidence import decide_evidence, snapshot_integrity_ok
from .importer import calculate_sha256
from .normalization import normalize_query
from .repository import get_product
from .runtime import RuntimeCompositionError, RuntimeRequest, run_runtime

bp = Blueprint('pcn_review', __name__)
NOTICE = '仅审查记录层面匹配，不证明真实执行；不得用于员工定责、绩效扣罚或自动拦截。'
# Recognize only an explicit, complete obligation with all four scope conditions.
# Product and notice identity come from the current PDF, never a preselected PCN ID.
REQUIREMENT = re.compile(
    r'(?m)^For (?P<product>[^\n,]{1,80}) devices with internal storage,\s+'
    r'after NTF capture of test photos or videos and before returning the device,\s+'
    r'delete all test photos and videos from internal storage\.(?=\s|$)')
NTF = re.compile(r'NTF 检测(?:未复现故障|正常)[。.]?')
CAPTURE = re.compile(r'已在机身存储拍摄测试(?:图片|视频)(?:和测试(?:图片|视频))?[。.]?')
DONE = re.compile(r'NTF 测试拍摄后[、，,]\s*交还设备前[，,]\s*已删除机身存储中的全部测试图片和测试视频[。.]?')
OPPOSITE = re.compile(r'设备已交还[，,]\s*机身存储中的测试图片和测试视频未删除[。.]?')


def _result(status, *, decision=None, requirements=None, checks=None, records=None,
            message='', token=None, reasons=None):
    return jsonify(status=status, decision=decision, requirements=requirements or [],
                   checks=checks or [], case_records=records or [], message=message,
                   notice=NOTICE, model_called=False, confirmation_token=token,
                   reason_codes=reasons or [])


def _source_ready(row, snapshot):
    today = datetime.now(timezone(timedelta(hours=8))).date()
    source = urlsplit(row['source_url'] or '')
    if (snapshot.authority_level != 'authoritative' or snapshot.document_lifecycle != 'effective'
            or snapshot.document_id != row['id'] or row['document_type'] != 'PCN'
            or row['superseded_by_document_id'] is not None
            or not row['version'] or row['version'].casefold() in ('未知', 'unknown', '不详', '未确认', '待确认')
            or source.scheme not in ('http', 'https') or not source.hostname or source.username or source.password):
        return False
    try:
        if not row['effective_date'] or date.fromisoformat(row['effective_date']) > today:
            return False
        if not row['release_date'] or date.fromisoformat(row['release_date']) > today:
            return False
        if row['expiration_date'] and date.fromisoformat(row['expiration_date']) <= today:
            return False
        return calculate_sha256(row['file_path']) == snapshot.pdf_sha256
    except (ValueError, OSError):
        return False


def _requirements(db, packet, decision, product):
    approved = set(decision.evidence_ids)
    requirements = []
    for source in packet.evidence:
        if source.evidence_id not in approved:
            continue
        row = db.execute('SELECT * FROM documents WHERE id=?', (source.document_id,)).fetchone()
        if not row or not _source_ready(row, source):
            return []
        matches = list(REQUIREMENT.finditer(source.supporting_original_text))
        for match in matches:
            if match['product'] != product['standard_name'] or source.canonical_product_id != product['id']:
                return []
            quote = match.group(0)
            requirements.append({'name':'NTF 测试影像清理', 'node':'测试拍摄后、交还设备前',
                'conditions':'所选型号、机身存储、NTF 测试拍摄；交还设备前清理全部测试图片和视频。',
                'pcn_quote':quote, 'evidence_id':source.evidence_id, 'document_id':source.document_id,
                'filename':source.filename, 'page_number':source.page_number, 'pdf_sha256':source.pdf_sha256,
                'version':row['version'], 'synthetic':bool(re.search(r'fictional|虚构',source.supporting_original_text,re.I)),
                'pdf_url':url_for('main.document_file',document_id=source.document_id,sha256=source.pdf_sha256)+f'#page={source.page_number}'})
    return requirements[:4] if 0 < len(requirements) <= 4 else []


def _compare(requirement, records):
    observations = [r for r in records if r['field'] in ('detection_records','completed_actions')]
    ntf = [r for r in observations if NTF.fullmatch(r['quote'])]
    capture = [r for r in observations if CAPTURE.fullmatch(r['quote'])]
    completed = [r for r in observations if DONE.fullmatch(r['quote'])]
    opposite = [r for r in observations if OPPOSITE.fullmatch(r['quote'])]
    pending_opposite = [r for r in records if r['field'] == 'pending_actions' and OPPOSITE.fullmatch(r['quote'])]
    relevant = [r for r in records if r['field'] != 'category' and re.search(r'删除|清理|清空',r['quote'])]
    missing = []
    if not ntf: missing.append('缺少可核对的 NTF 检测场景记录。')
    if not capture: missing.append('缺少机身存储中测试拍摄图片／视频的记录。')
    state, message = '待确认', '未识别到执行记录，请人工确认；缺少记录不能证明实际执行情况。'
    # Completion must never be inferred from a plan, broad keyword, or unknown ordering.
    if missing:
        message = '适用场景条件不完整，暂不判断记录是否满足清理要求。'
    elif completed and opposite:
        state, message = '风险提醒', '案例记录互相矛盾，需核对执行时间及实际清理情况。'
    elif opposite:
        state, message = '明确冲突', '记录明确表示设备已交还且测试影像未删除，与已确认要求的执行节点直接相反；仅为记录层面冲突。'
    elif pending_opposite:
        state, message = '风险提醒', '反向描述仅在计划／完成状态未确认字段中，不能据此判定明确冲突；请人工核实执行状态。'
    elif completed:
        # A second ambiguous/partial cleaning record cannot silently be discarded.
        uncertain = [r for r in relevant if r not in completed and r['field'] != 'pending_actions']
        if uncertain:
            state, message = '风险提醒', '存在其他无法确认范围或顺序的清理记录，请人工核对。'
        else:
            state, message = '通过', '案例明确记录 NTF 测试拍摄后、交还前删除全部测试图片和视频，与已确认要求在记录层面匹配，不证明真实执行。'
    elif relevant:
        plans = all(r['field'] == 'pending_actions' and re.match(r'计划|建议|准备|拟',r['quote'])
                    and not re.search(r'已交还|已经交还',r['quote']) for r in relevant)
        if plans:
            message = '未识别到执行记录；只有计划或建议，请核对实际执行情况。'
        else:
            state, message = '风险提醒', '识别到尚未清理或范围／节点不完整的记录；尚不能确认直接冲突，需人工核实。'
    if state == '待确认' and not missing:
        missing.append('补充清理范围、交还节点及相应执行原文记录。')
    return {**requirement, 'state':state, 'message':message,
            'case_matches':list({r['record_id']:r for r in ntf+capture+relevant}.values()),
            'missing_information':missing, 'human_review':'请人工对照完整 PCN、适用条件和实际执行情况；'+NOTICE}


@bp.post('/api/assistant/pcn-review')
def review():
    token = request.headers.get('X-Assistant-CSRF','')
    origin = request.headers.get('Origin')
    if (not token or not token.isascii() or not hmac.compare_digest(token,session.get('assistant_csrf',''))
            or (origin and origin.rstrip('/') != request.host_url.rstrip('/'))
            or request.headers.get('Sec-Fetch-Site') == 'cross-site'):
        return _result('forbidden',message='请求校验失败，请刷新页面。'),403
    if request.content_length and request.content_length > 20000:
        return _result('invalid_request',message='审查输入超过长度限制。'),400
    body = request.get_json(silent=True)
    allowed = {'query','product_id','firmware_version','case_context','confirmation_token'}
    if not isinstance(body,dict) or not set(body) <= allowed:
        return _result('invalid_request',message='仅接受确认后的案例字段，禁止自定义义务、引文或状态。'),400
    query, product_id, firmware = body.get('query'),body.get('product_id'),body.get('firmware_version','')
    if (not isinstance(query,str) or not 1 <= len(query.strip()) <= 1000
            or not isinstance(product_id,str) or not product_id.isascii() or not product_id.isdigit()
            or len(product_id)>10 or not isinstance(firmware,str) or len(firmware)>80
            or ('confirmation_token' in body and (not isinstance(body['confirmation_token'],str)
                or not body['confirmation_token'].isascii() or len(body['confirmation_token'])>64))):
        return _result('invalid_request',message='请核对问题、产品、固件和确认参数。'),400
    try:
        records = validate_case_context(body.get('case_context'),query,firmware)
    except ValueError:
        return _result('invalid_request',message='请人工确认案例，移除敏感信息、指令及不合法字段。'),400
    try:
        with connect_database(current_app.config['DATABASE']) as db:
            product = get_product(db,int(product_id))
            if product is None:
                return _result('invalid_request',message='所选产品不存在。'),400
            terms = normalize_query(db,query).product_names
            search_query = ' '.join(dict.fromkeys((product['standard_name'],*terms,'NTF','internal storage')))
            result = run_runtime(db,RuntimeRequest('1',search_query,product_id=product_id,
                document_type='PCN',firmware_version=firmware.strip() or None),retrieval_deadline_s=3.0)
            if not result.ok or result.packet is None or result.decision is None:
                return _result('unavailable',records=records,message='PCN 检索失败，无法审查。')
            packet, decision = result.packet,result.decision
            if decision != decide_evidence(packet) or any(not snapshot_integrity_ok(e) for e in packet.evidence):
                return _result('unavailable',records=records,message='PCN 来源校验失败，无法审查。')
            if decision.decision_type != 'supported':
                return _result('unavailable',decision=decision.decision_type,records=records,
                    reasons=list(decision.reason_codes),message='待确认：没有本次可信适用的 PCN，不能建立正式标准依据。')
            requirements = _requirements(db,packet,decision,product)
    except (sqlite3.Error,RuntimeCompositionError):
        return _result('unavailable',records=records,message='PCN 检索服务失败，无法审查。'),503
    if not requirements:
        return _result('unavailable',decision='supported',records=records,
            message='待确认：PCN 版本、来源、有效期或本版可识别的完整要求未确认，无法建立检查项。')
    binding = json.dumps({'input':{k:v for k,v in body.items() if k!='confirmation_token'},
                          'requirements':requirements},ensure_ascii=False,sort_keys=True).encode('utf-8')
    confirmation = hmac.new(str(current_app.secret_key).encode('utf-8'),binding,hashlib.sha256).hexdigest()
    if 'confirmation_token' not in body:
        return _result('awaiting_confirmation',decision='supported',records=records,requirements=requirements,
            token=confirmation,message='已识别完整原文要求候选。请人工核对义务解释及适用条件，再确认比对。')
    if not hmac.compare_digest(body['confirmation_token'],confirmation):
        return _result('stale_confirmation',records=records,message='案例或 PCN 来源已变化，请重新匹配并确认。'),409
    return _result('reviewed',decision='supported',records=records,requirements=requirements,
        checks=[_compare(item,records) for item in requirements],message='已完成本次记录层面逐项审查，仍需人工核实。')
