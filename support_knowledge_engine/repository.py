from __future__ import annotations

import sqlite3
import time

from .governance import match_product_alias
from .normalization import normalize_query
from .search_telemetry import emit_search_telemetry


DOCUMENT_SELECT = """
    SELECT d.*,
           p.standard_name AS canonical_product_name,
           replacement.title AS replacement_title
    FROM documents d
    LEFT JOIN products p ON p.id = d.canonical_product_id
    LEFT JOIN documents replacement ON replacement.id = d.superseded_by_document_id
"""


def filter_options(connection: sqlite3.Connection) -> dict[str, list[sqlite3.Row] | list[str]]:
    options: dict[str, list[sqlite3.Row] | list[str]] = {}
    for field in ("product_series", "document_type"):
        rows = connection.execute(
            f"SELECT DISTINCT {field} AS value FROM documents ORDER BY {field} COLLATE NOCASE"
        ).fetchall()
        options[field] = [row["value"] for row in rows]
    options["products"] = connection.execute(
        "SELECT id, standard_name FROM products ORDER BY standard_name COLLATE NOCASE"
    ).fetchall()
    return options


def _document_filters(
    *,
    product_series: str = "",
    document_type: str = "",
    status: str = "",
    association: str = "",
    product_id: str = "",
) -> tuple[list[str], list[object]]:
    conditions: list[str] = []
    parameters: list[object] = []
    if product_series:
        conditions.append("d.product_series = ?")
        parameters.append(product_series)
    if document_type:
        conditions.append("d.document_type = ?")
        parameters.append(document_type)
    if status:
        conditions.append("d.status = ?")
        parameters.append(status)
    if association == "unlinked":
        conditions.append("d.canonical_product_id IS NULL")
    elif association == "linked":
        conditions.append("d.canonical_product_id IS NOT NULL")
    if product_id.isdigit():
        conditions.append("d.canonical_product_id = ?")
        parameters.append(int(product_id))
    return conditions, parameters


def list_documents(
    connection: sqlite3.Connection,
    product_series: str = "",
    document_type: str = "",
    status: str = "",
    association: str = "",
    product_id: str = "",
) -> list[sqlite3.Row]:
    conditions, parameters = _document_filters(
        product_series=product_series,
        document_type=document_type,
        status=status,
        association=association,
        product_id=product_id,
    )
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    return connection.execute(
        f"""{DOCUMENT_SELECT} {where}
            ORDER BY CASE d.status
                         WHEN 'effective' THEN 0
                         WHEN 'needs_review' THEN 1
                         WHEN 'draft' THEN 2
                         WHEN 'superseded' THEN 3
                         WHEN 'archived' THEN 4
                         ELSE 5 END,
                     d.imported_at DESC, d.filename COLLATE NOCASE""",
        parameters,
    ).fetchall()


def _fts_expression(query: str) -> str:
    terms = [term for term in query.split() if term]
    if not terms:
        terms = [query]
    return " AND ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms)


def _status_priority(status: str) -> int:
    return {
        "effective": 0,
        "needs_review": 1,
        "draft": 2,
        "superseded": 3,
        "archived": 4,
    }.get(status, 5)


def search_documents(
    connection: sqlite3.Connection,
    query: str,
    product_series: str = "",
    document_type: str = "",
    status: str = "",
    association: str = "",
    product_id: str = "",
) -> list[dict[str, object]]:
    query = query.strip()
    if not query:
        return []

    conditions, filter_parameters = _document_filters(
        product_series=product_series,
        document_type=document_type,
        status=status,
        association=association,
        product_id=product_id,
    )
    extra_where = f" AND {' AND '.join(conditions)}" if conditions else ""
    terms = [term for term in query.split() if term] or [query]

    if all(len(term) >= 3 for term in terms):
        rows = connection.execute(
            f"""SELECT d.*, p.standard_name AS canonical_product_name,
                       page_fts.page_number,
                       snippet(page_fts, 0, '', '', ' … ', 24) AS snippet,
                       bm25(page_fts) AS rank,
                       0 AS alias_match
                FROM page_fts
                JOIN documents d ON d.id = page_fts.document_id
                LEFT JOIN products p ON p.id = d.canonical_product_id
                WHERE page_fts MATCH ? {extra_where}""",
            [_fts_expression(query), *filter_parameters],
        ).fetchall()
    else:
        escaped_query = query.replace("%", r"\%").replace("_", r"\_")
        like_value = f"%{escaped_query}%"
        rows = connection.execute(
            f"""SELECT d.*, product.standard_name AS canonical_product_name,
                       page.page_number,
                       substr(page.content, max(instr(lower(page.content), lower(?)) - 48, 1), 180) AS snippet,
                       0 AS rank,
                       0 AS alias_match
                FROM pages page
                JOIN documents d ON d.id = page.document_id
                LEFT JOIN products product ON product.id = d.canonical_product_id
                WHERE page.content LIKE ? ESCAPE '\\' {extra_where}""",
            [query, like_value, *filter_parameters],
        ).fetchall()

    results: dict[tuple[int, int], dict[str, object]] = {
        (row["id"], row["page_number"]): dict(row) for row in rows
    }

    alias_match = match_product_alias(connection, query)
    if alias_match.status == "matched" and alias_match.product_id is not None:
        alias_conditions = ["d.canonical_product_id = ?", *conditions]
        alias_parameters: list[object] = [alias_match.product_id, *filter_parameters]
        alias_where = " AND ".join(alias_conditions)
        alias_rows = connection.execute(
            f"""SELECT d.*, product.standard_name AS canonical_product_name,
                       page.page_number,
                       substr(page.content, 1, 180) AS snippet,
                       -1000.0 AS rank,
                       1 AS alias_match
                FROM documents d
                JOIN pages page ON page.document_id = d.id AND page.page_number = 1
                LEFT JOIN products product ON product.id = d.canonical_product_id
                WHERE {alias_where}""",
            alias_parameters,
        ).fetchall()
        for row in alias_rows:
            results[(row["id"], row["page_number"])] = dict(row)

    return sorted(
        results.values(),
        key=lambda row: (
            _status_priority(str(row["status"])),
            0 if str(row.get("document_type", "")).casefold() == "service handbook" else 1,
            float(row["rank"]),
            str(row["filename"]).casefold(),
            int(row["page_number"]),
        ),
    )


