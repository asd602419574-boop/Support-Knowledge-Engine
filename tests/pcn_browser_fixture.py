"""Explicit isolated PCN demo; no real model and no production database writes."""
from pathlib import Path
import sys

from support_knowledge_engine import create_app
from support_knowledge_engine.db import connect_database
from support_knowledge_engine.demo_data import seed_demo_data
from support_knowledge_engine.governance import EDITABLE_METADATA_FIELDS, update_document
from support_knowledge_engine.importer import import_directory
from tests.helpers import PROJECT_ROOT

FIXTURE_DIR = PROJECT_ROOT / 'tests' / 'fixtures' / 'pcn'
PDF_PATH = FIXTURE_DIR / 'AeroCam-Mini-2_Fictional-NTF-PCN_v1.0.pdf'


def create_fixture_pdf():
    from scripts.create_sample_pdfs import create_pdf
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    create_pdf(PDF_PATH, 'Fictional NTF storage cleanup PCN', [[
        'Document Title: Fictional NTF storage cleanup PCN',
        'Product Series: AeroCam', 'Product Model: AeroCam Mini 2',
        'Document Type: PCN', 'Language: en-US', 'Version: 1.0',
        'Release Date: 2026-01-01',
        'Source URL: https://example.invalid/fictional/ntf-storage',
        'Fictional test notice, not a real manufacturer standard.',
        'For AeroCam Mini 2 devices with internal storage,',
        'after NTF capture of test photos or videos and before returning the device,',
        'delete all test photos and videos from internal storage.',
    ]], 'Helvetica')


def seed_fixture(database):
    seed_demo_data(database)
    summary = import_directory(FIXTURE_DIR, database)
    if summary.failed:
        raise RuntimeError('Fixture PDF import failed')
    with connect_database(database) as db:
        row = db.execute('SELECT * FROM documents WHERE filename=?', (PDF_PATH.name,)).fetchone()
        product = db.execute("SELECT id FROM products WHERE standard_name='AeroCam Mini 2'").fetchone()
        values = {name: str(row[name] or '') for name in EDITABLE_METADATA_FIELDS}
        values.update(title='Fictional NTF storage cleanup PCN', product_series='AeroCam',
            product_model='AeroCam Mini 2', document_type='PCN', language='en-US', version='1.0',
            release_date='2026-01-01', source_url='https://example.invalid/fictional/ntf-storage',
            status='effective', canonical_product_id=str(product['id']), authority_level='authoritative',
            effective_date='2026-01-01', expiration_date='', firmware_range='',
            superseded_by_document_id='', status_note='虚构测试 PCN，非真实厂商标准；人工确认测试资料身份')
        if any(str(row[name] or '') != value for name,value in values.items()):
            update_document(db, row['id'], values, '测试 PCN 正常导入后人工治理确认', '虚构资料维护者')
        return row['id']


def main():
    if '--create-pdf' in sys.argv:
        create_fixture_pdf()
        return
    database = PROJECT_ROOT / 'instance' / 'pcn-browser-fixture.db'
    seed_fixture(database)
    app = create_app({'DATABASE': str(database), 'LLM_API_KEY': '', 'LLM_MODEL': '', 'LLM_BASE_URL': ''})
    print('ISOLATED FICTIONAL PCN DEMO: local rules only; no LLM or production data.', flush=True)
    app.run(host='127.0.0.1', port=5054, debug=False)


if __name__ == '__main__':
    main()
