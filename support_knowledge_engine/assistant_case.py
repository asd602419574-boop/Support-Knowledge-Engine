"""Ephemeral user-confirmed observations, distinct from trusted PDF evidence."""
import json
import re
import unicodedata

from .assistant_llm import ModelError

CASE_FIELDS = {'category': 120, 'detection_records': 1200,
               'completed_actions': 1200, 'pending_actions': 1200}
CASE_LABELS = {'category': '故障分类', 'detection_records': '案例已记录的检测结果',
               'completed_actions': '案例所述已执行处理', 'pending_actions': '计划／建议／状态未确认'}
PLANNED = r'计划|建议|拟|待执行|待检查|待更换|尚未|未执行|未完成|未更换|未检查|未校准|准备|将要'


def unsafe_case_text(text):
    text = unicodedata.normalize('NFKC', text)
    if re.search(r'(?:\+?86[-\s]*)?1[3-9](?:[-\s]*\d){9}(?!\d)|[\w.+-]+@[\w.-]+\.[a-z]{2,}|\b(?:CASE|CAS|WO)[-_][A-Z0-9][A-Z0-9_-]{3,}', text, re.I):
        return True
    labels = r'(?:SN|序列号|工单号|案例号|案例编号|工单编号|工单标识|客户姓名|客户身份|客户|姓名|操作人|操作人员|地址|客户地址|电话|手机号|邮箱)'
    for value in re.findall(r'(?:^|[\n,，;； ])' + labels + r'[ \t]*(?:[:：=][ \t]*|[ \t]+)([^\n,，;；]+)', text, re.I):
        if not re.fullmatch(r'已脱敏|已移除|未提供|无|[＊*Xx_-]+', value.strip()):
            return True
    return bool(re.search(r'[<>]|https?://|www\.|(?:忽略|覆盖|绕过).{0,16}(?:指令|规则|验证|系统)|系统提示词|system\s*prompt|(?:system|assistant|role)\s*[:：]|tool_calls|function_call|执行命令|调用工具|泄露.*密钥', text, re.I))


def validate_case_context(value, query='', firmware=''):
    if (not isinstance(value, dict) or not set(value) <= {*CASE_FIELDS, 'consent'}
            or value.get('consent') is not True):
        raise ValueError('case_schema')
    context = {}
    for field, limit in CASE_FIELDS.items():
        text = value.get(field, '')
        if not isinstance(text, str) or len(text) > limit:
            raise ValueError('case_limits')
        context[field] = text.strip()
    if (not any(context.values()) or sum(map(len, context.values())) > 3000
            or len(json.dumps(context, ensure_ascii=False).encode('utf-8')) > 9000):
        raise ValueError('case_limits')
    if unsafe_case_text('\n'.join((*context.values(), query, firmware))):
        raise ValueError('case_privacy_or_instruction')
    records = []
    for field, text in context.items():
        for index, line in enumerate(text.splitlines(), 1):
            quote = line.strip()
            if not quote:
                continue
            if len(quote) > 400:
                raise ValueError('case_limits')
            if field in ('detection_records', 'completed_actions') and re.search(PLANNED, quote):
                raise ValueError('case_status')
            records.append({'record_id': f'case-{field}-{index}', 'field': field,
                            'label': CASE_LABELS[field], 'quote': quote})
    if len(records) > 12:
        raise ValueError('case_limits')
    return records


def validate_case_assessment(entries, records):
    if not isinstance(entries, list) or len(entries) > len(records):
        raise ModelError('invalid_answer', 'case_schema')
    allowed = {r['record_id']: r for r in records}
    seen, result = set(), []
    for item in entries:
        if not isinstance(item, dict) or set(item) != {'record_id', 'status'}:
            raise ModelError('invalid_answer', 'case_schema')
        record_id, status = item['record_id'], item['status']
        if not isinstance(record_id, str) or record_id not in allowed or record_id in seen:
            raise ModelError('invalid_answer', 'case_reference')
        record = allowed[record_id]
        expected = 'pending' if record['field'] == 'pending_actions' else 'recorded'
        if not isinstance(status, str) or status not in (expected, 'needs_review'):
            raise ModelError('invalid_answer', 'case_status')
        seen.add(record_id)
        if status == 'needs_review':
            hint = '模型标记需核对：请对照实际案例记录与本次 PDF 适用条件；案例不能覆盖手册。'
        elif status == 'pending':
            hint = '案例记录为计划、建议或完成状态未确认，不代表已经执行，也不代表手册要求执行。'
        elif record['field'] in ('completed_actions', 'detection_records'):
            hint = '案例记录显示已有检测结果或已执行该项目，请先核对实际记录，避免机械重复；不能据此排除全部问题或认定设备正常。'
        else:
            hint = '这是案例中填写的分类，不是 PDF 证据或现场确诊。'
        result.append({**record, 'status': status, 'hint': hint})
    if seen != set(allowed):
        raise ModelError('invalid_answer', 'case_reference')
    return result