MATCH_STATE_LABELS = {
    "high_confidence": "高可信匹配",
    "possible_match": "可能匹配",
    "ambiguous_product": "产品存在歧义",
    "version_conflict": "版本存在冲突",
    "outdated_only": "仅命中过期文档",
    "insufficient_evidence": "证据不足",
}


def retrieve_with_context(
    connection: sqlite3.Connection,
    query: str,
    product_series: str = "",
    document_type: str = "",
    status: str = "",
    association: str = "",
    product_id: str = "",
) -> dict[str, object]:
    """Shared retrieval core. It does not write telemetry."""
    started = time.perf_counter()
    normalized = normalize_query(connection, query)
    risks: list[str] = []

    if normalized.ambiguous:
        results: list[dict[str, object]] = []
        state = "ambiguous_product"
        risks.append("查询中的产品名称同时匹配多个规范产品，系统未自动选择。")
    else:
        inferred_product = product_id
        if not inferred_product and len(normalized.product_ids) == 1:
            inferred_product = str(normalized.product_ids[0])
        results = search_documents(
            connection,
            normalized.retrieval_query,
            product_series,
            document_type,
            status,
            association,
            inferred_product,
        )
        statuses = {str(row["status"]) for row in results}
        if not results:
            state = "insufficient_evidence"
            risks.append("没有找到足够可靠的页级原文，未返回低相关内容。")
        elif statuses <= {"superseded", "archived"}:
            state = "outdated_only"
            risks.append("当前只有已被替代或已归档文档命中，请勿将其视为现行依据。")
        else:
            active = [row for row in results if row["status"] == "effective"]
            historical = [row for row in results if row["status"] in {"superseded", "archived"}]
            conflicting_pairs = {
                (row.get("canonical_product_id"), row.get("document_type")) for row in active
            } & {
                (row.get("canonical_product_id"), row.get("document_type")) for row in historical
            }
            if conflicting_pairs:
                state = "version_conflict"
                risks.append("同一产品和文档类型的新旧版本同时命中，请核对生效日期和替代关系。")
            elif len(normalized.product_ids) == 1 and active:
                state = "high_confidence"
            else:
                state = "possible_match"
                risks.append("结果包含原文命中，但产品或版本证据尚不足以判定为高可信。")

    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "original_query": normalized.original,
        "normalized_query": normalized.normalized,
        "retrieval_query": normalized.retrieval_query,
        "applied_rules": list(normalized.applied_rules),
        "recognized_products": [
            {"id": item, "name": name}
            for item, name in zip(normalized.product_ids, normalized.product_names)
        ],
        "match_state": state,
        "match_state_label": MATCH_STATE_LABELS[state],
        "risk_messages": risks,
        "elapsed_ms": elapsed_ms,
        "results": results,
    }


def search_with_context(
    connection: sqlite3.Connection,
    query: str,
    product_series: str = "",
    document_type: str = "",
    status: str = "",
    association: str = "",
    product_id: str = "",
) -> dict[str, object]:
    result = retrieve_with_context(
        connection,
        query,
        product_series,
        document_type,
        status,
        association,
        product_id,
    )
    emit_search_telemetry(connection, result)
    return result


