from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import date
from urllib.parse import urlparse


UNKNOWN = "待确认"


LABELS = {
    "title": ("文档标题", "标题", "document title", "title"),
    "product_series": ("产品系列", "product series", "series"),
    "product_model": ("产品型号", "型号", "product model", "model"),
    "document_type": ("文档类型", "document type", "doc type"),
    "language": ("语言", "language"),
    "version": ("版本号", "版本", "version"),
    "release_date": ("发布日期", "发布日", "release date", "published"),
    "source_url": ("原始来源网址", "来源网址", "source url", "source"),
}


@dataclass(frozen=True)
class DocumentMetadata:
    title: str = UNKNOWN
    product_series: str = UNKNOWN
    product_model: str = UNKNOWN
    document_type: str = UNKNOWN
    language: str = UNKNOWN
    version: str = UNKNOWN
    release_date: str = UNKNOWN
    source_url: str = UNKNOWN

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def _clean_value(value: str) -> str:
    return value.strip().strip("|：:;；").strip()


def _extract_labeled_value(text: str, aliases: tuple[str, ...]) -> str | None:
    alias_pattern = "|".join(re.escape(alias) for alias in aliases)
    pattern = re.compile(
        rf"^\s*(?:{alias_pattern})\s*[:：]\s*(.+?)\s*$",
        flags=re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        return None
    value = _clean_value(match.group(1))
    return value or None


def _valid_date(value: str | None) -> str:
    if not value:
        return UNKNOWN
    normalized = value.replace("/", "-").replace(".", "-")
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError:
        return UNKNOWN


def _valid_url(value: str | None) -> str:
    if not value:
        return UNKNOWN
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    return UNKNOWN


def parse_metadata(text: str, pdf_metadata: dict | None = None) -> DocumentMetadata:
    """Extract only explicit labels and trustworthy PDF title metadata.

    No product information is inferred from prose or filenames. Missing or
    malformed values remain ``待确认``.
    """
    extracted = {
        field: _extract_labeled_value(text, aliases)
        for field, aliases in LABELS.items()
    }

    pdf_title = _clean_value(str((pdf_metadata or {}).get("title") or ""))
    title = pdf_title or extracted["title"] or UNKNOWN

    return DocumentMetadata(
        title=title,
        product_series=extracted["product_series"] or UNKNOWN,
        product_model=extracted["product_model"] or UNKNOWN,
        document_type=extracted["document_type"] or UNKNOWN,
        language=extracted["language"] or UNKNOWN,
        version=extracted["version"] or UNKNOWN,
        release_date=_valid_date(extracted["release_date"]),
        source_url=_valid_url(extracted["source_url"]),
    )


def metadata_status(metadata: DocumentMetadata) -> str:
    return "待确认" if UNKNOWN in metadata.to_dict().values() else "已索引"

