"""Build a NEW fictional corpus through the real PDF importer and governance.

Refuses an existing DB. No vendor/customer material is read.
"""
import argparse
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from support_knowledge_engine.db import init_database, connect_database
from support_knowledge_engine.importer import import_directory
from support_knowledge_engine.governance import create_product, update_document

PRODUCT = 'Aurora R3 A11'
SPECS = [
    ('sop-v1', 'SOP', '1.0', 'superseded', '', '共振标记：先记录指示灯颜色，再核实电池触点。旧版仅供追溯。'),
    ('sop-v2', 'SOP', '2.0', 'effective', '', '共振标记：先记录指示灯颜色，再核实电池触点。\n星环校准：仅记录蓝色诊断灯闪烁次数，交由人工核实。'),
    ('pcn-v1', 'PCN', '1.0', 'superseded', '2020-01-01', '紫光巡检：保留测试记录，等待人工确认。过期虚构测试资料。'),
    ('pcn-v2', 'PCN', '2.0', 'effective', '', '紫光巡检：记录测试条件。示例 PCN 不构成厂商正式规定。'),
]


def seed(root, *, empty=False):
    root = Path(root).resolve()
    db = root / 'knowledge.sqlite3'
    if db.exists():
        raise ValueError('Refusing existing database')
    pdfs = root / 'pdfs'
    pdfs.mkdir(parents=True, exist_ok=True)
    init_database(db)
    if empty:
        return {'database': str(db), 'empty': True}
    pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
    for name, kind, version, status, expiry, body in SPECS:
        path = pdfs / (name + '.pdf')
        c = canvas.Canvas(str(path), invariant=1)
        c.setTitle('Fictional Aurora ' + kind + ' ' + version)
        lines = [f'文档标题：虚构 Aurora {kind} v{version}', '产品系列：Aurora R3',
                 f'产品型号：{PRODUCT}', f'文档类型：{kind}', '语言：zh-CN',
                 f'版本号：{version}', '发布日期：2026-01-01',
                 f'原始来源网址：https://example.invalid/fictional/{name}',
                 '完全虚构测试资料，不代表任何厂商授权或服务承诺。']
        for page in (lines, ['虚构资料 / Fictional test only'] + body.splitlines()):
            c.setFont('STSong-Light', 13)
            for i, line in enumerate(page):
                c.drawString(40, 790 - 27 * i, line)
            c.showPage()
        c.save()
    with connect_database(db) as conn:
        product_id = create_product(conn, {'standard_name': PRODUCT, 'product_series': 'Aurora R3', 'status': 'active'},
                                    'R3 fictional fixture', 'fixture')
    summary = import_directory(pdfs, db)
    if summary.imported != 4 or summary.failed:
        raise ValueError('PDF import failed')
    with connect_database(db) as conn:
        for name, kind, version, status, expiry, _ in SPECS:
            doc = conn.execute('SELECT * FROM documents WHERE filename=?', (name + '.pdf',)).fetchone()
            values = {key: str(doc[key] or '') for key in (
                'title', 'product_series', 'product_model', 'document_type', 'language', 'version',
                'release_date', 'source_url', 'status', 'canonical_product_id', 'superseded_by_document_id',
                'effective_date', 'expiration_date', 'firmware_range', 'authority_level', 'status_note')}
            values.update(canonical_product_id=str(product_id), status=status, expiration_date=expiry,
                          authority_level='reference', status_note='Fictional only; no manufacturer authorization')
            if status == 'superseded':
                replacement = conn.execute('SELECT id FROM documents WHERE filename=?',
                                           (name.replace('v1', 'v2') + '.pdf',)).fetchone()
                values['superseded_by_document_id'] = str(replacement['id'])
            update_document(conn, doc['id'], values, 'R3 fictional lifecycle fixture', 'fixture')
        docs = [dict(r) for r in conn.execute('SELECT id,title,version,status,sha256,page_count FROM documents')]
    return {'database': str(db), 'documents': docs, 'imported': summary.imported}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--empty', action='store_true')
    args = parser.parse_args()
    with redirect_stdout(sys.stderr):
        result = seed(args.root, empty=args.empty)
    print(json.dumps(result, ensure_ascii=False, indent=2))
