from __future__ import annotations

import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from urllib.parse import urlparse

from .metadata import UNKNOWN


EDITABLE_METADATA_FIELDS = (
    "title",
    "product_series",
    "product_model",
    "document_type",
    "language",
    "version",
    "release_date",
    "source_url",
    "status",
)

DOCUMENT_STATUS_LABELS = {
    "needs_review": "待确认",
    "effective": "生效中",
    "superseded": "已被替代",
    "draft": "草稿",
    "archived": "已归档",
}

PRODUCT_STATUS_LABELS = {
    "active": "使用中",
    "planned": "规划中",
    "inactive": "已停用",
    "archived": "已归档",
}

ALIAS_TYPE_LABELS = {
    "official_name": "标准名称",
    "english_name": "英文名称",
    "chinese_name": "中文名称",
    "abbreviation": "缩写",
    "common": "常见写法",
}

AUTHORITY_LEVEL_LABELS = {
    "reference": "参考资料",
    "verified": "已核验",
    "authoritative": "权威来源",
}


class ValidationError(ValueError):
    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("；".join(errors.values()))


@dataclass(frozen=True)
class AliasMatch:
    status: str
    product_ids: tuple[int, ...] = ()

    @property
    def product_id(self) -> int | None:
        return self.product_ids[0] if self.status == "matched" else None


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_alias(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return " ".join(normalized.split()).casefold()


def _audit_value(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def record_audit(
    connection: sqlite3.Connection,
    *,
    object_type: str,
    object_id: int,
    field_name: str,
    before_value: object,
    after_value: object,
    reason: str,
    operator: str,
    operation_type: str,
) -> None:
    connection.execute(
        """INSERT INTO audit_log
           (object_type, object_id, field_name, before_value, after_value,
            changed_at, reason, operator, operation_type)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            object_type,
            object_id,
            field_name,
            _audit_value(before_value),
            _audit_value(after_value),
            _now(),
            reason,
            operator,
            operation_type,
        ),
    )


def match_product_alias(connection: sqlite3.Connection, value: str) -> AliasMatch:
    normalized = normalize_alias(value)
    if not normalized:
        return AliasMatch("none")
    rows = connection.execute(
        """SELECT DISTINCT a.product_id
           FROM product_aliases a
           JOIN products p ON p.id = a.product_id
           WHERE a.normalized_alias = ? AND a.is_enabled = 1 AND p.status != 'archived'
           ORDER BY a.product_id""",
        (normalized,),
    ).fetchall()
    product_ids = tuple(row["product_id"] for row in rows)
    if not product_ids:
        return AliasMatch("none")
    if len(product_ids) == 1:
        return AliasMatch("matched", product_ids)
    return AliasMatch("conflict", product_ids)


def match_document_product(connection: sqlite3.Connection, metadata: dict[str, str]) -> AliasMatch:
    matched_ids: set[int] = set()
    conflict_ids: set[int] = set()
    for field_name in ("product_model", "title"):
        value = metadata.get(field_name, "")
        if not value or value == UNKNOWN:
            continue
        result = match_product_alias(connection, value)
        if result.status == "matched" and result.product_id is not None:
            matched_ids.add(result.product_id)
        elif result.status == "conflict":
            conflict_ids.update(result.product_ids)
    if conflict_ids or len(matched_ids) > 1:
        return AliasMatch("conflict", tuple(sorted(conflict_ids | matched_ids)))
    if matched_ids:
        return AliasMatch("matched", tuple(matched_ids))
    return AliasMatch("none")


def _require_reason(reason: str, operator: str) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not reason.strip():
        errors["reason"] = "必须填写简短修改原因。"
    if not operator.strip():
        errors["operator"] = "本地操作者名称不能为空。"
    return errors


def _valid_iso_date(value: str, field_label: str, allow_unknown: bool = False) -> str | None:
    if not value:
        return f"{field_label}不能为空。"
    if allow_unknown and value == UNKNOWN:
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return f"{field_label}必须使用 YYYY-MM-DD 格式。"
    return None


def _valid_optional_iso_date(value: str, field_label: str) -> str | None:
    if not value:
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return f"{field_label}必须使用 YYYY-MM-DD 格式。"
    return None


def _validate_document_values(
    connection: sqlite3.Connection,
    document_id: int,
    values: dict[str, str],
    reason: str,
    operator: str,
) -> dict[str, object]:
    errors = _require_reason(reason, operator)
    labels = {
        "title": "文档标题",
        "product_series": "产品系列",
        "product_model": "产品型号",
        "document_type": "文档类型",
        "language": "语言",
        "version": "版本号",
        "release_date": "发布日期",
        "source_url": "来源网址",
        "status": "文档状态",
    }
    cleaned: dict[str, object] = {}
    for field_name in EDITABLE_METADATA_FIELDS:
        value = values.get(field_name, "").strip()
        cleaned[field_name] = value
        if not value:
            errors[field_name] = f"{labels[field_name]}不能为空。"

    release_error = _valid_iso_date(str(cleaned["release_date"]), "发布日期", allow_unknown=True)
    if release_error:
        errors["release_date"] = release_error

    source_url = str(cleaned["source_url"])
    if source_url != UNKNOWN:
        parsed = urlparse(source_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors["source_url"] = "来源网址必须是有效的 HTTP 或 HTTPS 地址。"

    if cleaned["status"] not in DOCUMENT_STATUS_LABELS:
        errors["status"] = "文档状态不在允许范围内。"

    product_value = values.get("canonical_product_id", "").strip()
    cleaned["canonical_product_id"] = int(product_value) if product_value.isdigit() else None
    if product_value and cleaned["canonical_product_id"] is None:
        errors["canonical_product_id"] = "规范产品无效。"
    elif cleaned["canonical_product_id"] is not None:
        exists = connection.execute(
            "SELECT 1 FROM products WHERE id = ?", (cleaned["canonical_product_id"],)
        ).fetchone()
        if not exists:
            errors["canonical_product_id"] = "选择的规范产品不存在。"

    replacement_value = values.get("superseded_by_document_id", "").strip()
    cleaned["superseded_by_document_id"] = int(replacement_value) if replacement_value.isdigit() else None
    if replacement_value and cleaned["superseded_by_document_id"] is None:
        errors["superseded_by_document_id"] = "替代文档无效。"

    for field_name, label in (
        ("effective_date", "生效日期"),
        ("expiration_date", "失效日期"),
    ):
        cleaned[field_name] = values.get(field_name, "").strip()
        date_error = _valid_optional_iso_date(str(cleaned[field_name]), label)
        if date_error:
            errors[field_name] = date_error

    effective_date = str(cleaned["effective_date"])
    expiration_date = str(cleaned["expiration_date"])
    if effective_date and expiration_date and expiration_date < effective_date:
        errors["expiration_date"] = "失效日期不得早于生效日期。"

    cleaned["firmware_range"] = values.get("firmware_range", "").strip()
    cleaned["authority_level"] = values.get("authority_level", "").strip()
    cleaned["status_note"] = values.get("status_note", "").strip()
    if cleaned["authority_level"] not in AUTHORITY_LEVEL_LABELS:
        errors["authority_level"] = "权威等级不在允许范围内。"

    replacement_id = cleaned["superseded_by_document_id"]
    if cleaned["status"] == "superseded" and replacement_id is None:
        errors["superseded_by_document_id"] = "状态为“已被替代”时必须选择替代文档。"
    if replacement_id is not None:
        replacement = connection.execute(
            "SELECT id FROM documents WHERE id = ?", (replacement_id,)
        ).fetchone()
        if not replacement:
            errors["superseded_by_document_id"] = "选择的替代文档不存在。"
        elif replacement_id == document_id:
            errors["superseded_by_document_id"] = "文档不能替代自身。"
        elif _would_create_supersession_cycle(connection, document_id, replacement_id):
            errors["superseded_by_document_id"] = "替代关系会形成循环，已拒绝保存。"

    if errors:
        raise ValidationError(errors)
    return cleaned


def _would_create_supersession_cycle(
    connection: sqlite3.Connection, document_id: int, replacement_id: int
) -> bool:
    current_id: int | None = replacement_id
    visited: set[int] = set()
    while current_id is not None:
        if current_id == document_id:
            return True
        if current_id in visited:
            return True
        visited.add(current_id)
        row = connection.execute(
            "SELECT superseded_by_document_id FROM documents WHERE id = ?", (current_id,)
        ).fetchone()
        current_id = row["superseded_by_document_id"] if row else None
    return False


def update_document(
    connection: sqlite3.Connection,
    document_id: int,
    values: dict[str, str],
    reason: str,
    operator: str,
) -> int:
    document = connection.execute(
        "SELECT * FROM documents WHERE id = ?", (document_id,)
    ).fetchone()
    if not document:
        raise ValidationError({"document": "文档不存在。"})
    cleaned = _validate_document_values(connection, document_id, values, reason, operator)

    changed = 0
    for field_name in EDITABLE_METADATA_FIELDS:
        before_value = document[field_name]
        after_value = cleaned[field_name]
        if before_value == after_value:
            continue
        connection.execute(
            f"UPDATE documents SET {field_name} = ? WHERE id = ?",
            (after_value, document_id),
        )
        connection.execute(
            """UPDATE document_field_values
               SET revised_value = ?, updated_at = ?
               WHERE document_id = ? AND field_name = ?""",
            (after_value, _now(), document_id, field_name),
        )
        record_audit(
            connection,
            object_type="document",
            object_id=document_id,
            field_name=field_name,
            before_value=before_value,
            after_value=after_value,
            reason=reason.strip(),
            operator=operator.strip(),
            operation_type="update",
        )
        changed += 1

    lifecycle_fields = (
        "canonical_product_id",
        "superseded_by_document_id",
        "effective_date",
        "expiration_date",
        "firmware_range",
        "authority_level",
        "status_note",
    )
    for field_name in lifecycle_fields:
        before_value = document[field_name]
        after_value = cleaned[field_name]
        if before_value == after_value or (before_value is None and after_value == ""):
            continue
        connection.execute(
            f"UPDATE documents SET {field_name} = ? WHERE id = ?",
            (after_value or None if field_name.endswith("_date") else after_value, document_id),
        )
        record_audit(
            connection,
            object_type="document",
            object_id=document_id,
            field_name=field_name,
            before_value=before_value,
            after_value=after_value,
            reason=reason.strip(),
            operator=operator.strip(),
            operation_type="update",
        )
        changed += 1

    if changed == 0:
        raise ValidationError({"changes": "没有检测到需要保存的修改。"})
    return changed


def _validate_product_values(values: dict[str, str], reason: str, operator: str) -> dict[str, str]:
    errors = _require_reason(reason, operator)
    cleaned = {
        "standard_name": values.get("standard_name", "").strip(),
        "product_series": values.get("product_series", "").strip(),
        "status": values.get("status", "").strip(),
    }
    if not cleaned["standard_name"]:
        errors["standard_name"] = "标准产品名称不能为空。"
    if not cleaned["product_series"]:
        errors["product_series"] = "产品系列不能为空。"
    if cleaned["status"] not in PRODUCT_STATUS_LABELS:
        errors["status"] = "产品状态不在允许范围内。"
    if errors:
        raise ValidationError(errors)
    return cleaned


def create_product(
    connection: sqlite3.Connection,
    values: dict[str, str],
    reason: str,
    operator: str,
) -> int:
    cleaned = _validate_product_values(values, reason, operator)
    timestamp = _now()
    try:
        cursor = connection.execute(
            """INSERT INTO products
               (standard_name, product_series, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                cleaned["standard_name"],
                cleaned["product_series"],
                cleaned["status"],
                timestamp,
                timestamp,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise ValidationError({"standard_name": "标准产品名称已存在。"}) from exc
    product_id = cursor.lastrowid
    alias_cursor = connection.execute(
        """INSERT INTO product_aliases
           (product_id, alias_text, normalized_alias, alias_type, is_enabled, created_at)
           VALUES (?, ?, ?, 'official_name', 1, ?)""",
        (product_id, cleaned["standard_name"], normalize_alias(cleaned["standard_name"]), timestamp),
    )
    for field_name, value in cleaned.items():
        record_audit(
            connection,
            object_type="product",
            object_id=product_id,
            field_name=field_name,
            before_value=None,
            after_value=value,
            reason=reason.strip(),
            operator=operator.strip(),
            operation_type="create",
        )
    record_audit(
        connection,
        object_type="product_alias",
        object_id=alias_cursor.lastrowid,
        field_name="alias_text",
        before_value=None,
        after_value=cleaned["standard_name"],
        reason=reason.strip(),
        operator=operator.strip(),
        operation_type="create",
    )
    return product_id


def update_product(
    connection: sqlite3.Connection,
    product_id: int,
    values: dict[str, str],
    reason: str,
    operator: str,
) -> int:
    product = connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        raise ValidationError({"product": "产品不存在。"})
    cleaned = _validate_product_values(values, reason, operator)
    changed = 0
    for field_name in ("standard_name", "product_series", "status"):
        if product[field_name] == cleaned[field_name]:
            continue
        try:
            connection.execute(
                f"UPDATE products SET {field_name} = ?, updated_at = ? WHERE id = ?",
                (cleaned[field_name], _now(), product_id),
            )
        except sqlite3.IntegrityError as exc:
            raise ValidationError({field_name: "该标准产品名称已存在。"}) from exc
        record_audit(
            connection,
            object_type="product",
            object_id=product_id,
            field_name=field_name,
            before_value=product[field_name],
            after_value=cleaned[field_name],
            reason=reason.strip(),
            operator=operator.strip(),
            operation_type="update",
        )
        changed += 1
    if changed == 0:
        raise ValidationError({"changes": "没有检测到需要保存的产品修改。"})
    return changed


def add_product_alias(
    connection: sqlite3.Connection,
    product_id: int,
    alias_text: str,
    alias_type: str,
    reason: str,
    operator: str,
) -> tuple[int, AliasMatch]:
    errors = _require_reason(reason, operator)
    alias_text = alias_text.strip()
    if not alias_text:
        errors["alias_text"] = "别名文本不能为空。"
    if alias_type not in ALIAS_TYPE_LABELS:
        errors["alias_type"] = "别名类型不在允许范围内。"
    if not connection.execute("SELECT 1 FROM products WHERE id = ?", (product_id,)).fetchone():
        errors["product"] = "所属产品不存在。"
    if errors:
        raise ValidationError(errors)
    try:
        cursor = connection.execute(
            """INSERT INTO product_aliases
               (product_id, alias_text, normalized_alias, alias_type, is_enabled, created_at)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (product_id, alias_text, normalize_alias(alias_text), alias_type, _now()),
        )
    except sqlite3.IntegrityError as exc:
        raise ValidationError({"alias_text": "该产品已存在相同别名。"}) from exc
    alias_id = cursor.lastrowid
    record_audit(
        connection,
        object_type="product_alias",
        object_id=alias_id,
        field_name="alias_text",
        before_value=None,
        after_value=alias_text,
        reason=reason.strip(),
        operator=operator.strip(),
        operation_type="create",
    )
    return alias_id, match_product_alias(connection, alias_text)


def set_alias_enabled(
    connection: sqlite3.Connection,
    alias_id: int,
    enabled: bool,
    reason: str,
    operator: str,
) -> None:
    errors = _require_reason(reason, operator)
    alias = connection.execute("SELECT * FROM product_aliases WHERE id = ?", (alias_id,)).fetchone()
    if not alias:
        errors["alias"] = "产品别名不存在。"
    if errors:
        raise ValidationError(errors)
    before = bool(alias["is_enabled"])
    if before == enabled:
        raise ValidationError({"changes": "别名启用状态没有变化。"})
    connection.execute(
        "UPDATE product_aliases SET is_enabled = ? WHERE id = ?", (int(enabled), alias_id)
    )
    record_audit(
        connection,
        object_type="product_alias",
        object_id=alias_id,
        field_name="is_enabled",
        before_value=json.dumps(before),
        after_value=json.dumps(enabled),
        reason=reason.strip(),
        operator=operator.strip(),
        operation_type="update",
    )
