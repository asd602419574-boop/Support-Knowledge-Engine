from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .db import connect_database
from .dji_catalog import load_dji_catalog
from .governance import EDITABLE_METADATA_FIELDS, match_document_product
from .metadata import parse_metadata


MAX_IN_MEMORY_PDF_BYTES = 64 * 1024 * 1024


def _load_pdf_backend():
    try:
        import fitz
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "PyMuPDF 无法加载，Windows 应用程序控制策略可能阻止了其原生 DLL。"
            "现有知识库仍可检索，但新增 PDF 暂时无法解析；请联系管理员放行 "
            "PyMuPDF，或在允许加载该组件的环境中执行导入。"
        ) from exc
    return fitz


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
    fitz = _load_pdf_backend()
    pages: list[str] = []
    path = Path(file_path)
    # Opening from an already-read byte stream avoids a PyMuPDF file-handle leak
    # on Windows when a malformed PDF fails during document construction.
    if path.stat().st_size <= MAX_IN_MEMORY_PDF_BYTES:
        pdf_bytes = path.read_bytes()
        document_source = fitz.open(stream=pdf_bytes, filetype="pdf")
    else:
        # Real vendor manuals can be hundreds of MB. Opening large files by path
        # prevents a second full-size in-memory copy during text extraction.
        document_source = fitz.open(str(path), filetype="pdf")

    with document_source as document:
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
                   metadata: dict[str, str], pages: list[str],
                   authority_level: str = "reference") -> None:
    existing_path = connection.execute(
        "SELECT id FROM documents WHERE file_path = ?", (str(file_path),)
    ).fetchone()

    product_match = match_document_product(connection, metadata)
    system_status = "needs_review" if "待确认" in metadata.values() else "effective"
    if product_match.status == "conflict":
        system_status = "needs_review"
    extracted_values = {**metadata, "status": system_status}

    if existing_path:
        document_id = existing_path["id"]
        field_rows = connection.execute(
            """SELECT field_name, revised_value FROM document_field_values
               WHERE document_id = ?""",
            (document_id,),
        ).fetchall()
        revised_values = {row["field_name"]: row["revised_value"] for row in field_rows}
        effective_values: dict[str, str] = {}
        for field_name in EDITABLE_METADATA_FIELDS:
            extracted_value = extracted_values[field_name]
            connection.execute(
                """INSERT INTO document_field_values
                   (document_id, field_name, extracted_value, revised_value, updated_at)
                   VALUES (?, ?, ?, NULL, ?)
                   ON CONFLICT(document_id, field_name) DO UPDATE SET
                       extracted_value = excluded.extracted_value,
                       updated_at = excluded.updated_at""",
                (document_id, field_name, extracted_value, utc_now()),
            )
            effective_values[field_name] = revised_values.get(field_name) or extracted_value

        current = connection.execute(
            "SELECT canonical_product_id, status_note FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        canonical_product_id = current["canonical_product_id"]
        if canonical_product_id is None and product_match.status == "matched":
            canonical_product_id = product_match.product_id
        status_note = current["status_note"]
        if product_match.status == "conflict" and not status_note:
            status_note = "产品别名匹配冲突，需人工关联。"

        connection.execute("DELETE FROM page_fts WHERE document_id = ?", (document_id,))
        connection.execute("DELETE FROM pages WHERE document_id = ?", (document_id,))
        connection.execute(
            """UPDATE documents SET filename = ?, title = ?, product_series = ?,
               product_model = ?, document_type = ?, language = ?, version = ?,
               release_date = ?, source_url = ?, sha256 = ?, imported_at = ?,
               status = ?, page_count = ?, error_reason = NULL,
               canonical_product_id = ?, status_note = ?, authority_level = ? WHERE id = ?""",
            (
                file_path.name, effective_values["title"], effective_values["product_series"],
                effective_values["product_model"], effective_values["document_type"],
                effective_values["language"], effective_values["version"],
                effective_values["release_date"], effective_values["source_url"],
                sha256, utc_now(), effective_values["status"], len(pages),
                canonical_product_id, status_note, authority_level, document_id,
            ),
        )
    else:
        cursor = connection.execute(
            """INSERT INTO documents
               (file_path, filename, title, product_series, product_model,
                document_type, language, version, release_date, source_url,
                sha256, imported_at, status, page_count, error_reason, authority_level)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
            (
                str(file_path), file_path.name, metadata["title"],
                metadata["product_series"], metadata["product_model"],
                metadata["document_type"], metadata["language"], metadata["version"],
                metadata["release_date"], metadata["source_url"], sha256,
                utc_now(), system_status, len(pages), authority_level,
            ),
        )
        document_id = cursor.lastrowid
        for field_name in EDITABLE_METADATA_FIELDS:
            connection.execute(
                """INSERT INTO document_field_values
                   (document_id, field_name, extracted_value, revised_value, updated_at)
                   VALUES (?, ?, ?, NULL, ?)""",
                (document_id, field_name, extracted_values[field_name], utc_now()),
            )
        if product_match.status == "matched":
            connection.execute(
                "UPDATE documents SET canonical_product_id = ? WHERE id = ?",
                (product_match.product_id, document_id),
            )
        elif product_match.status == "conflict":
            connection.execute(
                "UPDATE documents SET status_note = ? WHERE id = ?",
                ("产品别名匹配冲突，需人工关联。", document_id),
            )

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
            catalog = load_dji_catalog(root)
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
                catalog_metadata = (
                    catalog.documents_by_path.get(file_path) if catalog is not None else None
                )
                authority_level = "reference"
                if catalog_metadata is not None:
                    metadata = {**metadata, **catalog_metadata}
                    authority_level = "authoritative"
                with connection:
                    _save_document(
                        connection,
                        file_path,
                        file_hash,
                        metadata,
                        pages,
                        authority_level,
                    )
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
