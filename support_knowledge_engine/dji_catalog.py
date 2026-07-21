from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from .governance import add_product_alias, create_product, normalize_alias
from .metadata import UNKNOWN


SUCCESSFUL_DOWNLOAD_STATUSES = {"downloaded", "skipped_existing"}
DJI_IMPORT_OPERATOR = "DJI 中国大陆官网清单导入器"
DJI_IMPORT_REASON = "根据 DJI 中国大陆官网下载清单建立真实产品目录"


@dataclass(frozen=True)
class DjiProduct:
    slug: str
    standard_name: str
    product_series: str


@dataclass(frozen=True)
class DjiCatalog:
    manifest_path: Path
    documents_by_path: dict[Path, dict[str, str]]
    products: tuple[DjiProduct, ...]
    listed_documents: int
    failed_downloads: int
    non_pdf_downloads: int


def _text(value: object) -> str:
    return " ".join(str(value or "").split())


def _date_or_unknown(value: object) -> str:
    candidate = _text(value)
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return UNKNOWN


def _url_or_unknown(value: object) -> str:
    candidate = _text(value)
    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return candidate
    return UNKNOWN


def _find_manifest(root: Path) -> Path | None:
    direct = root / "manifest.json"
    if direct.is_file():
        return direct
    if root.name.casefold() == "files":
        parent_manifest = root.parent / "manifest.json"
        if parent_manifest.is_file():
            return parent_manifest
    return None


def load_dji_catalog(directory: str | Path) -> DjiCatalog | None:
    root = Path(directory).resolve(strict=True)
    manifest_path = _find_manifest(root)
    if manifest_path is None:
        return None

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"DJI 清单读取失败：{exc}") from exc

    rows = payload.get("documents")
    if not isinstance(rows, list):
        raise ValueError("DJI 清单缺少 documents 数组")

    base_directory = manifest_path.parent.resolve()
    documents_by_path: dict[Path, dict[str, str]] = {}
    products: dict[str, DjiProduct] = {}
    failed_downloads = 0
    non_pdf_downloads = 0

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"DJI 清单第 {index} 条记录不是对象")

        product_slug = _text(row.get("product_slug"))
        product_name = _text(row.get("product_title"))
        product_series = _text(row.get("series_title"))
        if product_slug and product_name and product_series:
            products.setdefault(
                product_slug,
                DjiProduct(product_slug, product_name, product_series),
            )

        status = _text(row.get("download_status"))
        if status not in SUCCESSFUL_DOWNLOAD_STATUSES:
            if status in {"failed", "product_metadata_failed"}:
                failed_downloads += 1
            continue

        local_path_value = _text(row.get("local_path"))
        if not local_path_value:
            raise ValueError(f"DJI 清单第 {index} 条成功记录缺少 local_path")
        file_path = (base_directory / local_path_value).resolve()
        if not file_path.is_relative_to(base_directory):
            raise ValueError(f"DJI 清单第 {index} 条记录越出资料目录")
        if not file_path.is_relative_to(root):
            continue
        if file_path.suffix.casefold() != ".pdf":
            non_pdf_downloads += 1
            continue

        metadata = {
            "title": _text(row.get("manual_title")) or file_path.stem,
            "product_series": product_series or UNKNOWN,
            "product_model": product_name or UNKNOWN,
            "document_type": _text(row.get("manual_category")) or "未分类",
            "language": _text(row.get("language")) or "zh-CN",
            "version": _text(row.get("version")) or "未标注",
            "release_date": _date_or_unknown(row.get("release_at")),
            "source_url": _url_or_unknown(row.get("source_url")),
        }
        existing = documents_by_path.get(file_path)
        if existing is not None and existing != metadata:
            raise ValueError(f"DJI 清单包含冲突的本地路径：{local_path_value}")
        documents_by_path[file_path] = metadata

    return DjiCatalog(
        manifest_path=manifest_path,
        documents_by_path=documents_by_path,
        products=tuple(products.values()),
        listed_documents=len(rows),
        failed_downloads=failed_downloads,
        non_pdf_downloads=non_pdf_downloads,
    )


def ensure_dji_products(
    connection: sqlite3.Connection,
    catalog: DjiCatalog,
    *,
    operator: str = DJI_IMPORT_OPERATOR,
    reason: str = DJI_IMPORT_REASON,
) -> dict[str, int]:
    created_products = 0
    created_aliases = 0

    for product in catalog.products:
        existing = connection.execute(
            "SELECT id FROM products WHERE standard_name = ? COLLATE NOCASE",
            (product.standard_name,),
        ).fetchone()
        if existing:
            product_id = existing["id"]
        else:
            product_id = create_product(
                connection,
                {
                    "standard_name": product.standard_name,
                    "product_series": product.product_series,
                    "status": "active",
                },
                reason,
                operator,
            )
            created_products += 1

        normalized_slug = normalize_alias(product.slug)
        alias_exists = connection.execute(
            """SELECT 1 FROM product_aliases
               WHERE product_id = ? AND normalized_alias = ?""",
            (product_id, normalized_slug),
        ).fetchone()
        if normalized_slug and not alias_exists:
            add_product_alias(
                connection,
                product_id,
                product.slug,
                "common",
                reason,
                operator,
            )
            created_aliases += 1

    return {
        "created_products": created_products,
        "created_aliases": created_aliases,
    }
