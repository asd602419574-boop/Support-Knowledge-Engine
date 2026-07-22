from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from support_knowledge_engine.db import connect_database
from support_knowledge_engine.sources import acquire_sources, load_manifest


PDF = b"%PDF-1.4\n% synthetic acquisition fixture\n%%EOF\n"


class Handler(BaseHTTPRequestHandler):
    hits = 0
    payload = PDF

    def do_GET(self):
        type(self).hits += 1
        if self.path == "/missing.pdf":
            self.send_error(404)
            return
        if self.path == "/large.pdf":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(999999))
            self.end_headers()
            return
        if self.path == "/interrupted.pdf":
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(PDF) + 10))
            self.end_headers()
            self.wfile.write(PDF)
            return
        payload = b"<html>not pdf</html>" if self.path == "/fake.pdf" else type(self).payload
        content_type = "text/html" if self.path == "/fake.pdf" else "application/pdf"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("ETag", '"fixture-v1"')
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass


class ControlledSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        Handler.hits = 0
        Handler.payload = PDF
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def write_manifest(self, sources):
        path = self.root / "sources.json"
        path.write_text(json.dumps({"sources": sources}, ensure_ascii=False), encoding="utf-8")
        return path

    def source(self, source_id, path="/document.pdf"):
        return {
            "source_id": source_id, "product_name": "AeroCam Mini 2",
            "document_type": "手册", "url": f"http://127.0.0.1:{self.server.server_port}{path}",
            "language": "zh-CN", "expected_filename": f"{source_id}.pdf",
            "enabled": True, "notes": "本地测试服务",
        }

    def test_download_signature_metadata_and_hash_deduplication(self):
        manifest = self.write_manifest([self.source("one"), self.source("two")])
        database = self.root / "knowledge.db"
        output = self.root / "raw"
        summary = acquire_sources(manifest, database, output, retries=0)
        self.assertEqual((summary.downloaded, summary.duplicates, summary.failed), (1, 1, 0))
        self.assertTrue((output / "one" / "one.pdf").is_file())
        self.assertFalse((output / "two" / "two.pdf").exists())
        with connect_database(database) as connection:
            rows = connection.execute("SELECT * FROM source_fetches ORDER BY id").fetchall()
        self.assertEqual(rows[0]["http_status"], 200)
        self.assertEqual(rows[0]["etag"], '"fixture-v1"')
        self.assertEqual(len(rows[0]["sha256"]), 64)
        self.assertEqual(rows[1]["result"], "duplicate")

    def test_dry_run_and_non_pdf_failure_never_write_files(self):
        manifest = self.write_manifest([
            self.source("check"), self.source("fake", "/fake.pdf"),
            self.source("missing", "/missing.pdf"),
        ])
        database = self.root / "knowledge.db"
        summary = acquire_sources(manifest, database, self.root / "raw", dry_run=True, retries=0)
        self.assertEqual((summary.checked, summary.failed), (1, 2))
        self.assertFalse((self.root / "raw").exists())

    def test_manifest_rejects_duplicate_ids_and_link_discovery_fields_are_ignored(self):
        item = self.source("same")
        manifest = self.write_manifest([item, dict(item)])
        with self.assertRaisesRegex(ValueError, "重复"):
            load_manifest(manifest)
        unsafe = self.source("../escape")
        unsafe_manifest = self.write_manifest([unsafe])
        with self.assertRaisesRegex(ValueError, "不安全"):
            load_manifest(unsafe_manifest)

    def test_changed_remote_file_is_fetched_again_and_size_limit_is_logged(self):
        manifest = self.write_manifest([self.source("changing")])
        database = self.root / "knowledge.db"
        output = self.root / "raw"
        first = acquire_sources(manifest, database, output, retries=0)
        Handler.payload = PDF.replace(b"fixture", b"fixture-v2")
        second = acquire_sources(manifest, database, output, retries=0)
        self.assertEqual((first.downloaded, second.downloaded), (1, 1))
        with connect_database(database) as connection:
            hashes = [row[0] for row in connection.execute(
                "SELECT sha256 FROM source_fetches WHERE source_id='changing' ORDER BY id"
            )]
        self.assertNotEqual(hashes[0], hashes[1])

        large_manifest = self.write_manifest([self.source("large", "/large.pdf")])
        summary = acquire_sources(large_manifest, database, output, retries=0, max_bytes=100)
        self.assertEqual(summary.failed, 1)
        with connect_database(database) as connection:
            error = connection.execute(
                "SELECT error_reason FROM source_fetches WHERE source_id='large' ORDER BY id DESC"
            ).fetchone()[0]
        self.assertIn("大小限制", error)

        interrupted_manifest = self.write_manifest([
            self.source("interrupted", "/interrupted.pdf")
        ])
        interrupted = acquire_sources(interrupted_manifest, database, output, retries=0)
        self.assertEqual(interrupted.failed, 1)
        with connect_database(database) as connection:
            interruption_error = connection.execute(
                "SELECT error_reason FROM source_fetches WHERE source_id='interrupted' ORDER BY id DESC"
            ).fetchone()[0]
        self.assertIn("下载中断", interruption_error)


if __name__ == "__main__":
    unittest.main()