def alias_conflict_products(connection: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    match = match_product_alias(connection, query)
    if match.status != "conflict":
        return []
    placeholders = ",".join("?" for _ in match.product_ids)
    return connection.execute(
        f"SELECT id, standard_name FROM products WHERE id IN ({placeholders}) ORDER BY standard_name",
        match.product_ids,
    ).fetchall()


def get_document(connection: sqlite3.Connection, document_id: int) -> sqlite3.Row | None:
    return connection.execute(
        f"{DOCUMENT_SELECT} WHERE d.id = ?", (document_id,)
    ).fetchone()


def get_document_pages(connection: sqlite3.Connection, document_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT page_number, content FROM pages WHERE document_id = ? ORDER BY page_number",
        (document_id,),
    ).fetchall()


def get_document_field_values(
    connection: sqlite3.Connection, document_id: int
) -> dict[str, sqlite3.Row]:
    rows = connection.execute(
        """SELECT field_name, extracted_value, revised_value,
                  COALESCE(revised_value, extracted_value) AS effective_value, updated_at
           FROM document_field_values WHERE document_id = ? ORDER BY id""",
        (document_id,),
    ).fetchall()
    return {row["field_name"]: row for row in rows}


def get_replacement_candidates(
    connection: sqlite3.Connection, document_id: int
) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT id, title, version, status FROM documents
           WHERE id != ? ORDER BY title COLLATE NOCASE, version COLLATE NOCASE""",
        (document_id,),
    ).fetchall()


def get_import_runs(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM import_runs ORDER BY id DESC LIMIT 100"
    ).fetchall()


def get_import_items(connection: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    rows = connection.execute(
        "SELECT * FROM import_items ORDER BY id DESC LIMIT 1000"
    ).fetchall()
    grouped: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(row["run_id"], []).append(row)
    return grouped


def get_source_fetches(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM source_fetches ORDER BY id DESC LIMIT 200"
    ).fetchall()


def list_products(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT p.*,
                  COUNT(DISTINCT a.id) AS alias_count,
                  COUNT(DISTINCT d.id) AS document_count,
                  COUNT(DISTINCT CASE WHEN conflicts.product_count > 1 THEN a.id END) AS conflict_count
           FROM products p
           LEFT JOIN product_aliases a ON a.product_id = p.id
           LEFT JOIN documents d ON d.canonical_product_id = p.id
           LEFT JOIN (
               SELECT normalized_alias, COUNT(DISTINCT product_id) AS product_count
               FROM product_aliases WHERE is_enabled = 1
               GROUP BY normalized_alias
           ) conflicts ON conflicts.normalized_alias = a.normalized_alias
           GROUP BY p.id
           ORDER BY p.standard_name COLLATE NOCASE"""
    ).fetchall()


def get_product(connection: sqlite3.Connection, product_id: int) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


def get_product_aliases(connection: sqlite3.Connection, product_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT a.*,
                  (SELECT COUNT(DISTINCT other.product_id)
                   FROM product_aliases other
                   WHERE other.normalized_alias = a.normalized_alias
                     AND other.is_enabled = 1) AS matching_product_count
           FROM product_aliases a
           WHERE a.product_id = ?
           ORDER BY a.is_enabled DESC, a.alias_type, a.alias_text COLLATE NOCASE""",
        (product_id,),
    ).fetchall()


def get_product_documents(connection: sqlite3.Connection, product_id: int) -> list[sqlite3.Row]:
    return connection.execute(
        f"{DOCUMENT_SELECT} WHERE d.canonical_product_id = ? ORDER BY d.title COLLATE NOCASE",
        (product_id,),
    ).fetchall()


def get_audit_log(
    connection: sqlite3.Connection,
    *,
    object_type: str = "",
    field_name: str = "",
    operator: str = "",
    object_id: int | None = None,
    limit: int = 500,
) -> list[sqlite3.Row]:
    conditions: list[str] = []
    parameters: list[object] = []
    if object_type:
        conditions.append("object_type = ?")
        parameters.append(object_type)
    if field_name:
        conditions.append("field_name = ?")
        parameters.append(field_name)
    if operator:
        conditions.append("operator = ?")
        parameters.append(operator)
    if object_id is not None:
        conditions.append("object_id = ?")
        parameters.append(object_id)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    parameters.append(limit)
    return connection.execute(
        f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?", parameters
    ).fetchall()


def audit_filter_options(connection: sqlite3.Connection) -> dict[str, list[str]]:
    return {
        "object_types": [
            row["value"]
            for row in connection.execute(
                "SELECT DISTINCT object_type AS value FROM audit_log ORDER BY object_type"
            )
        ],
        "field_names": [
            row["value"]
            for row in connection.execute(
                "SELECT DISTINCT field_name AS value FROM audit_log ORDER BY field_name"
            )
        ],
        "operators": [
            row["value"]
            for row in connection.execute(
                "SELECT DISTINCT operator AS value FROM audit_log ORDER BY operator"
            )
        ],
    }
