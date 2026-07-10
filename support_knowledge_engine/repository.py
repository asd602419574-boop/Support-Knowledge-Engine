from __future__ import annotations

import sqlite3

from .governance import match_product_alias


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
            float(row["rank"]),
            str(row["filename"]).casefold(),
            int(row["page_number"]),
        ),
    )


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
