from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import fitz

from .db import connect_database
from .metadata import parse_metadata


@dataclass(frozen=True)
class ImportSummary:
    run_id: int
    discovered: int
    imported: int
    duplicates: int
    failed: int


def utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def calculate_sha256(file_path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(file_path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_pdf(file_path: str | Path) -> tuple[dict[str, str], list[str]]:
    pages: list[str] = []
    # Opening from an already-read byte stream avoids a PyMuPDF file-handle leak
    # on Windows when a malformed PDF fails during document construction.
    pdf_bytes = Path(file_path).read_bytes()
    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        if document.needs_pass:
            raise ValueError("PDF 已加密，无法读取")
        pdf_metadata = document.metadata or {}
        for page in document:
            pages.append(page.get_text("text").strip())

    if not pages:
        raise ValueError("PDF 没有页面")
    if not any(pages):
        raise ValueError("未提取到可索引文本，可能是扫描件或文本受保护")

    metadata_text = "\n".join(pages[:2])[:16000]
    return parse_metadata(metadata_text, pdf_metadata).to_dict(), pages


def _record_item(connection, run_id: int, file_path: Path, sha256: str | None,
                 outcome: str, message: str) -> None:
    connection.execute(
        """INSERT INTO import_items
           (run_id, file_path, sha256, outcome, message, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (run_id, str(file_path), sha256, outcome, message, utc_now()),
    )


def _save_document(connection, file_path: Path, sha256: str,
                   metadata: dict[str, str], pages: list[str]) -> None:
    existing_path = connection.execute(
        "SELECT id FROM documents WHERE file_path = ?", (str(file_path),)
    ).fetchone()

    if existing_path:
        document_id = existing_path["id"]
        connection.execute("DELETE FROM page_fts WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM pages WHERE document_id = ?", (document_id,))
        connection.execute(
            """UPDATE documents SET filename = ?, title = ?, product_series = ?,
               product_model = ?, document_type = ?, language = ?, version = ?,
               release_date = ?, source_url = ?, sha256 = ?, imported_at = ?,
               status = ?, page_count = ?, error_reason = NULL WHERE id = ?""",
            (
                file_path.name, metadata["title"], metadata["product_series"],
                metadata["product_model"], metadata["document_type"],
                metadata["language"], metadata["version"], metadata["release_date"],
                metadata["source_url"], sha256, utc_now(),
                "待确认" if "待确认" in metadata.values() else "已索引",
                len(pages), document_id,
            ),
        )
    else:
        cursor = connection.execute(
            """INSERT INTO documents
               (file_path, filename, title, product_series, product_model,
                document_type, language, version, release_date, source_url,
                sha256, imported_at, status, page_count, error_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (
                str(file_path), file_path.name, metadata["title"],
                metadata["product_series"], metadata["product_model"],
                metadata["document_type"], metadata["language"], metadata["version"],
                metadata["release_date"], metadata["source_url"], sha256,
                utc_now(), "待确认" if "待确认" in metadata.values() else "已索引",
                len(pages),
            ),
        )
        document_id = cursor.lastrowid

    for page_number, content in enumerate(pages, start=1):
        connection.execute(
            "INSERT INTO pages (document_id, page_number, content) VALUES (?, ?, ?)",
            (document_id, page_number, content),
        )
        connection.execute(
            "INSERT INTO page_fts (content, document_id, page_number) VALUES (?, ?, ?)",
            (content, document_id, page_number),
        )


def import_directory(directory: str | Path, database_path: str | Path) -> ImportSummary:
    requested_path = Path(str(directory).strip()).expanduser()
    imported = duplicates = failed = 0

    with connect_database(database_path) as connection:
        cursor = connection.execute(
            "INSERT INTO import_runs (directory, started_at, status) VALUES (?, ?, ?)",
            (str(requested_path), utc_now(), "进行中"),
        )
        run_id = cursor.lastrowid
        connection.commit()

        try:
            root = requested_path.resolve(strict=True)
            if not root.is_dir():
                raise ValueError("指定路径不是目录")
            pdf_files = sorted(
                (path.resolve() for path in root.rglob("*")
                 if path.is_file() and path.suffix.lower() == ".pdf"),
                key=lambda path: str(path).casefold(),
            )
        except Exception as exc:
            message = f"目录扫描失败：{exc}"
            connection.execute(
                """UPDATE import_runs SET finished_at = ?, status = ?, error_message = ?
                   WHERE id = ?""",
                (utc_now(), "失败", message, run_id),
            )
            connection.commit()
            raise ValueError(message) from exc

        for file_path in pdf_files:
            file_hash: str | None = None
            try:
                file_hash = calculate_sha256(file_path)
                duplicate = connection.execute(
                    "SELECT filename FROM documents WHERE sha256 = ?", (file_hash,)
                ).fetchone()
                if duplicate:
                    duplicates += 1
                    _record_item(
                        connection, run_id, file_path, file_hash, "重复",
                        f"与已导入文档 {duplicate['filename']} 内容相同",
                    )
                    connection.commit()
                    continue

                metadata, pages = extract_pdf(file_path)
                with connection:
                    _save_document(connection, file_path, file_hash, metadata, pages)
                    _record_item(connection, run_id, file_path, file_hash, "已导入", "解析并建立索引成功")
                imported += 1
            except Exception as exc:
                failed += 1
                connection.rollback()
                _record_item(connection, run_id, file_path, file_hash, "失败", str(exc))
                connection.commit()

        status = "完成" if failed == 0 else "部分失败"
        connection.execute(
            """UPDATE import_runs SET finished_at = ?, status = ?, discovered_count = ?,
               imported_count = ?, duplicate_count = ?, failed_count = ? WHERE id = ?""",
            (utc_now(), status, len(pdf_files), imported, duplicates, failed, run_id),
        )
        connection.commit()

    return ImportSummary(run_id, len(pdf_files), imported, duplicates, failed)
